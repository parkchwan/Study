"""보기 전용 정적 사이트 내보내기.

결과물(index.html + data.json + img/)은 웹 서버만 있으면 열립니다.
Python·Docker가 안 되는 NAS(예: Synology DS216play)의 Web Station에 두고
휴대폰/태블릿에서 24시간 볼 수 있습니다. 검색은 브라우저에서 처리합니다.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .config import settings
from .db import DB

STATIC = Path(__file__).parent / "static"


def _sync_dir(src: Path, dst: Path, keep: set[str]) -> None:
    """바뀐 이미지만 복사하고, 더 이상 없는 이미지는 삭제 (NAS 전송량 최소화)."""
    dst.mkdir(parents=True, exist_ok=True)
    for name in keep:
        s, d = src / name, dst / name
        if not s.exists():
            continue
        ss = s.stat()
        if d.exists():
            ds = d.stat()
            if ds.st_size == ss.st_size and ds.st_mtime >= ss.st_mtime - 2:   # 같은 파일 → 복사 생략
                continue
        shutil.copy2(s, d)
    for d in dst.glob("*.webp"):
        if d.name not in keep:
            d.unlink()


ORIGINAL_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def original_name(p: dict) -> str:
    """'원본 열기'용 파일 이름 (내 PC 폴더로 색인한 PDF·이미지만)."""
    ap = p.get("abs_path")
    if not ap or not settings.export_originals:
        return ""
    ext = Path(ap).suffix.lower()
    return f"{p['file_id']}{ext}" if ext in ORIGINAL_EXTS else ""


def fix_title(it: dict) -> dict:
    """손글씨 항목 제목이 워터마크·로고로 잘못 붙은 경우 → 받아 적은 내용 첫 줄로 바꿈."""
    from .vision import _JUNK
    title = str(it.get("title") or "")
    if it.get("kind") == "handwriting" and any(w in title.lower() for w in _JUNK):
        first = next((l.strip() for l in str(it.get("description") or "").splitlines() if l.strip()), "")
        it = dict(it, title=first[:40] or "손글씨 필기")
    return it


def build_payload(db: DB) -> tuple[dict, dict, list]:
    """(data, text, thesaurus): 화면이 쓰는 데이터. NAS 내보내기와 PC 화면이 같은 것을 씀."""
    from . import thesaurus
    from .vision import is_junk, looks_like_toc
    # 내보낼 때도 목차·색인·단순 목록·로고를 거름 → 파일을 다시 처리하기 전이라도 바로 반영
    concepts = db.concepts()     # ① AI가 정리한 페이지별 요약·개념·질문
    pages = []
    for p in db.export_pages(with_text=True):
        toc = looks_like_toc(p.pop("text", ""))
        items = [fix_title(it) for it in p["items"] if not is_junk(it) and not (toc and it["kind"] != "handwriting")]
        if items:
            p["items"] = items
            pages.append(p)
    # 태블릿이 처음 받는 data.json은 최대한 작게: 파일 정보는 한 번만, 항목은 [종류, 제목, 짧은 설명]만.
    # 본문(OCR)·손글씨 전문·키워드는 text.json에 따로 → 화면을 먼저 띄운 뒤 받아서 검색/상세 표시
    files: list[list] = []
    fidx: dict[tuple, int] = {}
    cpages, texts = [], {}
    for p in pages:
        fk = (p["file_name"], p["path"], p["web_link"], p["page_count"], p["v"], original_name(p))
        if fk not in fidx:
            fidx[fk] = len(files)
            files.append(list(fk))
        cpages.append([p["page_key"], fidx[fk], p["page_no"],
                       [[it["kind"], it["title"] or "", (it["description"] or "")[:120], it["source"]]
                        for it in p["items"]]])
        c = concepts.get(p["page_key"]) or {}
        texts[p["page_key"]] = [p["t"], [[it["description"] or "", it["keywords"] or ""] for it in p["items"]],
                                c.get("summary", ""), c.get("concepts", []), c.get("questions", [])]
    data = {
        "meta": {"folder_name": db.get_meta("folder_name"), "v": 2,
                 "ai": {"model": db.get_meta("gemini_model") or settings.gemini_model},
                 "exported_at": time.strftime("%Y-%m-%d %H:%M")},
        "files": files,      # [파일명, 경로, 링크, 총쪽수, 이미지버전, 원본파일이름(files/ 아래, 없으면 "")]
        "pages": cpages,     # [page_key, 파일번호, 쪽, [[종류, 제목, 짧은설명, 분석]]]
    }
    return data, texts, thesaurus.groups(db)


def _copy_viewer(out_dir: Path) -> None:
    """원본 PDF 보기 화면(pdf.html + pdf.js)을 함께 내보냄."""
    (out_dir / "pdfjs").mkdir(exist_ok=True)
    for rel in ("pdf.html", "pdfjs/pdf.min.js", "pdfjs/pdf.worker.min.js", "pdfjs/LICENSE"):
        s, d = STATIC / rel, out_dir / rel
        if not d.exists() or d.stat().st_size != s.stat().st_size or rel == "pdf.html":
            shutil.copyfile(s, d)


def _sync_originals(db: DB, dst: Path, keep: set[str]) -> None:
    """원본 파일을 NAS로 복사 (바뀐 것만). G: 드라이브 파일은 이때 내려받아짐."""
    dst.mkdir(parents=True, exist_ok=True)
    for name in sorted(keep):
        src = db.file_abs_path(Path(name).stem)
        if not src or not Path(src).exists():
            continue
        d, ss = dst / name, Path(src).stat()
        if d.exists() and d.stat().st_size == ss.st_size and d.stat().st_mtime >= ss.st_mtime - 2:
            continue
        tmp = dst / (name + ".part")
        shutil.copyfile(src, tmp)            # 복사 중 끊겨도 반쪽 파일이 원본 행세를 하지 않게
        tmp.replace(d)
    for d in dst.iterdir():
        if d.name not in keep:
            d.unlink()


def export_site(db: DB, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from . import embed
    data, texts, thes = build_payload(db)
    pages = [{"page_key": p[0]} for p in data["pages"]]
    stamp = time.strftime("%Y%m%d%H%M%S")
    data["meta"]["text"] = f"text.json?{stamp}"
    data["meta"]["thesaurus"] = f"thesaurus.json?{stamp}"
    blob = db.embedding_blob([p["page_key"] for p in pages], embed.DIM)
    if any(blob):
        # 페이지 순서대로 이어 붙인 의미 벡터. API 키는 넣지 않음 (태블릿에서 직접 입력)
        data["meta"]["embed"] = {"file": f"embeddings.bin?{stamp}", "dim": embed.DIM, "model": settings.embed_model}
        b = out_dir / "embeddings.bin.tmp"
        b.write_bytes(blob)
        b.replace(out_dir / "embeddings.bin")
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    for name, obj in (("text.json", texts), ("thesaurus.json", thes)):
        t = out_dir / (name + ".tmp")
        t.write_text(dump(obj), encoding="utf-8")
        t.replace(out_dir / name)
    tmp = out_dir / "data.json.tmp"
    tmp.write_text(dump(data), encoding="utf-8")
    keep = {p["page_key"] + ".webp" for p in pages}
    _copy_viewer(out_dir)
    if settings.export_originals:
        _sync_originals(db, out_dir / "files", {f[5] for f in data["files"] if f[5]})
    _sync_dir(settings.pages_dir, out_dir / "img" / "pages", keep)
    _sync_dir(settings.thumbs_dir, out_dir / "img" / "thumbs", keep)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("const STATIC_MODE = false;", "const STATIC_MODE = true;", 1)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    tmp.replace(out_dir / "data.json")          # 마지막에 교체 → 보는 중에도 깨지지 않음
    return {"dir": str(out_dir), "pages": len(pages)}


if __name__ == "__main__":      # python -m app.export [폴더]
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(settings.export_dir)
    print(export_site(DB(settings.db_path), target))
