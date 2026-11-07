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
from .vision import VisionRouter, analysis_signature

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
            # 이번에 색인하는 폴더 '안에서' 사라진 파일만 정리 (다른 폴더로 색인해 둔 자료는 그대로 둠)
            root_key = getattr(drive, "root_key", None)
            scope = self.db.files_under(root_key) if root_key else self.db.drive_api_files()
            for gone in scope - live_ids:
                self._remove_renders(self.db.page_keys(gone))
                self.db.delete_file(gone)

            for meta in files:
                st["current"] = f"{meta['path'].rstrip('/')}/{meta['name']}"
                expected = f"{meta['version_key']}|{analysis_signature()}"
                if meta["version_key"] and self.db.file_version(meta["file_id"]) == expected:
                    st["skipped"] += 1          # 변경 없음 → 다운로드/AI 모두 생략
                    st["done"] += 1
                    continue
                try:
                    local = drive.fetch(meta, settings.cache_dir)
                    self.index_local_file(meta, local, router)
                except extract.OfficeUnavailable:
                    # LibreOffice가 없으면 엑셀·워드·파워포인트는 조용히 건너뜀 (오류 아님)
                    log.warning("건너뜀(LibreOffice 없음): %s", meta["name"])
                    st["skipped_office"] = st.get("skipped_office", 0) + 1
                except Exception as e:
                    log.exception("색인 실패: %s", meta["name"])
                    st["errors"].append(f"{meta['name']}: {e}")
                st["done"] += 1
                st["ai_calls"] = dict(router.calls)
                st["gemini_model"] = router.gemini_model
                st["ai_error"] = router.last_error
                self._maybe_export(st)
            st["ai_calls"] = dict(router.calls)
            st["ai_error"] = router.last_error
            if settings.export_dir:            # NAS 보기 전용 사이트 자동 갱신
                st["phase"] = "NAS로 내보내는 중"
                from .export import export_site
                try:
                    export_site(self.db, Path(settings.export_dir))
                except OSError as e:
                    st["errors"].append(f"내보내기 실패: {e}")
            st["phase"] = "완료"
        except Exception as e:
            st["phase"] = "오류"
            st["errors"].append(str(e))
            log.error(traceback.format_exc())
        finally:
            st["running"] = False
            st["finished"] = time.time()

    def _maybe_export(self, st: dict, every_sec: int = 600) -> None:
        """며칠씩 걸리는 색인 중에도 태블릿에서 볼 수 있게 10분마다 NAS로 중간 내보내기."""
        if not settings.export_dir or time.time() - st.get("last_export", st.get("started", 0)) < every_sec:
            return
        from .export import export_site
        try:
            export_site(self.db, Path(settings.export_dir))
            st["last_export"] = time.time()
        except OSError as e:
            st["last_export"] = time.time()
            log.warning("NAS 중간 내보내기 실패: %s", e)

    @staticmethod
    def _remove_renders(keys: set[str]) -> None:
        for k in keys:
            for d in (settings.pages_dir, settings.thumbs_dir):
                try:
                    (d / f"{k}.webp").unlink()
                except OSError:
                    pass

    def index_local_file(self, meta: dict, local: Path, router: VisionRouter | None) -> int:
        """로컬 파일 하나를 분석·렌더·저장. 저장된 항목 수 반환.

        - 페이지를 하나씩 읽어 메모리를 적게 씀 (수백 MB 스캔 교재 대응)
        - AI 호출은 VISION_CONCURRENCY 개씩 동시에 보내 속도 향상
        """
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor

        st = self.state
        st["page"], st["pages"] = 0, (extract.count_pages(local) if local.suffix.lower() == ".pdf" else 0)
        use_ai = router is not None and router.configured
        conc = settings.vision_concurrency if use_ai else 1
        out = {"pages": [], "items": 0, "incomplete": False}

        def finish(pi, wants_ai, fut):
            key = f"{meta['file_id']}_{pi.page_no}"
            items: list[dict] = []
            res = None
            if wants_ai:
                res = fut.result() if fut is not None else None
                if res is None:
                    out["incomplete"] = True      # AI 분석 못 함 → 다음 색인 때 다시 시도
                elif res[0] is None:
                    res = None                    # AI가 거부한 페이지 → 아래에서 로컬 분석으로 대체(재시도 안 함)
                else:
                    ai_items, provider = res
                    items = [dict(it, source=provider) for it in ai_items]
                    # 로컬 캡션은 정확한 제목이므로 AI 결과에 덧붙여 보존
                    titles = " ".join(i.get("title", "") for i in items)
                    items += [dict(c, source="local") for c in pi.captions if c["title"] not in titles]
            if res is None and not items and pi.has_visual:
                # AI를 못 쓴 경우에만 로컬 추정으로 채움. AI가 '시각자료 없음'이라 답하면 그 판단을 따름
                items = pi.local_items()
            st["page"] = pi.page_no
            if not items:
                return      # 시각 요소 없는 페이지는 색인·렌더 생략 (저장공간/시간 절약)
            extract.save_renders(pi.page_png, settings.pages_dir / f"{key}.webp",
                                 settings.thumbs_dir / f"{key}.webp", settings.thumb_px)
            out["pages"].append({"page_key": key, "page_no": pi.page_no, "width": pi.width,
                                 "height": pi.height, "text": pi.text, "items": items})
            out["items"] += len(items)

        pending: deque = deque()
        with ThreadPoolExecutor(max_workers=conc) as ex:
            for pi in extract.analyze(local, dpi=settings.page_dpi):
                wants_ai = pi.has_visual and use_ai
                fut = None
                if wants_ai and router.enabled:
                    fut = ex.submit(router.describe, extract.shrink_for_ai(pi.page_png, settings.ai_image_px))
                pending.append((pi, wants_ai, fut))
                while len(pending) > conc * 2:       # 메모리에 올려 둔 페이지 수 제한
                    finish(*pending.popleft())
            while pending:
                finish(*pending.popleft())

        meta = dict(meta)
        if out["incomplete"]:
            # 버전을 비워 두면 다음 색인 때 이 파일을 다시 처리 (이미 분석된 페이지는 캐시라 추가 비용 없음)
            meta["version_key"] = ""
            st["incomplete"] = st.get("incomplete", 0) + 1
        elif meta.get("version_key"):
            meta["version_key"] = f"{meta['version_key']}|{analysis_signature()}"
        # 이번에 색인되지 않은 예전 페이지 이미지는 삭제 (로고·빈 양식 등 잘못 잡혔던 페이지 정리)
        self._remove_renders(self.db.page_keys(meta["file_id"]) - {p["page_key"] for p in out["pages"]})
        self.db.save_file(meta, out["pages"])
        # 원본 캐시 파일은 렌더 후 삭제해 디스크 절약
        try:
            if local.parent == settings.cache_dir:
                local.unlink()
        except OSError:
            pass
        return out["items"]
