"""AI 비전 분석 라우터: Gemini(저가) 우선 → 실패 시 OpenAI 폴백.

크레딧 절약 장치
 1. 로컬 분석으로 시각 요소가 있는 페이지만 호출 (extract.PageInfo.has_visual)
 2. 이미지 해시 캐시: 같은 그림/재색인 시 재호출 안 함
 3. 이미지를 768px JPEG로 축소해 전송 (필기 인식 켜면 1280px — 작은 글씨 판독용)
 4. JSON 강제 + 짧은 출력 (maxOutputTokens 제한)
 5. VISION_MAX_CALLS 로 1회 인덱싱 당 호출 상한
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging

import httpx

from .config import settings

log = logging.getLogger(__name__)

KINDS = ["figure", "table", "flowchart", "chart", "diagram", "photo", "screenshot", "handwriting"]
BASE_PROMPT = (
    "이 문서 페이지 이미지에 있는 시각 요소(그림, 표, 플로우차트, 차트, 다이어그램, 사진, 스크린샷)를 모두 찾아라. "
    "본문 텍스트만 있는 영역은 무시. 각 요소마다 kind는 "
    + "|".join(KINDS)
    + " 중 하나, title은 캡션이나 짧은 제목(없으면 내용으로 만든 10단어 이하 제목), "
    "description은 검색에 유용한 1~2문장 한국어 설명, keywords는 한국어/영어 검색어 3~8개. "
    'JSON만 출력: {"items":[{"kind":"","title":"","description":"","keywords":[]}]}. 요소가 없으면 {"items":[]}.'
)
HANDWRITING_PROMPT = (
    BASE_PROMPT
    + " 추가로, 페이지에 손글씨(펜 필기, 메모, 밑줄 옆 주석 등)가 있으면 kind를 handwriting으로 하는 항목을 하나 더 넣고, "
    "title은 필기 주제 요약(10단어 이하), description에는 손글씨 내용을 읽은 그대로 받아 적어라(인쇄된 글자는 제외, "
    "최대 1500자, 못 읽는 글자는 ?로). 손글씨가 없으면 handwriting 항목을 넣지 마라."
)


def prompt() -> str:
    return HANDWRITING_PROMPT if settings.handwriting else BASE_PROMPT


def prompt_version() -> str:
    return "v2-hw" if settings.handwriting else "v1"


def max_tokens() -> int:
    return 2500 if settings.handwriting else 800


class QuotaError(Exception):
    pass


def _parse(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    items = []
    for it in data.get("items", []):
        kind = str(it.get("kind", "figure")).lower()
        items.append({
            "kind": kind if kind in KINDS else "figure",
            "title": str(it.get("title", ""))[:200],
            "description": str(it.get("description", ""))[:2000 if kind == "handwriting" else 600],
            "keywords": [str(k) for k in it.get("keywords", [])][:10],
        })
    return items


def _gemini(client: httpx.Client, jpeg: bytes) -> list[dict]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}:generateContent"
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
            {"text": prompt()},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1,
                             "maxOutputTokens": max_tokens()},
    }
    r = client.post(url, params={"key": settings.gemini_api_key}, json=body)
    if r.status_code in (429, 403):
        raise QuotaError(f"gemini {r.status_code}")
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    return _parse("".join(p.get("text", "") for p in parts))


def _openai(client: httpx.Client, jpeg: bytes) -> list[dict]:
    body = {
        "model": settings.openai_model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt()},
            {"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode(),
                # 손글씨는 저해상도(low)로는 읽기 어려움 → 필기 인식 시에만 high
                "detail": "high" if settings.handwriting else "low"}},
        ]}],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": max_tokens(),
    }
    r = client.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"}, json=body)
    if r.status_code in (429, 403):
        raise QuotaError(f"openai {r.status_code}")
    r.raise_for_status()
    return _parse(r.json()["choices"][0]["message"]["content"] or "{}")


class VisionRouter:
    def __init__(self, db):
        self.db = db
        self.calls = {"gemini": 0, "openai": 0, "cache": 0, "failed": 0}
        self.client = httpx.Client(timeout=60)
        self.disabled: set[str] = set()

    def providers(self) -> list[str]:
        mode = settings.vision_provider
        order = {"auto": ["gemini", "openai"], "gemini": ["gemini"], "openai": ["openai"]}.get(mode, [])
        keys = {"gemini": settings.gemini_api_key, "openai": settings.openai_api_key}
        return [p for p in order if keys[p] and p not in self.disabled]

    @property
    def enabled(self) -> bool:
        return bool(self.providers())

    def budget_left(self) -> bool:
        cap = settings.vision_max_calls
        return cap <= 0 or (self.calls["gemini"] + self.calls["openai"]) < cap

    def describe(self, jpeg: bytes) -> tuple[list[dict], str] | None:
        """(항목 목록, 사용한 제공자) 반환. AI를 못 쓰면 None."""
        h = hashlib.sha256(jpeg + prompt_version().encode()).hexdigest()
        cached = self.db.cache_get(h)
        if cached is not None:
            self.calls["cache"] += 1
            return cached["items"], cached.get("provider", "cache")
        if not self.budget_left():
            return None
        for p in self.providers():
            try:
                items = (_gemini if p == "gemini" else _openai)(self.client, jpeg)
                self.calls[p] += 1
                self.db.cache_put(h, p, {"items": items, "provider": p})
                return items, p
            except QuotaError as e:
                log.warning("%s 한도 초과 → 다음 제공자로 전환: %s", p, e)
                self.disabled.add(p)
            except Exception as e:  # 네트워크/파싱 오류: 이번 페이지만 다음 제공자로
                log.warning("%s 실패: %s", p, e)
        self.calls["failed"] += 1
        return None
