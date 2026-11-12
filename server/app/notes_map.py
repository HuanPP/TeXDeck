"""Line study notes up with their source PDFs.

The notes template asks the model to end every sheet, \\notehead and \\notesub
line with \\src{N} (\\src{D:N}, \\src{N-M}): the source page that part
summarises. Synctex says where each of those lines landed in the notes PDF;
the heading's own text, found on that page, pins the exact height.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import fitz

from .edit import _is_comment_line, latex_to_text, read_synctex_points

SRC_RE = re.compile(r"\\src\s*\{\s*(?:(\d+)\s*:\s*)?(\d+)(?:\s*[-\u2013]\s*(\d+))?\s*\}")
# The heading a \src sits on: sheet title, \notehead or \notesub (one level of
# nested braces, as in a title with \key{...} or CO$_2$).
_ARG = r"\{((?:[^{}]|\{[^{}]*\})*)\}"
HEAD_RE = re.compile(
    rf"\\(?:begin\{{(notesheet)\}}(?:\[[^\]]*\])?|(notehead)|(notesub))\s*{_ARG}"
)
LEVELS = {"notesheet": 0, "notehead": 1, "notesub": 2}


@dataclass(slots=True)
class Anchor:
    line: int  # 1-based line of the \src in main.tex
    doc: int  # 1-based source document
    page: int  # first source page
    to: int  # last source page (== page for a single page)
    level: int  # 0 sheet, 1 \notehead, 2 \notesub, 3 anywhere else
    title: str  # heading text, plain
    notes_page: int = 0  # page in the notes PDF; 0 if unresolved
    y: float = 0.0  # top of the heading on that page, as a fraction of its height


def parse_anchors(tex: str) -> list[Anchor]:
    anchors: list[Anchor] = []
    for i, line in enumerate(tex.splitlines(), start=1):
        if _is_comment_line(line):
            continue
        code = re.split(r"(?<!\\)%", line, maxsplit=1)[0]
        head = HEAD_RE.search(code)
        level = 3
        title = ""
        if head:
            kind = head.group(1) or head.group(2) or head.group(3)
            level = LEVELS[kind]
            title = latex_to_text(SRC_RE.sub("", head.group(4)))
        for m in SRC_RE.finditer(code):
            doc = int(m.group(1) or 1)
            first = int(m.group(2))
            last = max(first, int(m.group(3) or first))
            anchors.append(Anchor(i, doc, first, last, level, title))
    return anchors


def place_anchors(anchors: list[Anchor], tex_path: Path, pdf_path: Path) -> list[Anchor]:
    """Fill notes_page and y; anchors that cannot be placed keep notes_page 0."""
    points = read_synctex_points(tex_path)
    with fitz.open(pdf_path) as pdf:
        for anchor in anchors:
            hit = _forward_point(points, anchor.line)
            if hit is None or not 1 <= hit[0] <= pdf.page_count:
                continue
            page_no, baseline = hit
            page = pdf.load_page(page_no - 1)
            height = page.rect.height or 1.0
            top = _heading_top(page, anchor.title, baseline)
            anchor.notes_page = page_no
            anchor.y = min(1.0, max(0.0, top / height))
    return anchors


def _forward_point(points: dict[int, tuple[int, float]], line: int) -> tuple[int, float] | None:
    """Where the \\src line landed; a bare \\src line prints nothing, so take
    the next line that does (the content it introduces)."""
    for candidate in range(line, line + 30):
        if candidate in points:
            return points[candidate]
    return None


def _heading_top(page: fitz.Page, title: str, baseline: float) -> float:
    """Top of the heading's own text nearest the synctex baseline, or an
    estimate one line above the baseline when the text is not found."""
    needle = title.split("\n", 1)[0].strip()[:24]
    if needle:
        rects = page.search_for(needle)
        near = [r for r in rects if abs(r.y1 - baseline) < 60]
        if near:
            return min(near, key=lambda r: abs(r.y1 - baseline)).y0
    return max(0.0, baseline - 30)
