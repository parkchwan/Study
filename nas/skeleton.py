# -*- coding: utf-8 -*-
"""야간 답안 골격 생성 (NAS 작업 스케줄러, 매일 01:00). 표준 라이브러리만 사용.

1) 드라이브 '건축시공기술사/_답안골격_교재그림/gemini/' 에서 gem_input.json·prompt.txt·type_1/2.txt 받기 (rclone)
2) 문제마다 drive-index에서 관련 교재 6~8쪽 검색 → 요약·본문을 {{REF}}에 넣고,
   그림·표가 있는 상위 3~4쪽은 페이지 이미지(1200px)를 함께 첨부 → Gemini로 골격 생성
3) 골격의 fig/figs마다 가장 잘 맞는 교재 그림 조각을 src_img·src_ref로 연결
4) 목차 키워드·표 칸·그림 글자를 교재 색인에서 찾아 보고, 근거 없으면 nosrc:true, 문제별 src_check 기록
5) out/골격_NNN회.json (기존과 같은 형식)으로 만들어 드라이브에 올림

문제마다 work/raw2/<id>.json 으로 저장 → 끊기거나 하루 무료 한도가 차면 다음 날 이어서 함.
환경 변수: GEMINI_API_KEY(필수), DRIVE_INDEX_DIR, RCLONE, SKEL_REMOTE, SKEL_MODEL, SKEL_MAX, SKEL_SLEEP
"""
from __future__ import print_function

import argparse
import base64
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from dindex import Index, norm  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

DATA = os.environ.get("DRIVE_INDEX_DIR", "/volume1/web/drive-index")
API_DIR = os.path.join(DATA, "api")
WORK = os.environ.get("SKEL_WORK", os.path.join(HERE, "work"))
RCLONE = os.environ.get("RCLONE", os.path.join(HERE, "rclone"))
REMOTE = os.environ.get("SKEL_REMOTE", "gdrive:건축시공기술사/_답안골격_교재그림/gemini")
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
INPUTS = ("gem_input.json", "prompt.txt", "type_1.txt", "type_2.txt", "model.txt")
N_REF, N_IMG = 8, 4


def log(*a):
    print(time.strftime("%m-%d %H:%M:%S"), *a, flush=True)


def read(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    except OSError:
        return default


def write_atomic(path, text):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ── 드라이브 주고받기 (rclone) ─────────────────────────────────
def rclone(*args):
    cmd = [RCLONE] + list(args) + ["--retries", "3", "--low-level-retries", "5"]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    if r.returncode != 0:
        raise RuntimeError("rclone 실패: %s\n%s" % (" ".join(args[:2]), r.stdout[-800:]))
    return r.stdout


def pull(inp, prev):
    flt = []
    for n in INPUTS:
        flt += ["--include", "/" + n]
    rclone("copy", REMOTE, inp, *flt)
    rclone("copy", REMOTE + "/out", prev, "--include", "/골격_*회.json")


def push(out):
    rclone("copy", out, REMOTE + "/out", "--include", "/골격_*회.json")


class RemoteIndex(object):
    """이미 켜져 있는 검색 API를 씀 → 교재 색인을 메모리에 두 번 올리지 않음 (NAS 메모리 1GB)."""

    def __init__(self, port):
        self.base = "http://127.0.0.1:%s/drive-index/api" % port
        h = self._get("/health")
        self.n_pages, self.dim = h["pages"], h.get("dim", 0)

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))

    def search(self, q, k=8, key=None, semantic=True):
        return self._get("/search?" + urlencode({"q": q, "k": k, "raw": 1, "semantic": 1 if semantic else 0}))

    def contains(self, term):
        return self._get("/contains?" + urlencode({"t": term}))["found"][0]


def open_index():
    port = os.environ.get("API_PORT", "8765")
    try:
        ix = RemoteIndex(port)
        if ix.n_pages:
            return ix, ix.n_pages, ix.dim, "검색 API"
    except Exception:
        pass
    ix = Index(API_DIR)
    return ix, len(ix.pages), ix.dim, "직접 읽기"


# ── Gemini ────────────────────────────────────────────────────
class DailyLimit(Exception):
    pass


class Fatal(Exception):
    pass


def call_gemini(model, key, prompt, images):
    parts = [{"text": prompt}]
    for jpg in images:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpg).decode("ascii")}})
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4}}).encode("utf-8")
    url = "%s/models/%s:generateContent" % (GEMINI_BASE, model)
    for attempt in range(6):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                j = json.loads(r.read().decode("utf-8"))
            c = (j.get("candidates") or [{}])[0]
            txt = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts") or [] if not p.get("thought"))
            u = j.get("usageMetadata") or {}
            return txt, u
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")
            if e.code == 429:
                if re.search(r"PerDay|per day|daily", msg, re.I):
                    raise DailyLimit(msg[:300])
                log("  분당 한도 - 60초 대기")
                time.sleep(60)
                continue
            if e.code in (400, 401, 403, 404):
                raise Fatal("API 오류 %d: %s" % (e.code, msg[:300]))
            log("  서버 오류 %d - 30초 후 재시도" % e.code)
            time.sleep(30)
        except Exception as e:      # 네트워크 끊김 등
            log("  네트워크 오류(%s) - 30초 후 재시도" % e.__class__.__name__)
            time.sleep(30)
    return "", {}


def parse_json(raw):
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        o = json.loads(raw, strict=False)
    except ValueError:
        return None
    return o if isinstance(o, dict) else None


# ── 교재 근거 모으기 ───────────────────────────────────────────
def simpler_queries(q):
    """문제 문장이 길면 교재 표현과 어긋나기 쉬움 → 괄호 속 영어를 빼고, '과/와/및'으로 나눠서도 찾아 봄."""
    base = re.sub(r"\s*\([^)]*\)\s*", " ", q)
    base = re.sub(r"\s+([과와])\s", r"\1 ", base)
    base = re.sub(r"(에 대하여|에 대해|을|를)?\s*(설명하시오|기술하시오|논하시오|쓰시오)\.?", " ", base)
    out = [base]
    out += [x for x in re.split(r",|·|\s및\s|(?<=[가-힣A-Za-z])[과와]\s", base) if len(norm(x)) >= 2]
    seen, res = {norm(q)}, []
    for x in out:
        x = " ".join(x.split())
        if norm(x) not in seen:
            seen.add(norm(x))
            res.append(x)
    return res


def related(index, q, key):
    res = index.search(q, k=12, key=key)["results"]
    if sum(1 for r in res if not r["weak"]) < 6:
        have = {r["page_key"] for r in res}
        for sq in simpler_queries(q):
            for r in index.search(sq, k=8, key=key)["results"]:
                if r["page_key"] not in have:
                    have.add(r["page_key"])
                    r["weak"] = True             # 원래 문제로 찾은 것보다 뒤에
                    res.append(r)
    strong = [r for r in res if not r["weak"]]
    picks = strong[:N_REF]
    if len(picks) < 6:                       # 관련 높은 게 적으면 낮은 것도 채워서 6쪽까지
        picks += [r for r in res if r["weak"]][:6 - len(picks)]
    return picks


def build_ref(q_ref, picks, n_img):
    marks = "①②③④⑤⑥⑦⑧⑨⑩"
    lines = ["[교재 근거 — 아래 교재 페이지의 용어·분류·비교 항목·표현을 그대로 쓸 것]"]
    for i, r in enumerate(picks):
        its = "; ".join("%s: %s" % ({"table": "표"}.get(it[0], "그림"), it[1]) for it in r["_items"]
                        if it[0] != "handwriting" and it[1])[:200]
        lines.append("%s %s p.%d%s%s | 본문: %s" % (
            marks[i], r["book"], r["page"], (" | " + its) if its else "",
            (" | 요약: " + r["summary"]) if r["summary"] else "", r["text"][:600]))
    if n_img:
        lines.append("※ 첨부 이미지 %d장은 위 %s 페이지 원본이다. 이미지 속 분류·비교표의 항목명과 순서, 그림 구성을 "
                     "골격의 표·그림에 그대로 반영할 것." % (n_img, "·".join(marks[i] for i in range(n_img))))
    if q_ref:
        lines += ["", "[기존 참고자료]", q_ref[:2500]]
    return "\n".join(lines)


def page_images(picks, n=N_IMG):
    out, used = [], []
    for i, r in enumerate(picks):
        if len(out) >= n:
            break
        if not r["page_img"] or not any(it[0] != "handwriting" for it in r["_items"]):
            continue
        try:
            with open(os.path.join(API_DIR, r["page_img"]), "rb") as f:
                out.append(f.read())
            used.append(i)
        except OSError:
            continue
    return out, used


# ── 그림 출처 연결 + 출처 확인 ─────────────────────────────────
TOK = re.compile(r"[A-Za-z0-9]{2,}|[가-힣]{2,}")
STOPT = {"그림", "도표", "도해", "figure", "fig", "table", "vs", "및", "또는", "개념도", "모식도", "비교표"}


def _tokens(s):
    return list({t for t in (x.lower() for x in TOK.findall(s or "")) if t not in STOPT})


def match_score(fig_title, fig_all, item_text, same_kind):
    """교재 그림 제목의 말이 골격 그림 설명에 얼마나 들어 있나(60%) + 골격 그림 제목의 말이 교재 그림에 얼마나 있나(40%)."""
    it_toks, ft_toks = _tokens(item_text), _tokens(fig_title)
    if not it_toks or not ft_toks:
        return 0.0
    fa, it = norm(fig_all), norm(item_text)
    a = sum(len(t) for t in it_toks if norm(t) in fa) / float(sum(len(t) for t in it_toks))
    b = sum(len(t) for t in ft_toks if norm(t) in it) / float(sum(len(t) for t in ft_toks))
    return (0.6 * a + 0.4 * b) * (1.0 if same_kind else 0.6)


def attach_src(fig, index, picks, key):
    """fig 하나에 가장 잘 맞는 교재 그림 조각을 붙임."""
    if not isinstance(fig, dict):
        return
    title = " ".join(str(fig.get(k) or "") for k in ("t", "ref"))
    every = " ".join(str(fig.get(k) or "") for k in ("t", "d", "ref", "lib"))
    if not title.strip():
        return
    cands = list(picks) + index.search(title[:120], k=5, key=key, semantic=bool(key))["results"]
    want_table = "표" in str(fig.get("kind") or "")
    best, best_sc = None, 0.0
    for r in cands:
        for it in r["_items"]:
            if not it[4] or it[0] == "handwriting":
                continue
            sc = match_score(title, every, "%s %s" % (it[1], it[3]), (it[0] == "table") == want_table)
            if sc > best_sc:
                best, best_sc = (it[4], r), sc
    if best and best_sc >= 0.25:
        name, r = best
        fig["src_img"] = "figs/" + name
        fig["src_ref"] = "%s p.%d" % (r["book"], r["page"])


GENERIC = {norm(x) for x in ("구분", "특징", "장점", "단점", "비고", "내용", "항목", "정의", "개요", "결론", "종류", "방법",
                             "기준", "원인", "대책", "비교", "용도", "적용", "효과", "문제점", "유의사항", "관리", "단계",
                             "순서", "시공", "목적", "특성", "요소", "o", "x", "있음", "없음", "가능", "불가", "우수", "보통",
                             "양호", "불량", "높음", "낮음", "대", "중", "소", "상", "하")}
NUMERIC = re.compile(r"^[0-9.,~%°℃㎜㎝㎡㎥mkgtcnpa/+\-×x·:↑↓→←↔()]+$", re.I)
SPLIT = re.compile(r"[()\[\]{}<>·,/+→←↔⇒⇔=:;|~]|\s+|vs\.?|VS", re.I)


def _check_terms(terms):
    out = []
    for t in terms:
        t = html.unescape(str(t or "")).strip()
        t = re.sub(r"^[\s•·\-*▶▷■□○●◎※①-⑳]+|^\(?\d+[.)]\s*", "", t).strip()
        n = norm(t)
        if len(n) < 2 or n in GENERIC or NUMERIC.match(n):
            continue
        out.append(t)
    return out


def found(index, term):
    if index.contains(term):
        return True
    parts = [p for p in SPLIT.split(term) if p and len(norm(p)) >= 2 and norm(p) not in GENERIC and not NUMERIC.match(norm(p))]
    return bool(parts) and all(index.contains(p) for p in parts)


def svg_labels(svg):
    return [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<text[^>]*>(.*?)</text>", svg or "", re.S)]


def src_check(obj, index):
    ok, ng = 0, []
    cache = {}

    def test(terms):
        nonlocal ok
        terms = _check_terms(terms)
        good = 0
        for t in terms:
            if t not in cache:
                cache[t] = found(index, t)
                if cache[t]:
                    ok += 1
                elif t not in ng:
                    ng.append(t)
            good += 1 if cache[t] else 0
        return terms, good

    for e in obj.get("toc") or []:
        if isinstance(e, dict):
            terms, good = test(e.get("kw") or [])
            if terms and good * 2 < len(terms):
                e["nosrc"] = True
    tbls = [obj.get("tbl")] + list(obj.get("tbls") or [])
    for tb in tbls:
        if isinstance(tb, dict):
            cells = [c for c in (tb.get("cols") or [])[1:]]
            for row in tb.get("rows") or []:
                cells += row if isinstance(row, list) else [row]
            terms, good = test(cells)
            if terms and good * 2 < len(terms):
                tb["nosrc"] = True
    figs = [obj.get("fig")] + list(obj.get("figs") or [])
    for fg in figs:
        if isinstance(fg, dict):
            terms, good = test(svg_labels(fg.get("svg")))
            if terms and good * 2 < len(terms):
                fg["nosrc"] = True
    obj["src_check"] = {"ok": ok, "ng": ng[:40]}
    return obj


# ── 회차별 파일 만들기 ─────────────────────────────────────────
def build_rounds(items, raw2, prev_dir, out_dir):
    """기존 형식 {"items":{id: {...}}}. 새로 만든 것(raw2)이 우선, 없으면 예전 골격을 그대로 둠."""
    changed = []
    rounds = []
    for it in items:
        if it["r"] not in rounds:
            rounds.append(it["r"])
    for r in rounds:
        name = "골격_%s회.json" % r
        prev = {}
        txt = read(os.path.join(prev_dir, name))
        if txt:
            try:
                prev = json.loads(txt, strict=False).get("items") or {}
            except ValueError:
                prev = {}
        merged = {}
        for it in items:
            if it["r"] != r:
                continue
            new = read(os.path.join(raw2, it["id"] + ".json"))
            if new:
                merged[it["id"]] = json.loads(new)
            elif it["id"] in prev:
                merged[it["id"]] = prev[it["id"]]
        for k, v in prev.items():                 # 입력에 없는 예전 문제도 지우지 않음
            merged.setdefault(k, v)
        if not merged:
            continue
        body = json.dumps({"items": merged}, ensure_ascii=False)
        dst = os.path.join(out_dir, name)
        if read(dst) != body:
            write_atomic(dst, body)
            changed.append(name)
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sync", action="store_true", help="드라이브와 주고받지 않음 (work/in 에 있는 입력 사용)")
    ap.add_argument("--limit", type=int, default=int(os.environ.get("SKEL_MAX", "0") or 0), help="이번에 만들 최대 문제 수")
    ap.add_argument("--only", default="", help="이 문제 id만 (예: 139-1-1)")
    ap.add_argument("--redo", action="store_true", help="--only 문제를 이미 만들었어도 다시")
    args = ap.parse_args()

    for d in ("in", "prev", "out", "raw2"):
        os.makedirs(os.path.join(WORK, d), exist_ok=True)
    inp, prev, out, raw2 = (os.path.join(WORK, d) for d in ("in", "prev", "out", "raw2"))
    lockf = open(os.path.join(WORK, ".lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("이미 실행 중 → 종료")
        return 0

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log("GEMINI_API_KEY 가 없습니다 (env.sh 확인)")
        return 1
    if not args.no_sync:
        log("드라이브에서 입력 받는 중")
        pull(inp, prev)
    items = json.loads(read(os.path.join(inp, "gem_input.json"), "[]"))
    tpl = read(os.path.join(inp, "prompt.txt"), "")
    t1, t2 = read(os.path.join(inp, "type_1.txt"), ""), read(os.path.join(inp, "type_2.txt"), "")
    model = os.environ.get("SKEL_MODEL") or (read(os.path.join(inp, "model.txt"), "") or "").strip() or "gemini-flash-latest"
    if not items or "{{Q}}" not in tpl:
        log("입력 파일(gem_input.json / prompt.txt)이 비었습니다")
        return 1
    index, n_pages, dim, how = open_index()
    log("모델 %s · 교재 %d쪽(%s) · 의미검색 %s" % (model, n_pages, how, "켜짐" if dim else "꺼짐"))
    if not n_pages:
        log("교재 색인이 없습니다: %s/pages.jsonl (PC에서 'NAS로 내보내기')" % API_DIR)
        return 1

    todo = [it for it in items if (not args.only or it["id"] == args.only)
            and (args.redo or not os.path.exists(os.path.join(raw2, it["id"] + ".json")))]
    log("남은 문제 %d / 전체 %d" % (len(todo), len(items)))
    stopped, made, tokens = "", 0, 0
    sleep = float(os.environ.get("SKEL_SLEEP", "7"))
    for n, it in enumerate(todo, 1):
        if args.limit and made >= args.limit:
            stopped = "이번 실행 최대 %d문제" % args.limit
            break
        log("[%d/%d] %s회 %s교시 %s" % (n, len(todo), it["r"], it["p"], it["q"][:40]))
        picks = related(index, it["q"], key)
        imgs, used = page_images(picks)
        # 첨부 이미지를 ①②③… 순서와 맞추려고, 이미지가 있는 페이지를 앞으로
        picks = [picks[i] for i in used] + [r for i, r in enumerate(picks) if i not in used]
        ref = build_ref(it.get("ref") or "", picks, len(imgs))
        prompt = tpl.replace("{{Q}}", it["q"]).replace("{{TYPE}}", t1 if it["p"] == 1 else t2).replace("{{REF}}", ref)
        obj = None
        try:
            for _ in range(2):
                raw, usage = call_gemini(model, key, prompt, imgs)
                tokens += int(usage.get("totalTokenCount") or 0)
                obj = parse_json(raw)
                if obj and len(obj.get("toc") or []) >= 3:
                    break
                log("  목차 부족/JSON 오류 - 다시 생성")
                obj = None
        except DailyLimit:
            stopped = "오늘 무료 한도 소진 → 내일 01:00에 이어서"
            break
        except Fatal as e:
            stopped = str(e)
            break
        if not obj:
            log("  건너뜀 (다음 실행 때 다시)")
            continue
        for fg in [obj.get("fig")] + list(obj.get("figs") or []):
            attach_src(fg, index, picks, key)
        src_check(obj, index)
        obj["refs"] = ["%s p.%d" % (r["book"], r["page"]) for r in picks]
        rec = {"src": "gemini"}
        rec.update(obj)
        write_atomic(os.path.join(raw2, it["id"] + ".json"), json.dumps(rec, ensure_ascii=False))
        made += 1
        log("  저장 · 출처 확인 %d개 맞음, 근거 없음 %d개" % (rec["src_check"]["ok"], len(rec["src_check"]["ng"])))
        if n < len(todo):
            time.sleep(sleep)
    changed = build_rounds(items, raw2, prev, out)
    if changed and not args.no_sync:
        log("드라이브에 올리는 중: %s" % ", ".join(changed))
        push(out)
    left = sum(1 for it in items if not os.path.exists(os.path.join(raw2, it["id"] + ".json")))
    status = {"at": time.strftime("%Y-%m-%d %H:%M"), "made": made, "left": left, "tokens": tokens,
              "stopped": stopped, "changed": changed, "model": model}
    write_atomic(os.path.join(WORK, "last_run.json"), json.dumps(status, ensure_ascii=False, indent=1))
    log("끝: 이번에 %d문제, 남은 %d문제 %s" % (made, left, ("(" + stopped + ")") if stopped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
