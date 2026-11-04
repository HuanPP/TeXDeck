"""TeXDeck desktop entry point: the local server plus a native window.

Frozen by PyInstaller (desktop/texdeck.spec). The bundle carries the server,
the built web UI, the templates and a Tectonic binary; user data (decks, the
ChatGPT sign-in, an optional texdeck.env) lives in the per-user data directory.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "TeXDeck"

ENV_TEMPLATE = """\
# TeXDeck settings. Lines are KEY=VALUE; restart TeXDeck after editing.
#
# Signing in with ChatGPT from the window needs nothing here. To use an API key
# instead (any OpenAI-compatible vision model), fill these in:
#
# TEXDECK_LLM_API_KEY=
# TEXDECK_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
# TEXDECK_LLM_MODEL=gemini-pro-latest
#
# Model used with a ChatGPT sign-in:
# TEXDECK_CHATGPT_MODEL=gpt-6.1-sol
"""


def bundle_dir() -> Path:
    """Root of bundled resources: PyInstaller's unpack dir, or the repo when run from source."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    path = base / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_env_file(path: Path) -> None:
    """Apply KEY=VALUE lines from path; variables already in the environment win."""
    if not path.is_file():
        path.write_text(ENV_TEMPLATE, encoding="utf-8")
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configure(data: Path) -> None:
    root = bundle_dir()
    load_env_file(data / "texdeck.env")
    defaults = {
        "TEXDECK_TEMPLATES_DIR": root / "templates",
        "TEXDECK_WEB_DIST_DIR": root / "web" / "dist",
        "TEXDECK_ARTIFACT_ROOT": data / "artifacts",
        "TEXDECK_CHATGPT_AUTH_FILE": data / "chatgpt-auth.json",
    }
    tectonic = root / "bin" / ("tectonic.exe" if os.name == "nt" else "tectonic")
    if tectonic.is_file():
        # Pin the bundled engine so a half-installed system TeX never takes over.
        defaults["TEXDECK_TEX_ENGINE"] = "tectonic"
        defaults["TEXDECK_TECTONIC_COMMAND"] = tectonic
    for key, value in defaults.items():
        os.environ.setdefault(key, str(value))


def setup_logging(data: Path) -> None:
    # Windowed builds have no console: stdout/stderr are None and uvicorn would crash on them.
    log = open(data / "texdeck.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(log)],
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_healthy(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}health", timeout=2) as res:
                if res.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("TeXDeck server did not start")


def main() -> None:
    data = data_dir()
    setup_logging(data)
    configure(data)

    # Import after configure(): settings read the environment at import time.
    import uvicorn

    from app.main import app

    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_healthy(url)
    logging.info("TeXDeck serving %s, data in %s", url, data)

    if os.environ.get("TEXDECK_DESKTOP_SMOKE"):
        # CI: one template preview exercises the frozen server, the bundled
        # templates, the bundled Tectonic and PDF rendering together.
        key = os.environ["TEXDECK_DESKTOP_SMOKE"]
        with urllib.request.urlopen(f"{url}api/templates/{key}/preview.png", timeout=900) as res:
            ok = res.status == 200 and res.read(8) == b"\x89PNG\r\n\x1a\n"
        print(f"SMOKE {'OK' if ok else 'FAILED'} {key}", flush=True)
        server.should_exit = True
        thread.join(10)
        sys.exit(0 if ok else 1)

    try:
        import webview

        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.create_window(APP_NAME, url, width=1280, height=900, min_size=(900, 600))
        webview.start()
    except Exception:  # noqa: BLE001 - any GUI backend failure falls back to the browser
        logging.exception("Native window unavailable; opening the system browser")
        webbrowser.open(url)
        try:
            while thread.is_alive():
                thread.join(1)
        except KeyboardInterrupt:
            pass
    server.should_exit = True
    thread.join(10)


if __name__ == "__main__":
    main()
