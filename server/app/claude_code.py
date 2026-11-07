"""Generation through the user's own Claude Code CLI in headless mode.

TeXDeck never reads or stores Claude credentials. It starts the locally
installed `claude` binary with `-p`, which runs on whatever account that CLI is
signed in with, and reads the reply from its stream-json output. The child runs
in an empty temp directory with no tools, no MCP servers and --safe-mode, so
the user's CLAUDE.md, hooks and plugins stay out of the prompt.

Sign-in works the same way: TeXDeck runs the CLI's own `claude auth login`,
shows the link it prints and pipes back the code the user pastes. The CLI
stores the login; TeXDeck never sees it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import settings

UsageCallback = Callable[[int, int], None]

# Aliases `claude --model` accepts; each resolves to the latest model of that
# line. Whether a plan includes one is up to Anthropic; the CLI says if not.
MODELS = [
    {"value": "fable", "label": "Fable"},
    {"value": "opus", "label": "Opus"},
    {"value": "sonnet", "label": "Sonnet"},
    {"value": "haiku", "label": "Haiku"},
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ClaudeCodeError(RuntimeError):
    """Claude Code call failed; message is user-facing English."""


def _fallback_locations() -> list[Path]:
    # A desktop app launched from Finder or the Start menu does not inherit the
    # shell PATH, so look where the official installers put the binary.
    home = Path.home()
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return [home / ".local" / "bin" / "claude.exe", appdata / "npm" / "claude.cmd"]
    return [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    ]


def _require_command() -> str:
    command = find_command()
    if command is None:
        raise ClaudeCodeError(
            "Claude Code CLI not found. Install Claude Code on this computer, "
            "or point TEXDECK_CLAUDE_CODE_COMMAND at the binary"
        )
    return command


def find_command() -> str | None:
    command = settings.claude_code_command
    found = shutil.which(command)
    if found:
        return found
    if command != "claude":
        return None
    return next((str(p) for p in _fallback_locations() if p.is_file()), None)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")


def _image(part: dict[str, Any]) -> dict[str, Any]:
    url = part["image_url"]["url"]
    header, sep, data = url.partition(",")
    if not (sep and header.startswith("data:") and header.endswith(";base64")):
        raise ClaudeCodeError("Only base64 data-URL images can be sent to Claude Code")
    media_type = header[len("data:") : -len(";base64")]
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def to_stream_input(messages: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Convert chat-completions messages to (system prompt, one stream-json user message).

    `claude -p` answers one user turn, so the whole conversation is folded into
    a single message; an earlier assistant reply is quoted back as text.
    """
    system: list[str] = []
    blocks: list[dict[str, Any]] = []
    for message in messages:
        role, content = message["role"], message["content"]
        if role == "system":
            system.append(_text(content))
            continue
        if role == "assistant":
            content = f"Your previous reply was:\n<previous_reply>\n{_text(content)}\n</previous_reply>"
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
        for part in parts:
            if part.get("type") == "text":
                if part["text"].strip():
                    blocks.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                blocks.append(_image(part))
    if not blocks:
        raise ClaudeCodeError("Nothing to send: the request has no user content")
    line = {"type": "user", "message": {"role": "user", "content": blocks}}
    return "\n\n".join(s for s in system if s.strip()), line


def build_args(command: str, system_file: Path, model: str = "") -> list[str]:
    args = [
        command, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",  # stream-json output requires it
        "--tools", "",
        "--system-prompt-file", str(system_file),
        "--safe-mode",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if model:
        args += ["--model", model]
    if settings.claude_code_effort:
        args += ["--effort", settings.claude_code_effort]
    return args


def parse_result(stdout: str) -> dict[str, Any] | None:
    result = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event
    return result


def complete(
    messages: list[dict[str, Any]], on_usage: UsageCallback | None = None, model: str = ""
) -> str:
    command = _require_command()
    system, line = to_stream_input(messages)
    with tempfile.TemporaryDirectory(prefix="texdeck-claude-") as tmp:
        system_file = Path(tmp) / "system.txt"
        system_file.write_text(system or "You are a helpful assistant.", encoding="utf-8")
        try:
            proc = subprocess.run(
                build_args(command, system_file, model),
                input=(json.dumps(line) + "\n").encode("utf-8"),
                capture_output=True,
                cwd=tmp,
                timeout=settings.llm_timeout_seconds,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(f"Claude Code timed out after {exc.timeout:.0f}s") from exc
        except OSError as exc:
            raise ClaudeCodeError(f"Could not start Claude Code: {exc}") from exc

    result = parse_result(proc.stdout.decode("utf-8", errors="replace"))
    if result is None:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ClaudeCodeError(
            f"Claude Code exited with code {proc.returncode} without a result: {stderr[-500:] or 'no output'}"
        )

    usage = result.get("usage") or {}
    if on_usage is not None:
        prompt_tokens = sum(
            int(usage.get(key) or 0)
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        on_usage(prompt_tokens, int(usage.get("output_tokens") or 0))

    text = str(result.get("result") or "")
    if result.get("is_error"):
        raise ClaudeCodeError(f"Claude Code: {text or result.get('subtype') or 'unknown error'}")
    if result.get("stop_reason") == "max_tokens":
        raise ClaudeCodeError("Claude Code reply was cut off at the output token limit")
    return text


# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------

_URL_RE = re.compile(r"https://\S+/oauth/authorize\?\S+")
_AUTH_TTL_SECONDS = 10.0
_auth_cache: tuple[float, dict[str, Any]] | None = None


def auth_status(*, refresh: bool = False) -> dict[str, Any]:
    """The CLI's own `claude auth status`, cached briefly (each call spawns the CLI)."""
    global _auth_cache
    if not refresh and _auth_cache and time.monotonic() - _auth_cache[0] < _AUTH_TTL_SECONDS:
        return _auth_cache[1]
    info: dict[str, Any] = {"logged_in": False, "email": "", "subscription": ""}
    command = find_command()
    if command is not None:
        try:
            proc = subprocess.run(
                [command, "auth", "status", "--json"],
                capture_output=True, stdin=subprocess.DEVNULL, timeout=30, creationflags=_NO_WINDOW,
            )
            data = json.loads(proc.stdout.decode("utf-8", errors="replace"))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            data = {}
        if isinstance(data, dict) and data.get("loggedIn"):
            info = {
                "logged_in": True,
                "email": str(data.get("email") or ""),
                "subscription": str(data.get("subscriptionType") or data.get("authMethod") or ""),
            }
    _auth_cache = (time.monotonic(), info)
    return info


# Called once the CLI is signed in; app.llm uses it to switch to Claude Code.
on_signed_in: Callable[[], None] | None = None


def _signed_in() -> None:
    if on_signed_in is not None:
        on_signed_in()


@dataclass(eq=False)
class _Login:
    proc: subprocess.Popen[bytes]
    url: str = ""
    error: str = ""
    cancelled: bool = False
    url_seen: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


_login_lock = threading.Lock()
_login: _Login | None = None
_last_error = ""


def _read_url(login: _Login) -> None:
    assert login.proc.stdout is not None
    for raw in iter(login.proc.stdout.readline, b""):
        match = _URL_RE.search(raw.decode("utf-8", errors="replace"))
        if match and not login.url:
            login.url = match.group(0)
            login.url_seen.set()
    login.url_seen.set()


def _wait_for_exit(login: _Login) -> None:
    global _login, _auth_cache, _last_error
    assert login.proc.stderr is not None
    stderr = login.proc.stderr.read().decode("utf-8", errors="replace")
    code = login.proc.wait()
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    with _login_lock:
        if _login is login:
            _login = None
        _auth_cache = None
        if login.cancelled:
            pass
        elif code == 0:
            _last_error = ""
        else:
            login.error = lines[-1] if lines else f"Claude Code sign-in exited with code {code}"
            _last_error = login.error
    if code == 0 and not login.cancelled:
        _signed_in()
    login.done.set()


def start_login() -> str:
    """Begin connecting Claude Code. Returns the sign-in URL, or "" when the
    CLI is already signed in (then it is selected straight away)."""
    global _login, _last_error
    command = _require_command()
    if auth_status(refresh=True)["logged_in"]:
        _signed_in()
        return ""
    cancel_login()
    try:
        proc = subprocess.Popen(
            [command, "auth", "login", "--claudeai"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        raise ClaudeCodeError(f"Could not start Claude Code: {exc}") from exc
    login = _Login(proc)
    with _login_lock:
        _login, _last_error = login, ""
    threading.Thread(target=_read_url, args=(login,), daemon=True).start()
    threading.Thread(target=_wait_for_exit, args=(login,), daemon=True).start()
    login.url_seen.wait(30)
    if not login.url:
        login.done.wait(5)
        error = login.error
        cancel_login()
        raise ClaudeCodeError(error or "Claude Code did not print a sign-in link")
    return login.url


def submit_code(code: str) -> None:
    """Pipe the code from the Claude sign-in page into the waiting CLI."""
    with _login_lock:
        login = _login
    if login is None:
        raise ClaudeCodeError("No Claude sign-in is in progress; start again")
    assert login.proc.stdin is not None
    try:
        login.proc.stdin.write((code.strip() + "\n").encode("utf-8"))
        login.proc.stdin.close()
    except OSError:
        pass  # the CLI already exited; its result is reported below
    if not login.done.wait(90):
        cancel_login()
        raise ClaudeCodeError("Timed out waiting for Claude Code to finish signing in")
    if login.error:
        raise ClaudeCodeError(login.error)


def cancel_login() -> None:
    global _login
    with _login_lock:
        login, _login = _login, None
    if login is not None:
        login.cancelled = True
        if login.proc.poll() is None:
            login.proc.kill()


def status() -> dict[str, Any]:
    available = find_command() is not None
    auth = auth_status() if available else {}
    with _login_lock:
        pending, error = _login is not None, _last_error
    return {
        "available": available,
        "signed_in": bool(auth.get("logged_in")),
        "email": auth.get("email", ""),
        "subscription": auth.get("subscription", ""),
        "pending": pending,
        "login_error": error,
    }
