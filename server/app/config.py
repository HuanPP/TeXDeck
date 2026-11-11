from __future__ import annotations

from pathlib import Path
from typing import Literal

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

    # "auto" lets the page choose (ChatGPT, Claude Code or an API key) and
    # remembers it in model_state_file; api / chatgpt / claude-code pin one.
    llm_provider: Literal["auto", "api", "chatgpt", "claude-code", "agy"] = "auto"
    chatgpt_model: str = "gpt-6.1-sol"
    chatgpt_reasoning_effort: str = ""  # "" leaves the model default; low/medium/high
    chatgpt_auth_file: Path = REPO_ROOT / "server" / ".chatgpt-auth.json"
    claude_code_command: str = "claude"
    claude_code_model: str = "sonnet"  # alias or full name; "" leaves the CLI default
    claude_code_effort: str = ""  # "" leaves the CLI default; low/medium/high/xhigh/max
    # Antigravity CLI (app.agy). It only runs inside bubblewrap, so only on Linux.
    agy_command: str = "agy"
    agy_model: str = ""  # an id from `agy models`; "" leaves the CLI default
    agy_effort: str = ""  # "" leaves the CLI default; low/medium/high/xhigh/max
    bwrap_command: str = "bwrap"
    # agy's own home without accounts (with accounts: accounts/<name>/agy). Never
    # the real home: its settings may let the agent run commands unasked.
    agy_home: Path = REPO_ROOT / "server" / ".agy-home"
    # The page's model choice and any API key entered there (written 0600).
    model_state_file: Path = REPO_ROOT / "server" / ".model-state.json"
    # Sign-in accounts (python -m app.auth add <name>); sign-in is off while empty
    # unless registration is on. Each account's own model settings and sign-ins
    # live in accounts/<name>/ next to this file.
    users_file: Path = REPO_ROOT / "server" / ".users.json"
    # Invite-code sign-up (app.auth): sign-in is required, the first account
    # signs up with the code hi_my_texdeck and then hands out codes.
    registration: bool = False

    templates_dir: Path = REPO_ROOT / "templates"
    # Templates made in the template editor when there are no accounts
    # (with accounts each keeps its own in accounts/<name>/templates/).
    custom_templates_dir: Path = REPO_ROOT / "server" / ".custom-templates"
    artifact_root: Path = REPO_ROOT / "artifacts"
    web_dist_dir: Path = REPO_ROOT / "web" / "dist"  # SPA build; served when present

    pdf_render_dpi: int = 150  # high-res page images sent to generation
    pdf_thumbnail_dpi: int = 60  # thumbnails for page selection
    pdf_max_vision_pages: int = 40  # trigger page selection above this count
    pdf_max_page_pixels: int = 40_000_000  # per-page render clamp (decompression-bomb guard)

    max_upload_bytes: int = 100 * 1024 * 1024  # reject uploads larger than this

    # Keep pages/ and thumbs/ renders after a SUCCESSFUL generation (they are
    # always kept on failure for debugging). Env: TEXDECK_KEEP_PAGE_RENDERS.
    keep_page_renders: bool = False

    # "auto" prefers a system TeX Live (xelatex) and falls back to Tectonic.
    tex_engine: Literal["auto", "xelatex", "tectonic"] = "auto"
    xelatex_command: str = "xelatex"
    xelatex_timeout_seconds: float = 120.0
    tectonic_command: str = "tectonic"
    # Tectonic fetches packages on first use, so a cold run is much slower.
    tectonic_timeout_seconds: float = 600.0
    max_compile_repairs: int = 2

    # absolute env_file so settings load regardless of process cwd
    model_config = SettingsConfigDict(
        env_prefix="TEXDECK_",
        env_file=REPO_ROOT / "server" / ".env",
        extra="ignore",
    )


settings = Settings()
