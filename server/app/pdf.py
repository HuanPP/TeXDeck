from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from .config import settings
from .llm import ProgressCallback, create_client, image_block, text_block

# Hard cap: refuse above this page count (independent of vision selection limit)
_PDF_HARD_MAX_PAGES = 200


@dataclass(slots=True)
class PdfPage:
    number: int  # 1-based
    image_path: Path  # high-res PNG
    text: str  # plain text from PyMuPDF


@dataclass(slots=True)
class PdfDocument:
    path: Path
    pages: list[PdfPage]
    selected_numbers: list[int]  # page numbers actually sent to generation (ascending)
    selection_reason: str  # non-empty only when selection ran; one English sentence

    @property
    def selected_pages(self) -> list[PdfPage]:
        wanted = set(self.selected_numbers)
        return [p for p in self.pages if p.number in wanted]


def _strip_code_fence(raw: str) -> str:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0]
    return cleaned.strip()


def _sanitize_messages_for_disk(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace base64 image_url payloads with placeholders so dumps stay small."""
    out: list[dict[str, Any]] = []
    page_counter = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_parts: list[dict[str, Any]] = []
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "image_url"
                and isinstance(part.get("image_url"), dict)
            ):
                page_counter += 1
                new_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"<image:page_{page_counter:03d}>"},
                    }
                )
            else:
                new_parts.append(part)
        out.append({**msg, "content": new_parts})
    return out


def _parse_and_validate_selection(raw: str, *, total_pages: int, k: int) -> tuple[list[int], str]:
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Page selection failed: model did not return valid JSON ({exc})") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Page selection failed: response must be a JSON object")

    pages_raw = data.get("pages")
    reason = data.get("reason")

    if not isinstance(pages_raw, list):
        raise RuntimeError("Page selection failed: pages must be a list of integers")
    if not isinstance(reason, str) or not reason.strip():
        raise RuntimeError("Page selection failed: reason must be a non-empty string")

    numbers: list[int] = []
    for item in pages_raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise RuntimeError("Page selection failed: pages must be a list of integers")
        numbers.append(item)

    unique = sorted(set(numbers))
    if len(unique) != k:
        raise RuntimeError(
            f"Page selection failed: after dedup, page count must be exactly {k}, got {len(unique)}"
        )
    for n in unique:
        if n < 1 or n > total_pages:
            raise RuntimeError(
                f"Page selection failed: page number {n} is outside 1..{total_pages}"
            )

    return unique, reason.strip()


def _render_page_png(page: fitz.Page, out_path: Path, dpi: int) -> None:
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(out_path))


def _select_pages_with_llm(
    pages: list[PdfPage],
    work_dir: Path,
    doc: fitz.Document,
    *,
    progress: ProgressCallback | None,
) -> tuple[list[int], str]:
    k = settings.pdf_max_vision_pages
    m = len(pages)
    thumbs_dir = work_dir / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    thumb_paths: list[Path] = []
    for page in pages:
        thumb_path = thumbs_dir / f"page_{page.number:03d}.png"
        _render_page_png(doc.load_page(page.number - 1), thumb_path, settings.pdf_thumbnail_dpi)
        thumb_paths.append(thumb_path)
        if progress is not None and page.number % max(1, m // 10) == 0:
            pct = min(89, 50 + int(page.number / m * 30))
            progress(f"Rendering thumbnail {page.number}/{m}", pct)

    user_content: list[dict[str, Any]] = [
        text_block(
            f"This research report has {m} pages. Select exactly {k} pages that form the main argument. "
            f'Return strict JSON only: {{"pages":[page numbers...],"reason":"one English sentence on the selection"}}. '
            f"pages must be exactly {k} unique integer page numbers in range 1..{m}."
        )
    ]
    for page, thumb_path in zip(pages, thumb_paths, strict=True):
        preview = page.text[:200]
        user_content.append(image_block(thumb_path))
        user_content.append(text_block(f"Page {page.number} | {preview}"))

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are selecting pages that form the main argument of a research report. "
                "Return strict JSON only; no explanation and no markdown fences."
            ),
        },
        {"role": "user", "content": user_content},
    ]

    if progress is not None:
        progress("Selecting key pages with the model", 90)

    response = create_client().complete(messages, temperature=0.0)

    llm_dir = work_dir / "llm"
    llm_dir.mkdir(parents=True, exist_ok=True)
    (llm_dir / "page_select_prompt.json").write_text(
        json.dumps(_sanitize_messages_for_disk(messages), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (llm_dir / "page_select_response.txt").write_text(response, encoding="utf-8")

    selected, reason = _parse_and_validate_selection(response, total_pages=m, k=k)
    return selected, reason


def load_pdf(
    pdf_path: Path,
    work_dir: Path,
    *,
    progress: ProgressCallback | None = None,
) -> PdfDocument:
    pdf_path = Path(pdf_path)
    work_dir = Path(work_dir)
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    try:
        page_count = doc.page_count
        if page_count == 0:
            raise RuntimeError("PDF has no renderable pages")
        if page_count > _PDF_HARD_MAX_PAGES:
            raise RuntimeError(
                f"PDF has {page_count} pages, exceeding the {_PDF_HARD_MAX_PAGES}-page limit. "
                "Please split it first."
            )

        pages: list[PdfPage] = []
        # Report progress every few pages
        report_every = max(1, page_count // 20)

        for index in range(page_count):
            page = doc.load_page(index)
            number = index + 1
            image_path = pages_dir / f"page_{number:03d}.png"
            _render_page_png(page, image_path, settings.pdf_render_dpi)
            text = page.get_text("text")
            pages.append(PdfPage(number=number, image_path=image_path, text=text))

            if progress is not None and (number % report_every == 0 or number == page_count):
                # Rendering stage maps to 0–50%
                pct = int(number / page_count * 50)
                progress(f"Rendering page {number}/{page_count}", pct)

        if len(pages) <= settings.pdf_max_vision_pages:
            selected_numbers = [p.number for p in pages]
            selection_reason = ""
        else:
            selected_numbers, selection_reason = _select_pages_with_llm(
                pages, work_dir, doc, progress=progress
            )

        if progress is not None:
            progress("PDF processing complete", 100)

        return PdfDocument(
            path=pdf_path,
            pages=pages,
            selected_numbers=selected_numbers,
            selection_reason=selection_reason,
        )
    finally:
        doc.close()
