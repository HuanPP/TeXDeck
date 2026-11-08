"""Claude Code headless provider, driven against a fake `claude` executable."""

from __future__ import annotations

import json
import os
import sys
import time
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
logged_in = os.environ["FAKE_CLAUDE_LOGIN"]
if argv[:2] == ["auth", "status"]:
    if os.path.exists(logged_in):
        print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com",
                          "subscriptionType": "pro"}}))
    else:
        print(json.dumps({{"loggedIn": False, "authMethod": "none"}}))
        sys.exit(1)
    sys.exit(0)
if argv[:2] == ["auth", "login"]:
    assert argv[2] == "--claudeai"
    print("Opening browser to sign in...")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=s1")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code != "good#s1":
        sys.stderr.write("Login failed: That code wasn't accepted.\\n")
        sys.exit(1)
    open(logged_in, "w").close()
    print("Login successful.")
    sys.exit(0)
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
    monkeypatch.setenv("FAKE_CLAUDE_LOGIN", str(tmp_path / "logged-in"))
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


def test_pinned_provider(fake_cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "claude_code_model", "")
    assert active_provider() == "claude-code" and active_model() == "default"
    monkeypatch.setattr(settings, "claude_code_command", "/nonexistent/claude")
    with pytest.raises(LlmError, match="not found"):
        create_client()


@pytest.fixture
def auto_mode(fake_cli, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "llm_api_key", "")
    return fake_cli


def test_auto_never_picks_unchosen_claude(auto_mode, tmp_path: Path) -> None:
    (tmp_path / "logged-in").touch()
    assert active_provider() == "api"


def test_sign_in_relays_url_and_pasted_code(auto_mode) -> None:
    from app.main import app

    http = TestClient(app)
    before = http.get("/api/model").json()
    assert before["claude"]["available"] is True and before["claude"]["signed_in"] is False
    assert before["provider"] == "api"
    started = http.post("/api/auth/claude/login").json()
    assert started["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=s1"
    assert started["claude"]["pending"] is True
    bad = http.post("/api/auth/claude/code", json={"code": "wrong"})
    assert bad.status_code == 400 and "wasn't accepted" in bad.json()["detail"]
    assert http.get("/api/model").json()["claude"]["login_error"].startswith("Login failed")
    assert active_provider() == "api"

    http.post("/api/auth/claude/login")
    done = http.post("/api/auth/claude/code", json={"code": "  good#s1 \n"}).json()
    assert done["provider"] == "claude-code" and done["model"] == settings.claude_code_model
    claude = done["claude"]
    assert claude["signed_in"] is True and claude["email"] == "me@example.com"
    assert claude["subscription"] == "pro" and claude["pending"] is False and claude["login_error"] == ""


def test_already_signed_in_cli_is_selected_without_a_link(auto_mode, tmp_path: Path) -> None:
    from app.main import app

    (tmp_path / "logged-in").touch()
    started = TestClient(app).post("/api/auth/claude/login").json()
    assert started["url"] == "" and started["provider"] == "claude-code"


def test_cancel_kills_pending_login(auto_mode) -> None:
    from app.main import app

    http = TestClient(app)
    http.post("/api/auth/claude/login")
    proc = claude_code._logins[None].proc
    after = http.delete("/api/auth/claude/login").json()
    assert after["claude"]["pending"] is False
    assert proc.wait(5) is not None
    assert http.post("/api/auth/claude/code", json={"code": "good#s1"}).status_code == 400
    assert after["provider"] == "api"


def test_switching_providers(auto_mode, tmp_path: Path) -> None:
    from app.main import app

    http = TestClient(app)
    assert http.put("/api/model/provider", json={"provider": "claude-code"}).status_code == 400
    assert http.put("/api/model/provider", json={"provider": "chatgpt"}).status_code == 400
    assert http.put("/api/model/provider", json={"provider": "api"}).status_code == 400

    saved = http.put("/api/model/api", json={
        "api_key": "sk-secret-1234", "base_url": "https://example.com/v1", "model": "vision-1",
    }).json()
    assert saved["provider"] == "api" and saved["model"] == "vision-1"
    assert saved["api"] == {"configured": True, "base_url": "https://example.com/v1", "model": "vision-1",
                            "key_hint": "\u2022\u2022\u2022\u20221234", "saved": True}
    assert "sk-secret" not in json.dumps(saved)
    assert (settings.model_state_file.stat().st_mode & 0o777) == 0o600

    (tmp_path / "logged-in").touch()
    assert http.put("/api/model/provider", json={"provider": "claude-code"}).json()["provider"] == "claude-code"
    assert (tmp_path / "logged-in").exists()
    back = http.put("/api/model/provider", json={"provider": "api"}).json()
    assert back["provider"] == "api"

    # A blank key keeps the stored one when editing the other fields.
    edited = http.put("/api/model/api", json={"api_key": "", "base_url": "https://example.com/v2", "model": "vision-2"}).json()
    assert edited["api"]["key_hint"].endswith("1234") and edited["model"] == "vision-2"
    client = create_client()
    assert client.model == "vision-2"

    removed = http.delete("/api/model/api").json()
    assert removed["api"]["configured"] is False and removed["provider"] == "api"
    with pytest.raises(LlmError, match="No model connected"):
        create_client()


def test_chatgpt_sign_in_becomes_the_choice(auto_mode, tmp_path: Path) -> None:
    from app import chatgpt
    from app.main import app

    http = TestClient(app)
    (tmp_path / "logged-in").touch()
    http.post("/api/auth/claude/login")
    assert active_provider() == "claude-code"
    chatgpt.save_credential(chatgpt.Credential(access="a", refresh="r", expires=time.time() + 600, client_id="c"))
    assert active_provider() == "claude-code"  # an existing token alone does not switch
    chatgpt.on_signed_in()  # what a finished ChatGPT sign-in calls
    assert active_provider() == "chatgpt"
    chatgpt.sign_out()
    assert active_provider() == "api"  # chosen provider gone: fall back


def test_pinned_provider_cannot_be_switched(auto_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setattr(settings, "llm_provider", "api")
    response = TestClient(app).put("/api/model/provider", json={"provider": "chatgpt"})
    assert response.status_code == 400 and "TEXDECK_LLM_PROVIDER" in response.json()["detail"]
    assert TestClient(app).get("/api/model").json()["locked"] is True


def test_sign_in_without_cli_explains() -> None:
    from app.main import app

    response = TestClient(app).post("/api/auth/claude/login")
    assert response.status_code == 400 and "not found" in response.json()["detail"]


def test_chosen_claude_model_reaches_the_cli(auto_mode, tmp_path: Path) -> None:
    from app.main import app

    _, seen = auto_mode
    (tmp_path / "logged-in").touch()
    http = TestClient(app)
    http.post("/api/auth/claude/login")
    status = http.put("/api/model/model", json={"provider": "claude-code", "model": "opus"}).json()
    assert status["model"] == "opus" and status["claude"]["model"] == "opus"
    assert [m["value"] for m in status["claude"]["models"]] == ["fable", "opus", "sonnet", "haiku"]
    create_client().complete([{"role": "user", "content": "hi"}])
    argv = seen()["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
