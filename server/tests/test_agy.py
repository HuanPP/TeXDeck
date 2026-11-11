"""agy provider: prompt folding, sandbox arguments, result parsing, gating. No real CLI."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app import agy, auth, llm
from app.config import settings

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
_JPG = base64.b64encode(b"\xff\xd8\xff fake").decode()


@pytest.fixture
def agy_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "agy-home"
    monkeypatch.setattr(settings, "agy_home", home)
    monkeypatch.setattr(settings, "model_state_file", tmp_path / "state.json")
    return home


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "find_command", lambda: "/usr/bin/true")
    monkeypatch.setattr(agy, "find_bwrap", lambda: "/usr/bin/bwrap")


def _sign_in(home: Path) -> None:
    token = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("{}")


# ---- prompt -----------------------------------------------------------------------


def test_to_prompt_folds_messages_and_writes_images(tmp_path: Path) -> None:
    prompt = agy.to_prompt(
        [
            {"role": "system", "content": "Output only LaTeX."},
            {"role": "user", "content": [
                {"type": "text", "text": "Page 1"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_JPG}"}},
            ]},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "Try again"},
        ],
        tmp_path,
    )
    assert prompt.startswith("<instructions>\n")
    head, body = prompt.split("</instructions>")
    assert "Do not run commands" in head and "/work/images" in head and "Output only LaTeX." in head
    assert "[Image 1: /work/images/001.png]" in body and "[Image 2: /work/images/002.jpg]" in body
    assert body.index("Page 1") < body.index("[Image 1") < body.index("old reply") < body.index("Try again")
    assert (tmp_path / "images" / "001.png").read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    assert (tmp_path / "images" / "002.jpg").is_file()


def test_to_prompt_without_images_does_not_mention_them(tmp_path: Path) -> None:
    prompt = agy.to_prompt([{"role": "user", "content": "Hi"}], tmp_path)
    assert "/work/images" not in prompt and not (tmp_path / "images").exists()


def test_to_prompt_rejects_remote_images_and_empty_requests(tmp_path: Path) -> None:
    with pytest.raises(agy.AgyError, match="data-URL"):
        agy.to_prompt([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}]}], tmp_path)
    with pytest.raises(agy.AgyError, match="Nothing to send"):
        agy.to_prompt([{"role": "system", "content": "x"}], tmp_path)


def test_build_args_keeps_the_prompt_flag_last() -> None:
    args = agy.build_args("gemini-3.8-flash-high")
    assert args[-1] == "-p=" and args[args.index("--model") + 1] == "gemini-3.8-flash-high"
    assert "--dangerously-skip-permissions" not in args
    assert "--model" not in agy.build_args("")


# ---- sandbox ----------------------------------------------------------------------


def test_sandbox_shows_only_system_home_and_work(tmp_path: Path) -> None:
    home, work = tmp_path / "h", tmp_path / "w"
    args = agy.sandbox_args(home, work, "/usr/bin/true", "/usr/bin/bwrap")
    joined = " ".join(args)
    assert args[:4] == ["/usr/bin/bwrap", "--ro-bind", "/usr", "/usr"]
    assert f"--bind {home} /home/agy" in joined and f"--bind {work} /work" in joined
    for flag in ("--unshare-all", "--share-net", "--die-with-parent", "--clearenv", "--new-session"):
        assert flag in args
    writable = [args[i + 1] for i, a in enumerate(args) if a == "--bind"]
    assert writable == [str(home), str(work)]
    # nothing else from the host is mounted: no /data, no real home, no whole /etc
    sources = [args[i + 1] for i, a in enumerate(args) if a in ("--bind", "--ro-bind", "--ro-bind-try")]
    assert not any(s in ("/", "/data", "/home", "/etc", str(Path.home())) for s in sources)
    assert args[-1] == "/opt/agy/agy"


def test_each_account_has_its_own_home(agy_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert agy.home() == agy_home
    monkeypatch.setattr(settings, "users_file", tmp_path / "users.json")
    assert auth.run_as("alice", agy.home) == tmp_path / "accounts" / "alice" / "agy"


def test_not_offered_without_bubblewrap(agy_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agy, "find_command", lambda: "/usr/bin/true")
    monkeypatch.setattr(agy, "find_bwrap", lambda: None)
    _sign_in(agy_home)
    assert "bubblewrap" in agy.availability()
    assert agy.status()["available"] is False and agy.status()["signed_in"] is False
    with pytest.raises(llm.LlmError, match="bubblewrap"):
        llm.select_provider("agy")
    with pytest.raises(agy.AgyError, match="bubblewrap"):
        agy.start_login()


# ---- calls ------------------------------------------------------------------------


def _stream(result: dict[str, Any]) -> bytes:
    lines = [{"event": "init", "init": {}}, {"event": "result", "result": result}]
    return "\n".join(json.dumps(line) for line in lines).encode()


def _fake_run(stdout: bytes, seen: list[list[str]], stderr: bytes = b""):
    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(cmd)
        sent = json.loads(kwargs["input"].decode())
        assert sent["event"] == "user"
        return subprocess.CompletedProcess(cmd, 0, stdout, stderr)
    return run


def test_complete_returns_text_and_usage(agy_home: Path, installed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(agy_home)
    seen: list[list[str]] = []
    monkeypatch.setattr(agy.subprocess, "run", _fake_run(_stream({
        "status": "SUCCESS", "response": "\\documentclass{beamer}",
        "usage": {"input_tokens": 900, "output_tokens": 40, "thinking_tokens": 10},
    }), seen))
    usage: list[tuple[int, int]] = []
    text = agy.complete([{"role": "user", "content": "Hi"}], lambda p, c: usage.append((p, c)), "m1")
    assert text == "\\documentclass{beamer}" and usage == [(900, 50)]
    assert seen[0][0] == "/usr/bin/bwrap" and seen[0][-1] == "-p=" and "m1" in seen[0]


def test_complete_reports_agent_errors(agy_home: Path, installed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(agy_home)
    monkeypatch.setattr(agy.subprocess, "run", _fake_run(
        _stream({"status": "ERROR", "error": "quota exhausted", "usage": {}}), []))
    with pytest.raises(agy.AgyError, match="quota exhausted"):
        agy.complete([{"role": "user", "content": "Hi"}])
    monkeypatch.setattr(agy.subprocess, "run", _fake_run(
        b"Authentication required. Please visit the URL to log in:", []))
    with pytest.raises(agy.AgyError, match="signed out"):
        agy.complete([{"role": "user", "content": "Hi"}])


def test_complete_refuses_to_start_signed_out(agy_home: Path, installed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_a: Any, **_k: Any) -> None:
        raise AssertionError("must not start agy: it would read the prompt as an OAuth code")
    monkeypatch.setattr(agy.subprocess, "run", never)
    with pytest.raises(agy.AgyError, match="not signed in"):
        agy.complete([{"role": "user", "content": "Hi"}])


def test_parse_models() -> None:
    out = "Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-sonnet-4-6\tClaude Sonnet 4.6\n"
    assert agy.parse_models(out) == [
        {"value": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
        {"value": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
    ]


# ---- provider selection -------------------------------------------------------------


def test_signed_in_agy_can_be_selected_and_used(agy_home: Path, installed: None) -> None:
    _sign_in(agy_home)
    llm.select_provider("agy")
    assert llm.active_provider() == "agy"
    llm.set_model("agy", "claude-sonnet-4-6")
    client = llm.create_client()
    assert isinstance(client, llm.AgyClient) and client.model == "claude-sonnet-4-6"
    status = llm.model_status()
    assert status["agy"]["signed_in"] and status["agy"]["model"] == "claude-sonnet-4-6"


def test_signed_out_agy_falls_back(agy_home: Path, installed: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in(agy_home)
    llm.select_provider("agy")
    agy.sign_out()
    monkeypatch.setattr(llm.chatgpt, "load_credential", lambda: None)
    assert llm.active_provider() == "api"


def test_sign_in_stops_agy_once_the_token_is_saved(
    agy_home: Path, installed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the code, agy answers its prompt (a slow model call); the token is enough."""
    import sys
    import time

    token = agy_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    script = (
        "import sys, time, pathlib\n"
        "print('Authentication required. Please visit the URL to log in:', flush=True)\n"
        "print('  https://accounts.google.com/o/oauth2/auth?x=1', flush=True)\n"
        "sys.stdin.readline()\n"
        f"p = pathlib.Path({str(token)!r}); p.parent.mkdir(parents=True, exist_ok=True)\n"
        "p.write_text('{\"token\": {\"access_token\": \"a\"}}')\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(agy, "_jailed", lambda _work, *_args: [sys.executable, "-c", script])
    monkeypatch.setattr(settings, "model_state_file", agy_home.parent / "state.json")
    url = agy.start_login()
    assert url.startswith("https://accounts.google.com/o/oauth2/")
    started = time.monotonic()
    agy.submit_code("4/real-looking-code")
    assert time.monotonic() - started < 10
    status = agy.status()
    assert status["signed_in"] and not status["pending"] and status["login_error"] == ""
    assert llm.active_provider() == "agy"
