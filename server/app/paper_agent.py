"""The Papering agent: a coding CLI that edits a project's files directly.

Four kinds: Claude Code (the account's Claude login), Codex (its own ChatGPT
device sign-in), Antigravity (the account's agy login) and an API key, which
runs Claude Code against an Anthropic-compatible endpoint or Codex against an
OpenAI-compatible one.

Each run is a process tree of
    pasta (its own network namespace) -> sh (blocks private ranges) -> bwrap -> CLI
The CLI sees the system read-only (with TeX, so it can compile to check its
work), its own home (that account's sign-in and session history) and the
project at /work, nothing else. pasta gives it the internet but not the host's
loopback, and the routes it sets make link-local and private addresses
unreachable: no other local service, no cloud metadata server. Inside bwrap the
CLI holds no capabilities, so it cannot undo those routes.

Sessions continue across runs with the CLI's own resume (claude --resume, codex
exec resume, agy --conversation). TeXDeck keeps its own transcript of each
session in `<gen>/agent/` for the page.
"""

from __future__ import annotations

import contextvars
import json
import os
import pty
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import agy, auth, claude_code, llm, paper, store
from .config import REPO_ROOT, settings

KINDS = ("claude", "codex", "agy", "api")
LABELS = {"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity", "api": "API key"}
RUN_TIMEOUT_SECONDS = 60 * 60
MAX_PROMPT_CHARS = 20_000
MAX_TOOL_OUTPUT = 4000

_HOME_IN_JAIL = "/home/agent"
_WORK_IN_JAIL = "/work"
_BIN_IN_JAIL = "/opt/agent/bin"
_BLOCKED = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
            "0.0.0.0/8", "198.18.0.0/15")
_RESOLV = "nameserver 8.8.8.8\nnameserver 1.1.1.1\noptions edns0\n"


class AgentError(RuntimeError):
    """User-facing English message."""


# --------------------------------------------------------------------------
# Binaries and homes
# --------------------------------------------------------------------------


def _which(name: str, *fallbacks: Path) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return next((str(p) for p in fallbacks if p.is_file()), None)


def _native(command: str | None) -> Path | None:
    """The executable to bind into the jail: the real binary behind a symlink;
    for the npm Codex package, the platform binary it wraps."""
    if command is None:
        return None
    path = Path(command).resolve()
    if path.suffix == ".js":
        package = path.parent.parent
        found = sorted(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
        return found[0] if found else None
    try:
        with open(path, "rb") as fh:
            if fh.read(2) == b"#!":
                return None  # a script needs its interpreter; not supported in the jail
    except OSError:
        return None
    return path


def claude_binary() -> Path | None:
    return _native(claude_code.find_command())


def codex_binary() -> Path | None:
    return _native(_which(settings.codex_command, Path.home() / ".local" / "bin" / "codex"))


def agy_binary() -> Path | None:
    return _native(agy.find_command())


def _account_home(name: str) -> Path:
    account = auth.user_dir()
    base = account if account is not None else REPO_ROOT / "server" / ".agent-homes"
    path = base / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def codex_home(api: bool = False) -> Path:
    return _account_home("codex-api" if api else "codex")


def codex_signed_in() -> bool:
    try:
        data = json.loads((codex_home() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("tokens") or data.get("OPENAI_API_KEY"))


def claude_home(api: bool = False) -> tuple[Path, bool]:
    """(folder, is_config_dir). With accounts: the account's CLAUDE_CONFIG_DIR.
    Without: the user's own ~/.claude login (bound as a home)."""
    if api:
        return _account_home("claude-api"), True
    account = auth.user_dir()
    if account is not None:
        folder = account / "claude"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder, True
    return Path.home(), False


def _requirements() -> str:
    if os.name != "posix" or not Path("/proc").is_dir():
        return "The agent runs only on a Linux server"
    for tool in (settings.bwrap_command, "pasta"):
        if not shutil.which(tool):
            return f"The agent needs {tool} on the server ({'bubblewrap' if tool == settings.bwrap_command else 'passt'})"
    if not _ip_command():
        return "The agent needs the ip command (iproute2) on the server"
    return ""


def _ip_command() -> str | None:
    return _which("ip", Path("/usr/sbin/ip"), Path("/sbin/ip"), Path("/usr/bin/ip"))


def api_protocol() -> str:
    return llm.api_config().protocol


def options() -> dict[str, Any]:
    """Which agents this account can use now, and why not otherwise."""
    base = _requirements()
    api = llm.api_config()
    out: dict[str, Any] = {}

    def entry(kind: str, reason: str, signed_in: bool, model: str) -> None:
        out[kind] = {"label": LABELS[kind], "available": not reason and signed_in, "reason": reason,
                     "signed_in": signed_in, "model": model}

    entry("claude", base or ("" if claude_binary() else "Claude Code is not installed on the server"),
          bool(claude_binary()) and claude_code.auth_status()["logged_in"], llm.claude_model())
    entry("codex", base or ("" if codex_binary() else "Codex is not installed on the server"),
          codex_signed_in(), "")
    entry("agy", base or agy.availability() or ("" if agy_binary() else "agy is not installed on the server"),
          agy.signed_in(), llm.agy_model())
    api_binary = claude_binary() if api.protocol == "anthropic" else codex_binary()
    entry("api", base or ("" if api_binary else f"{'Claude Code' if api.protocol == 'anthropic' else 'Codex'} is not installed on the server"),
          bool(api.api_key), api.model)
    out["api"]["protocol"] = api.protocol
    preferred = {"chatgpt": "codex", "claude-code": "claude", "agy": "agy", "api": "api"}.get(llm.active_provider(), "claude")
    return {"agents": out, "default": preferred, "codex_login": codex_login_state()}


# --------------------------------------------------------------------------
# The jail
# --------------------------------------------------------------------------


def _resolv_file() -> Path:
    path = REPO_ROOT / "server" / ".agent-resolv.conf"
    if not path.is_file() or path.read_text() != _RESOLV:
        path.write_text(_RESOLV)
    return path


def jail(binary: Path, home: Path, work: Path | None, env: dict[str, str], *, home_is_config: bool = True,
         extra: list[str] | None = None) -> list[str]:
    """pasta + routes + bwrap command line around `binary` (arguments follow)."""
    reason = _requirements()
    if reason:
        raise AgentError(reason)
    bwrap = [shutil.which(settings.bwrap_command) or "bwrap", "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        top = Path("/") / name
        if top.is_symlink():
            bwrap += ["--symlink", os.readlink(top), str(top)]
        elif top.is_dir():
            bwrap += ["--ro-bind", str(top), str(top)]
    for etc in ("hosts", "nsswitch.conf", "ssl", "ca-certificates", "localtime", "alternatives", "texmf", "fonts"):
        bwrap += ["--ro-bind-try", f"/etc/{etc}", f"/etc/{etc}"]
    bwrap += ["--ro-bind", str(_resolv_file()), "/etc/resolv.conf"]
    for var in ("/var/lib/texmf", "/var/cache/fontconfig"):
        bwrap += ["--ro-bind-try", var, var]
    bwrap += ["--ro-bind", str(binary), _BIN_IN_JAIL]
    if home_is_config:
        bwrap += ["--bind", str(home), _HOME_IN_JAIL]
    else:  # the user's own ~/.claude and ~/.claude.json, without the rest of their home
        bwrap += ["--dir", _HOME_IN_JAIL]
        for name in (".claude", ".claude.json"):
            if (home / name).exists():
                bwrap += ["--bind", str(home / name), f"{_HOME_IN_JAIL}/{name}"]
    if work is not None:
        bwrap += ["--bind", str(work), _WORK_IN_JAIL]
    bwrap += extra or []
    bwrap += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--unshare-all", "--share-net", "--die-with-parent", "--new-session", "--clearenv",
        "--setenv", "HOME", _HOME_IN_JAIL,
        "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "TERM", "dumb",
    ]
    for key, value in env.items():
        bwrap += ["--setenv", key, value]
    bwrap += ["--chdir", _WORK_IN_JAIL if work is not None else _HOME_IN_JAIL, _BIN_IN_JAIL]
    routes = "".join(f"route add unreachable {net}\\n" for net in _BLOCKED)
    # One `ip -batch` (not one ip per route): bwrap misbehaves under this pasta
    # when it starts with a higher pid in the namespace.
    script = f'printf "{routes}" | {_ip_command()} -batch - >/dev/null && exec "$@"'
    return ["pasta", "--config-net", "--no-map-gw", "--", "/bin/sh", "-c", script, "sh", *bwrap]


# --------------------------------------------------------------------------
# Sessions on disk
# --------------------------------------------------------------------------


def _dir(gen_id: str) -> Path:
    path = store.generation_dir(gen_id) / "agent"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _now() -> float:
    return round(time.time(), 3)


def list_sessions(gen_id: str) -> list[dict[str, Any]]:
    try:
        data = json.loads((_dir(gen_id) / "sessions.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save_sessions(gen_id: str, sessions: list[dict[str, Any]]) -> None:
    paper._write_json(_dir(gen_id) / "sessions.json", sessions)


def _session_file(gen_id: str, sid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", sid):
        raise AgentError("Unknown session")
    return _dir(gen_id) / f"{sid}.json"


def transcript(gen_id: str, sid: str) -> list[dict[str, Any]]:
    run = _runs.get(gen_id)
    if run is not None and run.sid == sid:
        with run.lock:
            return [dict(item) for item in run.items]
    try:
        data = json.loads(_session_file(gen_id, sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def delete_session(gen_id: str, sid: str) -> None:
    run = _runs.get(gen_id)
    if run is not None and run.sid == sid:
        raise AgentError("Stop the agent before deleting this session")
    sessions = list_sessions(gen_id)
    gone = next((s for s in sessions if s.get("id") == sid), None)
    _save_sessions(gen_id, [s for s in sessions if s.get("id") != sid])
    _session_file(gen_id, sid).unlink(missing_ok=True)
    if gone and gone.get("agent") == "agy" and gone.get("cli_id"):
        _forget_agy_conversation(str(gone["cli_id"]))


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@dataclass(eq=False)
class Run:
    gen_id: str
    sid: str
    kind: str
    items: list[dict[str, Any]]
    lock: threading.Lock = field(default_factory=threading.Lock)
    proc: subprocess.Popen[bytes] | None = None
    stopped: bool = False
    started: float = field(default_factory=time.time)
    cli_id: str = ""
    usage: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    saved_at: float = 0.0


_runs: dict[str, Run] = {}
_runs_lock = threading.Lock()
_versions: dict[str, int] = {}
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="texdeck-agent")


def version(gen_id: str) -> int:
    return _versions.get(gen_id, 0)


def _bump(gen_id: str) -> None:
    _versions[gen_id] = _versions.get(gen_id, 0) + 1


def running(gen_id: str) -> str:
    """The session id of the run in progress, or ""."""
    run = _runs.get(gen_id)
    return run.sid if run is not None else ""


INSTRUCTIONS = """You are the writing agent inside TeXDeck Papering, working on the user's LaTeX project in the current directory ({work}). Edit its files directly; the user watches the editor and the PDF beside it.

The main file is {main}, built with {engine}. TeXDeck recompiles the project by itself after you finish, so you do not have to. If you want to check a build, run
  latexmk -{engine_flag} -interaction=nonstopmode -halt-on-error -outdir=/tmp/build {main}
and read /tmp/build/{stem}.log. Never write build output into the project, and never create hidden files or folders in it.

Keep the user's packages, macros and style; change only what the request needs. Write in the language of the surrounding text unless asked otherwise. The project has no internet access to private or local addresses, and nothing outside {work} is the user's.

When you are done, reply with a short summary of what you changed (files and sections), in the language the user wrote to you in."""


def _instructions(gen_id: str) -> str:
    conf = paper.load_settings(gen_id)
    flag = {"pdflatex": "pdf", "xelatex": "xelatex", "lualatex": "lualatex"}[conf["engine"]]
    return INSTRUCTIONS.format(work=_WORK_IN_JAIL, main=conf["main"], engine=conf["engine"], engine_flag=flag,
                               stem=Path(conf["main"]).stem)


def _context(gen_id: str, context: dict[str, Any]) -> str:
    """What the user is looking at, prepended to each request."""
    lines = []
    path = str(context.get("path") or "")
    if path:
        where = f"The user has {path} open"
        if context.get("line"):
            where += f", cursor at line {int(context['line'])}"
        lines.append(where + ".")
    selection = str(context.get("selection") or "").strip()
    if selection:
        lines.append(f"They selected:\n<selection>\n{selection[:4000]}\n</selection>")
    build = paper.build_state(gen_id)
    if build.error and not build.running:
        lines.append(f"The last TeXDeck build failed: {build.error}")
    return "\n".join(lines)


def start(gen_id: str, kind: str, prompt: str, sid: str = "", context: dict[str, Any] | None = None) -> str:
    """Start a run; returns the session id (new when `sid` is empty)."""
    prompt = prompt.strip()
    if not prompt:
        raise AgentError("Write a request for the agent")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AgentError("The request is too long")
    if kind not in KINDS:
        raise AgentError(f"Unknown agent: {kind}")
    status = options()["agents"][kind]
    if not status["available"]:
        raise AgentError(status["reason"] or f"{LABELS[kind]} is not signed in; connect it first")
    sessions = list_sessions(gen_id)
    session = next((s for s in sessions if s.get("id") == sid), None) if sid else None
    if sid and session is None:
        raise AgentError("Unknown session")
    if session is not None and session.get("agent") != kind:
        raise AgentError(f"This session belongs to {LABELS.get(session.get('agent', ''), 'another agent')}; start a new one")
    with _runs_lock:
        if gen_id in _runs:
            raise AgentError("The agent is already working on this project")
        if session is None:
            session = {"id": secrets.token_hex(6), "agent": kind, "cli_id": "", "title": prompt[:80],
                       "created_at": _now(), "updated_at": _now()}
            sessions.insert(0, session)
            _save_sessions(gen_id, sessions)
        items = transcript(gen_id, session["id"])
        items.append({"kind": "user", "text": prompt, "at": _now()})
        run = Run(gen_id=gen_id, sid=session["id"], kind=kind, items=items, cli_id=session.get("cli_id", ""))
        _runs[gen_id] = run
    _persist(run, force=True)
    full = prompt
    ctx = _context(gen_id, context or {})
    if ctx:
        full = f"<context>\n{ctx}\n</context>\n\n{prompt}"
    if not run.cli_id and kind != "claude" and not (kind == "api" and api_protocol() == "anthropic"):
        full = f"<instructions>\n{_instructions(gen_id)}\n</instructions>\n\n{full}"
    _pool.submit(contextvars.copy_context().run, _execute, run, full)
    return run.sid


def stop(gen_id: str) -> None:
    run = _runs.get(gen_id)
    if run is None:
        return
    run.stopped = True
    _kill(run.proc)


def _kill(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _persist(run: Run, *, force: bool = False) -> None:
    now = time.time()
    if not force and now - run.saved_at < 0.4:
        _bump(run.gen_id)
        return
    run.saved_at = now
    with run.lock:
        items = [dict(item) for item in run.items]
    paper._write_json(_session_file(run.gen_id, run.sid), items)
    _bump(run.gen_id)


def _add(run: Run, item: dict[str, Any]) -> dict[str, Any]:
    with run.lock:
        run.items.append(item)
    _persist(run)
    return item


def _append_text(run: Run, text: str) -> None:
    """Streamed text joins the assistant message it continues."""
    with run.lock:
        last = run.items[-1] if run.items else None
        if last is not None and last.get("kind") == "text" and last.get("streaming"):
            last["text"] += text
        else:
            run.items.append({"kind": "text", "text": text, "streaming": True})
    _persist(run)


def _end_text(run: Run, text: str | None = None) -> None:
    with run.lock:
        last = run.items[-1] if run.items else None
        if last is not None and last.get("kind") == "text" and last.get("streaming"):
            last.pop("streaming", None)
            if text is not None:
                last["text"] = text
            return
    if text:
        _add(run, {"kind": "text", "text": text})


def _tool(run: Run, key: str, name: str, detail: str, status: str, output: str = "") -> None:
    with run.lock:
        found = next((i for i in reversed(run.items) if i.get("kind") == "tool" and i.get("key") == key), None)
        if found is None:
            found = {"kind": "tool", "key": key, "name": name, "detail": detail, "status": status}
            run.items.append(found)
        found["status"] = status
        if detail:
            found["detail"] = detail
        if output:
            found["output"] = output[-MAX_TOOL_OUTPUT:]
    _persist(run)


def _detail(params: dict[str, Any]) -> str:
    """One line describing a tool call: its path, command or pattern."""
    for key in ("file_path", "path", "Path", "TargetFile", "AbsolutePath", "command", "CommandLine", "cmd",
                "pattern", "Pattern", "Query", "query", "url", "Url", "description"):
        value = params.get(key)
        if isinstance(value, list):
            value = " ".join(str(v) for v in value)
        if isinstance(value, str) and value.strip():
            return _short_path(value.strip())
    return ""


def _short_path(text: str) -> str:
    text = text.replace(_WORK_IN_JAIL + "/", "").replace("file://", "")
    return text if len(text) <= 300 else text[:300] + "…"


def _execute(run: Run, prompt: str) -> None:
    error = ""
    try:
        args, stdin, env_home = _command(run, prompt)
        run.proc = subprocess.Popen(args, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        if stdin is not None and run.proc.stdin is not None:
            try:
                run.proc.stdin.write(stdin.encode("utf-8"))
                run.proc.stdin.close()
            except BrokenPipeError:
                pass
        stderr: list[bytes] = []
        reader = threading.Thread(target=lambda: stderr.append(run.proc.stderr.read() if run.proc and run.proc.stderr else b""),
                                  daemon=True)
        reader.start()
        timer = threading.Timer(RUN_TIMEOUT_SECONDS, lambda: _kill(run.proc))
        timer.start()
        parse = _PARSERS[_engine(run.kind)]
        state: dict[str, Any] = {}
        assert run.proc.stdout is not None
        for raw in run.proc.stdout:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                try:
                    parse(run, event, state)
                except Exception as exc:  # noqa: BLE001 - an odd event must not end the run
                    state.setdefault("parse_errors", []).append(str(exc))
        code = run.proc.wait()
        timer.cancel()
        reader.join(timeout=5)
        tail = b"".join(stderr).decode("utf-8", errors="replace")
        tail = "\n".join(l for l in tail.splitlines() if "routable interface" not in l and "nameserver address" not in l)
        if run.stopped:
            error = "Stopped"
        elif state.get("error"):
            error = state["error"]
        elif code != 0 and not state.get("done"):
            error = _explain(run.kind, tail) or f"The agent exited with code {code}: {tail[-600:] or 'no output'}"
        if env_home is not None:
            env_home()
    except AgentError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - surfaced in the transcript
        error = f"The agent failed: {exc}"
    finally:
        _finish(run, error)


def _explain(kind: str, stderr: str) -> str:
    low = stderr.lower()
    if "not logged in" in low or "authentication required" in low or "please run /login" in low or "401" in low:
        return f"{LABELS[kind]} is signed out; connect it again"
    if "rate limit" in low or "usage limit" in low or "429" in low:
        return "The plan's usage limit was reached; try again later"
    return ""


def _finish(run: Run, error: str) -> None:
    _end_text(run)
    with run.lock:
        for item in run.items:
            if item.get("kind") == "tool" and item.get("status") == "running":
                item["status"] = "stopped" if run.stopped else "done"
    if error:
        _add(run, {"kind": "error", "text": error})
    _add(run, {"kind": "done", "seconds": round(time.time() - run.started, 1), "usage": dict(run.usage)})
    sessions = list_sessions(run.gen_id)
    for session in sessions:
        if session.get("id") == run.sid:
            if run.cli_id:
                session["cli_id"] = run.cli_id
            session["updated_at"] = _now()
    _save_sessions(run.gen_id, sessions)
    with _runs_lock:
        _runs.pop(run.gen_id, None)
    _persist(run, force=True)
    if run.kind == "agy":
        _remember_agy_conversations(run.gen_id)


def _engine(kind: str) -> str:
    if kind == "api":
        return "claude" if api_protocol() == "anthropic" else "codex"
    return kind


def _command(run: Run, prompt: str) -> tuple[list[str], str | None, Callable[[], None] | None]:
    """(argv, stdin text, cleanup)."""
    gen_id = run.gen_id
    work = paper.root(gen_id)
    engine = _engine(run.kind)
    if engine == "claude":
        binary = claude_binary()
        if binary is None:
            raise AgentError("Claude Code is not installed on the server")
        home, is_config = claude_home(api=run.kind == "api")
        env = {"CLAUDE_CONFIG_DIR": _HOME_IN_JAIL} if is_config else {}
        env.update({"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        model = llm.claude_model()
        if run.kind == "api":
            api = llm.api_config()
            env.update({"ANTHROPIC_BASE_URL": api.base_url, "ANTHROPIC_API_KEY": api.api_key,
                        "ANTHROPIC_MODEL": api.model, "ANTHROPIC_SMALL_FAST_MODEL": api.model})
            model = api.model
        args = ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions",
                "--append-system-prompt", _instructions(gen_id), "--strict-mcp-config", "--disable-slash-commands"]
        if model and model != "default":
            args += ["--model", model]
        if run.cli_id:
            args += ["--resume", run.cli_id]
        return jail(binary, home, work, env, home_is_config=is_config) + args, prompt, None
    if engine == "codex":
        binary = codex_binary()
        if binary is None:
            raise AgentError("Codex is not installed on the server")
        api = run.kind == "api"
        env = {"CODEX_HOME": _HOME_IN_JAIL}
        args = ["exec"]
        if run.cli_id:
            args += ["resume"]
        args += ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if api:
            conf = llm.api_config()
            env["TEXDECK_API_KEY"] = conf.api_key
            args += ["-c", "model_provider=\"texdeck\"",
                     "-c", f"model_providers.texdeck={{name=\"API key\", base_url={json.dumps(conf.base_url)}, "
                           f"env_key=\"TEXDECK_API_KEY\", wire_api={json.dumps(settings.codex_api_wire)}}}",
                     "-m", conf.model]
        if not run.cli_id:
            args += ["-C", _WORK_IN_JAIL]
        if run.cli_id:
            args += [run.cli_id]
        args += ["-"]
        return jail(binary, codex_home(api=api), work, env) + args, prompt, None
    binary = agy_binary()
    if binary is None:
        raise AgentError("agy is not installed on the server")
    if not agy.signed_in():
        raise AgentError("agy is not signed in; connect it from the model button at the top")
    args = ["--output-format", "stream-json", "--dangerously-skip-permissions", "--disable-slash-commands",
            "--print-timeout", f"{RUN_TIMEOUT_SECONDS}s"]
    model = llm.agy_model()
    if model and model != "default":
        args += ["--model", model]
    if run.cli_id:
        args += ["--conversation", run.cli_id]
    args.append("-p=" + prompt)
    return jail(binary, agy.home(), work, {}) + args, None, None


# --------------------------------------------------------------------------
# Stream parsers: CLI events -> transcript items
# --------------------------------------------------------------------------


def _parse_claude(run: Run, event: dict[str, Any], state: dict[str, Any]) -> None:
    kind = event.get("type")
    if event.get("session_id"):
        run.cli_id = str(event["session_id"])
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                _end_text(run)
                _add(run, {"kind": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                _tool(run, str(block.get("id")), str(block.get("name")), _detail(block.get("input") or {}), "running")
    elif kind == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
                _tool(run, str(block.get("tool_use_id")), "", "", "error" if block.get("is_error") else "done",
                      str(content or ""))
    elif kind == "result":
        state["done"] = True
        usage = event.get("usage") or {}
        run.usage["input"] += int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) \
            + int(usage.get("cache_creation_input_tokens") or 0)
        run.usage["output"] += int(usage.get("output_tokens") or 0)
        if event.get("is_error"):
            state["error"] = _explain("claude", str(event.get("result") or "")) or str(event.get("result") or "The agent failed")


def _parse_codex(run: Run, event: dict[str, Any], state: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "thread.started":
        run.cli_id = str(event.get("thread_id") or run.cli_id)
    elif kind in ("item.started", "item.updated", "item.completed"):
        item = event.get("item") or {}
        itype = item.get("type")
        key = str(item.get("id"))
        done = kind == "item.completed"
        if itype == "agent_message" and done and str(item.get("text", "")).strip():
            _add(run, {"kind": "text", "text": item["text"]})
        elif itype == "command_execution":
            status = "running" if not done else ("error" if item.get("exit_code") not in (0, None) else "done")
            _tool(run, key, "shell", _short_path(str(item.get("command") or "")), status,
                  str(item.get("aggregated_output") or "") if done else "")
        elif itype == "file_change":
            changes = item.get("changes") or []
            detail = ", ".join(f"{c.get('kind', 'edit')} {_short_path(str(c.get('path', '')))}" for c in changes)
            _tool(run, key, "edit", detail, "done" if done else "running")
        elif itype in ("web_search", "mcp_tool_call"):
            _tool(run, key, str(itype), _detail(item), "done" if done else "running")
        elif itype == "error" and done:  # a warning; a real failure ends the turn with turn.failed
            _add(run, {"kind": "notice", "text": str(item.get("message") or "")})
    elif kind == "turn.completed":
        state["done"] = True
        usage = event.get("usage") or {}
        run.usage["input"] += int(usage.get("input_tokens") or 0)
        run.usage["output"] += int(usage.get("output_tokens") or 0)
    elif kind == "turn.failed":
        state["error"] = str((event.get("error") or {}).get("message") or "The agent failed")
    elif kind == "error":
        message = str(event.get("message") or "")
        if message and not message.startswith("Reconnecting"):
            state["error"] = _explain("codex", message) or message


def _parse_agy(run: Run, event: dict[str, Any], state: dict[str, Any]) -> None:
    kind = event.get("event")
    if kind == "init":
        run.cli_id = str(event.get("conversation_id") or run.cli_id)
    elif kind == "step_update":
        step = event.get("step_update") or {}
        stype, done = step.get("step_type"), step.get("state") == "DONE"
        if stype == "agent_response":
            if step.get("text_delta"):
                _append_text(run, step["text_delta"])
            if done:
                _end_text(run)
                usage = step.get("usage") or {}
                run.usage["input"] += int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_tokens") or 0)
                run.usage["output"] += int(usage.get("output_tokens") or 0) + int(usage.get("thinking_tokens") or 0)
        elif stype == "tool":
            info = step.get("tool_info") or {}
            name = str(info.get("name") or step.get("tool_name") or "tool")
            status = "running" if not done else ("error" if step.get("error") else "done")
            _tool(run, f"{step.get('step_index')}", name, _detail(info.get("parameters") or {}), status,
                  str(info.get("output") or "") if done else "")
    elif kind == "result":
        result = event.get("result") or {}
        run.cli_id = str(result.get("conversation_id") or run.cli_id)
        state["done"] = True
        if result.get("status") != "SUCCESS":
            state["error"] = str(result.get("error") or result.get("finished_reason") or "The agent failed")


_PARSERS: dict[str, Callable[[Run, dict[str, Any], dict[str, Any]], None]] = {
    "claude": _parse_claude, "codex": _parse_codex, "agy": _parse_agy,
}


# agy's own text generation prunes conversation history after an hour; the ones
# a Papering session continues are kept (agy.KEEP_FILE).


def _agy_conversations() -> set[str]:
    try:
        return set(json.loads((agy.home() / agy.KEEP_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


def _remember_agy_conversations(gen_id: str) -> None:
    ids = _agy_conversations() | {s["cli_id"] for s in list_sessions(gen_id) if s.get("agent") == "agy" and s.get("cli_id")}
    paper._write_json(agy.home() / agy.KEEP_FILE, sorted(ids))


def _forget_agy_conversation(cli_id: str) -> None:
    """A deleted session's conversation goes back to the normal one-hour pruning."""
    paper._write_json(agy.home() / agy.KEEP_FILE, sorted(_agy_conversations() - {cli_id}))


# --------------------------------------------------------------------------
# Codex sign-in (device code)
# --------------------------------------------------------------------------

_DEVICE_URL_RE = re.compile(r"https://auth\.openai\.com/\S*device\S*")
_DEVICE_CODE_RE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


@dataclass(eq=False)
class _CodexLogin:
    proc: subprocess.Popen[bytes]
    user: str | None
    url: str = ""
    code: str = ""
    error: str = ""
    done: bool = False


_codex_logins: dict[str | None, _CodexLogin] = {}


def codex_login_state() -> dict[str, Any]:
    login = _codex_logins.get(auth.current_user.get())
    if login is None:
        return {"active": False}
    return {"active": not login.done, "url": login.url, "code": login.code, "error": login.error}


def start_codex_login() -> dict[str, Any]:
    binary = codex_binary()
    if binary is None:
        raise AgentError("Codex is not installed on the server")
    user = auth.current_user.get()
    cancel_codex_login()
    args = jail(binary, codex_home(), None, {"CODEX_HOME": _HOME_IN_JAIL}) + ["login", "--device-auth"]
    master, slave = pty.openpty()
    proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    login = _CodexLogin(proc=proc, user=user)
    _codex_logins[user] = login
    threading.Thread(target=_watch_codex_login, args=(login, master), daemon=True).start()
    deadline = time.time() + 20
    while time.time() < deadline and not (login.url and login.code) and not login.done:
        time.sleep(0.2)
    if not login.url and not login.error:
        login.error = "Codex did not show a sign-in code"
    return codex_login_state()


def _watch_codex_login(login: _CodexLogin, master: int) -> None:
    buffer = ""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer = (buffer + _ANSI_RE.sub("", chunk.decode("utf-8", errors="replace")))[-8000:]
            if not login.url and (m := _DEVICE_URL_RE.search(buffer)):
                login.url = m.group(0)
            if not login.code and (m := _DEVICE_CODE_RE.search(buffer.split("code", 1)[-1])):
                login.code = m.group(1)
    finally:
        os.close(master)
        code = login.proc.wait()
        login.done = True
        if code != 0 and not login.error:
            login.error = "Sign-in did not finish" if "expired" not in buffer.lower() else "The code expired; start again"


def cancel_codex_login() -> None:
    login = _codex_logins.pop(auth.current_user.get(), None)
    if login is not None:
        _kill(login.proc)


def sign_out_codex() -> None:
    (codex_home() / "auth.json").unlink(missing_ok=True)
