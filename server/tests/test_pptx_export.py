from __future__ import annotations

from pathlib import Path

import fitz
from pptx import Presentation

from app.pptx_export import _is_math_font, pdf_to_pptx


def _make_pdf(path: Path) -> None:
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page(width=453.54, height=255.12)
        page.draw_rect(fitz.Rect(0, 0, 453.54, 30), color=None, fill=(0.2, 0.4, 0.75))
        page.insert_text((30, 60), f"Slide title {i + 1}", fontsize=14, color=(0.2, 0.4, 0.76))
        # Built-in CJK font: the base-14 Helvetica cannot encode U+2022.
        page.insert_font(fontname="cjk", fontbuffer=fitz.Font("cjk").buffer)
        page.insert_text((40, 90), "• First point", fontsize=9, fontname="cjk")
    doc.save(path)


def test_pdf_to_pptx_one_slide_per_page_with_editable_text(tmp_path: Path) -> None:
    pdf = tmp_path / "main.pdf"
    out = tmp_path / "main.pptx"
    _make_pdf(pdf)
    pdf_to_pptx(pdf, out)

    prs = Presentation(str(out))
    assert len(prs.slides) == 2
    assert prs.slide_width == 12192000  # standard 13.333 in
    slide = prs.slides[1]
    pictures = [s for s in slide.shapes if s.shape_type == 13]
    assert pictures and pictures[0].width == prs.slide_width  # background
    texts = [s.text_frame for s in slide.shapes if s.has_text_frame]
    joined = [tf.text for tf in texts]
    assert "Slide title 2" in joined
    title = next(tf for tf in texts if tf.text == "Slide title 2")
    run = title.paragraphs[0].runs[0]
    assert round(run.font.size.pt) == round(14 * 960 / 453.54)
    assert str(run.font.color.rgb) == "3366C2"
    # The bullet glyph becomes a real PowerPoint bullet, not text.
    bullet = next(tf for tf in texts if "First point" in tf.text)
    assert bullet.text == "First point"
    assert bullet.paragraphs[0]._p.pPr.find(
        "{http://schemas.openxmlformats.org/drawingml/2006/main}buChar"
    ).get("char") == "•"
    assert not list(tmp_path.glob(".pptx-*"))


def test_math_fonts_are_recognised() -> None:
    for font in ("CMR9", "CMMI10", "NewPXMI", "pxsys", "ABCDEF+txsyb", "LMMath-Regular", "MSBM10"):
        assert _is_math_font(font), font
    for font in ("TeXGyrePagella-Regular", "NotoSerifCJKjp-Bold", "LMSans10-Regular", "Helvetica", "CMUSerif"):
        assert not _is_math_font(font), font


def _chars(spec: list[tuple[str, float, float]]) -> list[dict]:
    return [{"c": c, "bbox": (x0, 0.0, x1, 10.0)} for c, x0, x1 in spec]


def test_respace_rebuilds_missing_word_space() -> None:
    from app.pptx_export import _respace

    # "Full Title": TeX glue between the words, no space glyph.
    seg = _chars([("F", 0, 5), ("u", 5, 10), ("l", 10, 13), ("l", 13, 16),
                  ("T", 19, 24), ("i", 24, 27), ("t", 27, 30), ("l", 30, 33), ("e", 33, 38)])
    text, xs, tracking = _respace(seg, [c["c"] for c in seg], 10.0)
    assert text == "Full Title" and tracking == 0.0
    assert len(xs) == len(text)


def test_respace_letter_spaced_drops_guessed_spaces() -> None:
    from app.pptx_export import _respace

    # "01 TITLE" letter-spaced by 1pt; MuPDF put a space between every letter.
    spec, x = [], 0.0
    for word_i, word in enumerate(["01", "TITLE"]):
        if word_i:
            x += 2.5  # word space on top of the tracking
        for k, ch in enumerate(word):
            if spec:
                spec.append((" ", x, x + 1))
                x += 1
            spec.append((ch, x, x + 3))
            x += 3
    seg = _chars(spec)
    text, xs, tracking = _respace(seg, [c["c"] for c in seg], 5.0)
    assert text == "01 TITLE"
    assert tracking == 1.0
    assert len(xs) == len(text)
