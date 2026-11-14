"""📷 문제로 검색: 문제 사진을 Gemini가 읽고 검색할 문장을 만들어 줌 (PC 화면용; 태블릿은 브라우저가 직접)."""
from __future__ import annotations

import httpx

from .vision import gemini_json

PROMPT = (
    "이미지는 건축시공기술사 시험 문제, 교재·필기의 일부, 또는 도면·그림·사진이다. JSON으로 답하라. "
    "문제나 제목 글이 있으면 question: 그 문장을 쓰인 그대로(번호·배점 제외, 80자 이내). "
    "글이 거의 없는 그림·도면·사진이면 question: 그림이 무엇을 나타내는지 건설 용어로 한 문장(예: '흙막이 벽체 단면과 지하수위'). "
    "query: 이 문제나 그림과 관련된 자료를 찾기 위한 핵심 주제 구절(30자 이내). keywords: 핵심어 3~6개(공법·부재·현상 이름). "
    "건설과 무관하거나 알아볼 수 없으면 question을 빈 문자열로."
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {"question": {"type": "STRING"}, "query": {"type": "STRING"},
                   "keywords": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["question", "query", "keywords"],
}


def read_question(jpeg: bytes, model: str | None = None) -> dict:
    from .rerank import _alternatives
    from .vision import ModelNotFound, QuotaError, ServerBusy
    with httpx.Client(timeout=60) as client:
        err: Exception | None = None
        for m in _alternatives(client, model) or [None]:
            try:
                data, _ = gemini_json(client, PROMPT, SCHEMA, 400, m, image_jpeg=jpeg)
                return {"question": str(data.get("question", ""))[:120], "query": str(data.get("query", ""))[:60],
                        "keywords": [str(k) for k in data.get("keywords", [])][:6]}
            except (QuotaError, ModelNotFound, ServerBusy) as e:
                err = e
        raise err or RuntimeError("사진 읽기 실패")
