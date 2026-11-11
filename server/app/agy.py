"""Generation through the user's Antigravity plan, via the `agy` CLI in print mode.

agy is a coding agent whose tools cannot be switched off: with a permissive
settings.json it runs shell commands unasked, and even with none it reads any
file it is pointed at. A source PDF could carry instructions to do exactly
that. So every agy process runs inside bubblewrap and sees only the system
files (read-only), its own home (the account's sign-in) and a scratch folder
with this request's images. Without bubblewrap, i.e. off Linux, agy is not
offered.

Each account gets its own agy home, never the real one. Its stream-json input
takes text only, so images go into the scratch folder and the prompt asks the
agent to open them with its file viewer.

Sign-in runs the CLI's own OAuth: signed out, print mode prints a Google link
and waits about a minute for the code the user pastes back, but only when its
stdin looks like a terminal (a pipe gets "run agy to log in"), so it gets a
pty. The CLI keeps the token in its home.
"""

from __future__ import annotations

import base64
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

from . import auth
from .config import settings

UsageCallback = Callable[[int, int], None]

_HOME_IN_JAIL = "/home/agy"
_WORK_IN_JAIL = "/work"
_AGY_IN_JAIL = "/opt/agy/agy"
_CLI_DIR = Path(".gemini") / "antigravity-cli"
_TOKEN = _CLI_DIR / "antigravity-oauth-token"
# Conversation records hold the user's material; drop them once a run is old.
_HISTORY_DIRS = ("conversations", "brain", "implicit", "annotations")
_HISTORY_MAX_AGE_SECONDS = 3600

_PREAMBLE = (
    "TeXDeck uses you as a plain text generator. Do not run commands, create or edit "
    "files, or browse or search the web. {images}Your final reply must be exactly the "
    "output requested below, with no commentary."
)
_IMAGES_NOTE = (
    "The images mentioned below are files in {folder}; open every one of them with "
    "your file viewing tool, in the order given, before you write anything. "
)


class AgyError(RuntimeError):
    """agy call failed; message is user-facing English."""


def find_command() -> str | None:
    found = shutil.which(settings.agy_command)
    if found:
        return found
    if settings.agy_command != "agy":
        return None
    fallback = Path.home() / ".local" / "bin" / "agy"
    return str(fallback) if fallback.is_file() else None


def find_bwrap() -> str | None:
    return shutil.which(settings.bwrap_command) if os.name == "posix" else None


def availability() -> str:
    """"" when agy can be used here, else why not (user-facing)."""
    if find_bwrap() is None:
        return "agy needs bubblewrap (Linux) to run safely, and this server has none"
    if find_command() is None:
        return "The agy CLI is not installed here"
    return ""


def home() -> Path:
    """This account's agy home (made on first use, private)."""
    account = auth.user_dir()
    path = account / "agy" if account is not None else settings.agy_home
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def signed_in() -> bool:
    return (home() / _TOKEN).is_file()


def sandbox_args(agy_home: Path, work: Path, command: str, bwrap: str) -> list[str]:
    """bwrap command line that shows agy only the system, its home and `work`."""
    args = [bwrap, "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        root = Path("/") / name
        if root.is_symlink():
            args += ["--symlink", os.readlink(root), str(root)]
        elif root.is_dir():
            args += ["--ro-bind", str(root), str(root)]
    for etc in ("resolv.conf", "hosts", "nsswitch.conf", "ssl", "ca-certificates",
                "localtime", "alternatives"):
        args += ["--ro-bind-try", f"/etc/{etc}", f"/etc/{etc}"]
    args += [
        "--ro-bind", str(Path(command).resolve()), _AGY_IN_JAIL,
        "--bind", str(agy_home), _HOME_IN_JAIL,
        "--bind", str(work), _WORK_IN_JAIL,
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--unshare-all", "--share-net", "--die-with-parent", "--new-session",
        "--clearenv",
        "--setenv", "HOME", _HOME_IN_JAIL,
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--chdir", _WORK_IN_JAIL,
        _AGY_IN_JAIL,
    ]
    return args


def _jailed(work: Path, *agy_args: str) -> list[str]:
    command, bwrap = find_command(), find_bwrap()
    if bwrap is None or command is None:
        raise AgyError(availability())
    return sandbox_args(home(), work, command, bwrap) + list(agy_args)


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content if part.get("type") == "text")


def _save_image(part: dict[str, Any], folder: Path, n: int) -> str:
    url = part["image_url"]["url"]
    header, sep, data = url.partition(",")
    if not (sep and header.startswith("data:") and header.endswith(";base64")):
        raise AgyError("Only base64 data-URL images can be sent to agy")
    ext = "jpg" if header == "data:image/jpeg;base64" else "png"
    name = f"{n:03d}.{ext}"
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(base64.b64decode(data))
    return f"{_WORK_IN_JAIL}/images/{name}"


def to_prompt(messages: list[dict[str, Any]], work: Path) -> str:
    """Fold the conversation into one text prompt; images become files in work/images.

    agy has no system prompt flag and its stream input takes text only, so the
    system message leads the prompt and every image is named where it sat.
    """
    system: list[str] = []
    body: list[str] = []
    images = 0
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
                    body.append(part["text"])
            elif part.get("type") == "image_url":
                images += 1
                body.append(f"[Image {images}: {_save_image(part, work / 'images', images)}]")
    if not body:
        raise AgyError("Nothing to send: the request has no user content")
    note = _IMAGES_NOTE.format(folder=f"{_WORK_IN_JAIL}/images") if images else ""
    head = [_PREAMBLE.format(images=note)] + [s for s in system if s.strip()]
    return "<instructions>\n" + "\n\n".join(head) + "\n</instructions>\n\n" + "\n\n".join(body)


def build_args(model: str = "") -> list[str]:
    timeout = int(settings.llm_timeout_seconds)
    args = [
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--disable-slash-commands",
        "--print-timeout", f"{timeout}s",
    ]
    if model:
        args += ["--model", model]
    if settings.agy_effort:
        args += ["--effort", settings.agy_effort]
    return args + ["-p="]  # -p would swallow the next flag as its prompt


def parse_result(stdout: str) -> dict[str, Any] | None:
    result = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result = event.get("result")
    return result if isinstance(result, dict) else None


def _prune_history() -> None:
    cutoff = time.time() - _HISTORY_MAX_AGE_SECONDS
    for name in _HISTORY_DIRS:
        folder = home() / _CLI_DIR / name
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
            except OSError:
                pass


def complete(messages: list[dict[str, Any]], on_usage: UsageCallback | None = None, model: str = "") -> str:
    if not signed_in():
        # Signed out, agy would wait for an OAuth code on stdin, where the prompt is.
        raise AgyError("agy is not signed in; connect it from the model button at the top")
    with tempfile.TemporaryDirectory(prefix="texdeck-agy-") as tmp:
        work = Path(tmp)
        prompt = to_prompt(messages, work)
        line = {"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
        try:
            proc = subprocess.run(
                _jailed(work, *build_args(model)),
                input=(json.dumps(line) + "\n").encode("utf-8"),
                capture_output=True,
                timeout=settings.llm_timeout_seconds + 60,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyError(f"agy timed out after {exc.timeout:.0f}s") from exc
        except OSError as exc:
            raise AgyError(f"Could not start agy: {exc}") from exc
    _prune_history()

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if "Authentication required" in stdout + stderr:
        raise AgyError("agy is signed out; connect it again from the model button at the top")
    result = parse_result(stdout)
    if result is None:
        raise AgyError(f"agy exited with code {proc.returncode} without a result: {stderr[-500:] or 'no output'}")

    usage = result.get("usage") or {}
    if on_usage is not None:
        on_usage(
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0) + int(usage.get("thinking_tokens") or 0),
        )
    if result.get("status") != "SUCCESS":
        raise AgyError(f"agy: {result.get('error') or result.get('finished_reason') or 'unknown error'}")
    text = str(result.get("response") or "")
    if not text.strip():
        raise AgyError(f"agy returned no text{': ' + stderr[-300:] if stderr else ''}")
    return text


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

_MODELS_TTL_SECONDS = 600
_models_cache: dict[str | None, tuple[float, list[dict[str, str]]]] = {}


def parse_models(output: str) -> list[dict[str, str]]:
    models = []
    for line in output.splitlines():
        value, tab, label = line.partition("\t")
        if tab and value.strip() and not any(ch.isspace() for ch in value.strip()):
            models.append({"value": value.strip(), "label": label.strip() or value.strip()})
    return models


def list_models() -> list[dict[str, str]]:
    """What this account's plan offers, from `agy models` (cached; [] on failure)."""
    user = auth.current_user.get()
    cached = _models_cache.get(user)
    if cached and time.monotonic() - cached[0] < _MODELS_TTL_SECONDS:
        return cached[1]
    if availability() or not signed_in():
        return []
    with tempfile.TemporaryDirectory(prefix="texdeck-agy-") as tmp:
        try:
            proc = subprocess.run(
                _jailed(Path(tmp), "models"), capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
    models = parse_models(proc.stdout.decode("utf-8", errors="replace"))
    if models:
        _models_cache[user] = (time.monotonic(), models)
    return models


# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------

_URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/\S+")
_EMAIL_RE = re.compile(r"authenticated successfully as (\S+@\S+)")


@dataclass(eq=False)
class _Login:
    proc: subprocess.Popen[bytes]
    user: str | None
    work: tempfile.TemporaryDirectory[str]
    tty: int  # pty master: agy reads the pasted code from the other end
    url: str = ""
    output: list[str] = field(default_factory=list)
    error: str = ""
    cancelled: bool = False
    url_seen: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


_login_lock = threading.Lock()
_logins: dict[str | None, _Login] = {}
_last_error: dict[str | None, str] = {}

# Called once agy is signed in; app.llm uses it to switch to agy.
on_signed_in: Callable[[], None] | None = None


def _signed_in() -> None:
    if on_signed_in is not None:
        on_signed_in()


def _watch(login: _Login) -> None:
    assert login.proc.stdout is not None
    for raw in iter(login.proc.stdout.readline, b""):
        text = raw.decode("utf-8", errors="replace").strip()
        login.output.append(text)
        match = _URL_RE.search(text)
        if match and not login.url:
            login.url = match.group(0)
            login.url_seen.set()
    code = login.proc.wait()
    login.url_seen.set()
    login.work.cleanup()
    os.close(login.tty)
    ok = code == 0 and signed_in_as(login.user)
    with _login_lock:
        if _logins.get(login.user) is login:
            del _logins[login.user]
        if login.cancelled:
            pass
        elif ok:
            _last_error[login.user] = ""
        else:
            errors = [line for line in login.output if "rror" in line or "failed" in line]
            login.error = errors[0] if errors else f"agy sign-in exited with code {code}"
            _last_error[login.user] = login.error
    if ok and not login.cancelled:
        _models_cache.pop(login.user, None)
        auth.run_as(login.user, _signed_in)
    login.done.set()


def signed_in_as(user: str | None) -> bool:
    return auth.run_as(user, signed_in)


def start_login() -> str:
    """Begin connecting agy. Returns the Google sign-in URL, or "" when this
    account's agy is already signed in (then it is selected straight away)."""
    reason = availability()
    if reason:
        raise AgyError(reason)
    if signed_in():
        _signed_in()
        return ""
    cancel_login()
    work = tempfile.TemporaryDirectory(prefix="texdeck-agy-")
    master, slave = os.openpty()
    try:
        # Any prompt: once signed in agy answers it and exits. `agy models`
        # would not sign in; it only says to run agy interactively.
        proc = subprocess.Popen(
            _jailed(Path(work.name), "--print-timeout", "120s", "-p=Reply with OK"),
            stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        work.cleanup()
        os.close(master)
        raise AgyError(f"Could not start agy: {exc}") from exc
    finally:
        os.close(slave)
    login = _Login(proc, auth.current_user.get(), work, master)
    with _login_lock:
        _logins[login.user], _last_error[login.user] = login, ""
    threading.Thread(target=_watch, args=(login,), daemon=True).start()
    login.url_seen.wait(30)
    if not login.url:
        login.done.wait(5)
        error = login.error
        cancel_login()
        raise AgyError(error or "agy did not print a sign-in link")
    return login.url


def submit_code(code: str) -> None:
    """Pipe the authorization code from Google's page into the waiting CLI."""
    with _login_lock:
        login = _logins.get(auth.current_user.get())
    if login is None:
        raise AgyError("No agy sign-in is in progress (it gives up after a minute); start again")
    try:
        os.write(login.tty, (code.strip() + "\n").encode("utf-8"))
    except OSError:
        pass  # the CLI already exited; its result is reported below
    if not login.done.wait(150):
        cancel_login()
        raise AgyError("Timed out waiting for agy to finish signing in")
    if login.error:
        raise AgyError(login.error)


def cancel_login() -> None:
    with _login_lock:
        login = _logins.pop(auth.current_user.get(), None)
    if login is not None:
        login.cancelled = True
        if login.proc.poll() is None:
            login.proc.kill()


def sign_out() -> None:
    cancel_login()
    (home() / _TOKEN).unlink(missing_ok=True)
    _models_cache.pop(auth.current_user.get(), None)


def _email() -> str:
    """Who agy signed in as, from its newest log (best effort)."""
    logs = sorted((home() / _CLI_DIR / "log").glob("cli-*.log"), reverse=True)
    for log in logs[:3]:
        try:
            match = _EMAIL_RE.search(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if match:
            return match.group(1)
    return ""


def status() -> dict[str, Any]:
    reason = availability()
    user = auth.current_user.get()
    with _login_lock:
        pending, error = user in _logins, _last_error.get(user, "")
    signed = not reason and signed_in()
    return {
        "available": not reason,
        "unavailable_reason": reason,
        "signed_in": signed,
        "email": _email() if signed else "",
        "pending": pending,
        "login_error": error,
    }
