"""Claude Code headless provider, driven against a fake `claude` executable."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import claude_code
from app.config import settings
from app.llm import ClaudeCodeClient, LlmError, active_model, active_provider, create_client

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake CLI is a shebang script")

# Records argv, cwd, the system prompt file and stdin, then prints whatever
# stream-json lines the test put in FAKE_CLAUDE_OUTPUT.
FAKE_CLI = """\
#!{python}
import json, os, sys
argv = sys.argv[1:]
system = open(argv[argv.index("--system-prompt-file") + 1], encoding="utf-8").read()
record = {{"argv": argv, "cwd": os.getcwd(), "system": system, "stdin": sys.stdin.read()}}
open(os.environ["FAKE_CLAUDE_RECORD"], "w").write(json.dumps(record))
sys.stdout.write(open(os.environ["FAKE_CLAUDE_OUTPUT"]).read())
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""


def _result(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "type": "result", "subtype": "success", "is_error": False, "result": text,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "cache_creation_input_tokens": 100,
                  "cache_read_input_tokens": 20, "output_tokens": 7},
        **extra,
    }


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.format(python=sys.executable))
    script.chmod(0o755)
    record, output = tmp_path / "record.json", tmp_path / "output.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setenv("FAKE_CLAUDE_OUTPUT", str(output))
    monkeypatch.setattr(settings, "claude_code_command", str(script))
    monkeypatch.setattr(settings, "llm_provider", "claude-code")

    def respond(*events: dict[str, Any], exit_code: int = 0) -> None:
        output.write_text("".join(json.dumps(e) + "\n" for e in events))
        monkeypatch.setenv("FAKE_CLAUDE_EXIT", str(exit_code))

    def seen() -> dict[str, Any]:
        return json.loads(record.read_text())

    respond({"type": "system", "subtype": "init"}, _result("ok"))
    return respond, seen


def test_messages_fold_into_one_user_turn() -> None:
    png = "data:image/png;base64,iVBORw0K"
    system, line = claude_code.to_stream_input([
        {"role": "system", "content": "Be a LaTeX expert."},
        {"role": "user", "content": "template"},
        {"role": "user", "content": [
            {"type": "text", "text": "page 1"},
            {"type": "image_url", "image_url": {"url": png}},
            {"type": "text", "text": "  "},
        ]},
        {"role": "assistant", "content": "bad tex"},
        {"role": "user", "content": "Previous output was invalid"},
    ])
    assert system == "Be a LaTeX expert."
    assert line["type"] == "user" and line["message"]["role"] == "user"
    blocks = line["message"]["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text", "text"]
    assert blocks[2]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}
    assert "<previous_reply>\nbad tex\n</previous_reply>" in blocks[3]["text"]


def test_remote_image_url_is_rejected() -> None:
    with pytest.raises(claude_code.ClaudeCodeError, match="data-URL"):
        claude_code.to_stream_input([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}])


def test_completion_runs_isolated_headless_cli(fake_cli) -> None:
    respond, seen = fake_cli
    respond({"type": "assistant"}, _result("\\documentclass{beamer}"))
    usage: list[tuple[int, int]] = []
    client = create_client(on_usage=lambda p, c: usage.append((p, c)))
    assert isinstance(client, ClaudeCodeClient)
    text = client.complete([{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}])
    assert text == "\\documentclass{beamer}"
    assert usage == [(123, 7)]
    record = seen()
    argv = record["argv"]
    assert argv[0] == "-p"
    for flag in ("--safe-mode", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--model") + 1] == settings.claude_code_model
    assert "--effort" not in argv
    assert record["system"] == "SYS"
    assert Path(record["cwd"]).name.startswith("texdeck-claude-")
    assert not Path(record["cwd"]).exists()  # temp dir removed afterwards
    sent = json.loads(record["stdin"])
    assert sent["message"]["content"] == [{"type": "text", "text": "hi"}]


def test_error_result_is_reported_with_usage(fake_cli) -> None:
    respond, _ = fake_cli
    respond(_result("Not logged in · Please run /login", is_error=True), exit_code=1)
    usage: list[tuple[int, int]] = []
    with pytest.raises(LlmError, match="Not logged in"):
        create_client().complete([{"role": "user", "content": "hi"}], on_usage=lambda p, c: usage.append((p, c)))
    assert usage == [(123, 7)]


def test_truncated_reply_fails(fake_cli) -> None:
    respond, _ = fake_cli
    respond(_result("\\begin{frame}", stop_reason="max_tokens"))
    with pytest.raises(LlmError, match="cut off"):
        create_client().complete([{"role": "user", "content": "hi"}])


def test_missing_result_reports_exit_code(fake_cli) -> None:
    respond, _ = fake_cli
    respond(exit_code=3)
    with pytest.raises(LlmError, match="exited with code 3"):
        create_client().complete([{"role": "user", "content": "hi"}])


def test_provider_selection(fake_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "claude_code_model", "")
    assert active_provider() == "claude-code" and active_model() == "default"
    monkeypatch.setattr(settings, "llm_provider", "auto")
    assert active_provider() != "claude-code"  # auto never picks it
    monkeypatch.setattr(settings, "llm_provider", "claude-code")
    monkeypatch.setattr(settings, "claude_code_command", "/nonexistent/claude")
    with pytest.raises(LlmError, match="not found"):
        create_client()


def test_model_route_reports_claude_code(fake_cli) -> None:
    from app.main import app

    body = TestClient(app).get("/api/model").json()
    assert body["provider"] == "claude-code" and body["claude_code_available"] is True
