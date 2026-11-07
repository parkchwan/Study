"""보기 전용 정적 사이트 내보내기.

결과물(index.html + data.json + img/)은 웹 서버만 있으면 열립니다.
Python·Docker가 안 되는 NAS(예: Synology DS216play)의 Web Station에 두고
휴대폰/태블릿에서 24시간 볼 수 있습니다. 검색은 브라우저에서 처리합니다.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from .config import settings
from .db import DB

STATIC = Path(__file__).parent / "static"


def _sync_dir(src: Path, dst: Path, keep: set[str]) -> None:
    """바뀐 이미지만 복사하고, 더 이상 없는 이미지는 삭제 (NAS 전송량 최소화)."""
    dst.mkdir(parents=True, exist_ok=True)
    for name in keep:
        s, d = src / name, dst / name
        if not s.exists():
            continue
        ss = s.stat()
        if d.exists():
            ds = d.stat()
            if ds.st_size == ss.st_size and ds.st_mtime >= ss.st_mtime - 2:   # 같은 파일 → 복사 생략
                continue
        shutil.copy2(s, d)
    for d in dst.glob("*.webp"):
        if d.name not in keep:
            d.unlink()


def build_payload(db: DB) -> tuple[dict, dict, list]:
    """(data, text, thesaurus): 화면이 쓰는 데이터. NAS 내보내기와 PC 화면이 같은 것을 씀."""
    from . import thesaurus
    pages = db.export_pages()
    # 태블릿이 처음 받는 data.json은 최대한 작게: 파일 정보는 한 번만, 항목은 [종류, 제목, 짧은 설명]만.
    # 본문(OCR)·손글씨 전문·키워드는 text.json에 따로 → 화면을 먼저 띄운 뒤 받아서 검색/상세 표시
    files: list[list] = []
    fidx: dict[tuple, int] = {}
    cpages, texts = [], {}
    for p in pages:
        fk = (p["file_name"], p["path"], p["web_link"], p["page_count"], p["v"])
        if fk not in fidx:
            fidx[fk] = len(files)
            files.append(list(fk))
        cpages.append([p["page_key"], fidx[fk], p["page_no"],
                       [[it["kind"], it["title"] or "", (it["description"] or "")[:120], it["source"]]
                        for it in p["items"]]])
        texts[p["page_key"]] = [p["t"], [[it["description"] or "", it["keywords"] or ""] for it in p["items"]]]
    data = {
        "meta": {"folder_name": db.get_meta("folder_name"), "v": 2,
                 "exported_at": time.strftime("%Y-%m-%d %H:%M")},
        "files": files,      # [파일명, 경로, 링크, 총쪽수, 이미지버전]
        "pages": cpages,     # [page_key, 파일번호, 쪽, [[종류, 제목, 짧은설명, 분석]]]
    }
    return data, texts, thesaurus.groups(db)


def export_site(db: DB, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    from . import embed
    data, texts, thes = build_payload(db)
    pages = [{"page_key": p[0]} for p in data["pages"]]
    stamp = time.strftime("%Y%m%d%H%M%S")
    data["meta"]["text"] = f"text.json?{stamp}"
    data["meta"]["thesaurus"] = f"thesaurus.json?{stamp}"
    blob = db.embedding_blob([p["page_key"] for p in pages], embed.DIM)
    if any(blob):
        # 페이지 순서대로 이어 붙인 의미 벡터. API 키는 넣지 않음 (태블릿에서 직접 입력)
        data["meta"]["embed"] = {"file": f"embeddings.bin?{stamp}", "dim": embed.DIM, "model": settings.embed_model}
        b = out_dir / "embeddings.bin.tmp"
        b.write_bytes(blob)
        b.replace(out_dir / "embeddings.bin")
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    for name, obj in (("text.json", texts), ("thesaurus.json", thes)):
        t = out_dir / (name + ".tmp")
        t.write_text(dump(obj), encoding="utf-8")
        t.replace(out_dir / name)
    tmp = out_dir / "data.json.tmp"
    tmp.write_text(dump(data), encoding="utf-8")
    keep = {p["page_key"] + ".webp" for p in pages}
    _sync_dir(settings.pages_dir, out_dir / "img" / "pages", keep)
    _sync_dir(settings.thumbs_dir, out_dir / "img" / "thumbs", keep)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("const STATIC_MODE = false;", "const STATIC_MODE = true;", 1)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    tmp.replace(out_dir / "data.json")          # 마지막에 교체 → 보는 중에도 깨지지 않음
    return {"dir": str(out_dir), "pages": len(pages)}


if __name__ == "__main__":      # python -m app.export [폴더]
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(settings.export_dir)
    print(export_site(DB(settings.db_path), target))
