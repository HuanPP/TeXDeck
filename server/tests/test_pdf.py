"""Tests for multi-PDF loading, the combined page cap, and page selection."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app import pdf
from app.config import settings
from app.pdf import _split_global_selection, load_pdfs


# ---- _split_global_selection (pure function) ---------------------------------


def test_split_global_selection_docstring_example() -> None:
    assert _split_global_selection([12, 46, 29], [3, 15, 60]) == [[3], [3], [2]]


def test_split_global_selection_boundary_pages() -> None:
    # Last global page of doc 1 and first global page of doc 2
    assert _split_global_selection([3, 3], [3, 4]) == [[3], [1]]
    # First and last global pages overall
    assert _split_global_selection([2, 5], [1, 7]) == [[1], [5]]


def test_split_global_selection_all_in_one_doc() -> None:
    assert _split_global_selection([5, 5], [6, 7, 8, 9, 10]) == [[], [1, 2, 3, 4, 5]]


def test_split_global_selection_preserves_per_doc_order() -> None:
    # Validation upstream sorts the numbers; mapping keeps them ascending per doc
    assert _split_global_selection([4, 4], [1, 3, 5, 8]) == [[1, 3], [1, 4]]


# ---- load_pdfs ----------------------------------------------------------------


def test_load_pdfs_two_docs_per_doc_dirs_and_numbering(
    tmp_path: Path, make_pdf: Callable[[str, int], Path]
) -> None:
    """Two small PDFs: per-doc page dirs, per-doc 1-based numbering, no LLM call."""
    pdf_a = make_pdf("a.pdf", 3)
    pdf_b = make_pdf("b.pdf", 3)
    work = tmp_path / "work"
    work.mkdir()

    stages: list[tuple[str, int]] = []
    documents = load_pdfs(
        [(pdf_a, "a.pdf"), (pdf_b, "b.pdf")],
        work,
        progress=lambda stage, pct: stages.append((stage, pct)),
    )

    assert len(documents) == 2
    for doc_index, (document, name) in enumerate(zip(documents, ["a.pdf", "b.pdf"]), start=1):
        assert document.index == doc_index
        assert document.name == name
        assert [p.number for p in document.pages] == [1, 2, 3]  # restarts per doc
        # Total 6 <= pdf_max_vision_pages: every page selected, no selection call
        assert document.selected_numbers == [1, 2, 3]
        pages_dir = work / "pages" / f"doc_{doc_index:02d}"
        assert sorted(p.name for p in pages_dir.glob("*.png")) == [
            "page_001.png",
            "page_002.png",
            "page_003.png",
        ]
        for page in document.pages:
            assert page.image_path.is_file()
            assert page.image_path.parent == pages_dir
    assert stages[-1] == ("PDF processing complete", 100)
    # No thumbnails and no LLM dump when selection did not run
    assert not (work / "thumbs").exists()
    assert not (work / "llm").exists()


def test_load_pdfs_combined_cap_lists_every_file(
    tmp_path: Path,
    make_pdf: Callable[[str, int], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf, "_PDF_HARD_MAX_PAGES", 4)
    pdf_a = make_pdf("alpha.pdf", 3)
    pdf_b = make_pdf("beta.pdf", 3)
    work = tmp_path / "work"
    work.mkdir()

    with pytest.raises(RuntimeError) as excinfo:
        load_pdfs([(pdf_a, "alpha.pdf"), (pdf_b, "beta.pdf")], work)
    message = str(excinfo.value)
    assert "total 6 pages" in message
    assert "4-page combined limit" in message
    assert "alpha.pdf: 3 pages" in message
    assert "beta.pdf: 3 pages" in message


# fitz refuses to save zero-page documents, so hand-craft one (repair mode opens it)
_ZERO_PAGE_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [] /Count 0 >>
endobj
trailer
<< /Root 1 0 R >>
%%EOF
"""


def test_load_pdfs_empty_doc_named(
    tmp_path: Path, make_pdf: Callable[[str, int], Path]
) -> None:
    pdf_a = make_pdf("a.pdf", 2)
    empty = tmp_path / "void.pdf"
    empty.write_bytes(_ZERO_PAGE_PDF)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError, match=r"no renderable pages: void\.pdf"):
        load_pdfs([(pdf_a, "a.pdf"), (empty, "void.pdf")], work)


# ---- selection across documents (fake LLM) -------------------------------------


class _FakeSelectClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> str:
        self.calls.append(messages)
        return self.response


def _user_texts(messages: list[dict[str, Any]]) -> list[str]:
    content = messages[1]["content"]
    return [part["text"] for part in content if part.get("type") == "text"]


def test_selection_across_docs_global_numbering_maps_back(
    tmp_path: Path,
    make_pdf: Callable[[str, int], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global 1..M selection prompt; results map back to per-doc page numbers."""
    monkeypatch.setattr(settings, "pdf_max_vision_pages", 4)
    fake = _FakeSelectClient(json.dumps({"pages": [1, 3, 4, 6], "reason": "test pick"}))
    monkeypatch.setattr(pdf, "create_client", lambda: fake)

    pdf_a = make_pdf("a.pdf", 3)
    pdf_b = make_pdf("b.pdf", 3)
    work = tmp_path / "work"
    work.mkdir()
    documents = load_pdfs([(pdf_a, "a.pdf"), (pdf_b, "b.pdf")], work)

    assert len(fake.calls) == 1
    # Global picks 1,3 fall in doc 1; 4,6 are doc 2 pages 1,3
    assert documents[0].selected_numbers == [1, 3]
    assert documents[1].selected_numbers == [1, 3]
    assert [p.number for p in documents[0].selected_pages] == [1, 3]

    texts = _user_texts(fake.calls[0])
    header = texts[0]
    assert "2 documents totaling 6 pages" in header
    assert 'Doc 1 "a.pdf" = global pages 1-3' in header
    assert 'Doc 2 "b.pdf" = global pages 4-6' in header
    assert "GLOBAL page numbers" in header
    # Captions carry the global number first, then the per-doc identity
    captions = texts[1:]
    assert any(c.startswith('Page 4 — Doc 2 ("b.pdf") p.1 | ') for c in captions)
    assert any(c.startswith('Page 3 — Doc 1 ("a.pdf") p.3 | ') for c in captions)

    # Thumbnails per doc; prompt/response dumps written
    assert (work / "thumbs" / "doc_01" / "page_001.png").is_file()
    assert (work / "thumbs" / "doc_02" / "page_003.png").is_file()
    assert (work / "llm" / "page_select_prompt.json").is_file()
    assert (work / "llm" / "page_select_response.txt").is_file()


def test_selection_single_doc_prompt_is_byte_identical(
    tmp_path: Path,
    make_pdf: Callable[[str, int], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One document must produce exactly the pre-multi-PDF selection prompt."""
    monkeypatch.setattr(settings, "pdf_max_vision_pages", 3)
    fake = _FakeSelectClient(json.dumps({"pages": [1, 3, 5], "reason": "ok"}))
    monkeypatch.setattr(pdf, "create_client", lambda: fake)

    source = make_pdf("solo.pdf", 5)
    work = tmp_path / "work"
    work.mkdir()
    documents = load_pdfs([(source, "solo.pdf")], work)

    assert documents[0].selected_numbers == [1, 3, 5]
    texts = _user_texts(fake.calls[0])
    assert texts[0] == (
        "This research report has 5 pages. Select exactly 3 pages that form the main argument. "
        'Return strict JSON only: {"pages":[page numbers...],"reason":"one English sentence on the selection"}. '
        "pages must be exactly 3 unique integer page numbers in range 1..5."
    )
    for number, caption in enumerate(texts[1:], start=1):
        assert caption.startswith(f"Page {number} | ")
        assert "Doc" not in caption
