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
    def gem(c, j, model=None): calls.append("gemini"); raise vision.QuotaError("429")
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
    assert any("revenue" in it[1].lower() for p in data["pages"] for it in p[3])
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
    def gem(c, j, model=None):
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
    def gem(c, j, model=None):
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
    assert db.file_version("B").startswith("v1|")  # 다 분석되면 정상 버전 기록


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
    def gem(c, j, model=None):
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


def test_gemini_model_auto_switch(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    assert vision.pick_gemini_model(["models/gemini-2.5-flash", "models/gemini-3.5-flash-lite",
                                     "models/gemini-3.5-flash-lite-preview-06", "models/gemini-3.8-flash",
                                     "models/gemini-3.5-flash-lite-tts"]) == "gemini-3.5-flash-lite"
    assert vision.pick_gemini_model(["models/gemini-3.8-flash", "models/gemma-4"]) == "gemini-3.8-flash"
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_model", "gemini-2.5-flash-lite")
    used = []
    def gem(c, j, model=None):
        used.append(model)
        if model == "gemini-2.5-flash-lite": raise vision.ModelNotFound(model)
        return [{"kind": "figure", "title": "ok", "description": "", "keywords": []}]
    monkeypatch.setattr(vision, "_gemini", gem)
    monkeypatch.setattr(vision, "list_gemini_models", lambda c: ["models/gemini-3.5-flash-lite", "models/gemini-3.8-flash"])
    r = vision.VisionRouter(db)
    items, prov = r.describe(b"x")
    assert prov == "gemini" and used == ["gemini-2.5-flash-lite", "gemini-3.5-flash-lite"]
    r.describe(b"y"); assert used[-1] == "gemini-3.5-flash-lite"      # 이후엔 바뀐 모델로 바로


def test_gemini_key_not_in_url(monkeypatch):
    import httpx
    from app import vision, config
    monkeypatch.setattr(config.settings, "gemini_api_key", "SECRET123")
    seen = {}
    def handler(req):
        seen["url"] = str(req.url); seen["hdr"] = req.headers.get("x-goog-api-key")
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"items":[]}'}]}}]})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert vision._gemini(c, b"img", "gemini-3.5-flash-lite") == []
    assert "SECRET123" not in seen["url"] and seen["hdr"] == "SECRET123"


def test_ai_says_nothing_means_nothing(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    doc = fitz.open(); p = doc.new_page()
    for r in range(60):                          # 빈 답안지처럼 괘선만 잔뜩
        p.draw_line((50, 50 + r * 12), (550, 50 + r * 12))
    doc.save(tmp / "blank.pdf")
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(vision, "_gemini", lambda c, j, model=None: [])
    n = job.index_local_file({"file_id": "K", "name": "blank.pdf", "mime_type": "application/pdf", "path": "/",
                              "web_link": "", "version_key": "1"}, tmp / "blank.pdf", vision.VisionRouter(db))
    assert n == 0 and db.stats()["pages"] == 0   # 로컬 '벡터 도형' 추정으로 채우지 않음
    assert "로고" in vision.prompt()


def test_reanalyze_when_prompt_changes_and_cleanup(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    src = tmp / "src.pdf"; make_pdf(src)
    class FakeDrive:
        def folder_name(self, fid): return "f"
        def walk(self, fid):
            yield {"file_id": "R", "name": "r.pdf", "mime_type": "application/pdf", "path": "/", "web_link": "", "version_key": "1"}
        def fetch(self, meta, d):
            out = d / "R.pdf"; out.write_bytes(src.read_bytes()); return out
    job.start("F", drive_factory=FakeDrive); job.thread.join(30)
    assert (settings.pages_dir / "R_2.webp").exists()
    job.start("F", drive_factory=FakeDrive); job.thread.join(30)
    assert job.status()["skipped"] == 1
    # AI 키를 넣는(=분석 방식이 바뀐) 경우 → 같은 파일도 다시 분석, AI가 '없음'이라 한 페이지 이미지는 삭제
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(vision, "_gemini", lambda c, j, model=None: [])
    job.start("F", drive_factory=FakeDrive); job.thread.join(30)
    assert job.status()["skipped"] == 0
    keys = db.page_keys("R")
    assert "R_2" in keys                              # 캡션(Figure 1.)이 있어 남음
    for k in {"R_2", "R_3"} - keys:
        assert not (settings.pages_dir / f"{k}.webp").exists()


def test_server_busy_and_bad_json_retry(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    seq = iter([vision.ServerBusy("503"), ValueError("Expecting ':' delimiter"),
                [{"kind": "figure", "title": "ok", "description": "", "keywords": []}]])
    def gem(c, j, model=None):
        v = next(seq)
        if isinstance(v, Exception): raise v
        return v
    monkeypatch.setattr(vision, "_gemini", gem)
    r = vision.VisionRouter(db); waits = []; r.sleep = waits.append
    items, prov = r.describe(b"z")
    assert prov == "gemini" and waits == [10] and "gemini" not in r.disabled


def test_subfolder_indexing_keeps_other_files(env):
    tmp, db, job, settings = env
    from app.drive import LocalFolder
    root = tmp / "건축시공기술사"; (root / "A").mkdir(parents=True); (root / "B").mkdir()
    make_pdf(root / "A" / "a.pdf"); make_pdf(root / "B" / "b.pdf")
    job.start(str(root), drive_factory=lambda: LocalFolder(root)); job.thread.join(30)
    ids_all = db.all_file_ids(); assert len(ids_all) == 2
    # 하위 폴더만 다시 색인해도 다른 폴더(B) 자료는 지워지지 않고, A는 같은 ID로 '변경 없음' 처리
    job.start(str(root / "A"), drive_factory=lambda: LocalFolder(root / "A")); job.thread.join(30)
    assert db.all_file_ids() == ids_all and job.status()["skipped"] == 1
    # 폴더 안에서 실제로 지운 파일만 정리
    (root / "B" / "b.pdf").unlink()
    job.start(str(root), drive_factory=lambda: LocalFolder(root)); job.thread.join(30)
    assert len(db.all_file_ids()) == 1


def test_blocked_and_bad_key(env, monkeypatch):
    import httpx
    tmp, db, job, settings = env
    from app import vision
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={"candidates": [{"finishReason": "RECITATION", "content": {"parts": []}}]})
    r = vision.VisionRouter(db); r.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert r.describe(b"page") == (None, "blocked")
    assert r.describe(b"page") == (None, "blocked") and len(calls) == 1       # 거부 결과도 저장 → 재요청 안 함
    def bad_key(req):
        return httpx.Response(400, json={"error": {"message": "API key not valid. Please pass a valid API key."}})
    r2 = vision.VisionRouter(db); r2.client = httpx.Client(transport=httpx.MockTransport(bad_key))
    assert r2.describe(b"other") is None and "gemini" in r2.disabled and "키" in r2.last_error


def test_thinking_config_fallback(monkeypatch):
    import httpx, json as _j
    from app import vision, config
    monkeypatch.setattr(config.settings, "gemini_api_key", "k"); monkeypatch.setattr(vision, "_thinking_cfg_ok", True)
    bodies = []
    def handler(req):
        b = _j.loads(req.content); bodies.append(b)
        if "thinkingConfig" in b["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "thinking_level is not supported"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"items":[]}'}]}}]})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert vision._gemini(c, b"i", "m") == [] and len(bodies) == 2
    vision._gemini(c, b"i", "m"); assert len(bodies) == 3                      # 이후엔 처음부터 빼고 보냄


def test_export_splits_text_and_versions_images(env):
    import json
    tmp, db, job, settings = env
    from app.export import export_site
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    job.index_local_file({"file_id": "X", "name": "x.pdf", "mime_type": "application/pdf", "path": "/",
                          "web_link": "", "version_key": "1"}, pdf, None)
    out = tmp / "web"; export_site(db, out)
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    text = json.loads((out / "text.json").read_text(encoding="utf-8"))
    assert all(f[4] for f in data["files"]) and "budget" in text["X_3"][0]


def test_db_concurrent_read_write(env):
    import threading
    tmp, db, job, settings = env
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    errors = []
    def writer():
        try:
            for i in range(15):
                job.index_local_file({"file_id": f"W{i}", "name": f"w{i}.pdf", "mime_type": "application/pdf",
                                      "path": "/", "web_link": "", "version_key": "1"}, pdf, None)
        except Exception as e: errors.append(e)
    def reader():
        try:
            for _ in range(300): db.search("revenue"); db.stats()
        except Exception as e: errors.append(e)
    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors, errors


def test_schema_sent_and_fallback(monkeypatch):
    import httpx, json as _j
    from app import vision, config
    monkeypatch.setattr(config.settings, "gemini_api_key", "k")
    monkeypatch.setattr(vision, "_schema_ok", True); monkeypatch.setattr(vision, "_thinking_cfg_ok", False)
    bodies = []
    def handler(req):
        b = _j.loads(req.content); bodies.append(b)
        if "responseSchema" in b["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": "Invalid JSON payload: responseSchema unsupported"}})
        # 문자열 안에 줄바꿈이 그대로 들어간 답 → strict=False 로 읽힘
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text":
            '{"items":[{"kind":"handwriting","title":"t","description":"줄1\n줄2","keywords":[]}]}'}]}}]})
    c = httpx.Client(transport=httpx.MockTransport(handler))
    items = vision._gemini(c, b"i", "m")
    assert "responseSchema" in bodies[0]["generationConfig"] and len(bodies) == 2
    assert items[0]["description"] == "줄1\n줄2"


def test_office_without_libreoffice_is_skipped(env, monkeypatch):
    tmp, db, job, settings = env
    from app import extract
    from app.drive import LocalFolder
    monkeypatch.setattr(extract.shutil, "which", lambda n: None)
    monkeypatch.setattr(extract.Path, "exists", lambda self: False if "soffice" in str(self) else True)
    root = tmp / "o"; root.mkdir(); (root / "a.xlsx").write_bytes(b"x")
    job.start(str(root), drive_factory=lambda: LocalFolder(root)); job.thread.join(30)
    st = job.status()
    assert st["skipped_office"] == 1 and not st["errors"]
