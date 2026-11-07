"""드라이브/AI 없이 로컬 파이프라인 전체를 검증."""
import io
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(config.settings, "gemini_api_key", "")
    monkeypatch.setattr(config.settings, "openai_api_key", "")
    config.settings.ensure_dirs()
    from app.db import DB
    from app.indexer import IndexJob
    db = DB(config.settings.db_path)
    return tmp_path, db, IndexJob(db), config.settings


def make_pdf(path: Path):
    doc = fitz.open()
    # p1: 텍스트만 → 색인되면 안 됨
    p = doc.new_page(); p.insert_text((72, 72), "Just plain body text here.")
    # p2: 그림 + 캡션
    p = doc.new_page()
    im = Image.new("RGB", (400, 300), (30, 120, 200)); b = io.BytesIO(); im.save(b, "PNG")
    p.insert_image(fitz.Rect(72, 100, 472, 400), stream=b.getvalue())
    p.insert_text((72, 420), "Figure 1. Quarterly revenue trend")
    p.insert_text((72, 440), "그림 2. 매출 추이", fontname="korea")
    # p3: 표 (격자 선)
    p = doc.new_page()
    for r in range(5):
        p.draw_line((72, 100 + r * 30), (472, 100 + r * 30))
    for c in range(5):
        p.draw_line((72 + c * 100, 100), (72 + c * 100, 220))
    for r in range(4):
        for c in range(4):
            p.insert_text((80 + c * 100, 120 + r * 30), f"r{r}c{c}")
    p.insert_text((72, 90), "Table 3: Budget summary")
    doc.save(path)


def test_local_pipeline_and_search(env):
    tmp, db, job, settings = env
    pdf = tmp / "doc.pdf"; make_pdf(pdf)
    meta = {"file_id": "F1", "name": "report.pdf", "mime_type": "application/pdf", "path": "/sub",
            "web_link": "https://drive/x", "version_key": "v1"}
    n = job.index_local_file(meta, pdf, router=None)
    assert n >= 3
    st = db.stats()
    assert st["pages"] == 2                      # 텍스트 전용 페이지 제외
    assert st["by_kind"].get("table", 0) >= 1
    assert st["by_kind"].get("figure", 0) >= 1
    assert (settings.pages_dir / "F1_2.webp").exists()
    assert (settings.thumbs_dir / "F1_3.webp").exists()

    t0 = time.perf_counter()
    res = db.search("revenue")
    assert time.perf_counter() - t0 < 0.1
    assert res and res[0]["page_no"] == 2
    assert db.search("매출")[0]["page_no"] == 2
    assert all(r["kind"] == "table" for r in db.search("", kind="table"))
    assert db.search("budget")[0]["page_no"] == 3


def test_vision_router_fallback_and_cache(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "openai_api_key", "o")
    monkeypatch.setattr(settings, "vision_provider", "auto")
    calls = []
    def gem(c, j): calls.append("gemini"); raise vision.QuotaError("429")
    def oai(c, j): calls.append("openai"); return [{"kind": "flowchart", "title": "결재 흐름", "description": "승인 절차", "keywords": ["결재"]}]
    monkeypatch.setattr(vision, "_gemini", gem); monkeypatch.setattr(vision, "_openai", oai)
    r = vision.VisionRouter(db)
    items, prov = r.describe(b"img")
    assert prov == "openai" and items[0]["kind"] == "flowchart"
    items2, _ = r.describe(b"img")               # 캐시 적중 → 추가 호출 없음
    assert calls == ["gemini", "openai"] and r.calls["cache"] == 1
    r.describe(b"img2")                          # gemini는 한도초과로 비활성 → 바로 openai
    assert calls[-1] == "openai" and calls.count("gemini") == 1


def test_parse_and_folder_id():
    from app.vision import _parse
    from app.drive import parse_folder_id
    assert _parse('```json\n{"items":[{"kind":"weird","title":"t"}]}\n```')[0]["kind"] == "figure"
    assert parse_folder_id("https://drive.google.com/drive/folders/1AbC_d-9?usp=sharing") == "1AbC_d-9"
    assert parse_folder_id("1AbC") == "1AbC"


def test_index_job_with_fake_drive(env):
    tmp, db, job, settings = env
    src = tmp / "src.pdf"; make_pdf(src)
    class FakeDrive:
        def folder_name(self, fid): return "테스트폴더"
        def walk(self, fid):
            yield {"file_id": "A", "name": "a.pdf", "mime_type": "application/pdf", "path": "/",
                   "web_link": "", "version_key": "1"}
        def fetch(self, meta, d):
            out = d / "A.pdf"; out.write_bytes(src.read_bytes()); return out
    job.start("FOLDER", drive_factory=FakeDrive); job.thread.join(30)
    assert job.status()["phase"] == "완료" and db.stats()["files"] == 1
    job.start("FOLDER", drive_factory=FakeDrive); job.thread.join(30)
    assert job.status()["skipped"] == 1          # 변경 없으면 재처리 안 함


def test_api(env, monkeypatch):
    tmp, db, job, settings = env
    from fastapi.testclient import TestClient
    import importlib, app.main as m
    importlib.reload(m)
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    m.job.index_local_file({"file_id": "Z", "name": "z.pdf", "mime_type": "application/pdf", "path": "/",
                            "web_link": "", "version_key": "1"}, pdf, None)
    c = TestClient(m.app)
    assert c.get("/").status_code == 200
    r = c.get("/api/search", params={"q": "revenue"}).json()["results"]
    assert r[0]["page_key"] == "Z_2"
    img = c.get("/img/pages/Z_2.webp")
    assert img.status_code == 200 and "immutable" in img.headers["cache-control"]
    assert c.get("/api/page/Z_2").json()["page_no"] == 2


def test_local_folder_mode(env):
    tmp, db, job, settings = env
    from app.drive import LocalFolder, local_dir
    root = tmp / "내 드라이브" / "건축"; (root / "sub").mkdir(parents=True)
    make_pdf(root / "sub" / "a.pdf"); (root / "note.txt").write_text("x")
    assert local_dir(str(root)) == root and local_dir("https://drive.google.com/x") is None
    job.start(str(root), drive_factory=lambda: LocalFolder(root)); job.thread.join(30)
    s = job.status()
    assert s["phase"] == "완료" and s["total"] == 1, s
    assert (root / "sub" / "a.pdf").exists()        # 원본은 절대 삭제하지 않음
    assert db.search("budget")[0]["path"] == "/sub"
