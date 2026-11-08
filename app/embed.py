"""의미 검색: 페이지의 '뜻'을 숫자 묶음(임베딩)으로 바꿔 두고, 검색어와 뜻이 가까운 페이지를 찾음.

- 색인할 때 AI가 그림을 보고 쓴 설명·손글씨·본문 앞부분을 임베딩 (전체 교재 수십 원 수준)
- 내용이 바뀐 페이지만 다시 계산
- 256차원 int8로 줄여 저장 → 1만 쪽에 약 2.5MB (태블릿에서 가볍게 받음)
"""
from __future__ import annotations

import hashlib
import logging
import math
import time

import httpx

from .config import settings

log = logging.getLogger(__name__)
DIM = 256


def page_text(p: dict, concept: str = "") -> str:
    """임베딩할 글: AI가 정리한 개념·요약·질문 + 파일명 + 그림 설명 + 본문 앞부분."""
    parts = [concept, p["file_name"]] if concept else [p["file_name"]]
    for it in p["items"]:
        parts.append(f"{it['kind']}: {it['title'] or ''} {it['description'] or ''} {it['keywords'] or ''}")
    parts.append((p.get("text") or "")[:600])
    return "\n".join(parts)[:2500]


def _sig(text: str) -> str:
    return hashlib.md5(f"{settings.embed_model}|{DIM}|{text}".encode()).hexdigest()


def quantize(vec: list[float]) -> bytes:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0      # 차원을 줄이면 길이가 1이 아니므로 다시 맞춤
    return bytes((max(-127, min(127, round(v / n * 127))) & 0xFF) for v in vec)


def embed_texts(client: httpx.Client, texts: list[str], task: str) -> list[list[float]]:
    from .vision import GEMINI_BASE
    model = settings.embed_model
    body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                          "taskType": task, "outputDimensionality": DIM} for t in texts]}
    for wait in (0, 10, 30, 60):
        if wait:
            time.sleep(wait)
        r = client.post(f"{GEMINI_BASE}/models/{model}:batchEmbedContents",
                        headers={"x-goog-api-key": settings.gemini_api_key}, json=body)
        if r.status_code == 429 or r.status_code >= 500:
            continue
        r.raise_for_status()
        return [e["values"] for e in r.json()["embeddings"]]
    raise RuntimeError("임베딩 응답 없음 (한도/서버 과부하)")


def enabled() -> bool:
    return bool(settings.gemini_api_key) and settings.vision_provider in ("auto", "gemini") and settings.semantic


def build(db, batch: int = 100, progress=None) -> int:
    """내용이 바뀌었거나 아직 없는 페이지만 임베딩. 새로 계산한 페이지 수 반환."""
    if not enabled():
        return 0
    from .enrich import as_text
    have = db.embedding_sigs()
    concepts = db.concepts()
    todo = []
    for p in db.export_pages(with_text=True):
        t = page_text(p, as_text(concepts.get(p["page_key"])))
        s = _sig(t)
        if have.get(p["page_key"]) != s:
            todo.append((p["page_key"], s, t))
    done = 0
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(todo), batch):
            chunk = todo[i: i + batch]
            try:
                vecs = embed_texts(client, [t for _, _, t in chunk], "RETRIEVAL_DOCUMENT")
            except Exception as e:
                log.warning("의미 검색 준비 중단: %s", e)
                break
            db.embedding_save([(k, s, quantize(v)) for (k, s, _), v in zip(chunk, vecs)])
            done += len(chunk)
            if progress:
                progress(done, len(todo))
    return done


def query_vector(q: str) -> list[float]:
    with httpx.Client(timeout=30) as client:
        v = embed_texts(client, [q], "RETRIEVAL_QUERY")[0]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]
