"""Regression: every template bundle compiles with the real TeX engine after cleanup."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from app.config import settings
from app import deck
from app.deck import copy_template_bundle, get_template
from app.edit import parse_frames, resolve_frame_pages


def _template_keys() -> list[str]:
    root = Path(settings.templates_dir)
    keys: list[str] = []
    if not root.is_dir():
        return keys
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "template.json").is_file() and (child / "main.tex").is_file():
            keys.append(child.name)
    return keys


def _strip_instruction_frames(tex: str) -> str:
    """Drop [STYLE REF] and AI GENERATION INSTRUCTIONS frames (model-normal output)."""
    frames = parse_frames(tex)
    drop: set[int] = set()
    for frame in frames:
        if "[STYLE REF]" in frame.tex or "AI GENERATION INSTRUCTIONS" in frame.tex:
            for n in range(frame.start_line, frame.end_line + 1):
                drop.add(n)
    lines = tex.splitlines()
    kept = [line for i, line in enumerate(lines, start=1) if i not in drop]
    result = "\n".join(kept)
    if tex.endswith("\n"):
        result += "\n"
    return result


def _compile(work_dir: Path) -> None:
    """Build main.tex with the production engine runner (xelatex or Tectonic)."""
    try:
        engine = deck.tex_engine()
    except deck.CompileError as exc:
        pytest.skip(f"{exc}; install TeX Live or Tectonic to run compile tests")
    # xelatex needs a second pass for TOC / references; Tectonic iterates itself.
    for pass_no in range(1, 3 if engine == "xelatex" else 2):
        ok, log = deck._run_xelatex(work_dir / "main.tex", work_dir)
        if not ok:
            pytest.fail(f"{engine} pass {pass_no} failed in {work_dir}\n--- log (tail) ---\n{log[-2500:]}")


@pytest.mark.slow
@pytest.mark.parametrize("template_key", _template_keys())
def test_template_compiles_after_stripping_instruction_frames(
    template_key: str,
    tmp_path: Path,
) -> None:
    src = Path(settings.templates_dir) / template_key
    assert src.is_dir(), f"template dir missing: {src}"

    work = tmp_path / template_key
    work.mkdir()

    # Build the work dir exactly like production (_run_generation): the bundle
    # copy plus a written main.tex. Copying files any other way here once masked
    # a bundle bug where theme .sty files never reached the generation dir.
    template = get_template(template_key)
    copy_template_bundle(template, work)

    original = template.main_tex
    cleaned = _strip_instruction_frames(original)
    # Sanity: instruction frames actually removed
    for frame in parse_frames(cleaned):
        assert "[STYLE REF]" not in frame.tex
        assert "AI GENERATION INSTRUCTIONS" not in frame.tex
    (work / "main.tex").write_text(cleaned, encoding="utf-8")

    _compile(work)

    pdf_path = work / "main.pdf"
    assert pdf_path.is_file(), f"main.pdf was not produced for template {template_key}"
    doc = fitz.open(pdf_path)
    try:
        assert doc.page_count > 0, f"PDF for {template_key} has zero pages"
        page_count = doc.page_count
    finally:
        doc.close()

    # Per-slide editing maps frames to pages through the synctex file.
    frames = resolve_frame_pages(parse_frames(cleaned), work / "main.tex")
    pages = [frame.page for frame in frames if frame.page]
    assert len(pages) >= len(frames) - 2, f"synctex resolved too few frames: {[f.page for f in frames]}"
    assert pages == sorted(pages) and pages[-1] <= page_count
