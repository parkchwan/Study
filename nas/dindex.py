# -*- coding: utf-8 -*-
"""NAS용 검색 엔진 (Python 3.8+ 표준 라이브러리만 사용 — DS216play 같은 ARM NAS에서 동작).

PC가 내보낸 EXPORT_DIR/api/ (pages.jsonl, emb.bin, meta.json)를 읽어
태블릿 화면(index.html)의 검색과 같은 방식으로 찾음:
 띄어쓰기·기호 무시, 동의어 사전, 일부 단어만 맞아도 관련도 순, 문장 그대로 있으면 맨 위,
 GEMINI_API_KEY가 있으면 의미 검색(뜻이 비슷한 페이지)까지 섞음.
"""
from __future__ import print_function

import array
import json
import math
import os
import re
import threading
import time
import urllib.request

KIND_KO = {"table": "표", "handwriting": "본문", "text": "본문"}
_NORM = re.compile(r"[\s\-_·.,/()\[\]{}:;'\"!?~]+")
STOPQ = {"따른", "따라", "대한", "대하여", "관한", "관련", "위한", "의한", "통한", "및", "또는", "그리고", "등", "등의",
         "의", "에", "와", "과", "시", "때"}
JOSAQ = ["에서는", "으로는", "에서", "으로", "에는", "에게", "까지", "부터", "보다", "이란", "란", "은", "는", "이", "가",
         "을", "를", "의", "에", "로", "와", "과", "도"]
SHORT = re.compile(r"^[a-z0-9]{1,4}$")
ASCII_WORDS = re.compile(r"[a-z0-9]+")
COARSE = 64        # 의미 검색 1차: 앞 64차원만으로 전체를 빠르게 훑고, 상위 400쪽만 256차원 전부로 다시 계산
LEAD_SPLIT = re.compile(r",|，|·|\s및\s|(?<=[가-힣])[과와]\s")


def norm(s):
    return _NORM.sub("", str(s or "").lower())


def kind_ko(kind):
    return KIND_KO.get(kind, "그림")


class Page(object):
    __slots__ = ("key", "book", "file", "path", "no", "count", "items", "summary", "concepts", "questions",
                 "text800", "hay", "head", "words", "img", "vec", "n64")


class Index(object):
    def __init__(self, api_dir):
        self.api_dir = api_dir
        self.pages = []
        self.thes = {}
        self.dim = 0
        self.embed_model = "gemini-embedding-001"
        self.stamp = None
        self.lock = threading.Lock()
        self.qv_cache = {}
        self.load()

    # ── 읽기 (PC가 새로 내보내면 자동으로 다시 읽음) ──
    def _stamp(self):
        try:
            return os.stat(os.path.join(self.api_dir, "pages.jsonl")).st_mtime
        except OSError:
            return None

    def maybe_reload(self):
        s = self._stamp()
        if s and s != self.stamp:
            with self.lock:
                if s != self.stamp:
                    self.load()

    def load(self):
        stamp = self._stamp()
        meta = {}
        try:
            with open(os.path.join(self.api_dir, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        thes = {}
        for g in meta.get("thesaurus") or []:
            alts = []
            for x in g:
                n = norm(x)
                if n and n not in alts:
                    alts.append(n)
            for a in alts:
                cur = thes.setdefault(a, [])
                for x in alts:
                    if x not in cur:
                        cur.append(x)
        emb = meta.get("embed") or {}
        dim = int(emb.get("dim") or 0)
        blob = b""
        if emb.get("ok") and dim:
            try:
                with open(os.path.join(self.api_dir, "emb.bin"), "rb") as f:
                    blob = f.read()
            except OSError:
                blob = b""
        pages = []
        path = os.path.join(self.api_dir, "pages.jsonl")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for i, line in enumerate(f):            # 한 줄씩 → 메모리 1GB NAS에서도 여유
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    p = Page()
                    p.key, p.book, p.file, p.path, p.no, p.count = d["k"], d["b"], d["f"], d.get("path", ""), d["n"], d.get("pc")
                    p.items = d["it"]           # [kind, title, desc, keywords, fig]
                    p.summary, p.concepts, p.questions = d.get("s", ""), d.get("c") or [], d.get("q") or []
                    t = d.get("t") or ""
                    p.text800 = t[:800]
                    p.img = bool(d.get("img"))
                    head = " ".join("%s %s" % (it[1], it[3]) for it in p.items) + " %s %s" % (p.summary, " ".join(p.concepts))
                    body = " ".join("%s %s" % (kind_ko(it[0]), it[2]) for it in p.items)
                    allt = "%s %s %s %s %s" % (p.file, head, body, " ".join(p.questions), t)
                    p.hay, p.head = norm(allt), norm(head)
                    p.words = frozenset(ASCII_WORDS.findall(allt.lower()))
                    p.vec, p.n64 = None, 0.0
                    if blob and len(blob) >= (i + 1) * dim:
                        v = array.array("b", blob[i * dim:(i + 1) * dim])
                        if any(v):
                            p.vec = v
                            p.n64 = math.sqrt(sum(x * x for x in v[:COARSE])) or 1.0
                    pages.append(p)
        self.pages, self.thes, self.dim, self.stamp = pages, thes, dim, stamp
        self.embed_model = emb.get("model") or self.embed_model
        self.exported_at = meta.get("exported_at", "")
        self.qv_cache = {}

    # ── 검색어 나누기 (index.html parseQuery 와 같은 규칙) ──
    def _clean(self, t):
        if t in STOPQ:
            return ""
        if re.search(r"[가-힣]$", t) and norm(t) not in self.thes:
            for j in JOSAQ:
                if t.endswith(j) and len(t) - len(j) >= 2:
                    return t[:-len(j)]
        return t

    def parse(self, q):
        toks = [x for x in (self._clean(t) for t in q.lower().split()) if x]
        units = []

        def unit(t):
            return (t, list(self.thes[t]) if t in self.thes else [t])
        i = 0
        while i < len(toks):
            took = 0
            for L in range(min(5, len(toks) - i), 1, -1):          # 여러 단어 묶음 (jumbo special pile)
                c = norm("".join(toks[i:i + L]))
                if c in self.thes:
                    units.append(unit(c))
                    took = L
                    break
            if took:
                i += took
                continue
            t, buf, pos = norm(toks[i]), "", 0
            while pos < len(t):                                      # 붙여 쓴 말 나누기 (타설적정속도)
                L = 0
                for l in range(min(20, len(t) - pos), 1, -1):
                    if t[pos:pos + l] in self.thes:
                        L = l
                        break
                if L:
                    if buf:
                        units.append(unit(buf))
                        buf = ""
                    units.append(unit(t[pos:pos + L]))
                    pos += L
                else:
                    buf += t[pos]
                    pos += 1
            if buf:
                units.append(unit(buf))
            i += 1
        return [u for u in units if u[0]]

    @staticmethod
    def has(p, alt):
        if SHORT.match(alt):        # 짧은 영문 약어는 단어 단위 (rate가 separate에 걸리지 않게)
            return alt in p.words
        return alt in p.hay

    def contains(self, term):
        """골격 출처 확인용: 이 말(또는 동의어)이 교재 어딘가에 있나."""
        n = norm(term)
        if len(n) < 2:
            return True
        if getattr(self, "_big_stamp", None) != self.stamp:      # 전체 교재를 한 덩어리로 → 한 번에 찾기
            self._big = "\x00".join(p.hay for p in self.pages)
            self._allwords = frozenset().union(*[p.words for p in self.pages]) if self.pages else frozenset()
            self._big_stamp = self.stamp
        for a in self.thes.get(n, [n]):
            if (a in self._allwords) if SHORT.match(a) else (a in self._big):
                return True
        return False

    # ── 의미 검색용 검색어 벡터 ──
    def query_vector(self, q, key, timeout=4):
        if not key or not self.dim:
            return None
        if q in self.qv_cache:
            return self.qv_cache[q]
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:embedContent" % self.embed_model
        body = json.dumps({"content": {"parts": [{"text": q}]}, "taskType": "RETRIEVAL_QUERY",
                           "outputDimensionality": self.dim}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                              "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                vals = json.loads(r.read().decode("utf-8"))["embedding"]["values"]
        except Exception:
            return None
        n = math.sqrt(sum(x * x for x in vals)) or 1.0
        v = [x / n for x in vals]
        if len(self.qv_cache) > 500:
            self.qv_cache = {}
        self.qv_cache[q] = v
        return v

    # ── 순위 매기기 (index.html rank 와 같은 계산) ──
    def rank(self, q, qv=None, kind=""):
        pages = self.pages
        if kind:
            pages = [p for p in pages if any(it[0] == kind for it in p.items)]
        units = self.parse(q)
        if not units:
            return []
        hits = []
        for p in pages:
            hits.append([2 if self.has(p, u[0]) else (1 if any(a != u[0] and self.has(p, a) for a in u[1]) else 0)
                         for u in units])
        N = len(pages)
        w = [math.log(1 + N / (1.0 + sum(1 for h in hits if h[k]))) for k in range(len(units))]
        W = sum(w) or 1.0
        sims, sim_cut = None, 1.0
        if qv is not None:
            sims = self.similarities(pages, qv)
            srt = sorted(sims, reverse=True)
            sim_cut = max(0.5, (srt[0] if srt else 0) - 0.15, srt[min(59, len(srt) - 1)] if srt else 0)
        phrase = norm(q)
        phrase2 = "".join(u[0] for u in units)
        n_tok = len(q.split())
        multi = n_tok >= 3 or (n_tok >= 2 and len(phrase) >= 10)
        lead = norm(LEAD_SPLIT.split(q)[0])

        def exact(p):
            return multi and (phrase in p.hay or (len(phrase2) >= 6 and phrase2 in p.hay)
                              or (len(lead) >= 12 and lead != phrase and lead in p.hay))

        def pos(p, u):
            best = -1
            for a in u[1]:
                if SHORT.match(a) and not self.has(p, a):
                    continue
                i = p.hay.find(a)
                if i >= 0 and (best < 0 or i < best):
                    best = i
            return best

        out = []
        for i, p in enumerate(pages):
            h = hits[i]
            if exact(p):
                out.append({"p": p, "lex": 99.0, "sim": 0.0, "exact": True, "i": i})
                continue
            cov = sum(w[k] * (1 if x == 2 else 0.85) for k, x in enumerate(h) if x) / W
            sim = max(0.0, sims[i]) if sims else 0.0
            allh = all(x > 0 for x in h)
            lex_ok = h[0] > 0 if len(units) == 1 else cov >= 0.5
            if not lex_ok and not (sims and sims[i] >= sim_cut):
                continue
            prox = 0.0
            if allh and len(units) > 1:
                ps = [x for x in (pos(p, u) for u in units) if x >= 0]
                if ps:
                    span = max(ps) - min(ps)
                    prox = 0.35 if span <= 25 else 0.2 if span <= 80 else 0.08 if span <= 250 else 0.0
            head = 0.1 if any(u[0] in p.head for u in units) else 0.0
            lex = (cov + prox + head + (0.15 if allh else 0)) if lex_ok else 0.0
            out.append({"p": p, "lex": lex, "sim": sim, "exact": False, "i": i})
        if not out:
            return []
        max_s = max([0.0001] + [r["sim"] for r in out])
        max_l2 = max([0.0001] + [r["lex"] for r in out if not r["exact"]])
        for r in out:
            if r["exact"]:
                r["score"] = 10 + r["lex"]
            elif sims:
                r["score"] = max(0.5 * r["lex"] / max_l2 + 0.5 * r["sim"] / max_s, 0.8 * r["sim"] / max_s)
            else:
                r["score"] = r["lex"] / max_l2
        out.sort(key=lambda r: (-r["score"], r["i"]))
        top = next((r["score"] for r in out if not r["exact"]), 0)
        cut = 0.55 if sims else 0.8
        for r in out:
            r["weak"] = (not r["exact"]) and r["score"] < top * cut
        return out

    @staticmethod
    def similarities(pages, qv):
        """페이지별 뜻 유사도(코사인). NAS CPU가 느려서 2단계로 계산."""
        from operator import mul
        q64 = qv[:COARSE]
        qn = math.sqrt(sum(x * x for x in q64)) or 1.0
        sims = [(sum(map(mul, q64, p.vec)) / (qn * p.n64)) if p.vec is not None else 0.0 for p in pages]
        order = sorted(range(len(pages)), key=lambda i: -sims[i])[:400]
        for i in order:
            if pages[i].vec is not None:
                sims[i] = sum(map(mul, qv, pages[i].vec)) / 127.0
        return sims

    def best_item(self, p, q):
        """페이지 안에서 검색어와 가장 잘 맞는 그림·표 항목 번호."""
        qn = [u[0] for u in self.parse(q)] or [norm(q)]
        best, bi = -1.0, 0
        for i, it in enumerate(p.items):
            s = norm("%s %s %s" % (it[1], it[3], it[2]))
            sc = sum(1 for u in qn if u in s) + (0.5 if it[0] not in ("handwriting", "text") else 0) + (0.3 if it[4] else 0)
            if sc > best:
                best, bi = sc, i
        return bi

    def search(self, q, k=8, key=None, semantic=True, kind=""):
        """API 응답용 결과 목록 (이미지는 파일 이름. URL은 서버가 붙임)."""
        self.maybe_reload()
        t0 = time.time()
        qv = self.query_vector(q, key) if semantic else None
        res = self.rank(q, qv, kind)
        out = []
        for r in res[:k]:
            p = r["p"]
            bi = self.best_item(p, q) if p.items else 0
            it = p.items[bi] if p.items else ["", "", "", "", None]
            out.append({"book": p.book, "page": p.no, "page_key": p.key, "kind": kind_ko(it[0]),
                        "title": it[1] or (it[2][:40] if it[2] else ""), "summary": p.summary, "text": p.text800,
                        "page_img": ("pages/%s.jpg" % p.key) if p.img else None,
                        "fig_img": ("figs/%s" % it[4]) if it[4] else None,
                        "score": round(r["score"], 3), "exact": r["exact"], "weak": r["weak"],
                        "_items": p.items})
        return {"q": q, "count": len(res), "semantic": qv is not None, "ms": int((time.time() - t0) * 1000),
                "results": out}
