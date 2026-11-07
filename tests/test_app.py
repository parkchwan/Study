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
    r = vision.VisionRouter(db); r.sleep = lambda s: None
    items, prov = r.describe(b"img")
    assert prov == "openai" and items[0]["kind"] == "flowchart"
    items2, _ = r.describe(b"img")               # 캐시 적중 → 추가 호출 없음
    # 429는 3번 재시도(총 4회) 후에야 OpenAI로 전환
    assert calls == ["gemini"] * 4 + ["openai"] and r.calls["cache"] == 1
    r.describe(b"img2")                          # gemini는 한도초과로 비활성 → 바로 openai
    assert calls[-1] == "openai" and calls.count("gemini") == 4


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


def test_static_export(env):
    import json
    tmp, db, job, settings = env
    from app.export import export_site
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    job.index_local_file({"file_id": "E", "name": "e.pdf", "mime_type": "application/pdf", "path": "/",
                          "web_link": "", "version_key": "1"}, pdf, None)
    out = tmp / "nas_web"
    r = export_site(db, out)
    assert r["pages"] == 2
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert any("revenue" in it["s"] for p in data["pages"] for it in p["items"])
    assert (out / "img" / "pages" / "E_2.webp").exists()
    assert "const STATIC_MODE = true;" in (out / "index.html").read_text(encoding="utf-8")
    db.delete_file("E"); export_site(db, out)      # 삭제된 페이지 이미지는 NAS에서도 정리
    assert not (out / "img" / "pages" / "E_2.webp").exists()


def test_handwriting(env, monkeypatch):
    tmp, db, job, settings = env
    from app import extract, vision
    # 펜 필기(Ink annotation)가 있는 텍스트 페이지 → 시각요소로 감지되어야 함
    doc = fitz.open(); p = doc.new_page(); p.insert_text((72, 72), "printed body")
    p.add_ink_annot([[(100, 100), (150, 130), (200, 110), (250, 160)]])
    t = p.add_text_annot((300, 300), "메모: 피복두께 확인"); doc.save(tmp / "ink.pdf")
    pages = list(extract.analyze(tmp / "ink.pdf"))
    assert pages[0].n_ink == 1 and pages[0].has_visual
    assert "피복두께" in pages[0].text                   # 메모 주석은 무료로 검색
    assert any(i["kind"] == "handwriting" for i in pages[0].local_items())

    # AI 응답의 handwriting 항목이 검색되는지 (라우터를 가짜로)
    monkeypatch.setattr(settings, "handwriting", True)
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    seen = {}
    def gem(c, j):
        seen["prompt"] = vision.prompt()
        return [{"kind": "handwriting", "title": "철근 필기", "description": "철근 콘크리트 피복두께 40mm 이상", "keywords": []}]
    monkeypatch.setattr(vision, "_gemini", gem)
    job.index_local_file({"file_id": "H", "name": "note.pdf", "mime_type": "application/pdf", "path": "/",
                          "web_link": "", "version_key": "1"}, tmp / "ink.pdf", vision.VisionRouter(db))
    assert "손글씨" in seen["prompt"]
    r = db.search("40mm")
    assert r and r[0]["kind"] == "handwriting"
    monkeypatch.setattr(settings, "handwriting", False)
    assert "손글씨" not in vision.prompt() and vision.prompt_version() != "v2-hw"


def test_rate_limit_retry_and_resume(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "vision_provider", "auto")
    n = {"c": 0}
    def gem(c, j):
        n["c"] += 1
        if n["c"] <= 2: raise vision.QuotaError("429", 429)
        return [{"kind": "figure", "title": "ok", "description": "", "keywords": []}]
    monkeypatch.setattr(vision, "_gemini", gem)
    r = vision.VisionRouter(db); waits = []; r.sleep = waits.append
    items, prov = r.describe(b"x")                # 분당 한도 → 기다렸다 재시도 후 성공
    assert prov == "gemini" and waits == [10, 30] and "gemini" not in r.disabled

    # 예산 소진 → 파일은 '미완료'로 저장되어 다음 색인 때 다시 처리
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    monkeypatch.setattr(settings, "vision_max_calls", 1)
    meta = {"file_id": "B", "name": "b.pdf", "mime_type": "application/pdf", "path": "/", "web_link": "", "version_key": "v1"}
    r2 = vision.VisionRouter(db); r2.sleep = lambda s: None
    job.index_local_file(meta, pdf, r2)
    assert db.file_version("B") == "" and job.state["incomplete"] == 1
    monkeypatch.setattr(settings, "vision_max_calls", 0)
    job.state = {}
    job.index_local_file(meta, pdf, vision.VisionRouter(db))
    assert db.file_version("B") == "v1"           # 다 분석되면 정상 버전 기록


def test_streaming_and_concurrency(env, monkeypatch):
    import threading, types
    tmp, db, job, settings = env
    from app import extract, vision
    doc = fitz.open()
    im = Image.new("RGB", (200, 150), (200, 50, 50)); b = io.BytesIO(); im.save(b, "PNG")
    for i in range(40):                          # 스캔본처럼 모든 페이지에 큰 그림
        p = doc.new_page(); p.insert_image(fitz.Rect(50, 50, 550, 750), stream=b.getvalue())
        p.insert_text((60, 780), f"page {i+1}")
    doc.save(tmp / "scan.pdf")
    assert isinstance(extract.analyze(tmp / "scan.pdf"), types.GeneratorType)   # 한 페이지씩
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "vision_concurrency", 4); monkeypatch.setattr(settings, "vision_max_calls", 0)
    active, peak, lock = [0], [0], threading.Lock()
    def gem(c, j):
        import time
        with lock: active[0] += 1; peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock: active[0] -= 1
        return [{"kind": "figure", "title": "fig", "description": "", "keywords": []}]
    monkeypatch.setattr(vision, "_gemini", gem)
    n = job.index_local_file({"file_id": "S", "name": "scan.pdf", "mime_type": "application/pdf", "path": "/",
                              "web_link": "", "version_key": "1"}, tmp / "scan.pdf", vision.VisionRouter(db))
    assert n == 40 and db.stats()["pages"] == 40
    assert 1 < peak[0] <= 4                       # 동시에 여러 장, 단 설정값 이하
    assert job.state["page"] == 40 and job.state["pages"] == 40
    assert db.search("page 17")[0]["page_no"] == 17
