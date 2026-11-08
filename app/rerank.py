"""③ AI 정밀 검색: 빠른 검색이 고른 상위 후보의 '정리된 요약'만 AI가 읽고 질문에 맞는 순서로 다시 정렬.

PC 화면은 이 서버가, 태블릿(NAS)은 브라우저가 자기 키로 같은 요청을 보냄 (index.html 참고).
"""
from __future__ import annotations

import httpx

from .config import settings
from .vision import gemini_json

SCHEMA = {"type": "OBJECT", "properties": {"order": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
          "required": ["order"]}


def prompt(q: str, cands: list[str]) -> str:
    lines = "\n".join(f"[{i}] {c[:400]}" for i, c in enumerate(cands))
    return ("건축시공기술사 수험 자료에서 사용자가 찾는 페이지를 고르는 일이다. 질문의 뜻(문맥·의도)을 이해하고, "
            "아래 후보 중 질문에 실제로 답이 되거나 직접 관련된 페이지 번호만 관련도가 높은 순서로 나열하라. "
            "단어만 겹치고 주제가 다른 페이지는 빼라. JSON {\"order\":[번호...]}.\n"
            f"질문: {q}\n후보:\n{lines}")


def rerank(q: str, cands: list[str], model: str | None = None) -> tuple[list[int], dict]:
    with httpx.Client(timeout=30) as client:
        data, usage = gemini_json(client, prompt(q, cands), SCHEMA, 300, model)
    seen, order = set(), []
    for i in data.get("order", []):
        if isinstance(i, int) and 0 <= i < len(cands) and i not in seen:
            seen.add(i)
            order.append(i)
    return order, usage
