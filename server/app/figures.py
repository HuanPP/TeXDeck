"""User images ("figures") that the model places on the slides.

The browser already turns every image into a PNG or JPEG of at most 2400 px a
side (it applies EXIF rotation and decodes WebP/HEIC); the server checks the
result again, strips metadata by re-encoding, and stores it as
<generation>/figures/fig-NN.<ext>. The meta keeps the original name, the
optional note and the pixel size.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

from . import deck
from .llm import image_block, text_block

FIGURE_DIR = deck.FIGURE_DIR
MAX_FIGURES = 12
MAX_FIGURE_BYTES = 20 * 1024 * 1024
MAX_FIGURE_SIDE = 4096  # the browser sends at most 2400; this guards direct API use
MAX_NOTE_CHARS = 300
_PREVIEW_SIDE = 1024  # what the model looks at
PREVIEW_DIR = "figure_previews"

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


def preview(gdir: Path, figure: dict[str, Any]) -> Path:
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
        blocks.append(image_block(preview(gdir, figure)))
    return blocks
