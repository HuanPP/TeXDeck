"""Disk hygiene: pages/ and thumbs/ renders removed after successful generations."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import store
from app.config import settings
from app.deck import DeckError
from app.main import _run_generation
from tests.conftest import FakeLlm


_DECK = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\begin{document}
\begin{frame}{Hello}
Body text.
\end{frame}
\end{document}
"""


def _make_text_generation(with_renders: bool = True) -> store.GenerationMeta:
    meta = store.new_generation(
        title="Cleanup Deck",
        template_key="navy",
        template_name="Navy",
        instruction="",
        source_kind="text",
        source_names=[],
    )
    gdir = store.generation_dir(meta.id)
    (gdir / "source.txt").write_text("Notes for the deck.", encoding="utf-8")
    if with_renders:
        # Simulate leftover render output (text mode never creates these itself,
        # which keeps the test independent of real PDF rendering).
        (gdir / "pages" / "doc_01").mkdir(parents=True)
        (gdir / "pages" / "doc_01" / "page_001.png").write_bytes(b"\x89PNG")
        (gdir / "thumbs" / "doc_01").mkdir(parents=True)
        (gdir / "thumbs" / "doc_01" / "page_001.png").write_bytes(b"\x89PNG")
    return meta


def _patch_llm(monkeypatch: pytest.MonkeyPatch, responses: list[str]) -> None:
    monkeypatch.setattr(
        "app.main.create_client",
        lambda on_usage=None: FakeLlm(responses, on_usage=on_usage),
    )


def test_success_deletes_pages_and_thumbs(
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    meta = _make_text_generation()
    gdir = store.generation_dir(meta.id)
    _patch_llm(monkeypatch, [_DECK])

    _run_generation(meta.id, lambda _s, _p: None)

    assert store.load_meta(meta.id).status == "succeeded"
    assert not (gdir / "pages").exists()
    assert not (gdir / "thumbs").exists()
    # Only renders are hygiene targets; user data stays
    assert (gdir / "source.txt").is_file()
    assert (gdir / "main.tex").is_file()


def test_keep_page_renders_setting_preserves_them(
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    monkeypatch.setattr(settings, "keep_page_renders", True)
    meta = _make_text_generation()
    gdir = store.generation_dir(meta.id)
    _patch_llm(monkeypatch, [_DECK])

    _run_generation(meta.id, lambda _s, _p: None)

    assert store.load_meta(meta.id).status == "succeeded"
    assert (gdir / "pages" / "doc_01" / "page_001.png").is_file()
    assert (gdir / "thumbs" / "doc_01" / "page_001.png").is_file()


def test_failure_keeps_renders_for_debugging(
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    meta = _make_text_generation()
    gdir = store.generation_dir(meta.id)
    # Invalid output twice: generation fails after its one retry
    _patch_llm(monkeypatch, ["not latex", "still not latex"])

    with pytest.raises(DeckError):
        _run_generation(meta.id, lambda _s, _p: None)

    assert store.load_meta(meta.id).status == "failed"
    assert (gdir / "pages" / "doc_01" / "page_001.png").is_file()
    assert (gdir / "thumbs" / "doc_01" / "page_001.png").is_file()
