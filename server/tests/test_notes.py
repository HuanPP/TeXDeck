"""Learn: study notes built from note-templates/ (kind "notes")."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import deck
from app.deck import DeckError, DeckRequest, build_generation_messages, extract_tex, get_template
from app.edit import (
    DECK_UNIT,
    NOTES_UNIT,
    apply_deck_reply,
    build_deck_edit_messages,
    build_frame_edit_messages,
    extract_frame,
    parse_frames,
    resolve_frame_pages,
    unit_of,
)

NOTES = r"""\documentclass{article}
\usepackage[zh]{texdecknotes}
\title{测试}
\begin{document}
\begin{notesheet}{第一部分}
\notehead{甲}
\begin{itemize}
    \item 一
    \item 二
\end{itemize}
\end{notesheet}

\begin{notesheet}[left]{第二部分}
\begin{itemize}
    \item 三
\end{itemize}
\end{notesheet}
\end{document}
"""


def _text(messages: list[dict]) -> str:
    out = []
    for m in messages:
        c = m["content"]
        out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c))
    return "\n".join(out)


def test_notes_template_is_listed_by_kind() -> None:
    notes = deck.list_templates("notes")
    assert [t.key for t in notes] == ["boxed"]
    assert all(t.kind == "deck" for t in deck.list_templates("deck"))
    assert "boxed" not in {t.key for t in deck.list_templates("deck")}
    assert get_template("boxed").kind == "notes"


def test_notes_generation_prompt_has_notes_rules_only() -> None:
    template = get_template("boxed")
    request = DeckRequest(
        template_key="boxed", title="", author="A", institute="B",
        instruction="短一点", source_text="材料", language="zh",
    )
    messages = build_generation_messages(template, request, [], None, "habit: 3 bullets a slide")
    text = _text(messages)
    assert "notes template" in messages[0]["content"]
    assert messages[1]["content"] == template.main_tex
    assert "Simplified Chinese" in text and "Requirements: 短一点" in text
    assert "Speaker notes" not in text and "Author" not in text and "habit" not in text
    assert "no image files" in text


def test_notes_prompt_places_user_images_in_sheets() -> None:
    template = get_template("boxed")
    request = DeckRequest(template_key="boxed", title="T", author="", institute="", instruction="",
                          source_text="材料")
    blocks = [{"type": "text", "text": "Image figures/fig-01.png"}]
    text = _text(build_generation_messages(template, request, [], blocks))
    assert deck.NOTES_PLACEMENT_RULE in text and deck.PLACEMENT_RULE not in text


def test_extract_tex_accepts_either_notes_package_option() -> None:
    template = get_template("boxed")
    assert extract_tex(NOTES, template).strip() == NOTES.strip()
    english = NOTES.replace(r"\usepackage[zh]{texdecknotes}", r"\usepackage{texdecknotes}")
    assert extract_tex(english, template).strip() == english.strip()
    with pytest.raises(DeckError, match="texdecknotes"):
        extract_tex(NOTES.replace(r"\usepackage[zh]{texdecknotes}", ""), template)


def test_notesheets_are_the_editable_units() -> None:
    assert unit_of(NOTES) is NOTES_UNIT
    sheets = parse_frames(NOTES)
    assert [(s.index, s.title) for s in sheets] == [(1, "第一部分"), (2, "第二部分")]
    assert sheets[0].tex.startswith(r"\begin{notesheet}") and sheets[0].tex.endswith(r"\end{notesheet}")
    deck_tex = get_template("navy").main_tex
    assert unit_of(deck_tex) is DECK_UNIT and parse_frames(deck_tex)


def test_notes_edit_prompts_speak_of_sheets() -> None:
    template = get_template("boxed")
    sheet = parse_frames(NOTES)[0]
    system = build_frame_edit_messages(sheet, "加一条", template.main_tex)[0]["content"]
    assert r"\begin{notesheet}...\end{notesheet}" in system and "sheet" in system
    assert r"\note{" not in system
    deck_system = build_frame_edit_messages(
        parse_frames(get_template("navy").main_tex)[0], "x", get_template("navy").main_tex
    )[0]["content"]
    assert r"\begin{frame}...\end{frame}" in deck_system and r"\note{" in deck_system
    assert deck.WRITING_RULE in deck_system and deck.WRITING_RULE not in system
    whole = _text(build_deck_edit_messages(NOTES, template.main_tex, "全部精简"))
    assert "Revision request for the notes" in whole and "notesheet" in whole


def test_notes_reply_blocks_apply_to_sheets() -> None:
    reply = "%% FRAME 1\n\\begin{notesheet}{改}\n\\begin{itemize}\n  \\item 新\n\\end{itemize}\n\\end{notesheet}\n%% DELETE 2\n"
    out = apply_deck_reply(NOTES, reply, [1, 2])
    sheets = parse_frames(out)
    assert [s.title for s in sheets] == ["改"]
    assert extract_frame("x\n\\begin{notesheet}{a}\nb\n\\end{notesheet}\n") == "\\begin{notesheet}{a}\nb\n\\end{notesheet}"


@pytest.mark.slow
def test_notes_template_compiles_and_sheets_map_to_page_ranges(tmp_path: Path) -> None:
    template = get_template("boxed")
    deck.copy_template_bundle(template, tmp_path)
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(template.main_tex, encoding="utf-8")
    try:
        deck.compile_tex(tex_path)
    except deck.CompileError as exc:  # no TeX engine on this machine
        pytest.skip(str(exc))
    sheets = resolve_frame_pages(parse_frames(template.main_tex), tex_path)
    assert sheets and all(s.page for s in sheets)
    assert all(1 <= s.first_page <= s.page for s in sheets)
    assert sheets[0].first_page == 1


def test_src_marks_parse_with_their_headings() -> None:
    from app.notes_map import parse_anchors

    tex = "\n".join([
        r"\begin{notesheet}{结构 \key{残差}}\src{3}",
        r"\notehead{训练}\src{2:4-6}",
        r"\notesub{细节}\src{ 7 }  % \src{99} in a comment",
        r"% \notehead{注释}\src{8}",
        r"\item 一句话 \src{9}",
    ])
    got = [(a.line, a.doc, a.page, a.to, a.level, a.title) for a in parse_anchors(tex)]
    assert got == [
        (1, 1, 3, 3, 0, "结构 残差"),
        (2, 2, 4, 6, 1, "训练"),
        (3, 1, 7, 7, 2, "细节"),
        (5, 1, 9, 9, 3, ""),
    ]


def test_notes_prompt_asks_for_source_pages_only_with_page_images() -> None:
    template = get_template("boxed")
    text_request = DeckRequest(template_key="boxed", title="", source_text="正文", language="zh")
    messages = build_generation_messages(template, text_request, [], None)
    assert deck.NOTES_NO_PAGES_RULE in messages[2]["content"]
    assert deck.NOTES_SOURCE_PAGES_RULE not in messages[2]["content"]


@pytest.mark.slow
def test_src_marks_land_on_their_headings(tmp_path: Path) -> None:
    from app.notes_map import parse_anchors, place_anchors

    template = get_template("boxed")
    deck.copy_template_bundle(template, tmp_path)
    tex_path = tmp_path / "main.tex"
    tex_path.write_text(template.main_tex, encoding="utf-8")
    try:
        deck.compile_tex(tex_path)
    except deck.CompileError as exc:
        pytest.skip(str(exc))
    import fitz

    anchors = place_anchors(parse_anchors(template.main_tex), tex_path, tex_path.with_suffix(".pdf"))
    assert anchors and all(a.notes_page for a in anchors)
    with fitz.open(tex_path.with_suffix(".pdf")) as pdf:
        for a in anchors:
            page = pdf.load_page(a.notes_page - 1)
            hits = page.search_for(a.title[:12])
            # The anchor sits on its heading's own line, not merely near it.
            assert any(abs(r.y0 - a.y * page.rect.height) < 2 for r in hits), a.title
