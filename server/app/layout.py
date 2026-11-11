"""After compiling: find layout faults and have the model fix them.

Three faults are checked. A slide runs off the page: TeX reports
`Overfull \\vbox (Npt too high) detected at line N`, where N is the frame's
\\end{frame} line. Text runs into other text: from the PDF, glyph by glyph
(see text_collisions). A user image is not on any slide. Slides with faults go
to the model in one call that returns only those slides; a missing image needs
a slide chosen for it, so then the whole deck is revised. A fix is kept only if
it compiles.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from . import deck, figures
from .deck import CompileError, DeckError, Template
from .edit import (
    EditError,
    Frame,
    build_frames_edit_messages,
    extract_frames,
    parse_frames,
    replace_frames,
    resolve_frame_pages,
)
from .llm import LlmClient, ProgressCallback, image_block, text_block

_OVERFULL_RE = re.compile(
    r"^Overfull \\vbox \((\d+(?:\.\d+)?)pt too high\) detected at line (\d+)", re.MULTILINE
)
# A slide may stick out by a hair without anyone seeing it.
_OVERFLOW_TOLERANCE_PT = 4.0
_MAX_ROUNDS = 2

# Glyphs are compared on a band above the baseline (no ascender or descender
# room, so tightly set lines do not touch). Kerned pairs such as "Ta" overlap by
# about a quarter of a glyph and a tenth of the font size; text that runs into
# other text overlaps by 40% of a glyph and a third of the size or more.
_BAND = (0.62, 0.05)  # top and bottom of the band, in font sizes above the baseline
_MIN_GLYPH_SHARE = 0.35
_MIN_SIZE_SHARE = 0.2


@dataclass(slots=True)
class _Glyph:
    x0: float
    x1: float
    top: float
    bottom: float
    char: str
    size: float
    origin: tuple[float, float]
    line: int  # index into the page's line texts
    pos: int  # position within that line


def _glyphs(page: fitz.Page) -> tuple[list[_Glyph], list[str]]:
    glyphs: list[_Glyph] = []
    lines: list[str] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            if tuple(line["dir"]) != (1.0, 0.0):
                continue  # rotated text
            chars = [(span, ch) for span in line["spans"] for ch in span["chars"]]
            line_no = len(lines)
            lines.append("".join(ch["c"] for _, ch in chars))
            for pos, (span, ch) in enumerate(chars):
                font = span["font"]
                # Math fonts stack and overlap on purpose (radicals, braces, fractions);
                # punctuation boxes overlap wherever CJK punctuation is squeezed.
                if "Math" in font or font.startswith(("CMEX", "CMSY")):
                    continue
                if not ch["c"].strip() or unicodedata.category(ch["c"]).startswith("P"):
                    continue
                size = span["size"]
                baseline = ch["origin"][1]
                glyphs.append(_Glyph(
                    ch["bbox"][0], ch["bbox"][2], baseline - _BAND[0] * size, baseline - _BAND[1] * size,
                    ch["c"], size, tuple(ch["origin"]), line_no, pos,
                ))
    return glyphs, lines


def _snippet(lines: list[str], glyph: _Glyph, other: _Glyph) -> str:
    if glyph.line == other.line:
        lo, hi = sorted((glyph.pos, other.pos))
        text = lines[glyph.line][max(0, lo - 6) : hi + 7]
        return f'labels run into each other in "{text.strip()}"'
    a, b = lines[glyph.line].strip()[:40], lines[other.line].strip()[:40]
    return f'"{a}" overlaps "{b}"'


def text_collisions(pdf_path: Path) -> dict[int, list[str]]:
    """Page number -> descriptions of text that runs into other text on it."""
    found: dict[int, list[str]] = {}
    try:
        doc = fitz.open(pdf_path)
    except Exception:  # MuPDF raises its own types; no readable PDF, nothing to check
        return found
    with doc:
        for number, page in enumerate(doc, start=1):
            glyphs, lines = _glyphs(page)
            glyphs.sort(key=lambda g: g.x0)
            seen: set[tuple[int, int]] = set()
            for i, a in enumerate(glyphs):
                for b in glyphs[i + 1 :]:
                    if b.x0 >= a.x1:
                        break
                    overlap_x = min(a.x1, b.x1) - b.x0
                    narrow = min(a.x1 - a.x0, b.x1 - b.x0) or 1.0
                    overlap_y = min(a.bottom, b.bottom) - max(a.top, b.top)
                    band = min(a.bottom - a.top, b.bottom - b.top) or 1.0
                    if overlap_y / band < 0.3 or overlap_x / narrow < _MIN_GLYPH_SHARE:
                        continue
                    if overlap_x < _MIN_SIZE_SHARE * min(a.size, b.size):
                        continue
                    if a.char == b.char and all(abs(p - q) < 0.15 * a.size for p, q in zip(a.origin, b.origin)):
                        continue  # one glyph drawn twice (fake bold, shadow)
                    key = (min(a.line, b.line), max(a.line, b.line))
                    if key in seen:
                        continue  # one report per pair of lines
                    seen.add(key)
                    found.setdefault(number, []).append(_snippet(lines, a, b))
    return found


def _frame_for_page(frames: list[Frame], page: int) -> Frame | None:
    placed = [f for f in frames if f.page and f.page <= page]
    return max(placed, key=lambda f: f.page) if placed else None


def frame_issues(tex: str, log: str, pdf_path: Path | None, tex_path: Path) -> dict[int, list[str]]:
    """Frame index -> what is wrong with that slide."""
    try:
        frames = parse_frames(tex)
    except EditError:
        return {}
    issues: dict[int, list[str]] = {}
    for match in _OVERFULL_RE.finditer(log):
        overflow, line = float(match.group(1)), int(match.group(2))
        if overflow <= _OVERFLOW_TOLERANCE_PT:
            continue
        frame = next((f for f in frames if f.start_line <= line <= f.end_line), None)
        if frame is not None and not issues.get(frame.index):
            issues.setdefault(frame.index, []).append(
                f"The slide runs {overflow:.0f}pt past the bottom of the page. Make an image or "
                "diagram smaller, put content side by side in columns, or cut text, so it all fits."
            )
    if pdf_path is not None and pdf_path.is_file():
        collisions = text_collisions(pdf_path)
        if collisions:
            try:
                placed = resolve_frame_pages(frames, tex_path)
            except EditError:
                placed = []
            for page, found in collisions.items():
                frame = _frame_for_page(placed, page)
                if frame is not None:
                    issues.setdefault(frame.index, []).append(
                        "Text collides with other text on the slide: " + "; ".join(found[:4])
                        + ". Move, shorten, shrink or wrap the labels, or space the diagram out, "
                        "so that nothing touches."
                    )
    return issues


def unused_figures(tex: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used = deck.graphics_refs(tex)
    return [f for f in records if not deck.ref_matches(f["file"], used)]


def build_deck_fix_messages(
    tex: str,
    problems: list[str],
    records: list[dict[str, Any]],
    unused_previews: list[Path],
    requirements: str,
) -> list[dict[str, Any]]:
    request = (
        "Problems in this deck:\n" + "\n".join(f"- {p}" for p in problems)
        + "\n\n" + figures.catalog(records) + "\n\n" + deck.PLACEMENT_RULE
    )
    if requirements.strip():
        request += (
            f"\n\nThe user's requirements for this deck: {requirements.strip()}\n"
            "If these requirements deliberately leave an image out, keep it out."
        )
    content: str | list[dict[str, Any]] = request
    if unused_previews:
        content = [text_block(request), text_block("The images that are not placed yet:")]
        content += [image_block(p) for p in unused_previews]
    return [
        {
            "role": "system",
            "content": (
                "You are revising a complete XeLaTeX Beamer deck to fix the problems listed. "
                "Output only the full revised, compilable document. No explanation, no markdown "
                "fences. Change only what the problems require; keep every other slide, all "
                "wording and the \\note{...} speaker notes as they are. " + deck.DIAGRAM_RULE
            ),
        },
        {"role": "user", "content": tex},
        {"role": "user", "content": content},
    ]


Dump = Callable[[str, list[dict[str, Any]], str], None]


def check_and_fix(
    gdir: Path,
    template: Template,
    records: list[dict[str, Any]],
    client: LlmClient,
    log: str,
    *,
    require_all_figures: bool,
    requirements: str = "",
    dump: Dump | None = None,
    progress: ProgressCallback | None = None,
    stem: str = "layout",
) -> None:
    """Best effort: the deck already compiled, so any failure here keeps that version."""
    tex_path = Path(gdir) / "main.tex"
    pdf_path = Path(gdir) / "main.pdf"
    for round_no in range(1, _MAX_ROUNDS + 1):
        current = tex_path.read_text(encoding="utf-8")
        issues = frame_issues(current, log, pdf_path, tex_path)
        missing = unused_figures(current, records) if require_all_figures else []
        if not issues and not missing:
            return
        if progress is not None:
            progress("Fixing layout problems", 96)
        files = deck.figure_files(gdir)
        frames = parse_frames(current)
        if missing:
            problems = [
                f"{figures.describe(f)} It is not on any slide; add it." for f in missing
            ] + [
                f"Slide {f.index} ({f.title or 'untitled'}): {issue}"
                for f in frames for issue in issues.get(f.index, [])
            ]
            previews = [figures.preview(gdir, f) for f in missing]
            messages = build_deck_fix_messages(current, problems, records, previews, requirements)
        else:
            requests = [(f, " ".join(issues[f.index])) for f in frames if f.index in issues]
            context = figures.catalog(records) if records else ""
            messages = build_frames_edit_messages(requests, template.main_tex, context)
        response = client.complete(messages)
        if dump is not None:
            dump(f"{stem}_{round_no}", messages, response)
        try:
            if missing:
                revised = deck.extract_tex(response, template, files)
            else:
                new = extract_frames(response, [f.index for f, _ in requests])
                for frame_tex in new.values():
                    deck.check_body(deck.strip_tex_comments(frame_tex), files)
                revised = replace_frames(current, frames, new)
        except (DeckError, EditError):
            return
        if deck.tex_effectively_equal(revised, current):
            return  # the model judged it fine (e.g. an image left out on purpose)
        tex_path.write_text(revised, encoding="utf-8")
        try:
            log = deck.compile_tex(tex_path, client=client, llm_dump_dir=Path(gdir) / "llm").log
        except CompileError:
            tex_path.write_text(current, encoding="utf-8")
            deck.compile_tex(tex_path, client=None)
            return
