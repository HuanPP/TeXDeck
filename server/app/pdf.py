from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fitz

from .config import settings
from .llm import ProgressCallback, UsageCallback, create_client, image_block, text_block

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
    name: str  # original upload filename, e.g. "report.pdf"
    index: int  # 1-based position in reading order
    pages: list[PdfPage]  # PdfPage.number stays per-document 1-based
    selected_numbers: list[int]  # per-doc page numbers actually sent to generation (ascending)

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
    # Clamp oversized pages (crafted or plotter-format PDFs) so one page cannot
    # allocate gigabytes of pixmap.
    rect = page.rect
    est_pixels = rect.width * rect.height * scale * scale
    if est_pixels > settings.pdf_max_page_pixels:
        scale = (settings.pdf_max_page_pixels / (rect.width * rect.height)) ** 0.5
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(str(out_path))


def _split_global_selection(page_counts: list[int], selected: list[int]) -> list[list[int]]:
    """Map globally numbered selections back to per-document 1-based page numbers.

    Example: page_counts [12, 46, 29], selected [3, 15, 60] -> [[3], [3], [2]].
    """
    out: list[list[int]] = [[] for _ in page_counts]
    bounds: list[tuple[int, int]] = []
    offset = 0
    for count in page_counts:
        bounds.append((offset + 1, offset + count))
        offset += count
    for g in selected:
        for i, (lo, hi) in enumerate(bounds):
            if lo <= g <= hi:
                out[i].append(g - lo + 1)
                break
    return out


def _selection_header(documents: list[PdfDocument], total: int, k: int) -> str:
    """Prompt header for the page-selection call.

    Single document keeps today's exact wording (no drift for the existing case);
    multiple documents get a doc-span header using GLOBAL page numbers 1..total.
    """
    if len(documents) == 1:
        return (
            f"This research report has {total} pages. Select exactly {k} pages that form the main argument. "
            f'Return strict JSON only: {{"pages":[page numbers...],"reason":"one English sentence on the selection"}}. '
            f"pages must be exactly {k} unique integer page numbers in range 1..{total}."
        )
    span_lines: list[str] = []
    offset = 0
    for document in documents:
        lo, hi = offset + 1, offset + len(document.pages)
        offset = hi
        span_lines.append(f'  Doc {document.index} "{document.name}" = global pages {lo}-{hi}')
    return (
        f"You are given {len(documents)} documents totaling {total} pages, "
        f"numbered globally 1..{total} in reading order:\n"
        + "\n".join(span_lines)
        + f"\nSelect exactly {k} pages that together form the main argument across all documents. "
        "Use the GLOBAL page numbers shown first in each caption. "
        f'Return strict JSON only: {{"pages":[global page numbers...],"reason":"one English sentence on the selection"}}. '
        f"pages must be exactly {k} unique integer GLOBAL page numbers in range 1..{total}."
    )


def _select_pages_with_llm(
    documents: list[PdfDocument],
    work_dir: Path,
    fitz_docs: list[fitz.Document],
    *,
    progress: ProgressCallback | None,
    on_usage: UsageCallback | None = None,
) -> None:
    """Pick the pages sent to generation; writes each document's selected_numbers.

    The selection prompt numbers pages globally 1..total across documents in
    reading order; results are mapped back to per-document page numbers.
    """
    k = settings.pdf_max_vision_pages
    multi = len(documents) > 1
    flat: list[tuple[int, PdfDocument, PdfPage, fitz.Document]] = []
    global_no = 0
    for document, fdoc in zip(documents, fitz_docs, strict=True):
        for page in document.pages:
            global_no += 1
            flat.append((global_no, document, page, fdoc))
    total = len(flat)

    thumb_paths: list[Path] = []
    for g, document, page, fdoc in flat:
        thumb_path = work_dir / "thumbs" / f"doc_{document.index:02d}" / f"page_{page.number:03d}.png"
        _render_page_png(fdoc.load_page(page.number - 1), thumb_path, settings.pdf_thumbnail_dpi)
        thumb_paths.append(thumb_path)
        if progress is not None and g % max(1, total // 10) == 0:
            pct = min(89, 50 + int(g / total * 30))
            progress(f"Rendering thumbnail {g}/{total}", pct)

    user_content: list[dict[str, Any]] = [text_block(_selection_header(documents, total, k))]
    for (g, document, page, _fdoc), thumb_path in zip(flat, thumb_paths, strict=True):
        preview = page.text[:200]
        user_content.append(image_block(thumb_path))
        if multi:
            caption = f'Page {g} — Doc {document.index} ("{document.name}") p.{page.number} | {preview}'
        else:
            caption = f"Page {page.number} | {preview}"
        user_content.append(text_block(caption))

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

    response = create_client().complete(messages, temperature=0.0, on_usage=on_usage)

    llm_dir = work_dir / "llm"
    llm_dir.mkdir(parents=True, exist_ok=True)
    (llm_dir / "page_select_prompt.json").write_text(
        json.dumps(_sanitize_messages_for_disk(messages), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (llm_dir / "page_select_response.txt").write_text(response, encoding="utf-8")

    selected, _reason = _parse_and_validate_selection(response, total_pages=total, k=k)
    per_doc = _split_global_selection([len(d.pages) for d in documents], selected)
    for document, numbers in zip(documents, per_doc, strict=True):
        document.selected_numbers = numbers


def load_pdfs(
    sources: list[tuple[Path, str]],
    work_dir: Path,
    *,
    progress: ProgressCallback | None = None,
    on_usage: UsageCallback | None = None,
) -> list[PdfDocument]:
    """Load PDFs in reading order and pick the pages sent to generation.

    sources: (path on disk, original upload filename) per document, reading order.
    The 200-page hard cap applies to the combined total across all documents.
    """
    work_dir = Path(work_dir)
    sources = [(Path(path), name) for path, name in sources]
    if not sources:
        raise RuntimeError("No PDF sources provided")

    fitz_docs = [fitz.open(path) for path, _ in sources]
    try:
        counts = [fdoc.page_count for fdoc in fitz_docs]
        for (_, name), count in zip(sources, counts, strict=True):
            if count == 0:
                raise RuntimeError(f"PDF has no renderable pages: {name}")
        total = sum(counts)
        if total > _PDF_HARD_MAX_PAGES:
            breakdown = "; ".join(
                f"{name}: {count} pages"
                for (_, name), count in zip(sources, counts, strict=True)
            )
            raise RuntimeError(
                f"Uploaded PDFs total {total} pages, exceeding the {_PDF_HARD_MAX_PAGES}-page "
                f"combined limit ({breakdown}). Please trim or split them first."
            )

        # Report progress every few pages; rendering stage maps to 0–50%
        report_every = max(1, total // 20)
        rendered = 0
        documents: list[PdfDocument] = []
        for doc_index, ((path, name), fdoc) in enumerate(
            zip(sources, fitz_docs, strict=True), start=1
        ):
            pages_dir = work_dir / "pages" / f"doc_{doc_index:02d}"
            pages_dir.mkdir(parents=True, exist_ok=True)
            pages: list[PdfPage] = []
            for page_index in range(fdoc.page_count):
                page = fdoc.load_page(page_index)
                number = page_index + 1
                image_path = pages_dir / f"page_{number:03d}.png"
                _render_page_png(page, image_path, settings.pdf_render_dpi)
                text = page.get_text("text")
                pages.append(PdfPage(number=number, image_path=image_path, text=text))
                rendered += 1
                if progress is not None and (rendered % report_every == 0 or rendered == total):
                    pct = int(rendered / total * 50)
                    progress(f"Rendering page {rendered}/{total}", pct)
            documents.append(
                PdfDocument(path=path, name=name, index=doc_index, pages=pages, selected_numbers=[])
            )

        if total <= settings.pdf_max_vision_pages:
            for document in documents:
                document.selected_numbers = [p.number for p in document.pages]
        else:
            _select_pages_with_llm(
                documents, work_dir, fitz_docs, progress=progress, on_usage=on_usage
            )

        if progress is not None:
            progress("PDF processing complete", 100)

        return documents
    finally:
        for fdoc in fitz_docs:
            fdoc.close()
