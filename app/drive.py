"""Google Drive 접근: 폴더 재귀 탐색 + 파일 다운로드/PDF 내보내기."""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Iterator

from .config import settings

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"

# Google 문서 → PDF로 내보내기
EXPORT_AS_PDF = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.drawing",
}
PDF_MIME = "application/pdf"
IMAGE_PREFIX = "image/"
# 업로드된 Office 파일: 다운로드 후 LibreOffice로 PDF 변환
OFFICE_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.ms-excel": ".xls",
}


def parse_folder_id(value: str) -> str:
    """폴더 URL 또는 ID를 받아 ID만 반환."""
    value = value.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", value) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value


def is_supported(mime: str) -> bool:
    return mime in EXPORT_AS_PDF or mime == PDF_MIME or mime.startswith(IMAGE_PREFIX) or mime in OFFICE_MIMES


def _credentials():
    if settings.service_account_file and Path(settings.service_account_file).exists():
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(settings.service_account_file, scopes=SCOPES)
    if settings.oauth_client_file and Path(settings.oauth_client_file).exists():
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        token = settings.data_dir / "token.json"
        creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(settings.oauth_client_file, SCOPES)
            creds = flow.run_local_server(port=0)
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(creds.to_json())
        return creds
    raise RuntimeError(
        "Google Drive 인증 파일이 없습니다. .env의 GOOGLE_SERVICE_ACCOUNT_FILE 또는 GOOGLE_OAUTH_CLIENT_FILE을 설정하세요."
    )


class Drive:
    def __init__(self):
        from googleapiclient.discovery import build
        self.svc = build("drive", "v3", credentials=_credentials(), cache_discovery=False)

    def folder_name(self, folder_id: str) -> str:
        return self.svc.files().get(fileId=folder_id, fields="name", supportsAllDrives=True).execute()["name"]

    def walk(self, folder_id: str, path: str = "") -> Iterator[dict]:
        """폴더 하위의 모든 지원 파일을 재귀적으로 나열."""
        token = None
        while True:
            resp = self.svc.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id,name,mimeType,md5Checksum,modifiedTime,webViewLink,"
                       "shortcutDetails)",
                pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            for f in resp.get("files", []):
                mime = f["mimeType"]
                if mime == "application/vnd.google-apps.shortcut":
                    sd = f.get("shortcutDetails", {})
                    if sd.get("targetMimeType") == FOLDER_MIME:
                        yield from self.walk(sd["targetId"], f"{path}/{f['name']}")
                    continue
                if mime == FOLDER_MIME:
                    yield from self.walk(f["id"], f"{path}/{f['name']}")
                elif is_supported(mime):
                    yield {
                        "file_id": f["id"],
                        "name": f["name"],
                        "mime_type": mime,
                        "path": path or "/",
                        "web_link": f.get("webViewLink", ""),
                        "version_key": f.get("md5Checksum") or f.get("modifiedTime", ""),
                    }
            token = resp.get("nextPageToken")
            if not token:
                break

    def fetch(self, meta: dict, dest_dir: Path) -> Path:
        """파일을 로컬로 받아 경로 반환 (Google 문서는 PDF로 내보냄)."""
        from googleapiclient.http import MediaIoBaseDownload
        mime, fid = meta["mime_type"], meta["file_id"]
        if mime in EXPORT_AS_PDF:
            req = self.svc.files().export_media(fileId=fid, mimeType=PDF_MIME)
            ext = ".pdf"
        else:
            req = self.svc.files().get_media(fileId=fid, supportsAllDrives=True)
            ext = OFFICE_MIMES.get(mime) or (".pdf" if mime == PDF_MIME else Path(meta["name"]).suffix or ".img")
        dest = dest_dir / f"{fid}{ext}"
        buf = io.FileIO(dest, "wb")
        dl = MediaIoBaseDownload(buf, req, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
        buf.close()
        return dest
