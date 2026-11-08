"""Real tests for frame parse / extract / replace public API."""

from __future__ import annotations

import pytest

from app.edit import EditError, extract_frame, parse_frames, replace_frame


_TEX_THREE_FRAME_STYLES = r"""
\documentclass{beamer}
\begin{document}
\begin{frame}[fragile]{Options And Title}
First body.
\end{frame}
\begin{frame}{Title Only}
Second body.
\end{frame}
\begin{frame}
Bare frame body.
\end{frame}
\end{document}
""".lstrip()


def test_parse_frames_three_begin_forms() -> None:
    frames = parse_frames(_TEX_THREE_FRAME_STYLES)
    assert len(frames) == 3

    assert frames[0].index == 1
    assert frames[0].title == "Options And Title"
    assert r"\begin{frame}[fragile]{Options And Title}" in frames[0].tex
    assert "First body." in frames[0].tex

    assert frames[1].index == 2
    assert frames[1].title == "Title Only"
    assert "Second body." in frames[1].tex

    assert frames[2].index == 3
    assert frames[2].title == ""
    assert "Bare frame body." in frames[2].tex


def test_parse_frames_ignores_commented_begin_frame() -> None:
    tex = r"""
\begin{document}
% \begin{frame}{Should Be Ignored}
% fake
% \end{frame}
\begin{frame}{Real}
ok
\end{frame}
\end{document}
""".lstrip()
    frames = parse_frames(tex)
    assert len(frames) == 1
    assert frames[0].title == "Real"


def test_parse_frames_unclosed_raises() -> None:
    tex = r"""
\begin{document}
\begin{frame}{Open}
no end
\end{document}
""".lstrip()
    with pytest.raises(EditError, match="Unclosed frame"):
        parse_frames(tex)


def test_extract_frame_normal() -> None:
    response = r"\begin{frame}{A}hi\end{frame}"
    assert extract_frame(response) == response


def test_extract_frame_with_markdown_fences() -> None:
    inner = r"\begin{frame}{B}content\end{frame}"
    fenced = f"```latex\n{inner}\n```"
    assert extract_frame(fenced) == inner


def test_extract_frame_rejects_two_frames() -> None:
    response = (
        r"\begin{frame}{One}a\end{frame}" "\n"
        r"\begin{frame}{Two}b\end{frame}"
    )
    with pytest.raises(EditError, match="complete slide frame"):
        extract_frame(response)


def test_extract_frame_missing_end_frame() -> None:
    with pytest.raises(EditError, match="complete slide frame"):
        extract_frame(r"\begin{frame}{Broken}no end")


def test_replace_frame_preserves_other_lines() -> None:
    tex = (
        "preamble line\n"
        "\\begin{frame}{Old}\n"
        "old body\n"
        "\\end{frame}\n"
        "after line\n"
        "final line\n"
    )
    frames = parse_frames(tex)
    assert len(frames) == 1
    frame = frames[0]
    new_frame = "\\begin{frame}{New}\nnew body\n\\end{frame}"
    result = replace_frame(tex, frame, new_frame)

    result_lines = result.splitlines()
    original_lines = tex.splitlines()
    # Lines outside the replaced range must be byte-for-byte identical
    for i, line in enumerate(original_lines):
        line_no = i + 1
        if frame.start_line <= line_no <= frame.end_line:
            continue
        # After replacement, lines before start stay at same index;
        # lines after end shift by (new_len - old_len).
        if line_no < frame.start_line:
            assert result_lines[i] == line
        else:
            shift = len(new_frame.splitlines()) - (frame.end_line - frame.start_line + 1)
            assert result_lines[i + shift] == line

    assert "\\begin{frame}{New}" in result
    assert "new body" in result
    assert "old body" not in result
    assert result_lines[0] == "preamble line"
    assert result_lines[-1] == "final line"
    # Trailing newline preserved
    assert result.endswith("\n")


def test_synctex_reader_maps_lines_to_first_page(tmp_path: Path) -> None:
    import gzip

    from app.edit import _forward_page, read_synctex_lines

    tex = tmp_path / "main.tex"
    tex.write_text("x", encoding="utf-8")
    body = "\n".join([
        "SyncTeX Version:1",
        f"Input:1:{tex}",
        "Input:2:/texmf/beamer.cls",
        "Content:",
        "{1", "[1,10:0,0:1,1,0", "h2,10:0,0:1,1,0", "]", "}1",
        "{2", "(1,20:0,0:1,1,0", "x1,20:0,0", ")", "}2",
        "{3", "(1,20:0,0:1,1,0", "k1,30:0,0:5", "}3",
        "Postamble:",
    ])
    with gzip.open(tex.with_suffix(".synctex.gz"), "wt", encoding="utf-8") as fh:
        fh.write(body)

    lines = read_synctex_lines(tex)
    assert lines == {10: 1, 20: 2, 30: 3}
    assert _forward_page(lines, 20) == 2  # overlays: first page wins
    assert _forward_page(lines, 25) == 3  # nearest later line first
    assert _forward_page(lines, 500) == 0


def test_synctex_reader_requires_file(tmp_path: Path) -> None:
    from app.edit import EditError, read_synctex_lines

    with pytest.raises(EditError, match="synctex"):
        read_synctex_lines(tmp_path / "main.tex")


def test_frame_notes_plain_text() -> None:
    from app.edit import frame_notes

    tex = (
        "\\begin{frame}{Result}\n"
        "\\begin{itemize}\\item a\\end{itemize}\n"
        "% a comment line inside the frame\n"
        "\\note{这一页讲 \\textbf{核心结论}：准确率提升 3\\%，成本 \\$5。\n"
        "其中 $\\alpha$ 取 0.2~左右。\\\\ 第二行 -- 结束}\n"
        "\\end{frame}"
    )
    assert frame_notes(tex) == "这一页讲 核心结论：准确率提升 3%，成本 $5。\n其中 α 取 0.2 左右。\n第二行 – 结束"


def test_frame_notes_absent_or_nested_braces() -> None:
    from app.edit import frame_notes

    assert frame_notes("\\begin{frame}{X}\nbody\n\\end{frame}") == ""
    tex = "\\begin{frame}\n\\note{Say {this} and \\emph{that}.}\n\\note[item]{Second.}\n\\end{frame}"
    assert frame_notes(tex) == "Say this and that.\n\nSecond."
