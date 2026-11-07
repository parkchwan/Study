"""동의어 사전: '정확한 단어가 아니어도' 찾게 해 주는 검색 보조.

- 색인된 자료에서 자주 나오는 용어를 모아 Gemini에게 한꺼번에 보내
  동의어·영어·외래어·약어(JSP↔Jumbo Special Pile)·띄어쓰기 변형을 받아 둠
- 글자만 보내므로 매우 저렴 (용어 수천 개에 수십 원 수준), 새 용어만 추가로 보냄
- 실제 검색(띄어쓰기 무시, 일부 단어만 맞아도 관련도 순 정렬)은 브라우저에서 처리
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter

import httpx

from .config import settings

log = logging.getLogger(__name__)

# AI 없이도 기본으로 쓰는 건설 용어 동의어 (AI 사전이 만들어지면 함께 사용)
SEED: list[list[str]] = [
    ["속도", "스피드", "speed", "rate", "velocity", "속력"],
    ["타설", "콘크리트 타설", "placing", "pouring", "부어넣기", "부어 넣기"],
    ["타설속도", "타설 속도", "타설 스피드", "placing rate", "pouring rate", "타설 speed"],
    ["콘크리트", "concrete", "콘크리이트"],
    ["철근", "rebar", "reinforcing bar", "reinforcement"],
    ["거푸집", "form", "formwork", "형틀"],
    ["동바리", "support", "shoring", "서포트"],
    ["양생", "curing", "큐어링"],
    ["균열", "crack", "크랙", "갈라짐"],
    ["강도", "strength", "압축강도"],
    ["방수", "waterproofing", "waterproof", "워터프루핑"],
    ["흙막이", "earth retaining", "토류벽", "retaining wall"],
    ["굴착", "excavation", "터파기", "굴토"],
    ["말뚝", "pile", "파일"],
    ["JSP", "jumbo special pile", "점보 스페셜 파일", "점보스페셜파일"],
    ["SCW", "soil cement wall", "소일 시멘트 월"],
    ["CIP", "cast in place pile", "현장타설 말뚝", "주열식 흙막이"],
    ["RCD", "reverse circulation drill", "역순환 공법"],
    ["PHC", "PHC 파일", "pretensioned spun high strength concrete pile", "고강도 콘크리트 파일"],
    ["비계", "scaffold", "scaffolding", "스캐폴드"],
    ["양중", "hoisting", "lifting", "인양"],
    ["품질관리", "품질 관리", "quality control", "QC"],
    ["안전관리", "안전 관리", "safety management"],
    ["단열", "insulation", "보온", "인슐레이션"],
    ["슬럼프", "slump"],
    ["블리딩", "bleeding", "블리이딩"],
    ["레이턴스", "laitance"],
    ["콜드조인트", "cold joint", "콜드 조인트"],
    ["매스콘크리트", "mass concrete", "매스 콘크리트"],
    ["폭렬", "spalling", "폭열"],
    ["측압", "lateral pressure", "거푸집 측압"],
]

JOSA = ("에서는", "으로는", "에서", "으로", "에는", "에게", "까지", "부터", "보다", "이며", "하는", "하여", "되는",
        "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "및")
STOP = {"그림", "사진", "표", "도표", "다이어그램", "플로우차트", "필기", "설명", "내용", "구조", "방법", "경우", "관련",
        "위한", "대한", "통한", "따른", "있는", "없는", "한다", "된다", "등의", "그리고", "또는"}


def norm(s: str) -> str:
    """비교용 정규화: 소문자, 띄어쓰기·기호 제거 (타설 속도 == 타설속도)."""
    return re.sub(r"[\s\-_·.,/()\[\]{}:;'\"!?~]+", "", (s or "").lower())


def _tokens(text: str):
    for w in re.findall(r"[가-힣A-Za-z][가-힣A-Za-z0-9\-]{1,30}", text or ""):
        if re.fullmatch(r"[가-힣]+", w):
            for j in JOSA:
                if w.endswith(j) and len(w) - len(j) >= 2:
                    w = w[: -len(j)]
                    break
        w = w.strip("-")
        if len(w) >= 2 and w not in STOP:
            yield w


def collect_terms(db, limit: int = 4000) -> list[str]:
    """색인된 자료에서 검색에 쓰일 만한 용어를 빈도순으로 모음."""
    strong, body = Counter(), Counter()
    for r in db.conn.execute("SELECT title, description, keywords FROM items"):
        for w in _tokens(f"{r['title'] or ''} {r['keywords'] or ''} {(r['description'] or '')[:600]}"):
            strong[w] += 1
    for r in db.conn.execute("SELECT substr(text, 1, 3000) AS t FROM pages"):
        body.update(_tokens(r["t"]))
    terms = Counter()
    for w, c in strong.items():
        terms[w] += c * 3
    for w, c in body.items():
        if c >= 5:
            terms[w] += c
    return [w for w, _ in terms.most_common(limit)]


PROMPT = (
    "다음은 건축·건설 기술 문서(건축시공기술사 수험 자료)에 나오는 용어 목록이다. 각 용어마다, 사람이 검색할 때 "
    "같은 것을 가리키려고 입력할 수 있는 다른 표현을 최대 8개 적어라: 동의어, 영어 원어·번역, 한글 외래어 표기"
    "(예: 속도↔스피드↔speed), 약어↔풀네임(예: JSP↔Jumbo Special Pile, SCW↔Soil Cement Wall), 한자어·순우리말, "
    "띄어쓰기·표기 변형(예: 타설속도↔타설 속도). 뜻이 다른 상위·하위 개념이나 관련어는 넣지 마라. "
    "일반 단어거나 확실하지 않으면 synonyms를 빈 배열로 둬라. 입력 용어를 그대로 term에 적어라.\n용어 목록:\n"
)
SCHEMA = {
    "type": "OBJECT",
    "properties": {"groups": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {"term": {"type": "STRING"}, "synonyms": {"type": "ARRAY", "items": {"type": "STRING"}}},
        "required": ["term", "synonyms"],
    }}},
    "required": ["groups"],
}


def _ask_gemini(client: httpx.Client, model: str, terms: list[str]) -> list[dict]:
    from .vision import GEMINI_BASE
    body = {
        "contents": [{"parts": [{"text": PROMPT + "\n".join(terms)}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                             "temperature": 0.1, "maxOutputTokens": 16000},
    }
    for wait in (0, 10, 30, 60):
        if wait:
            time.sleep(wait)
        r = client.post(f"{GEMINI_BASE}/models/{model}:generateContent",
                        headers={"x-goog-api-key": settings.gemini_api_key}, json=body)
        if r.status_code == 429 or r.status_code >= 500:
            continue
        r.raise_for_status()
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return json.loads(text[text.find("{"): text.rfind("}") + 1], strict=False).get("groups", [])
    raise RuntimeError("Gemini 응답 없음 (한도/서버 과부하)")


def build(db, model: str | None = None, batch: int = 150, progress=None) -> int:
    """아직 사전에 없는 용어만 AI에게 물어 저장. 새로 추가된 용어 수 반환."""
    if not settings.gemini_api_key or settings.vision_provider not in ("auto", "gemini"):
        return 0
    done = db.thesaurus_terms()
    todo = [t for t in collect_terms(db) if norm(t) not in done]
    if not todo:
        return 0
    model = model or settings.gemini_model
    added = 0
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(todo), batch):
            chunk = todo[i: i + batch]
            try:
                groups = _ask_gemini(client, model, chunk)
            except Exception as e:
                log.warning("동의어 사전 만들기 중단: %s", e)
                break
            got = {norm(g.get("term", "")): [s for s in g.get("synonyms", []) if s and len(s) <= 60][:8]
                   for g in groups}
            db.thesaurus_save({norm(t): [t] + got.get(norm(t), []) for t in chunk})
            added += len(chunk)
            if progress:
                progress(added, len(todo))
    return added


def groups(db) -> list[list[str]]:
    """브라우저로 보낼 동의어 묶음 (기본 사전 + AI 사전, 동의어가 있는 것만)."""
    out = [g for g in SEED]
    out += [g for g in db.thesaurus_groups() if len(g) > 1]
    return out
