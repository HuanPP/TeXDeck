"""Papering agent: stream parsing, the jail command, runs and sessions (fake CLI)."""

from __future__ import annotations

import json
import base64
import sys
import time
from pathlib import Path

import pytest

from app import paper, paper_agent


def _run(kind: str = "claude") -> paper_agent.Run:
    return paper_agent.Run(gen_id="g", sid="s", kind=kind, items=[])


@pytest.fixture(autouse=True)
def _no_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    # Parser tests touch only the Run in memory.
    monkeypatch.setattr(paper_agent, "_persist", lambda run, force=False: None)


def test_claude_stream() -> None:
    run, state = _run(), {}
    events = [
        {"type": "system", "subtype": "init", "session_id": "abc"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/work/sec/intro.tex"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        {"type": "result", "subtype": "success", "session_id": "abc", "is_error": False,
         "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}},
    ]
    for e in events:
        paper_agent._parse_claude(run, e, state)
    assert run.cli_id == "abc" and state["done"]
    assert [i["kind"] for i in run.items] == ["text", "tool", "text"]
    assert run.items[1] == {"kind": "tool", "key": "t1", "name": "Edit", "detail": "sec/intro.tex", "status": "done",
                            "output": "ok"}
    assert run.usage == {"input": 15, "output": 3}


def test_codex_stream_warnings_are_not_errors() -> None:
    run, state = _run("codex"), {}
    events = [
        {"type": "thread.started", "thread_id": "th-1"},
        {"type": "item.completed", "item": {"id": "i0", "type": "error", "message": "Model metadata not found"}},
        {"type": "item.started", "item": {"id": "i1", "type": "command_execution", "command": "bash -lc ls"}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution", "command": "bash -lc ls",
                                            "aggregated_output": "main.tex\n", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "i2", "type": "file_change",
                                            "changes": [{"path": "/work/main.tex", "kind": "update"}]}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "Updated."}},
        {"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 2}},
    ]
    for e in events:
        paper_agent._parse_codex(run, e, state)
    assert run.cli_id == "th-1" and state == {"done": True}
    assert [i["kind"] for i in run.items] == ["notice", "tool", "tool", "text"]
    assert run.items[1]["status"] == "done" and run.items[1]["output"] == "main.tex\n"
    assert run.items[2]["detail"] == "update main.tex"


def test_agy_stream_joins_text_deltas() -> None:
    run, state = _run("agy"), {}
    step = lambda **kw: {"event": "step_update", "step_update": kw}  # noqa: E731
    events = [
        {"event": "init", "conversation_id": "c-1"},
        step(step_index=2, state="ACTIVE", step_type="tool", tool_info={"name": "edit_file", "parameters": {"Path": "/work/a.tex"}}),
        step(step_index=2, state="DONE", step_type="tool", tool_info={"name": "edit_file", "parameters": {"Path": "/work/a.tex"}}),
        step(step_index=3, state="ACTIVE", step_type="agent_response", text_delta="Hel"),
        step(step_index=3, state="DONE", step_type="agent_response", text_delta="lo", usage={"input_tokens": 4, "output_tokens": 1}),
        {"event": "result", "result": {"conversation_id": "c-1", "status": "SUCCESS", "response": "Hello"}},
    ]
    for e in events:
        paper_agent._parse_agy(run, e, state)
    assert run.cli_id == "c-1" and state["done"]
    assert run.items == [{"kind": "tool", "key": "2", "name": "edit_file", "detail": "a.tex", "status": "done"},
                         {"kind": "text", "text": "Hello"}]


def test_jail_isolates_the_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paper_agent, "_requirements", lambda: "")
    monkeypatch.setattr(paper_agent, "_ip_command", lambda: "/usr/sbin/ip")
    monkeypatch.setattr(paper_agent, "_resolv_file", lambda: tmp_path / "resolv.conf")
    binary = tmp_path / "agent"
    args = paper_agent.jail(binary, tmp_path / "home", tmp_path / "work", {"K": "V"})
    assert args[:4] == ["pasta", "--config-net", "--no-map-gw", "--"]
    script = args[args.index("-c") + 1]
    for net in ("169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
        assert f"route add unreachable {net}" in script
    assert "ip -batch -" in script and 'exec "$@"' in script
    bwrap = args[args.index("sh", args.index("-c") + 2) + 1:]
    assert "--clearenv" in bwrap and "--share-net" in bwrap and "--unshare-all" in bwrap
    assert bwrap[bwrap.index("--bind", bwrap.index(str(binary))) + 1:][:3] == [str(tmp_path / "home"), "/home/agent", "--bind"]
    assert str(tmp_path / "work") in bwrap and "/work" in bwrap
    assert ["--setenv", "K", "V"] == bwrap[bwrap.index("K") - 1: bwrap.index("K") + 2]
    assert bwrap[-1] == "/opt/agent/bin"


FAKE_CLI = r'''
import json, sys, time
prompt = sys.stdin.read()
if "slow" in prompt:
    time.sleep(30)
print(json.dumps({"type": "system", "session_id": "sess-9"}), flush=True)
open("notes.tex", "w").write("% " + prompt[-20:])
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Wrote notes.tex"}]}}), flush=True)
print(json.dumps({"type": "result", "session_id": "sess-9", "usage": {"input_tokens": 1, "output_tokens": 1}}), flush=True)
'''


@pytest.fixture
def fake_cli(artifact_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.undo()  # keep the real _persist for these
    monkeypatch.setattr(paper.settings, "artifact_root", artifact_root)
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    seen: list[str] = []

    def command(run: paper_agent.Run, prompt: str):
        seen.append(run.cli_id)
        return [sys.executable, str(script)], prompt, None

    monkeypatch.setattr(paper_agent, "_command", command)
    monkeypatch.setattr(paper_agent, "options", lambda: {"agents": {k: {"available": True, "reason": ""} for k in paper_agent.KINDS}})
    monkeypatch.chdir(tmp_path)
    return seen


def _wait(gid: str) -> None:
    for _ in range(200):
        if not paper_agent.running(gid):
            return
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_run_resume_and_sessions(fake_cli: list[str]) -> None:
    gid = paper.create("Agent", owner="").id
    sid = paper_agent.start(gid, "claude", "first request")
    _wait(gid)
    items = paper_agent.transcript(gid, sid)
    assert [i["kind"] for i in items] == ["user", "text", "done"]
    session = paper_agent.list_sessions(gid)[0]
    assert session["cli_id"] == "sess-9" and session["title"] == "first request"
    quote = {"source": "tex", "path": "main.tex", "from": 3, "to": 3, "text": "a line", "extra": "dropped"}
    assert paper_agent.start(gid, "claude", "second", sid=sid, context={"quotes": [quote]}) == sid
    _wait(gid)
    assert fake_cli == ["", "sess-9"]  # the second run resumes the CLI session
    assert paper_agent.transcript(gid, sid)[3]["quotes"] == [{"label": "main.tex:3", "text": "a line"}]
    assert [i["kind"] for i in paper_agent.transcript(gid, sid)][-3:] == ["user", "text", "done"]
    with pytest.raises(paper_agent.AgentError):
        paper_agent.start(gid, "codex", "other agent", sid=sid)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    snip = {"source": "pdf", "page": 2, "text": "", "image": "data:image/png;base64," + base64.b64encode(png).decode()}
    bad = {"source": "pdf", "page": 2, "text": "", "image": "data:image/png;base64,bm90IGEgcG5n"}
    paper_agent.start(gid, "claude", "third", sid=sid, context={"quotes": [snip, bad]})
    _wait(gid)
    quotes = paper_agent.transcript(gid, sid)[-3]["quotes"]
    assert len(quotes) == 1 and quotes[0]["label"] == "PDF p.2 snip"
    assert paper_agent.snip_file(gid, sid, quotes[0]["image"]).read_bytes() == png
    paper_agent.delete_session(gid, sid)
    assert paper_agent.list_sessions(gid) == []
    assert not (paper_agent._dir(gid) / "snips" / sid).exists()


def test_one_run_per_project_and_stop(fake_cli: list[str]) -> None:
    gid = paper.create("Busy", owner="").id
    paper_agent.start(gid, "claude", "slow please")
    with pytest.raises(paper_agent.AgentError):
        paper_agent.start(gid, "claude", "another")
    time.sleep(0.3)
    paper_agent.stop(gid)
    _wait(gid)
    items = paper_agent.transcript(gid, paper_agent.list_sessions(gid)[0]["id"])
    assert {"kind": "error", "text": "Stopped"} in items


def test_context_mentions_quotes_and_build_error(artifact_root: Path) -> None:
    gid = paper.create("Ctx", owner="").id
    text = paper_agent._context(gid, {"path": "main.tex", "line": 12, "quotes": [
        {"source": "tex", "path": "main.tex", "from": 24, "to": 32, "text": "Some words"},
        {"source": "pdf", "page": 2, "path": "sec.tex", "line": 7, "text": "Rendered words"},
        {"source": "pdf", "page": 1, "text": "   "},
        "junk",
    ]})
    assert "main.tex open, cursor at line 12" in text
    assert '<quote from="main.tex:24-32">\nSome words\n</quote>' in text
    assert '<quote from="PDF p.2 (near sec.tex:7)">\nRendered words\n</quote>' in text
    assert text.count("<quote") == 2
