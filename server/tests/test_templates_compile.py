"""Regression: every template bundle compiles with real xelatex after cleanup."""

from __future__ import annotations

import shutil
from pathlib import Path

import fitz
import pytest

from app.config import settings
from app.edit import parse_frames


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


def _run_xelatex(work_dir: Path) -> None:
    xelatex = shutil.which(settings.xelatex_command)
    if xelatex is None:
        pytest.skip(
            f"{settings.xelatex_command!r} not found on PATH; install TeX Live to run compile tests"
        )

    import subprocess

    cmd = [
        xelatex,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "main.tex",
    ]
    # Two passes: TOC / references stabilize on the second run
    for pass_no in (1, 2):
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=settings.xelatex_timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            log_path = work_dir / "main.log"
            log_tail = ""
            if log_path.is_file():
                log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2500:]
            pytest.fail(
                f"xelatex pass {pass_no} failed (exit {proc.returncode}) in {work_dir}\n"
                f"--- stdout/stderr (tail) ---\n{(proc.stdout or '')[-1500:]}"
                f"{(proc.stderr or '')[-500:]}\n"
                f"--- main.log (tail) ---\n{log_tail}"
            )


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

    # main.tex + all .sty + src/ + font/ (when present)
    shutil.copy2(src / "main.tex", work / "main.tex")
    for sty in src.glob("*.sty"):
        shutil.copy2(sty, work / sty.name)
    for folder in ("src", "font"):
        src_folder = src / folder
        if src_folder.is_dir():
            shutil.copytree(src_folder, work / folder)

    original = (work / "main.tex").read_text(encoding="utf-8")
    cleaned = _strip_instruction_frames(original)
    # Sanity: instruction frames actually removed
    for frame in parse_frames(cleaned):
        assert "[STYLE REF]" not in frame.tex
        assert "AI GENERATION INSTRUCTIONS" not in frame.tex
    (work / "main.tex").write_text(cleaned, encoding="utf-8")

    _run_xelatex(work)

    pdf_path = work / "main.pdf"
    assert pdf_path.is_file(), f"main.pdf was not produced for template {template_key}"
    doc = fitz.open(pdf_path)
    try:
        assert doc.page_count > 0, f"PDF for {template_key} has zero pages"
    finally:
        doc.close()
