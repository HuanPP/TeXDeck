"""User images ("figures") that the model places on the slides.

The browser already turns every image into a PNG or JPEG of at most 2400 px a
side (it applies EXIF rotation and decodes WebP/HEIC); the server checks the
result again, strips metadata by re-encoding, and stores it as
<generation>/figures/fig-NN.<ext>. The meta keeps the original name, the
optional note and the pixel size.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import fitz

from . import deck
from .deck import CompileError, DeckError, Template
from .edit import EditError, parse_frames
from .llm import LlmClient, ProgressCallback, image_block, text_block

FIGURE_DIR = deck.FIGURE_DIR
MAX_FIGURES = 12
MAX_FIGURE_BYTES = 20 * 1024 * 1024
MAX_FIGURE_SIDE = 4096  # the browser sends at most 2400; this guards direct API use
MAX_NOTE_CHARS = 300
_PREVIEW_SIDE = 1024  # what the model looks at
PREVIEW_DIR = "figure_previews"

# An image may stick out of a frame by a hair without anyone seeing it.
_OVERFLOW_TOLERANCE_PT = 4.0
_MAX_LAYOUT_ROUNDS = 2

PLACEMENT_RULE = deck.PLACEMENT_RULE


class FigureError(ValueError):
    """Rejected upload; message is user-facing English."""


def store(gdir: Path, uploads: list[tuple[str, bytes, str]]) -> list[dict[str, Any]]:
    """Check and save (name, bytes, note) uploads in order; returns their meta records."""
    if len(uploads) > MAX_FIGURES:
        raise FigureError(f"At most {MAX_FIGURES} images per generation")
    folder = Path(gdir) / FIGURE_DIR
    records: list[dict[str, Any]] = []
    for i, (name, data, note) in enumerate(uploads, start=1):
        clean, ext, width, height = _normalize(name, data)
        folder.mkdir(parents=True, exist_ok=True)
        rel = f"{FIGURE_DIR}/fig-{i:02d}.{ext}"
        (Path(gdir) / rel).write_bytes(clean)
        records.append(
            {"file": rel, "name": name, "note": note.strip()[:MAX_NOTE_CHARS],
             "width": width, "height": height}
        )
    return records


def _normalize(name: str, data: bytes) -> tuple[bytes, str, int, int]:
    if not data:
        raise FigureError(f"Image is empty: {name}")
    if len(data) > MAX_FIGURE_BYTES:
        raise FigureError(f"Image is larger than {MAX_FIGURE_BYTES // (1024 * 1024)} MB: {name}")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    else:
        raise FigureError(f"Use a PNG or JPEG image: {name}")
    try:
        pixmap = fitz.Pixmap(data)
    except Exception as exc:  # MuPDF raises its own error types
        raise FigureError(f"The image could not be read: {name}") from exc
    if max(pixmap.width, pixmap.height) > MAX_FIGURE_SIDE:
        raise FigureError(f"Image is larger than {MAX_FIGURE_SIDE} pixels on a side: {name}")
    if pixmap.colorspace is not None and pixmap.colorspace.n not in (1, 3):  # CMYK JPEG
        pixmap = fitz.Pixmap(fitz.csRGB, pixmap)
    if ext == "jpg" and pixmap.alpha:
        pixmap = fitz.Pixmap(pixmap, 0)
    clean = pixmap.tobytes("png") if ext == "png" else pixmap.tobytes("jpg", jpg_quality=92)
    return clean, ext, pixmap.width, pixmap.height


def describe(figure: dict[str, Any]) -> str:
    width, height = int(figure["width"]), int(figure["height"])
    ratio = width / max(height, 1)
    shape = (
        "wide" if ratio >= 1.6 else "landscape" if ratio > 1.15
        else "roughly square" if ratio >= 0.87 else "tall"
    )
    note = str(figure.get("note") or "").strip()
    return (
        f'{figure["file"]}: "{figure["name"]}", {width}x{height} px, {shape} '
        f"(width/height {ratio:.2f}). " + (f"User note: {note}" if note else "No user note.")
    )


def catalog(figures: list[dict[str, Any]]) -> str:
    """Plain-text list for edit and refine prompts (no pixels: they are already placed)."""
    if not figures:
        return "This deck has no image files; do not use \\includegraphics."
    lines = "\n".join(f"- {describe(f)}" for f in figures)
    return (
        "Image files available to this deck (\\includegraphics may reference only these, "
        "with width and height bounds and keepaspectratio):\n" + lines
    )


def _preview(gdir: Path, figure: dict[str, Any]) -> Path:
    """A copy no larger than _PREVIEW_SIDE for the model; cached next to the figures."""
    source = Path(gdir) / figure["file"]
    target = Path(gdir) / PREVIEW_DIR / source.name
    if target.is_file():
        return target
    pixmap = fitz.Pixmap(str(source))
    scale = _PREVIEW_SIDE / max(pixmap.width, pixmap.height)
    if scale < 1:
        pixmap = fitz.Pixmap(
            pixmap, max(1, round(pixmap.width * scale)), max(1, round(pixmap.height * scale)), None
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".jpg":
        target.write_bytes(pixmap.tobytes("jpg", jpg_quality=85))
    else:
        target.write_bytes(pixmap.tobytes("png"))
    return target


def prompt_blocks(gdir: Path, figures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The images as the model sees them: description, then the picture."""
    blocks: list[dict[str, Any]] = [
        text_block(f"--- {len(figures)} user image(s) to place on the slides ---")
    ]
    for figure in figures:
        blocks.append(text_block(describe(figure)))
        blocks.append(image_block(_preview(gdir, figure)))
    return blocks


# ---- after compiling: is every image used, does any slide with one overflow? ----

_OVERFULL_RE = re.compile(
    r"^Overfull \\vbox \((\d+(?:\.\d+)?)pt too high\) detected at line (\d+)", re.MULTILINE
)


def layout_issues(
    tex: str, log: str, figures: list[dict[str, Any]], *, require_all: bool
) -> list[str]:
    issues: list[str] = []
    used = deck.graphics_refs(tex)
    if require_all:
        for figure in figures:
            if not deck.ref_matches(figure["file"], used):
                issues.append(f"{describe(figure)} It is not on any slide; add it.")
    try:
        frames = parse_frames(tex)
    except EditError:
        return issues
    reported: set[int] = set()
    for match in _OVERFULL_RE.finditer(log):
        overflow, line = float(match.group(1)), int(match.group(2))
        if overflow <= _OVERFLOW_TOLERANCE_PT:
            continue
        frame = next((f for f in frames if f.start_line <= line <= f.end_line), None)
        if frame is None or frame.index in reported or not deck.graphics_refs(frame.tex):
            continue
        reported.add(frame.index)
        title = f' "{frame.title}"' if frame.title else ""
        issues.append(
            f"The slide{title} at source lines {frame.start_line}-{frame.end_line} "
            f"runs {overflow:.0f}pt past the bottom of the page. Make its image smaller, move "
            "it beside the text in columns, or cut text, so the whole slide fits."
        )
    return issues


def build_layout_fix_messages(
    tex: str,
    issues: list[str],
    figures: list[dict[str, Any]],
    unused_previews: list[Path],
    requirements: str,
) -> list[dict[str, Any]]:
    request = (
        "Problems with the user's images in this deck:\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + "\n\n" + catalog(figures) + "\n\n" + PLACEMENT_RULE
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
                "You are revising a complete XeLaTeX Beamer deck to fix how the user's images "
                "are placed. Output only the full revised, compilable document. No explanation, "
                "no markdown fences. Change only what the problems require; keep every other "
                "slide, all wording and the \\note{...} speaker notes as they are."
            ),
        },
        {"role": "user", "content": tex},
        {"role": "user", "content": content},
    ]


Dump = Callable[[str, list[dict[str, Any]], str], None]


def fix_layout(
    gdir: Path,
    template: Template,
    figures: list[dict[str, Any]],
    client: LlmClient,
    log: str,
    *,
    require_all: bool,
    requirements: str = "",
    dump: Dump | None = None,
    progress: ProgressCallback | None = None,
    stem: str = "layout",
) -> None:
    """Ask the model to fix unused or overflowing images, keeping a fix only if it compiles.

    Best effort: the deck already compiled, so any failure here keeps that version.
    """
    tex_path = Path(gdir) / "main.tex"
    for round_no in range(1, _MAX_LAYOUT_ROUNDS + 1):
        current = tex_path.read_text(encoding="utf-8")
        issues = layout_issues(current, log, figures, require_all=require_all)
        if not issues:
            return
        if progress is not None:
            progress("Fixing image placement", 96)
        used = deck.graphics_refs(current)
        unused = [
            _preview(gdir, f) for f in figures if not deck.ref_matches(f["file"], used)
        ] if require_all else []
        messages = build_layout_fix_messages(current, issues, figures, unused, requirements)
        response = client.complete(messages)
        if dump is not None:
            dump(f"{stem}_{round_no}", messages, response)
        try:
            revised = deck.extract_tex(response, template, deck.figure_files(gdir))
        except DeckError:
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
