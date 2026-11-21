"""학습 앱 연동: PC 내보내기(api/) → NAS 검색 API → 야간 골격 생성 (가짜 Gemini) 전체 흐름."""
import json
import urllib.error
import urllib.parse
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pymupdf as fitz
import pytest

NAS = Path(__file__).resolve().parent.parent / "nas"


def make_book(path: Path):
    doc = fitz.open()
    p = doc.new_page()           # 1쪽: 표(격자) + 그림(벡터 도형) + 캡션
    p.insert_text((72, 60), "CLT와 GLT 비교 직교적층 평행적층 매스팀버", fontname="korea", fontsize=12)
    for r in range(4):
        p.draw_line((72, 100 + r * 30), (472, 100 + r * 30))
    for c in range(4):
        p.draw_line((72 + c * 133, 100), (72 + c * 133, 190))
    for r, row in enumerate([["구분", "CLT", "GLT"], ["적층", "직교적층", "평행적층"], ["용도", "슬래브 벽체", "기둥 보"]]):
        for c, cell in enumerate(row):
            p.insert_text((80 + c * 133, 120 + r * 30), cell, fontname="korea", fontsize=10)
    p.insert_text((72, 92), "표 1. CLT와 GLT 비교", fontname="korea", fontsize=10)
    for i in range(12):
        p.draw_rect(fitz.Rect(100 + i * 15, 300 + i * 6, 300 + i * 15, 320 + i * 6))
    p.insert_text((72, 480), "그림 1. CLT 직교 적층 구조", fontname="korea", fontsize=10)
    p = doc.new_page()           # 2쪽: 다른 주제
    p.insert_text((72, 60), "베이크 아웃 Bake out 실내 온도 30~40도 유해물질 배출 새집증후군", fontname="korea", fontsize=11)
    for i in range(20):
        p.draw_line((72, 100 + i * 10), (400, 110 + i * 10))
    p.insert_text((72, 330), "그림 2. 베이크 아웃 실시 순서", fontname="korea", fontsize=10)
    doc.save(path)


@pytest.fixture()
def exported(tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(config.settings, "gemini_api_key", "")
    monkeypatch.setattr(config.settings, "openai_api_key", "")
    monkeypatch.setattr(config.settings, "export_originals", False)
    config.settings.ensure_dirs()
    from app.db import DB
    from app.drive import LocalFolder
    from app.export import export_site
    from app.indexer import IndexJob
    root = tmp_path / "건축시공기술사"
    (root / "교재").mkdir(parents=True)
    make_book(root / "교재" / "목구조 교재.pdf")
    (root / "_drive_index_export" / "figs").mkdir(parents=True)     # NAS가 올린 결과물 → 색인하면 안 됨
    make_book(root / "_drive_index_export" / "figs" / "x.pdf")
    db = DB(config.settings.db_path)
    job = IndexJob(db)
    job.start("x", drive_factory=lambda: LocalFolder(root))
    job.thread.join(60)
    out = tmp_path / "nas_web" / "drive-index"
    res = export_site(db, out)
    return out, res, db


def test_api_export_files(exported):
    out, res, db = exported
    api = out / "api"
    assert res["api"]["pages"] == 2, res
    lines = [json.loads(l) for l in (api / "pages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {l["f"] for l in lines} == {"목구조 교재.pdf"}            # _drive_index_export 는 색인 안 됨
    from PIL import Image
    for l in lines:
        im = Image.open(api / "pages" / f"{l['k']}.jpg")
        assert im.width == 1200
        for it in l["it"]:
            if it[0] != "handwriting":
                f = Image.open(api / "figs" / it[4])
                assert f.width == 800
    # 1쪽 표 조각은 페이지 전체가 아니라 표 부분만 (가로 800, 세로는 페이지보다 훨씬 짧음)
    p1 = next(l for l in lines if l["n"] == 1)
    tbl = next(it for it in p1["it"] if it[0] == "table")
    assert Image.open(api / "figs" / tbl[4]).height < 600
    rows = [json.loads(l) for l in (api / "export" / "목구조 교재.jsonl").read_text(encoding="utf-8").splitlines()]
    assert set(rows[0]) == {"book", "page", "page_key", "kind", "title", "summary", "text", "page_img", "fig_img"}
    assert {r["kind"] for r in rows} >= {"표", "그림"}
    assert all(r["fig_img"].startswith("figs/") for r in rows if r["kind"] != "본문")
    # 두 번째 내보내기는 바뀐 것이 없으니 이미지를 새로 만들지 않음
    from app.apiexport import export_api
    assert export_api(db, out)["images_made"] == 0


def _free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_nas_search_api(exported):
    out, _, _ = exported
    port = _free_port()
    env = dict(os.environ, DRIVE_INDEX_DIR=str(out), API_PORT=str(port), GEMINI_API_KEY="")
    proc = subprocess.Popen([sys.executable, "-I", str(NAS / "api_server.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        url = f"http://127.0.0.1:{port}/drive-index/api/search?q=" + urllib.parse.quote("CLT GLT 비교") + "&k=8"
        for _ in range(50):
            try:
                r = json.loads(urllib.request.urlopen(url, timeout=2).read())
                break
            except OSError:
                time.sleep(0.1)
        top = r["results"][0]
        assert top["book"] == "목구조 교재" and top["page"] == 1 and top["kind"] == "표"
        assert set(top) >= {"book", "page", "page_key", "kind", "title", "summary", "text", "page_img", "fig_img"}
        assert top["page_img"].startswith(f"http://127.0.0.1:{port}/drive-index/api/pages/")
        img = urllib.request.urlopen(top["fig_img"], timeout=2)
        assert img.headers["Content-Type"] == "image/jpeg" and len(img.read()) > 1000
        r2 = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/drive-index/api/search?q="
                                               + urllib.parse.quote("bake out") + "&k=3", timeout=2).read())
        assert r2["results"][0]["page"] == 2
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/drive-index/api/figs/../meta.json", timeout=2)
    finally:
        proc.kill()


class FakeGemini(BaseHTTPRequestHandler):
    calls = []
    reply = None

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGemini.calls.append((self.path, self.headers.get("x-goog-api-key"), body))
        if len(FakeGemini.calls) > 2:            # 세 번째 문제에서 하루 한도
            msg = json.dumps({"error": {"code": 429, "message": "Quota exceeded GenerateRequestsPerDayPerProjectPerModel"}})
            self.send_response(429); self.end_headers(); self.wfile.write(msg.encode()); return
        txt = json.dumps(FakeGemini.reply, ensure_ascii=False)
        out = json.dumps({"candidates": [{"content": {"parts": [{"text": "생각 중", "thought": True}, {"text": txt}]}}],
                          "usageMetadata": {"totalTokenCount": 1234}})
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(out.encode())


def test_nightly_skeleton(exported, tmp_path):
    out, _, _ = exported
    work = tmp_path / "work"
    (work / "in").mkdir(parents=True); (work / "prev").mkdir()
    items = [{"id": "140-1-1", "r": 140, "p": 1, "q": "CLT와 GLT의 비교", "ref": "참고 글"},
             {"id": "140-2-1", "r": 140, "p": 2, "q": "베이크 아웃의 실시방법", "ref": ""},
             {"id": "140-2-2", "r": 140, "p": 2, "q": "세 번째 문제", "ref": ""}]
    (work / "in" / "gem_input.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    (work / "in" / "prompt.txt").write_text("문제: {{Q}}\n유형: {{TYPE}}\n참고:\n{{REF}}", encoding="utf-8")
    (work / "in" / "type_1.txt").write_text("용어", encoding="utf-8")
    (work / "in" / "type_2.txt").write_text("서술", encoding="utf-8")
    # 예전 골격 파일: 세 번째 문제는 예전 것이 남아 있어야 함
    (work / "prev" / "골격_140회.json").write_text(json.dumps({"items": {"140-2-2": {"src": "gemini", "toc": [1, 2, 3]}}}),
                                                  encoding="utf-8")
    FakeGemini.calls = []
    FakeGemini.reply = {
        "toc": [{"h": "1. 정의", "kw": ["직교적층", "평행적층"], "c": "", "v": ""},
                {"h": "2. 비교", "kw": ["매스팀버", "슬래브 벽체"], "c": "", "v": ""},
                {"h": "3. 엉터리", "kw": ["초전도 거푸집", "양자 철근"], "c": "", "v": ""}],
        "fig": {"t": "CLT 직교 적층 구조", "kind": "개념도", "d": "", "ref": "CLT", "lib": "",
                "svg": "<svg><text x='1'>직교적층</text><text>0°</text><text>평행적층</text></svg>"},
        "tbl": {"t": "CLT vs GLT", "cols": ["구분", "CLT", "GLT"], "rows": [["적층", "직교적층", "평행적층"]]},
        "figs": [{"t": "베이크 아웃 실시 순서", "kind": "플로우", "d": "", "svg": "<svg><text>무지개 공법</text></svg>", "at": 2}],
        "tbls": [], "kw": [], "num": [], "tip": ""}
    srv = HTTPServer(("127.0.0.1", 0), FakeGemini)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, DRIVE_INDEX_DIR=str(out), SKEL_WORK=str(work), GEMINI_API_KEY="test-key",
               GEMINI_BASE=f"http://127.0.0.1:{srv.server_port}/v1beta", SKEL_SLEEP="0")
    try:
        r = subprocess.run([sys.executable, "-I", str(NAS / "skeleton.py"), "--no-sync"], env=env,
                           capture_output=True, text=True, timeout=120)
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stdout + r.stderr
    path, key, body = FakeGemini.calls[0]
    assert key == "test-key" and "gemini-flash-latest:generateContent" in path
    parts = body["contents"][0]["parts"]
    assert "CLT와 GLT의 비교" in parts[0]["text"] and "목구조 교재 p.1" in parts[0]["text"] and "참고 글" in parts[0]["text"]
    assert 1 <= sum(1 for p in parts if "inline_data" in p) <= 4          # 교재 페이지 이미지 첨부
    status = json.loads((work / "last_run.json").read_text(encoding="utf-8"))
    assert status["made"] == 2 and status["left"] == 1 and "한도" in status["stopped"]
    g = json.loads((work / "out" / "골격_140회.json").read_text(encoding="utf-8"))["items"]
    assert list(g) == ["140-1-1", "140-2-1", "140-2-2"]
    a = g["140-1-1"]
    assert a["src"] == "gemini"
    assert a["fig"]["src_img"].startswith("figs/") and a["fig"]["src_ref"] == "목구조 교재 p.1"
    assert a["figs"][0]["src_ref"] == "목구조 교재 p.2"
    assert a["toc"][2].get("nosrc") is True and not a["toc"][0].get("nosrc")
    assert a["figs"][0].get("nosrc") is True and not a["fig"].get("nosrc") and not a["tbl"].get("nosrc")
    assert "초전도 거푸집" in a["src_check"]["ng"] and a["src_check"]["ok"] >= 4
    assert g["140-2-2"]["toc"] == [1, 2, 3]                          # 아직 못 만든 문제는 예전 골격 유지
    # 다음 날 다시 실행 → 남은 한 문제만 요청
    FakeGemini.calls = []
    srv = HTTPServer(("127.0.0.1", 0), FakeGemini)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env["GEMINI_BASE"] = f"http://127.0.0.1:{srv.server_port}/v1beta"
    try:
        subprocess.run([sys.executable, "-I", str(NAS / "skeleton.py"), "--no-sync"], env=env, capture_output=True, timeout=120)
    finally:
        srv.shutdown()
    assert len(FakeGemini.calls) == 1 and "세 번째 문제" in FakeGemini.calls[0][2]["contents"][0]["parts"][0]["text"]


def test_skeleton_uses_running_api(exported, tmp_path):
    """검색 API가 켜져 있으면 골격 생성기는 색인을 따로 읽지 않고 API를 씀 (NAS 메모리 절약)."""
    out, _, _ = exported
    port = _free_port()
    env = dict(os.environ, DRIVE_INDEX_DIR=str(out), API_PORT=str(port), GEMINI_API_KEY="")
    api = subprocess.Popen([sys.executable, "-I", str(NAS / "api_server.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    work = tmp_path / "work2"
    (work / "in").mkdir(parents=True)
    (work / "in" / "gem_input.json").write_text(json.dumps(
        [{"id": "140-1-1", "r": 140, "p": 1, "q": "CLT와 GLT의 비교", "ref": ""}], ensure_ascii=False), encoding="utf-8")
    (work / "in" / "prompt.txt").write_text("{{Q}} {{TYPE}} {{REF}}", encoding="utf-8")
    FakeGemini.calls = []
    FakeGemini.reply = {"toc": [{"h": "a", "kw": ["직교적층"]}, {"h": "b", "kw": ["없는말일세"]}, {"h": "c", "kw": []}],
                        "fig": {"t": "CLT와 GLT 비교", "kind": "표", "svg": ""}}
    srv = HTTPServer(("127.0.0.1", 0), FakeGemini)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/drive-index/api/health", timeout=1); break
            except OSError:
                time.sleep(0.1)
        r = subprocess.run([sys.executable, "-I", str(NAS / "skeleton.py"), "--no-sync"], capture_output=True, text=True,
                           timeout=120, env=dict(env, SKEL_WORK=str(work), GEMINI_API_KEY="k", SKEL_SLEEP="0",
                                                 GEMINI_BASE=f"http://127.0.0.1:{srv.server_port}/v1beta"))
    finally:
        srv.shutdown(); api.kill()
    assert "검색 API" in r.stdout, r.stdout + r.stderr
    a = json.loads((work / "out" / "골격_140회.json").read_text(encoding="utf-8"))["items"]["140-1-1"]
    assert a["fig"]["src_ref"] == "목구조 교재 p.1" and a["fig"]["src_img"].endswith("_1_0.jpg")   # 표 조각
    assert a["toc"][1]["nosrc"] is True and a["src_check"] == {"ok": 1, "ng": ["없는말일세"]}


def test_recent_questions_exported(tmp_path, monkeypatch):
    """최근 7년(21회) 기출만 questions.json 으로 내보냄 → 검색 화면 '오늘의 기출'."""
    from app import config
    from app.export import load_questions
    items = [{"id": f"{r}-1-1", "r": r, "p": 1, "q": f"{r}회 문제에 대하여 설명하시오."} for r in range(110, 140)]
    f = tmp_path / "gem_input.json"
    f.write_text("﻿" + json.dumps(items, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config.settings, "questions_file", str(f))
    qs = load_questions()
    assert [q["r"] for q in qs] == list(range(119, 140))
    assert set(qs[0]) == {"id", "r", "p", "q"}
    assert load_questions(tmp_path / "none.json") == []
