"""Layout checks after compiling: overflow, colliding text, unused images; per-slide fixes."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import deck, layout
from app.edit import EditError, extract_frames, parse_frames, replace_frames
from tests.conftest import FakeLlm

_RECORDS = [
    {"file": "figures/fig-01.png", "name": "a.png", "note": "", "width": 40, "height": 20},
    {"file": "figures/fig-02.jpg", "name": "b.jpg", "note": "", "width": 30, "height": 60},
]
_FIG1 = r"\includegraphics[width=0.8\linewidth,height=0.6\textheight,keepaspectratio]{figures/fig-01.png}"


def _deck(*frames: str) -> str:
    return (
        "\\documentclass[aspectratio=169,10pt]{beamer}\n"
        "\\usepackage[zh,bjtu]{collegebeamer}\n"
        "\\begin{document}\n" + "\n".join(frames) + "\n\\end{document}"
    )


def _frame(title: str, body: str) -> str:
    return f"\\begin{{frame}}{{{title}}}\n{body}\n\\end{{frame}}"


def test_unused_figures() -> None:
    tex = _deck(_frame("A", _FIG1))
    assert [f["file"] for f in layout.unused_figures(tex, _RECORDS)] == ["figures/fig-02.jpg"]


def test_overflow_is_reported_for_any_slide(tmp_path: Path) -> None:
    tex = _deck(_frame("Text only", "words"), _frame("Pictures", _FIG1))
    lines = tex.splitlines()
    text_end = lines.index(r"\end{frame}") + 1
    pic_end = len(lines) - 1
    log = (
        f"Overfull \\vbox (30.0pt too high) detected at line {text_end}\n"
        f"Overfull \\vbox (1.5pt too high) detected at line {pic_end}\n"
    )
    issues = layout.frame_issues(tex, log, None, tmp_path / "main.tex")
    assert list(issues) == [1] and "30pt" in issues[1][0]


# ---- several slides in one call -------------------------------------------------------


def test_extract_and_replace_several_frames() -> None:
    tex = _deck(_frame("A", "one"), _frame("B", "two"), _frame("C", "three"))
    frames = parse_frames(tex)
    reply = (
        "%% FRAME 3\n\\begin{frame}{C}\nTHREE\nmore\n\\end{frame}\n"
        "%% FRAME 1\n\\begin{frame}{A}\nONE\n\\end{frame}\n"
    )
    new = extract_frames(reply, [1, 3])
    out = replace_frames(tex, frames, new)
    assert "ONE" in out and "two" in out and "THREE\nmore" in out
    assert [f.title for f in parse_frames(out)] == ["A", "B", "C"]
    with pytest.raises(EditError, match="slide\\(s\\) 2"):
        extract_frames(reply, [1, 2])


def test_check_and_fix_sends_only_the_faulty_slides(
    tmp_path: Path, fake_xelatex_ok: dict[str, int]
) -> None:
    gdir = tmp_path
    tex = _deck(_frame("Fine", "ok"), _frame("Too long", "lots"))
    (gdir / "main.tex").write_text(tex, encoding="utf-8")
    end_line = len(tex.splitlines()) - 1
    log = f"Overfull \\vbox (50.0pt too high) detected at line {end_line}\n"
    fake = FakeLlm(["%% FRAME 2\n\\begin{frame}{Too long}\nless\n\\end{frame}"])
    template = deck.builtin_template("navy")

    layout.check_and_fix(gdir, template, [], fake, log, require_all_figures=True)

    sent = fake.calls[0][2]["content"]
    assert "%% FRAME 2" in sent and "%% FRAME 1" not in sent and "50pt" in sent
    out = (gdir / "main.tex").read_text(encoding="utf-8")
    assert "less" in out and "lots" not in out and "ok" in out
    assert len(fake.calls) == 1  # the recompiled log ("ok") has no faults left


def test_check_and_fix_keeps_the_deck_when_the_reply_is_unusable(
    tmp_path: Path, fake_xelatex_ok: dict[str, int]
) -> None:
    tex = _deck(_frame("Too long", "lots"))
    (tmp_path / "main.tex").write_text(tex, encoding="utf-8")
    log = f"Overfull \\vbox (50.0pt too high) detected at line {len(tex.splitlines()) - 1}\n"
    fake = FakeLlm(["sorry, no frames here"])
    layout.check_and_fix(tmp_path, deck.builtin_template("navy"), [], fake, log, require_all_figures=True)
    assert (tmp_path / "main.tex").read_text(encoding="utf-8") == tex


# ---- colliding text, on real PDFs ------------------------------------------------------

_COLLIDING = r"""\documentclass{beamer}
\usepackage{tikz}
\begin{document}
\begin{frame}{Diagram}
Plain text that is set normally, with Ta Va kerning.
\begin{tikzpicture}
\node at (0,0) {Alpha Beta};
\node at (0.5,0) {Gamma Delta};
\node at (0,-2) {Separate};
\node at (3,-2) {Apart};
\end{tikzpicture}
\end{frame}
\begin{frame}{Fine}
Fractions $\frac{a}{b}$ and roots $\sqrt{x^2+1}$ do not count.
\end{frame}
\end{document}
"""


@pytest.mark.slow
def test_text_collisions_on_a_real_pdf(tmp_path: Path) -> None:
    if not shutil.which("xelatex"):
        pytest.skip("xelatex not installed")
    (tmp_path / "main.tex").write_text(_COLLIDING, encoding="utf-8")
    deck.compile_tex(tmp_path / "main.tex")
    found = layout.text_collisions(tmp_path / "main.pdf")
    assert list(found) == [1]
    assert "Alpha Beta" in found[1][0] and "Gamma Delta" in found[1][0]
    issues = layout.frame_issues(_COLLIDING, "", tmp_path / "main.pdf", tmp_path / "main.tex")
    assert list(issues) == [1] and "collides" in issues[1][0]
