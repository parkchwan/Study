# -*- coding: utf-8 -*-
"""drive-index 검색 API (NAS에서 실행, 표준 라이브러리만 사용).

  GET /drive-index/api/search?q=검색어&k=8      → JSON
  GET /drive-index/api/pages/<page_key>.jpg      → 페이지 이미지 (1200px)
  GET /drive-index/api/figs/<이름>.jpg           → 그림·표 조각 (800px)
  GET /drive-index/api/health                    → 상태

같은 집 와이파이(192.168.0.x)와 Tailscale(100.x) 주소에서만 받음, 인증 없음.
환경 변수: DRIVE_INDEX_DIR (PC가 내보낸 폴더, 기본 /volume1/web/drive-index),
          API_PORT (기본 8765), GEMINI_API_KEY (있으면 의미 검색도), API_PUBLIC_BASE (이미지 URL 앞부분 강제)
"""
from __future__ import print_function

import ipaddress
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dindex import Index  # noqa: E402

DATA = os.environ.get("DRIVE_INDEX_DIR", "/volume1/web/drive-index")
API_DIR = os.path.join(DATA, "api")
PORT = int(os.environ.get("API_PORT", "8765"))
PUBLIC_BASE = os.environ.get("API_PUBLIC_BASE", "").rstrip("/")
PREFIX = "/drive-index/api"
FILE_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.jpg$")
# 집 공유기 대역 + Tailscale 대역 + NAS 자신만 허용 (인터넷에서 직접 오는 요청은 거절)
ALLOWED = [ipaddress.ip_network(n) for n in
           os.environ.get("API_ALLOW", "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12,100.64.0.0/10,127.0.0.0/8,::1/128,fd7a:115c:a1e0::/48").split(",")]

INDEX = Index(API_DIR)


def allowed(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    return any(a in n for n in ALLOWED if a.version == n.version)


class Handler(BaseHTTPRequestHandler):
    server_version = "drive-index-api/1"

    def log_message(self, fmt, *args):      # 검색어가 로그에 남지 않게 경로만 짧게
        sys.stderr.write("%s %s\n" % (self.address_string(), (args[0] if args else "")[:60].split("?")[0]))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self._cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), cache="no-store")

    def _base(self):
        if PUBLIC_BASE:
            return PUBLIC_BASE
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost:%d" % PORT
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return "%s://%s%s" % (proto, host, PREFIX)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not allowed(self.client_address[0]):
            return self._json(403, {"error": "같은 집 네트워크나 Tailscale에서만 쓸 수 있어요."})
        u = urlsplit(self.path)
        path = u.path
        # /drive-index/api/search, /api/search, /search 모두 받음 (tailscale serve 가 앞부분을 떼어도 되게)
        for pre in (PREFIX, "/api"):
            if path.startswith(pre + "/"):
                path = path[len(pre):]
                break
        if path in ("/search", "/search/"):
            return self.search(parse_qs(u.query))
        if path == "/contains":            # 골격 출처 확인용 (여러 개: ?t=a&t=b)
            ts = parse_qs(u.query).get("t") or []
            return self._json(200, {"found": [INDEX.contains(t) for t in ts[:200]]})
        if path in ("/health", "/"):
            INDEX.maybe_reload()
            return self._json(200, {"ok": True, "pages": len(INDEX.pages), "exported_at": INDEX.exported_at,
                                    "dim": INDEX.dim,
                                    "semantic": bool(os.environ.get("GEMINI_API_KEY") and INDEX.dim)})
        m = re.match(r"^/(pages|figs)/(.+)$", path)
        if m and FILE_RE.match(m.group(2)):
            f = os.path.join(API_DIR, m.group(1), m.group(2))
            if m.group(1) == "figs" and not os.path.exists(f):       # 그림 조각은 export/figs 에 있음
                f = os.path.join(API_DIR, "export", "figs", m.group(2))
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError:
                return self._json(404, {"error": "이미지 없음"})
            return self._send(200, data, "image/jpeg", cache="public, max-age=86400")
        return self._json(404, {"error": "없는 주소", "try": PREFIX + "/search?q=검색어&k=8"})

    def search(self, qs):
        q = (qs.get("q") or [""])[0].strip()
        if not q:
            return self._json(400, {"error": "q(검색어)를 넣어 주세요", "example": PREFIX + "/search?q=콘크리트 타설속도&k=8"})
        try:
            k = max(1, min(50, int((qs.get("k") or ["8"])[0])))
        except ValueError:
            k = 8
        sem = (qs.get("semantic") or ["1"])[0] not in ("0", "off", "false")
        res = INDEX.search(q, k=k, key=os.environ.get("GEMINI_API_KEY", ""), semantic=sem)
        if (qs.get("raw") or [""])[0] == "1":     # 같은 NAS의 골격 생성기용: 항목 목록·파일 이름 그대로
            return self._json(200, res)
        base = self._base()
        for r in res["results"]:
            r.pop("_items", None)
            for f in ("page_img", "fig_img"):
                if r[f]:
                    r[f] = base + "/" + quote(r[f])
        return self._json(200, res)


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    srv = Server(("0.0.0.0", PORT), Handler)
    print("drive-index API: http://0.0.0.0:%d%s/search?q=...  (페이지 %d쪽, 데이터 %s)"
          % (PORT, PREFIX, len(INDEX.pages), API_DIR), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
