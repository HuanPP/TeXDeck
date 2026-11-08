"""Shared fixtures for TeXDeck server tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import fitz
import pytest

from app.config import settings


class FakeLlm:
    """Stand-in for app.llm.LlmClient: scripted responses + on_usage semantics.

    Mirrors the real client's usage reporting: the per-call on_usage wins,
    otherwise the constructor-level callback fires, once per complete() call.
    """

    def __init__(
        self,
        responses: list[str],
        on_usage: Callable[[int, int], None] | None = None,
        usage: tuple[int, int] = (0, 0),
    ) -> None:
        self._responses = list(responses)
        self._on_usage = on_usage
        self._usage = usage
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        on_usage: Callable[[int, int], None] | None = None,
    ) -> str:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError(f"FakeLlm ran out of responses after {len(self.calls)} calls")
        callback = on_usage if on_usage is not None else self._on_usage
        if callback is not None:
            callback(*self._usage)
        return self._responses.pop(0)


@pytest.fixture
def fake_xelatex_ok(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the real xelatex with an instant success that writes a fake PDF."""
    counter = {"runs": 0}

    def fake_run(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
        counter["runs"] += 1
        (work_dir / f"{tex_path.stem}.pdf").write_bytes(b"%PDF-fake")
        return True, "ok"

    monkeypatch.setattr("app.deck._run_xelatex", fake_run)
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")
    return counter


@pytest.fixture
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.artifact_root at an isolated temp directory."""
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setattr(settings, "artifact_root", root)
    return root


@pytest.fixture
def make_pdf(tmp_path: Path) -> Callable[[str, int], Path]:
    """Factory producing a real n-page PDF on disk (blank pages via fitz)."""

    def _make(name: str, pages: int = 1) -> Path:
        doc = fitz.open()
        try:
            for _ in range(pages):
                doc.new_page()
            path = tmp_path / name
            doc.save(str(path))
        finally:
            doc.close()
        return path

    return _make


@pytest.fixture(autouse=True)
def _isolated_chatgpt_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep tests away from a developer's real ChatGPT sign-in."""
    path = tmp_path / "chatgpt-auth.json"
    monkeypatch.setattr(settings, "chatgpt_auth_file", path)
    monkeypatch.setattr("app.chatgpt._models_cache", None)
    return path


@pytest.fixture(autouse=True)
def _model_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs in tests run on fake clients; skip the submit-time model check."""
    monkeypatch.setattr("app.main.ensure_model_ready", lambda: None)


@pytest.fixture(autouse=True)
def _isolated_claude_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests away from a developer's real Claude Code CLI and saved model choice."""
    from app import claude_code

    monkeypatch.setattr(settings, "claude_code_command", str(tmp_path / "no-claude-here"))
    monkeypatch.setattr(settings, "model_state_file", tmp_path / "model-state.json")
    monkeypatch.setattr(settings, "users_file", tmp_path / "users.json")
    monkeypatch.setattr(claude_code, "_auth_cache", {})
    yield
    claude_code.cancel_login()
