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
import re
import threading
import time

import httpx

from .config import settings

log = logging.getLogger(__name__)

KINDS = ["figure", "table", "flowchart", "chart", "diagram", "photo", "screenshot", "handwriting"]
BASE_PROMPT = (
    "이 문서 페이지 이미지에 있는 시각 요소(그림, 표, 플로우차트, 차트, 다이어그램, 사진, 스크린샷)를 모두 찾아라. "
    "본문 텍스트만 있는 영역은 무시. 다음은 시각 요소가 아니므로 절대 넣지 마라: 로고·엠블럼·워터마크, "
    "머리글/바닥글/쪽번호, 페이지 테두리·장식선, 빈 답안지·원고지·괘선(줄만 있는 칸), 빈 양식, 배경 무늬, "
    "목차·차례·색인(찾아보기)·단순 항목/문제 목록 페이지. "
    "각 요소마다 kind는 "
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
    return "v3-hw" if settings.handwriting else "v3"


def analysis_signature() -> str:
    """분석 방식 식별자. 이게 바뀌면(AI 지시 변경, 키 추가 등) 이미 색인한 파일도 다시 분석."""
    ai = (settings.vision_provider in ("auto", "gemini") and settings.gemini_api_key) or \
         (settings.vision_provider in ("auto", "openai") and settings.openai_api_key)
    # "-f1": 결과 거르기 규칙 버전. 규칙만 바뀌면 AI를 다시 부르지 않고(캐시) 결과만 다시 정리
    return (prompt_version() if ai else "local") + "-f2"


_JUNK = ("로고", "logo", "엠블럼", "emblem", "워터마크", "watermark", "심볼마크", "바닥글", "머리글", "footer", "header")


# 목차·색인·단순 목록: 검색해도 의미 없는 페이지
_TOC = ("목차", "차례", "contents", "찾아보기", "색인", "index")
_LIST = ("목록", "리스트", "list")
_KEEP = ("체크리스트", "checklist", "check list")


def is_junk(item: dict) -> bool:
    """AI가 지시를 어기고 넣은 로고·워터마크·머리글, 목차·색인·단순 목록 같은 항목인지."""
    if item.get("kind") == "handwriting":
        return False
    title = str(item.get("title", "")).lower()
    text = f"{title} {str(item.get('description', ''))[:80].lower()}"
    if any(w in text for w in _JUNK) or any(w in text for w in _TOC):
        return True
    return any(w in title for w in _LIST) and not any(w in title for w in _KEEP)


_TOC_LINE = re.compile(r"(\.{3,}|·{3,}|…+|-{3,})\s*\d{1,4}\s*$")   # 점선 뒤 쪽 번호 (표의 숫자 열과 구분)


def looks_like_toc(text: str) -> bool:
    """'제1장 총칙 ······ 12'처럼 줄 끝에 쪽 번호가 붙은 줄이 많으면 목차 페이지."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    hits = sum(1 for l in lines if _TOC_LINE.search(l))
    if hits >= 8 and hits >= len(lines) * 0.3:
        return True
    # 점선이 없는 목차: '1. 콘크리트 공사 12'처럼 글 뒤 숫자(쪽 번호)가 줄마다 차례로 커짐
    nums = [int(m.group(1)) for l in lines if (m := _TAIL_NUM.search(l))]
    if len(nums) < 8 or len(nums) < len(lines) * 0.4:
        return False
    rising = sum(1 for a, b in zip(nums, nums[1:]) if b >= a)
    return rising >= (len(nums) - 1) * 0.8 and nums[-1] - nums[0] >= len(nums)


_TAIL_NUM = re.compile(r"[가-힣A-Za-z)\]]\s+(\d{1,4})\s*$")


def max_tokens() -> int:
    return 4096 if settings.handwriting else 1024   # 손글씨를 길게 받아 적다 답이 잘리지 않도록


class QuotaError(Exception):
    """429(요청 한도) 또는 403(키/권한 문제)."""
    def __init__(self, msg: str, status: int = 429, retry_after: float | None = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


def _quota(provider: str, r: httpx.Response) -> QuotaError:
    try:
        ra = float(r.headers.get("retry-after", ""))
    except ValueError:
        ra = None
    return QuotaError(f"{provider} {r.status_code}: {r.text[:200]}", r.status_code, ra)


def _parse(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    # strict=False: 문자열 안의 줄바꿈 등 제어문자 허용 (손글씨 받아 적기에서 자주 나옴)
    data = json.loads(text[text.find("{"): text.rfind("}") + 1], strict=False)
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


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


class ModelNotFound(Exception):
    pass


class ServerBusy(Exception):
    """5xx(서버 과부하) 또는 네트워크 끊김 — 잠시 후 다시 하면 대개 성공."""


class Blocked(Exception):
    """AI가 답을 거부(저작권·안전 필터 등). 다시 보내도 같은 결과라 재시도하지 않음."""


BLOCK_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "OTHER"}
_thinking_cfg_ok = True     # 모델이 thinkingConfig를 거부하면 False로 바꾸고 빼고 보냄
_schema_ok = True           # 모델이 responseSchema를 거부하면 False로 바꾸고 빼고 보냄

# 답 형식을 강제하는 설계도: 이걸 주면 Gemini가 따옴표 등을 올바르게 처리해 JSON이 깨지지 않음
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {"items": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING", "enum": KINDS},
            "title": {"type": "STRING"},
            "description": {"type": "STRING"},
            "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["kind", "title", "description"],
    }}},
    "required": ["items"],
}


def _gemini(client: httpx.Client, jpeg: bytes, model: str | None = None) -> list[dict]:
    global _thinking_cfg_ok, _schema_ok
    model = model or settings.gemini_model
    gen = {"responseMimeType": "application/json", "temperature": 0.1, "maxOutputTokens": max_tokens()}
    if _schema_ok:
        gen["responseSchema"] = RESPONSE_SCHEMA
    if _thinking_cfg_ok:
        # '생각(thinking)' 토큰은 출력 요금으로 청구되고 답 길이 한도도 잡아먹음 → 최소로
        gen["thinkingConfig"] = {"thinkingLevel": "minimal"}
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
            {"text": prompt()},
        ]}],
        "generationConfig": gen,
    }
    # 키는 주소(URL)가 아니라 헤더로 보냄 → 로그에 키가 찍히지 않음
    r = client.post(f"{GEMINI_BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": settings.gemini_api_key}, json=body)
    if r.status_code == 404:
        raise ModelNotFound(model)
    if r.status_code == 400 and _thinking_cfg_ok and "think" in r.text.lower():
        _thinking_cfg_ok = False                 # 이 모델은 해당 설정을 지원 안 함 → 빼고 다시
        return _gemini(client, jpeg, model)
    if r.status_code == 400 and _schema_ok and "schema" in r.text.lower():
        _schema_ok = False
        return _gemini(client, jpeg, model)
    if r.status_code in (400, 401) and "api key" in r.text.lower().replace("_", " "):
        raise QuotaError(f"gemini API 키 오류: {r.text[:200]}", 401)
    if r.status_code in (402, 429, 403):   # 402 = 결제 문제(잔액·카드), 403 = 권한
        raise _quota("gemini", r)
    if r.status_code >= 500:
        raise ServerBusy(f"gemini {r.status_code}")
    r.raise_for_status()
    data = r.json()
    block = (data.get("promptFeedback") or {}).get("blockReason")
    cands = data.get("candidates") or []
    if block or not cands:
        raise Blocked(block or "no candidates")
    cand = cands[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text and cand.get("finishReason") in BLOCK_REASONS:
        raise Blocked(cand["finishReason"])
    return _parse(text)


def _version(name: str) -> tuple:
    import re
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return (float(m.group(1)),) if m else (0.0,)


def pick_gemini_model(names: list[str]) -> str | None:
    """사용 가능한 모델 중 가장 저렴한 계열(최신 Flash-Lite → Flash)을 고름."""
    import re
    clean = [n.removeprefix("models/") for n in names]
    clean = [n for n in clean if not re.search(r"preview|exp|tts|image|live|audio|embedding|thinking", n)]
    for family in (r"^gemini-[\d.]+-flash-lite(-latest|-\d+)?$", r"^gemini-[\d.]+-flash(-latest|-\d+)?$"):
        cands = [n for n in clean if re.match(family, n)]
        if cands:
            return max(cands, key=lambda n: (_version(n), n.endswith("latest") is False, n))
    return None


def list_gemini_models(client: httpx.Client) -> list[str]:
    names, token = [], None
    while True:
        r = client.get(f"{GEMINI_BASE}/models", headers={"x-goog-api-key": settings.gemini_api_key},
                       params={"pageSize": 1000, **({"pageToken": token} if token else {})})
        r.raise_for_status()
        data = r.json()
        names += [m["name"] for m in data.get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
        token = data.get("nextPageToken")
        if not token:
            return names


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
    if r.status_code in (402, 429, 403):   # 402 = 결제 문제(잔액·카드), 403 = 권한
        raise _quota("openai", r)
    if r.status_code >= 500:
        raise ServerBusy(f"openai {r.status_code}")
    r.raise_for_status()
    return _parse(r.json()["choices"][0]["message"]["content"] or "{}")


class VisionRouter:
    def __init__(self, db):
        self.db = db
        self.calls = {"gemini": 0, "openai": 0, "cache": 0, "failed": 0}
        self.client = httpx.Client(timeout=60)
        self.disabled: set[str] = set()
        self.sleep = time.sleep          # 테스트에서 교체 가능
        self.gemini_model = settings.gemini_model
        self.model_lock = threading.Lock()
        self.retry_waits = (10, 30, 60)  # 분당 요청 한도(무료 등급) 대비: 기다렸다 재시도
        self.last_error = ""             # 화면에 보여 줄 마지막 문제 (키 오류, 한도 소진 등)

    @property
    def configured(self) -> bool:
        """설정상 AI를 쓰기로 되어 있는지 (한도 초과로 잠시 꺼진 것과 무관)."""
        mode = settings.vision_provider
        return (mode in ("auto", "gemini") and bool(settings.gemini_api_key)) or \
               (mode in ("auto", "openai") and bool(settings.openai_api_key))

    def providers(self) -> list[str]:
        mode = settings.vision_provider
        order = {"auto": ["gemini", "openai"], "gemini": ["gemini"], "openai": ["openai"]}.get(mode, [])
        keys = {"gemini": settings.gemini_api_key, "openai": settings.openai_api_key}
        return [p for p in order if keys[p] and p not in self.disabled]

    @property
    def enabled(self) -> bool:
        return bool(self.providers())

    def _switch_gemini_model(self, missing: str) -> bool:
        with self.model_lock:
            if self.gemini_model != missing:      # 다른 스레드가 이미 바꿈
                return True
            try:
                new = pick_gemini_model(list_gemini_models(self.client))
            except Exception as e:
                log.warning("Gemini 모델 목록 조회 실패: %s", e)
                return False
            if not new or new == missing:
                return False
            log.warning("Gemini 모델 '%s' 사용 불가 → '%s'로 자동 전환 (.env의 GEMINI_MODEL도 바꿔 두세요)",
                        missing, new)
            self.gemini_model = new
            return True

    def budget_left(self) -> bool:
        cap = settings.vision_max_calls
        return cap <= 0 or (self.calls["gemini"] + self.calls["openai"]) < cap

    def describe(self, jpeg: bytes) -> tuple[list[dict] | None, str] | None:
        """(항목 목록, 사용한 제공자) 반환. AI를 못 쓰면 None. AI가 거부하면 (None, "blocked")."""
        h = hashlib.sha256(jpeg + prompt_version().encode()).hexdigest()
        cached = self.db.cache_get(h)
        if cached is not None:
            self.calls["cache"] += 1
            return cached["items"], cached.get("provider", "cache")   # items가 None이면 '거부됨'(로컬로 대체)
        if not self.budget_left():
            return None
        for p in self.providers():
            parse_retry_used = False
            for attempt in range(len(self.retry_waits) + 2):
                try:
                    if p == "gemini":
                        items = _gemini(self.client, jpeg, self.gemini_model)
                    else:
                        items = _openai(self.client, jpeg)
                    self.calls[p] += 1
                    self.db.cache_put(h, p, {"items": items, "provider": p})
                    return items, p
                except QuotaError as e:
                    # 429는 대개 '분당 한도' → 잠깐 기다리면 풀림. 바로 비싼 제공자로 넘어가지 않음
                    if e.status == 429 and attempt < len(self.retry_waits):
                        wait = min(e.retry_after or self.retry_waits[attempt], 120)
                        log.warning("%s 요청 한도 → %ss 후 재시도", p, wait)
                        self.sleep(wait)
                        continue
                    log.warning("%s 사용 불가 → 다음 제공자로 전환: %s", p, e)
                    self.last_error = {
                        401: f"{p}: API 키가 잘못됐어요 (.env 확인)",
                        402: f"{p}: 결제 확인 필요 (AI Studio → Billing에서 잔액·카드 상태 확인)",
                    }.get(e.status, f"{p}: 사용 한도 소진/권한 없음 → 나머지는 다음 색인 때 이어서")
                    self.disabled.add(p)
                    break
                except Blocked as e:
                    # 저작권·안전 필터로 거부 → 다시 보내도 같으므로 결과를 저장해 두고 로컬 분석으로 대체
                    log.warning("%s 답변 거부(%s) → 이 페이지는 로컬 분석으로 대체", p, e)
                    self.calls["blocked"] = self.calls.get("blocked", 0) + 1
                    self.db.cache_put(h, "blocked", {"items": None, "provider": "blocked"})
                    return None, "blocked"
                except ModelNotFound as e:
                    # 모델이 없어졌거나 내 키로 못 씀 → 쓸 수 있는 가장 저렴한 모델을 자동으로 찾음
                    if self._switch_gemini_model(str(e)):
                        continue
                    log.warning("사용 가능한 Gemini 모델을 찾지 못함 → Gemini 사용 중지")
                    self.last_error = "gemini: 사용 가능한 모델이 없어요 (.env의 GEMINI_MODEL 확인)"
                    self.disabled.add(p)
                    break
                except (ServerBusy, httpx.TransportError) as e:
                    # 서버 과부하(503)·연결 끊김 → 기다렸다 재시도. 계속 안 되면 이 페이지만 다음으로 미룸
                    if attempt < len(self.retry_waits):
                        wait = self.retry_waits[attempt]
                        log.warning("%s 서버 응답 없음(%s) → %ss 후 재시도", p, e, wait)
                        self.sleep(wait)
                        continue
                    log.warning("%s 계속 응답 없음 → 이 페이지는 다음 색인 때 다시 시도", p)
                    break
                except (ValueError, KeyError, IndexError) as e:
                    # 답이 잘리거나 형식이 깨짐 → 바로 한 번 더 요청
                    if not parse_retry_used:
                        parse_retry_used = True
                        log.warning("%s 응답 형식 오류 → 다시 요청: %s", p, e)
                        continue
                    log.warning("%s 응답 형식 오류 반복 → 이 페이지는 다음 색인 때 다시 시도", p)
                    break
                except Exception as e:  # 그 밖의 오류: 이번 페이지만 다음 제공자로
                    log.warning("%s 실패: %s", p, e)
                    break
        self.calls["failed"] += 1
        return None
