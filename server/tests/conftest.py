"""Shared fixtures for TeXDeck server tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import fitz
import pytest

from app.config import settings


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
