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

    service_account_file: str = field(default_factory=lambda: os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", ""))
    oauth_client_file: str = field(default_factory=lambda: os.getenv("GOOGLE_OAUTH_CLIENT_FILE", ""))

    vision_provider: str = field(default_factory=lambda: os.getenv("VISION_PROVIDER", "auto").lower())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-nano"))
    vision_max_calls: int = field(default_factory=lambda: _int("VISION_MAX_CALLS", 500))
    vision_image_px: int = field(default_factory=lambda: _int("VISION_IMAGE_PX", 768))

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
