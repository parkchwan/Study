"""① 깊은 이해: 페이지마다 '무엇에 관한 페이지인지'를 AI가 미리 정리해 둠 (색인할 때 한 번).

- 재료: 이미 받아 둔 그림 설명·손글씨 받아 적기·본문 앞부분 (그림을 다시 보내지 않음 → 저렴)
- 결과: 한 줄 요약, 핵심 개념(동의어·상위 개념 포함), 이 페이지가 답하는 시험식 질문
- 검색(글자·의미)과 AI 정밀 검색의 재료로 쓰임. 내용이 바뀐 페이지만 다시 정리
"""
from __future__ import annotations

import hashlib
import logging

import httpx

from .config import settings
from .vision import gemini_json

log = logging.getLogger(__name__)
VERSION = "c1"
BATCH = 10

PROMPT = (
    "너는 건축시공기술사 수험 자료를 정리하는 조교다. 아래 각 페이지(그림·표·필기 설명과 본문 일부)를 읽고, "
    "나중에 수험생이 다양한 표현으로 검색해도 찾을 수 있게 정리하라. 페이지마다:\n"
    "- summary: 이 페이지가 무엇에 관한 것인지 한국어 한 문장(40자 내외)\n"
    "- concepts: 핵심 개념 4~8개. 공종·현상·공법·원인·대책 등 실제 주제어, 필요하면 상위 개념이나 관련 시공 상황"
    "(예: 기온과 강도 그래프 → 한중콘크리트, 적산온도, 초기강도, 양생온도)\n"
    "- questions: 이 페이지가 답이 되는 시험식 질문 2개(예: '기온이 콘크리트 강도 발현에 미치는 영향은?')\n"
    "목차·표지·빈 양식처럼 내용이 없으면 summary만 '내용 없음'으로 하고 나머지는 빈 배열. key는 받은 그대로.\n\n"
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {"pages": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "key": {"type": "STRING"},
            "summary": {"type": "STRING"},
            "concepts": {"type": "ARRAY", "items": {"type": "STRING"}},
            "questions": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["key", "summary", "concepts", "questions"],
    }}},
    "required": ["pages"],
}


def page_input(p: dict) -> str:
    items = "; ".join(f"[{it['kind']}] {it['title'] or ''}: {(it['description'] or '')[:300]}" for it in p["items"])
    return f"파일: {p['file_name']} ({p['page_no']}쪽)\n시각자료: {items[:900]}\n본문: {(p.get('text') or '')[:400]}"


def _sig(text: str) -> str:
    return hashlib.md5(f"{VERSION}|{text}".encode()).hexdigest()


def enabled() -> bool:
    return bool(settings.gemini_api_key) and settings.vision_provider in ("auto", "gemini") and settings.enrich


def build(db, model: str | None = None, progress=None) -> dict:
    """정리가 없거나 내용이 바뀐 페이지만 AI에게 보냄. {'pages': 처리 쪽수, 'in': 입력 토큰, 'out': 출력 토큰}."""
    stat = {"pages": 0, "in": 0, "out": 0}
    if not enabled():
        return stat
    have = db.concept_sigs()
    todo = []
    for p in db.export_pages(with_text=True):
        t = page_input(p)
        s = _sig(t)
        if have.get(p["page_key"]) != s:
            todo.append((p["page_key"], s, t))
    if settings.enrich_max_pages > 0:            # 시험용: 일부 쪽만 먼저 해 보고 비용 확인
        todo = todo[: settings.enrich_max_pages]
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(todo), BATCH):
            chunk = todo[i: i + BATCH]
            text = PROMPT + "\n\n".join(f"### key={k}\n{t}" for k, _, t in chunk)
            try:
                data, usage = gemini_json(client, text, SCHEMA, 3000, model)
            except Exception as e:
                log.warning("개념 정리 중단: %s", e)
                stat["error"] = str(e)
                break
            got = {g.get("key"): g for g in data.get("pages", [])}
            rows = []
            for k, s, _ in chunk:
                g = got.get(k) or {}
                rows.append((k, s, {"summary": str(g.get("summary", ""))[:120],
                                    "concepts": [str(c)[:40] for c in g.get("concepts", [])][:8],
                                    "questions": [str(q)[:80] for q in g.get("questions", [])][:3]}))
            db.concept_save(rows)
            stat["pages"] += len(chunk)
            stat["in"] += usage["in"]
            stat["out"] += usage["out"]
            if progress:
                progress(stat["pages"], len(todo), stat)
    return stat


def as_text(c: dict | None) -> str:
    """검색·의미 벡터에 넣을 글."""
    if not c or c.get("summary") == "내용 없음":
        return ""
    return " ".join([c.get("summary", "")] + c.get("concepts", []) + c.get("questions", []))
