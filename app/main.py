"""FastAPI 서버: 색인 시작/상태, 검색, 페이지 이미지 제공."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .db import DB, img_version
from .drive import LocalFolder, local_dir, parse_folder_id
from .export import build_payload, export_site
from .indexer import IndexJob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# 외부 요청 주소를 일일이 찍지 않음 (화면이 덜 지저분하고, 주소에 민감정보가 섞일 위험 차단)
for noisy in ("httpx", "httpcore", "googleapiclient.discovery"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
settings.ensure_dirs()

app = FastAPI(title="Drive Visual Index")
db = DB(settings.db_path)
job = IndexJob(db)
STATIC = Path(__file__).parent / "static"

# 렌더된 페이지/썸네일은 변경 불가 파일로 취급 → 브라우저가 1년 캐시 (재방문 즉시 표시)
class ImmutableStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


app.mount("/pdfjs", StaticFiles(directory=STATIC / "pdfjs"), name="pdfjs")
app.mount("/img/pages", ImmutableStatic(directory=settings.pages_dir), name="pages")
app.mount("/img/thumbs", ImmutableStatic(directory=settings.thumbs_dir), name="thumbs")


class IndexReq(BaseModel):
    folder: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/pdf.html")
def pdf_viewer():
    return FileResponse(STATIC / "pdf.html")


@app.get("/files/{name}")
def original_file(name: str):
    """PC 화면의 '원본 열기': 색인한 원본 파일 (Range 요청 지원 → 큰 PDF도 필요한 쪽만)."""
    path = db.file_abs_path(Path(name).stem)
    if not path or not Path(path).exists() or Path(path).suffix.lower() != Path(name).suffix.lower():
        raise HTTPException(404)
    return FileResponse(path)


@app.post("/api/index")
def start_index(req: IndexReq):
    local = local_dir(req.folder)
    if local:   # 내 컴퓨터 폴더 (PC용 Google Drive 동기화 폴더 등)
        folder_id, factory = str(local), (lambda: LocalFolder(local))
    else:
        folder_id, factory = parse_folder_id(req.folder), None
    if not folder_id:
        raise HTTPException(400, "폴더 ID, URL 또는 내 컴퓨터 폴더 경로를 입력하세요.")
    if not job.start(folder_id, drive_factory=factory):
        raise HTTPException(409, "이미 색인 작업이 진행 중입니다.")
    return {"ok": True, "folder_id": folder_id}


@app.post("/api/export")
def export():
    if not settings.export_dir:
        raise HTTPException(400, ".env에 EXPORT_DIR(예: \\\\PCHSNAS\\web\\drive-index)를 설정하세요.")
    try:
        return export_site(db, Path(settings.export_dir))
    except OSError as e:
        raise HTTPException(500, f"내보내기 실패 (NAS 폴더 접근 확인): {e}")


@app.get("/api/status")
def status():
    return {"job": job.status(), "stats": db.stats(),
            "folder_id": db.get_meta("folder_id"), "folder_name": db.get_meta("folder_name"),
            "vision_provider": settings.vision_provider, "default_folder": settings.default_folder,
            "gemini": bool(settings.gemini_api_key), "openai": bool(settings.openai_api_key)}


@app.get("/api/search")
def search(q: str = "", kind: str = "", limit: int = 3000, offset: int = 0):
    rows = db.search(q, kind, min(limit, 5000), offset)
    # 같은 페이지의 여러 항목은 하나의 결과로 묶음
    pages: dict[str, dict] = {}
    for r in rows:
        p = pages.setdefault(r["page_key"], {
            "page_key": r["page_key"], "file_name": r["file_name"], "path": r["path"], "page_no": r["page_no"],
            "page_count": r["page_count"], "web_link": r["web_link"], "width": r["width"], "height": r["height"],
            "v": img_version(r["version_key"]), "items": []})
        p["items"].append({k: r[k] for k in ("kind", "title", "description", "keywords", "source")})
    return {"results": list(pages.values())}


# ── 화면용 데이터 (NAS 내보내기와 같은 형식 → 같은 검색 엔진을 PC/태블릿에서 공용) ──
_payload: dict = {"key": None}


def _cached_payload():
    from . import embed
    st = db.stats()
    key = (st["pages"], st["items"], len(db.thesaurus_terms()), len(db.embedding_sigs()), len(db.concept_sigs()))
    if _payload["key"] != key:
        data, texts, thes = build_payload(db)
        data["meta"].update({"text": "/api/text", "thesaurus": "/api/thesaurus"})
        data["meta"]["ai"]["rerank"] = "/api/rerank"
        blob = db.embedding_blob([p[0] for p in data["pages"]], embed.DIM)
        if any(blob):
            data["meta"]["embed"] = {"file": "/api/embeddings", "dim": embed.DIM, "model": settings.embed_model,
                                     "query": "/api/qvec"}
        _payload.update(key=key, data=data, texts=texts, thes=thes, blob=blob)
    return _payload


@app.get("/api/data")
def api_data():
    return _cached_payload()["data"]


@app.get("/api/text")
def api_text():
    return _cached_payload()["texts"]


@app.get("/api/thesaurus")
def api_thesaurus():
    return _cached_payload()["thes"]


@app.get("/api/embeddings")
def api_embeddings():
    from fastapi import Response
    return Response(_cached_payload()["blob"], media_type="application/octet-stream")


class RerankReq(BaseModel):
    q: str
    cands: list[str]


@app.post("/api/rerank")
def api_rerank(req: RerankReq):
    """PC 화면용 AI 정밀 검색 (키는 PC의 .env)."""
    from . import rerank
    if not settings.gemini_api_key:
        raise HTTPException(400, "Gemini 키 없음")
    try:
        order, usage = rerank.rerank(req.q[:300], req.cands[:40], db.get_meta("gemini_model") or None)
    except Exception as e:
        msg = str(e)
        reason = "사용 한도 초과" if "429" in msg else "결제 확인 필요" if "402" in msg else "AI 응답 없음"
        raise HTTPException(503, reason)
    return {"order": order, "usage": usage}


@app.get("/api/qvec")
def api_qvec(q: str):
    """PC 화면용: 검색어를 의미 벡터로 (키는 PC의 .env에 있음)."""
    from . import embed
    if not embed.enabled():
        raise HTTPException(400, "의미 검색 꺼짐 (Gemini 키 없음)")
    q = q.strip()[:500]
    if q not in _qcache:
        if len(_qcache) > 500:
            _qcache.clear()
        _qcache[q] = embed.query_vector(q)
    return {"v": _qcache[q]}


_qcache: dict[str, list[float]] = {}


@app.get("/api/page/{page_key}")
def page(page_key: str):
    p = db.page(page_key)
    if not p:
        raise HTTPException(404)
    p.pop("text", None)
    return p
