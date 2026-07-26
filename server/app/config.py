from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# server/app/config.py -> parents[2] == repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    llm_api_key: str = ""
    llm_base_url: str = "https://api.moonshot.cn/v1"
    llm_model: str = "moonshot-v1-128k-vision-preview"  # must be a vision-capable model
    llm_max_tokens: int = 16384  # a full Beamer deck is ~6k-10k tokens; defaults are too low
    llm_timeout_seconds: float = 900.0
    llm_max_retries: int = 2  # OpenAI SDK network-layer retries

    templates_dir: Path = REPO_ROOT / "templates"
    artifact_root: Path = REPO_ROOT / "artifacts"

    pdf_render_dpi: int = 150  # high-res page images sent to generation
    pdf_thumbnail_dpi: int = 60  # thumbnails for page selection
    pdf_max_vision_pages: int = 40  # trigger page selection above this count
    pdf_max_page_pixels: int = 40_000_000  # per-page render clamp (decompression-bomb guard)

    max_upload_bytes: int = 100 * 1024 * 1024  # reject uploads larger than this

    # Keep pages/ and thumbs/ renders after a SUCCESSFUL generation (they are
    # always kept on failure for debugging). Env: TEXDECK_KEEP_PAGE_RENDERS.
    keep_page_renders: bool = False

    xelatex_command: str = "xelatex"
    xelatex_timeout_seconds: float = 120.0
    max_compile_repairs: int = 2

    # absolute env_file so settings load regardless of process cwd
    model_config = SettingsConfigDict(
        env_prefix="TEXDECK_",
        env_file=REPO_ROOT / "server" / ".env",
        extra="ignore",
    )


settings = Settings()
