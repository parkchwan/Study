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


_good_model: dict = {}


def _alternatives(client: httpx.Client, first: str | None) -> list[str]:
    """한도·과부하 때 시도할 같은 계열 모델 (모델마다 한도가 따로라서)."""
    from .vision import _version, list_gemini_models
    out = [m for m in (_good_model.get("m"), first) if m]
    try:
        names = [n.removeprefix("models/") for n in list_gemini_models(client)]
        import re
        fam = [n for n in names if re.match(r"^gemini-[\d.]+-flash(-lite)?(-latest|-\d+)?$", n)]
        out += sorted([n for n in fam if "lite" in n], key=_version, reverse=True)
        out += sorted([n for n in fam if "lite" not in n], key=_version, reverse=True)
    except Exception:
        pass
    return list(dict.fromkeys(out))[:4]


def rerank(q: str, cands: list[str], model: str | None = None) -> tuple[list[int], dict]:
    from .vision import ModelNotFound, QuotaError, ServerBusy
    with httpx.Client(timeout=30) as client:
        err: Exception | None = None
        for m in _alternatives(client, model) or [None]:
            try:
                data, usage = gemini_json(client, prompt(q, cands), SCHEMA, 300, m)
                _good_model["m"] = m
                break
            except (QuotaError, ModelNotFound, ServerBusy) as e:   # 다음 모델로
                err = e
        else:
            raise err or RuntimeError("AI 정밀 검색 실패")
    seen, order = set(), []
    for i in data.get("order", []):
        if isinstance(i, int) and 0 <= i < len(cands) and i not in seen:
            seen.add(i)
            order.append(i)
    return order, usage
