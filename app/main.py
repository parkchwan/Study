"""FastAPI 서버: 색인 시작/상태, 검색, 페이지 이미지 제공."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .db import DB
from .drive import parse_folder_id
from .indexer import IndexJob

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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


app.mount("/img/pages", ImmutableStatic(directory=settings.pages_dir), name="pages")
app.mount("/img/thumbs", ImmutableStatic(directory=settings.thumbs_dir), name="thumbs")


class IndexReq(BaseModel):
    folder: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/index")
def start_index(req: IndexReq):
    folder_id = parse_folder_id(req.folder)
    if not folder_id:
        raise HTTPException(400, "폴더 ID 또는 URL을 입력하세요.")
    if not job.start(folder_id):
        raise HTTPException(409, "이미 색인 작업이 진행 중입니다.")
    return {"ok": True, "folder_id": folder_id}


@app.get("/api/status")
def status():
    return {"job": job.status(), "stats": db.stats(),
            "folder_id": db.get_meta("folder_id"), "folder_name": db.get_meta("folder_name"),
            "vision_provider": settings.vision_provider,
            "gemini": bool(settings.gemini_api_key), "openai": bool(settings.openai_api_key)}


@app.get("/api/search")
def search(q: str = "", kind: str = "", limit: int = 200, offset: int = 0):
    rows = db.search(q, kind, min(limit, 500), offset)
    # 같은 페이지의 여러 항목은 하나의 결과로 묶음
    pages: dict[str, dict] = {}
    for r in rows:
        p = pages.setdefault(r["page_key"], {
            "page_key": r["page_key"], "file_name": r["file_name"], "path": r["path"], "page_no": r["page_no"],
            "page_count": r["page_count"], "web_link": r["web_link"], "width": r["width"], "height": r["height"],
            "items": []})
        p["items"].append({k: r[k] for k in ("kind", "title", "description", "keywords", "source")})
    return {"results": list(pages.values())}


@app.get("/api/page/{page_key}")
def page(page_key: str):
    p = db.page(page_key)
    if not p:
        raise HTTPException(404)
    p.pop("text", None)
    return p
