"""Shared fixtures for TeXDeck server tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture
def artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.artifact_root at an isolated temp directory."""
    root = tmp_path / "artifacts"
    root.mkdir()
    monkeypatch.setattr(settings, "artifact_root", root)
    return root
