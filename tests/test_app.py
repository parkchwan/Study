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


def test_logo_items_filtered(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    assert vision.is_junk({"kind": "figure", "title": "한국산업인력공단 로고", "description": ""})
    assert not vision.is_junk({"kind": "figure", "title": "흙막이 단면도", "description": "지하수위"})
    assert not vision.is_junk({"kind": "handwriting", "title": "필기", "description": "회사 로고 디자인 메모"})
    doc = fitz.open(); p = doc.new_page(); p.insert_text((72, 72), "blank answer sheet")
    for r in range(20): p.draw_line((50, 100 + r * 20), (550, 100 + r * 20))
    doc.save(tmp / "sheet.pdf")
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    monkeypatch.setattr(vision, "_gemini", lambda c, j, model=None: [{"kind": "figure", "title": "한국산업인력공단 로고",
                                                                       "description": "", "keywords": []}])
    n = job.index_local_file({"file_id": "G", "name": "s.pdf", "mime_type": "application/pdf", "path": "/",
                              "web_link": "", "version_key": "1"}, tmp / "sheet.pdf", vision.VisionRouter(db))
    assert n == 0 and db.stats()["pages"] == 0      # 로고만 있던 빈 답안지 → 색인 안 됨


def test_stop_when_ai_quota_exhausted(env, monkeypatch):
    tmp, db, job, settings = env
    from app import vision
    from app.drive import LocalFolder
    root = tmp / "q"; root.mkdir()
    for i in range(3): make_pdf(root / f"{i}.pdf")
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    def gem(c, j, model=None): raise vision.QuotaError("quota", 403)
    monkeypatch.setattr(vision, "_gemini", gem)
    job.start(str(root), drive_factory=lambda: LocalFolder(root)); job.thread.join(30)
    st = job.status()
    assert "중단" in st["phase"] and st["done"] == 1          # 첫 파일에서 한도 확인 후 나머지는 건드리지 않음


def test_thesaurus_and_embeddings_build_for_existing_index(env, monkeypatch):
    """이미 색인된 자료에도 동의어 사전·의미 벡터가 만들어지고 NAS로 내보내지는지."""
    import json
    tmp, db, job, settings = env
    from app import thesaurus, embed
    from app.export import export_site
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    job.index_local_file({"file_id": "T", "name": "t.pdf", "mime_type": "application/pdf", "path": "/",
                          "web_link": "", "version_key": "1"}, pdf, None)
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    asked = []
    def fake_ask(client, model, terms):
        asked.extend(terms)
        return [{"term": t, "synonyms": ["매출액", "sales"] if "revenue" in t.lower() else []} for t in terms]
    monkeypatch.setattr(thesaurus, "_ask_gemini", fake_ask)
    monkeypatch.setattr(embed, "embed_texts", lambda c, texts, task: [[1.0] + [0.0] * (embed.DIM - 1) for _ in texts])
    assert thesaurus.build(db) > 0 and any("revenue" in t.lower() for t in asked)
    assert thesaurus.build(db) == 0                       # 두 번째엔 새 용어 없음 → AI 안 부름
    assert embed.build(db) == 2 and embed.build(db) == 0  # 바뀐 페이지만 다시 계산
    out = tmp / "web"; export_site(db, out)
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    thes = json.loads((out / "thesaurus.json").read_text(encoding="utf-8"))
    assert data["meta"]["embed"]["dim"] == embed.DIM and "key" not in json.dumps(data["meta"]).lower()
    assert (out / "embeddings.bin").stat().st_size == embed.DIM * len(data["pages"])
    assert any("sales" in g for g in thes) and ["JSP", "jumbo special pile", "점보 스페셜 파일", "점보스페셜파일"] in thes


def test_norm_and_terms():
    from app import thesaurus
    assert thesaurus.norm("타설 속도") == thesaurus.norm("타설속도") == "타설속도"
    assert list(thesaurus._tokens("콘크리트의 타설속도는 JSP공법에서")) == ["콘크리트", "타설속도", "JSP공법"]


def test_payment_required_stops_ai(env, monkeypatch):
    import httpx
    tmp, db, job, settings = env
    from app import vision
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    r = vision.VisionRouter(db)
    r.client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(402, json={"error": {}})))
    assert r.describe(b"p") is None and "gemini" in r.disabled and "결제" in r.last_error


def test_toc_and_lists_filtered():
    from app import vision
    j = vision.is_junk
    assert j({"kind": "table", "title": "제3장 목차", "description": ""})
    assert j({"kind": "table", "title": "Table of Contents", "description": ""})
    assert j({"kind": "table", "title": "기출문제 리스트", "description": ""})
    assert j({"kind": "table", "title": "용어 목록", "description": ""})
    assert not j({"kind": "table", "title": "품질관리 체크리스트", "description": ""})
    assert not j({"kind": "chart", "title": "타설 속도와 측압", "description": ""})
    toc = "\n".join(f"제{i}장 콘크리트 공사 ········ {i * 12}" for i in range(1, 15))
    assert vision.looks_like_toc(toc)
    assert not vision.looks_like_toc("콘크리트 타설 시 주의사항\n타설 속도는 1.5m/h 이하\n측압 증가에 유의")


def test_numeric_table_is_not_toc():
    from app import vision
    table = "\n".join(f"압축강도 {i}   {i * 3}" for i in range(20))
    assert not vision.looks_like_toc(table)


def test_toc_without_leaders_and_export_filter(env):
    import json
    tmp, db, job, settings = env
    from app import vision
    from app.export import export_site
    toc = "\n".join(f"{i}. 콘크리트 공사 {i * 9 + 3}" for i in range(1, 16))
    assert vision.looks_like_toc(toc)
    assert not vision.looks_like_toc("\n".join(f"측정값 {v}" for v in [30, 12, 45, 8, 22, 51, 3, 40, 19, 27]))
    # DB에 예전 방식으로 저장된 목차/목록 항목 → 내보낼 때 빠지는지
    db.save_file({"file_id": "O", "name": "o.pdf", "mime_type": "application/pdf", "path": "/", "web_link": "",
                  "version_key": "1"},
                 [{"page_key": "O_1", "page_no": 1, "width": 1, "height": 1, "text": toc,
                   "items": [{"kind": "table", "title": "제1편 구성", "description": "", "keywords": []}]},
                  {"page_key": "O_2", "page_no": 2, "width": 1, "height": 1, "text": "",
                   "items": [{"kind": "table", "title": "기출문제 리스트", "description": "", "keywords": []}]},
                  {"page_key": "O_3", "page_no": 3, "width": 1, "height": 1, "text": "",
                   "items": [{"kind": "chart", "title": "측압 분포", "description": "", "keywords": []}]}])
    out = tmp / "web"; export_site(db, out)
    keys = [p[0] for p in json.loads((out / "data.json").read_text(encoding="utf-8"))["pages"]]
    assert keys == ["O_3"]


def test_originals_export_and_pc_route(env, monkeypatch):
    import json
    tmp, db, job, settings = env
    from app.drive import LocalFolder
    from app.export import export_site
    monkeypatch.setattr(settings, "export_originals", True)
    root = tmp / "drv"; root.mkdir(); make_pdf(root / "책.pdf")
    job.start(str(root), drive_factory=lambda: LocalFolder(root)); job.thread.join(30)
    out = tmp / "web"; export_site(db, out)
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    orig = data["files"][0][5]
    assert orig.endswith(".pdf") and (out / "files" / orig).stat().st_size == (root / "책.pdf").stat().st_size
    assert (out / "pdf.html").exists() and (out / "pdfjs" / "pdf.worker.min.js").exists()
    # PC 화면: /files/<id>.pdf 가 원본을 Range 요청으로 일부만 돌려줌
    from fastapi.testclient import TestClient
    import importlib, app.main as m
    importlib.reload(m)
    c = TestClient(m.app)
    r = c.get(f"/files/{orig}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100
    assert c.get("/files/nope.pdf").status_code == 404 and c.get("/pdf.html").status_code == 200


def test_enrich_build_export_and_rerank(env, monkeypatch):
    """① 개념 정리가 바뀐 페이지만 하고, 검색 자료·의미 벡터에 반영되며, ③ 재정렬 API가 동작하는지."""
    import json
    tmp, db, job, settings = env
    from app import enrich, embed, rerank
    from app.export import export_site
    pdf = tmp / "d.pdf"; make_pdf(pdf)
    job.index_local_file({"file_id": "C", "name": "c.pdf", "mime_type": "application/pdf", "path": "/",
                          "web_link": "", "version_key": "1"}, pdf, None)
    monkeypatch.setattr(settings, "gemini_api_key", "g"); monkeypatch.setattr(settings, "vision_provider", "gemini")
    sent = []
    def fake_json(client, text, schema, max_tokens, model=None):
        keys = [l.split("key=")[1] for l in text.splitlines() if l.startswith("### key=")]
        sent.append(keys)
        return {"pages": [{"key": k, "summary": "기온과 강도 관계", "concepts": ["한중콘크리트", "적산온도"],
                           "questions": ["기온이 강도에 미치는 영향은?"]} for k in keys]}, {"in": 100, "out": 50}
    monkeypatch.setattr(enrich, "gemini_json", fake_json)
    monkeypatch.setattr(settings, "enrich_max_pages", 1)
    st = enrich.build(db); assert st == {"pages": 1, "in": 100, "out": 50}     # 시험용 상한
    monkeypatch.setattr(settings, "enrich_max_pages", 0)
    assert enrich.build(db)["pages"] == 1 and enrich.build(db)["pages"] == 0  # 나머지만, 그다음엔 없음
    texts_seen = []
    monkeypatch.setattr(embed, "embed_texts", lambda c, t, task: (texts_seen.extend(t), [[1.0] + [0.0] * 255 for _ in t])[1])
    embed.build(db)
    assert all("적산온도" in t for t in texts_seen)                             # 의미 벡터에 개념 반영
    out = tmp / "web"; export_site(db, out)
    text = json.loads((out / "text.json").read_text(encoding="utf-8"))
    assert text["C_2"][2] == "기온과 강도 관계" and "한중콘크리트" in text["C_2"][3]
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert data["meta"]["ai"]["model"]
    monkeypatch.setattr(rerank, "gemini_json", lambda c, p, s, m, model=None: ({"order": [2, 0, 9, 2]}, {"in": 1, "out": 1}))
    order, _ = rerank.rerank("기온에 따른 강도", ["a", "b", "c"])
    assert order == [2, 0]                                                    # 범위 밖·중복 제거


def test_rerank_endpoint(env, monkeypatch):
    tmp, db, job, settings = env
    from fastapi.testclient import TestClient
    import importlib, app.main as m
    from app import rerank
    importlib.reload(m)
    monkeypatch.setattr(settings, "gemini_api_key", "g")
    monkeypatch.setattr(rerank, "gemini_json", lambda c, p, s, mx, model=None: ({"order": [1, 0]}, {"in": 1, "out": 1}))
    r = TestClient(m.app).post("/api/rerank", json={"q": "기온에 따른 강도", "cands": ["a", "b"]})
    assert r.status_code == 200 and r.json()["order"] == [1, 0]


def test_429_minute_waits_day_stops(monkeypatch):
    import httpx
    from app import vision, config
    monkeypatch.setattr(config.settings, "gemini_api_key", "k")
    waits = []; monkeypatch.setattr(vision.time, "sleep", waits.append)
    minute = {"error": {"code": 429, "message": "You exceeded your current quota, please check your plan and billing details.",
                        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                            {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel"}]},
                                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "21s"}]}}
    n = {"c": 0}
    def h(req):
        n["c"] += 1
        if n["c"] <= 2: return httpx.Response(429, json=minute)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"order":[0]}'}]}}],
                                         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}})
    c = httpx.Client(transport=httpx.MockTransport(h))
    data, usage = vision.gemini_json(c, "q", {}, 10, "m")
    assert data == {"order": [0]} and waits == [22.0, 22.0] and usage == {"in": 5, "out": 2}
    day = {"error": {"code": 429, "message": "quota", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}]}}
    c2 = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(429, json=day)))
    import pytest
    with pytest.raises(vision.QuotaError):
        vision.gemini_json(c2, "q", {}, 10, "m")


def test_rerank_falls_back_to_other_model(monkeypatch):
    from app import rerank, vision
    monkeypatch.setattr(rerank, "_good_model", {})
    monkeypatch.setattr(vision, "list_gemini_models", lambda c: ["models/gemini-3.5-flash-lite", "models/gemini-3.1-flash-lite",
                                                               "models/gemini-3.8-flash"])
    tried = []
    def fake(c, p, s, m, model=None):
        tried.append(model)
        if model == "gemini-3.5-flash-lite": raise vision.QuotaError("gemini 429", 429)
        return {"order": [1]}, {"in": 1, "out": 1}
    monkeypatch.setattr(rerank, "gemini_json", fake)
    order, _ = rerank.rerank("q", ["a", "b"], "gemini-3.5-flash-lite")
    assert order == [1] and tried == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    tried.clear(); rerank.rerank("q", ["a", "b"], "gemini-3.5-flash-lite")
    assert tried[0] == "gemini-3.1-flash-lite"                    # 성공한 모델을 먼저


def test_embedding_pace_stays_under_limits():
    from app.embed import Pace
    now = [0.0]; slept = []
    def sleep(s): slept.append(s); now[0] += s
    p = Pace(tpm=1000, rpm=10, clock=lambda: now[0], sleep=sleep)
    for _ in range(5):
        p.wait(400, 3)                         # 1분에 1000토큰 → 400씩이면 2번마다 대기
    assert len(slept) >= 2
    # 어떤 60초 구간에서도 1000토큰·10건을 넘지 않음
    for t0, _, _ in p.sent:
        win = [x for x in p.sent if t0 <= x[0] < t0 + 60]
        assert sum(x[1] for x in win) <= 1000 and sum(x[2] for x in win) <= 10


def test_full_body_text_and_watermark_title(env):
    import json
    tmp, db, job, settings = env
    from app.export import export_site
    body = ("말뚝 머리 보강 " * 150) + "문제 2) 준공된 철근콘크리트 구조물의 균열 발생원인"
    db.save_file({"file_id": "Q", "name": "q.pdf", "mime_type": "application/pdf", "path": "/", "web_link": "", "version_key": "1"},
                 [{"page_key": "Q_1", "page_no": 1, "width": 1, "height": 1, "text": body,
                   "items": [{"kind": "handwriting", "title": "archifoli 워터마크", "description": "균열의 원인과 대책\n건조수축",
                              "keywords": []}]}])
    out = tmp / "web"; export_site(db, out)
    text = json.loads((out / "text.json").read_text(encoding="utf-8"))
    data = json.loads((out / "data.json").read_text(encoding="utf-8"))
    assert "준공된 철근콘크리트 구조물의 균열 발생원인" in text["Q_1"][0]      # 페이지 중간(1000자 뒤)도 검색 대상
    assert data["pages"][0][3][0][1] == "균열의 원인과 대책"                  # 워터마크 제목 → 받아 적은 첫 줄


def test_photo_query_endpoint(env, monkeypatch):
    import base64
    tmp, db, job, settings = env
    from fastapi.testclient import TestClient
    import importlib, app.main as m
    from app import photo
    importlib.reload(m)
    monkeypatch.setattr(settings, "gemini_api_key", "g")
    monkeypatch.setattr(photo, "gemini_json", lambda c, p, s, mx, model=None, image_jpeg=None:
                        ({"question": "균열 발생 원인", "query": "균열 원인", "keywords": ["균열"]}, {"in": 1, "out": 1})
                        if image_jpeg else ({}, {}))
    r = TestClient(m.app).post("/api/photo_query", json={"image": base64.b64encode(b"jpg").decode()})
    assert r.status_code == 200 and r.json()["question"] == "균열 발생 원인"
