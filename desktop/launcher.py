"""TeXDeck desktop entry point: the local server plus a native window.

Frozen by PyInstaller (desktop/texdeck.spec). The bundle carries the server,
the built web UI, the templates and a Tectonic binary; user data (decks, the
ChatGPT sign-in, an optional texdeck.env) lives in the per-user data directory.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
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
# Pick ChatGPT, Claude or an API key from the model button in the window;
# nothing here is required. To preset an API key, fill these in:
#
# TEXDECK_LLM_API_KEY=
# TEXDECK_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
# TEXDECK_LLM_MODEL=gemini-pro-latest
#
# Default models; the model button can pick others:
# TEXDECK_CHATGPT_MODEL=gpt-6.1-sol
# TEXDECK_CLAUDE_CODE_MODEL=sonnet
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


_PATH_MARK = "__TEXDECK_PATH__"


def shell_path() -> None:
    """A Mac app started from Finder gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, so
    TeX (MacTeX), Node and the agent CLIs from Homebrew or npm are not found.
    Take the PATH the user's shell sets up, then the usual install folders."""
    if sys.platform != "darwin":
        return
    found: list[str] = []
    shell = os.environ.get("SHELL") or "/bin/zsh"
    try:
        out = subprocess.run([shell, "-ilc", f'printf "{_PATH_MARK}%s{_PATH_MARK}" "$PATH"'],
                             capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL).stdout
        if out.count(_PATH_MARK) >= 2:
            found = out.split(_PATH_MARK)[1].split(":")
    except (OSError, subprocess.SubprocessError):
        logging.warning("Could not read the login shell's PATH", exc_info=True)
    usual = ["/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]
    parts = os.environ.get("PATH", "").split(":") + found + usual
    os.environ["PATH"] = ":".join(dict.fromkeys(p for p in parts if p))


def configure(data: Path) -> None:
    root = bundle_dir()
    shell_path()
    load_env_file(data / "texdeck.env")
    defaults = {
        "TEXDECK_TEMPLATES_DIR": root / "templates",
        "TEXDECK_NOTE_TEMPLATES_DIR": root / "note-templates",
        "TEXDECK_WEB_DIST_DIR": root / "web" / "dist",
        "TEXDECK_ARTIFACT_ROOT": data / "artifacts",
        "TEXDECK_CHATGPT_AUTH_FILE": data / "chatgpt-auth.json",
        "TEXDECK_MODEL_STATE_FILE": data / "model-state.json",
        "TEXDECK_MEMORY_FILE": data / "memory.json",
        "TEXDECK_CUSTOM_TEMPLATES_DIR": data / "custom-templates",
        "TEXDECK_AGENT_HOME_DIR": data / "agent-homes",
        "TEXDECK_AGY_HOME": data / "agy-home",
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


def smoke_papering(url: str) -> bool:
    """CI: a Papering project builds with this machine's TeX (the bundled
    Tectonic on a fresh runner), SyncTeX maps both ways, and the agent list
    loads (the agent module imports on every platform)."""
    import json
    import urllib.parse

    def call(method: str, path: str, data: bytes | None = None, kind: str = "") -> object:
        req = urllib.request.Request(url + path, data, {"Content-Type": kind} if kind else {}, method=method)
        with urllib.request.urlopen(req, timeout=900) as res:
            body = res.read()
            return json.loads(body) if body else None

    form = urllib.parse.urlencode({"title": "Smoke", "language": "en"}).encode()
    gid = call("POST", "api/papers", form, "application/x-www-form-urlencoded")["id"]
    compiler = call("GET", f"api/papers/{gid}")["compiler"]
    call("POST", f"api/papers/{gid}/compile")
    deadline = time.time() + 900
    build = call("GET", f"api/papers/{gid}/build")
    while build.get("running") and time.time() < deadline:
        time.sleep(2)
        build = call("GET", f"api/papers/{gid}/build")
    if not build.get("ok"):
        print(f"SMOKE PAPER build failed with {compiler}: {build.get('error')}\n{build.get('log', '')[-3000:]}", flush=True)
        return False
    at = call("GET", f"api/papers/{gid}/forward?path=main.tex&line=15")
    back = call("GET", f"api/papers/{gid}/inverse?page={int(at['page'])}&x={at['x'] + 2:.2f}&y={max(0.0, at['y'] - 2):.2f}")
    agents = call("GET", f"api/papers/{gid}/agent")["agents"]
    ok = back == {"path": "main.tex", "line": 15} and "codex" in agents
    print(f"SMOKE PAPER {'OK' if ok else 'FAILED'} compiler={compiler} forward={at} inverse={back} "
          f"agy={agents['agy']['reason']!r}", flush=True)
    return ok


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
        try:
            paper = smoke_papering(url)
        except Exception as exc:  # noqa: BLE001 - reported, fails the build
            paper = False
            print(f"SMOKE PAPER FAILED {exc!r}", flush=True)
        ok = ok and paper
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
