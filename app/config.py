"""환경 변수 기반 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "data")))
    page_dpi: int = field(default_factory=lambda: _int("PAGE_DPI", 110))
    thumb_px: int = 360
    # 보기 전용 사이트를 내보낼 폴더 (예: NAS의 web 공유폴더). 설정하면 색인 끝날 때 자동 내보내기
    export_dir: str = field(default_factory=lambda: os.getenv("EXPORT_DIR", ""))
    # 원본 PDF도 NAS로 복사 → 태블릿에서 '원본 열기'로 해당 쪽을 바로 봄 (처음 한 번은 용량만큼 복사 시간)
    export_originals: bool = field(default_factory=lambda: os.getenv("EXPORT_ORIGINALS", "on").lower() in ("on", "1", "true", "yes"))
    # 폴더 칸에 처음 채워 둘 기본 폴더 (화면에서 바꿀 수 있음)
    default_folder: str = field(default_factory=lambda: os.getenv("DEFAULT_FOLDER", r"G:\내 드라이브\건축시공기술사"))

    service_account_file: str = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
    oauth_client_file: str = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_FILE", ""))

    vision_provider: str = field(default_factory=lambda: os.getenv("VISION_PROVIDER", "auto").lower())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-nano"))
    vision_max_calls: int = field(default_factory=lambda: _int("VISION_MAX_CALLS", 500))
    vision_image_px: int = field(default_factory=lambda: _int("VISION_IMAGE_PX", 768))
    # 동시에 AI로 보내는 페이지 수 (유료 등급이면 8까지 올리면 빨라짐, 무료 등급은 2~4 권장)
    vision_concurrency: int = field(default_factory=lambda: max(1, _int("VISION_CONCURRENCY", 3)))
    # 필기 인식: 손글씨를 받아 적어 검색 가능하게 함 (on/off). 켜면 이미지를 조금 더 크게 보냄
    handwriting: bool = field(default_factory=lambda: os.getenv("HANDWRITING", "on").lower() in ("on", "1", "true", "yes"))
    handwriting_image_px: int = field(default_factory=lambda: _int("HANDWRITING_IMAGE_PX", 1280))

    # 의미 검색 (뜻이 비슷한 페이지 찾기): on/off, 임베딩 모델
    semantic: bool = field(default_factory=lambda: os.getenv("SEMANTIC_SEARCH", "on").lower() in ("on", "1", "true", "yes"))
    embed_model: str = field(default_factory=lambda: os.getenv("EMBED_MODEL", "gemini-embedding-001"))
    # ① 깊은 이해(페이지별 개념·요약·질문 정리): on/off, 시험용 최대 쪽수(0 = 전체)
    enrich: bool = field(default_factory=lambda: os.getenv("ENRICH", "on").lower() in ("on", "1", "true", "yes"))
    enrich_max_pages: int = field(default_factory=lambda: _int("ENRICH_MAX_PAGES", 0))

    @property
    def ai_image_px(self) -> int:
        return max(self.vision_image_px, self.handwriting_image_px) if self.handwriting else self.vision_image_px

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite3"

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "files"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.pages_dir, self.thumbs_dir, self.cache_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
