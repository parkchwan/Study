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
        if s.exists() and (not d.exists() or d.stat().st_size != s.stat().st_size):
            shutil.copyfile(s, d)
    for d in dst.glob("*.webp"):
        if d.name not in keep:
            d.unlink()


def export_site(db: DB, out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = db.export_pages()
    data = {
        "meta": {"folder_name": db.get_meta("folder_name"),
                 "exported_at": time.strftime("%Y-%m-%d %H:%M")},
        "pages": pages,
    }
    tmp = out_dir / "data.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
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
