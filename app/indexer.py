"""폴더 색인 작업 (백그라운드 스레드)."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path

from . import extract
from .config import settings
from .db import DB
from .vision import VisionRouter

log = logging.getLogger(__name__)


class IndexJob:
    def __init__(self, db: DB):
        self.db = db
        self.thread: threading.Thread | None = None
        self.state: dict = {"running": False}

    def status(self) -> dict:
        return dict(self.state)

    def start(self, folder_id: str, drive_factory=None) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self.state = {"running": True, "folder_id": folder_id, "phase": "폴더 탐색 중", "done": 0, "total": 0,
                      "skipped": 0, "errors": [], "current": "", "ai_calls": {}, "started": time.time()}
        self.thread = threading.Thread(target=self._run, args=(folder_id, drive_factory), daemon=True)
        self.thread.start()
        return True

    def _run(self, folder_id: str, drive_factory) -> None:
        st = self.state
        try:
            from .drive import Drive
            drive = (drive_factory or Drive)()
            self.db.set_meta("folder_id", folder_id)
            try:
                self.db.set_meta("folder_name", drive.folder_name(folder_id))
            except Exception:
                pass
            files = list(drive.walk(folder_id))
            st["total"] = len(files)
            st["phase"] = "색인 중"
            router = VisionRouter(self.db)
            st["ai_enabled"] = router.enabled

            live_ids = {f["file_id"] for f in files}
            for gone in self.db.all_file_ids() - live_ids:      # 폴더에서 사라진 파일 제거
                self.db.delete_file(gone)

            for meta in files:
                st["current"] = f"{meta['path']}/{meta['name']}"
                if self.db.file_version(meta["file_id"]) == meta["version_key"] and meta["version_key"]:
                    st["skipped"] += 1          # 변경 없음 → 다운로드/AI 모두 생략
                    st["done"] += 1
                    continue
                try:
                    local = drive.fetch(meta, settings.cache_dir)
                    self.index_local_file(meta, local, router)
                except Exception as e:
                    log.exception("색인 실패: %s", meta["name"])
                    st["errors"].append(f"{meta['name']}: {e}")
                st["done"] += 1
                st["ai_calls"] = dict(router.calls)
            st["ai_calls"] = dict(router.calls)
            st["phase"] = "완료"
        except Exception as e:
            st["phase"] = "오류"
            st["errors"].append(str(e))
            log.error(traceback.format_exc())
        finally:
            st["running"] = False
            st["finished"] = time.time()

    def index_local_file(self, meta: dict, local: Path, router: VisionRouter | None) -> int:
        """로컬 파일 하나를 분석·렌더·저장. 저장된 항목 수 반환."""
        pages_out = []
        n_items = 0
        for pi in extract.analyze(local, dpi=settings.page_dpi):
            key = f"{meta['file_id']}_{pi.page_no}"
            items: list[dict] = []
            if pi.has_visual and router and router.enabled:
                res = router.describe(extract.shrink_for_ai(pi.page_png, settings.vision_image_px))
                if res is not None:
                    ai_items, provider = res
                    items = [dict(it, source=provider) for it in ai_items]
                    # 로컬 캡션은 정확한 제목이므로 AI 결과에 덧붙여 보존
                    titles = " ".join(i.get("title", "") for i in items)
                    items += [dict(c, source="local") for c in pi.captions if c["title"] not in titles]
            if not items and pi.has_visual:
                items = pi.local_items()
            if not items:
                continue    # 시각 요소 없는 페이지는 색인·렌더 생략 (저장공간/시간 절약)
            extract.save_renders(pi.page_png, settings.pages_dir / f"{key}.webp",
                                 settings.thumbs_dir / f"{key}.webp", settings.thumb_px)
            pages_out.append({"page_key": key, "page_no": pi.page_no, "width": pi.width, "height": pi.height,
                              "text": pi.text, "items": items})
            n_items += len(items)
        meta = dict(meta)
        self.db.save_file(meta, pages_out)
        # 원본 캐시 파일은 렌더 후 삭제해 디스크 절약
        try:
            if local.parent == settings.cache_dir:
                local.unlink()
        except OSError:
            pass
        return n_items
