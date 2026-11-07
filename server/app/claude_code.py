"""Generation through the user's own Claude Code CLI in headless mode.

TeXDeck never reads or stores Claude credentials. It starts the locally
installed `claude` binary with `-p`, which runs on whatever account that CLI is
signed in with, and reads the reply from its stream-json output. The child runs
in an empty temp directory with no tools, no MCP servers and --safe-mode, so
the user's CLAUDE.md, hooks and plugins stay out of the prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import settings

UsageCallback = Callable[[int, int], None]


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


def build_args(command: str, system_file: Path) -> list[str]:
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
    if settings.claude_code_model:
        args += ["--model", settings.claude_code_model]
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


def complete(messages: list[dict[str, Any]], on_usage: UsageCallback | None = None) -> str:
    command = find_command()
    if command is None:
        raise ClaudeCodeError(
            "Claude Code CLI not found. Install it and sign in with `claude auth login`, "
            "or point TEXDECK_CLAUDE_CODE_COMMAND at the binary"
        )
    system, line = to_stream_input(messages)
    with tempfile.TemporaryDirectory(prefix="texdeck-claude-") as tmp:
        system_file = Path(tmp) / "system.txt"
        system_file.write_text(system or "You are a helpful assistant.", encoding="utf-8")
        try:
            proc = subprocess.run(
                build_args(command, system_file),
                input=(json.dumps(line) + "\n").encode("utf-8"),
                capture_output=True,
                cwd=tmp,
                timeout=settings.llm_timeout_seconds,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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


def status() -> dict[str, Any]:
    return {"claude_code_available": find_command() is not None}
