"""Requests for the title page edit the preamble's title commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import store
from app.edit import (
    TITLE_INDEX,
    EditError,
    build_frames_edit_messages,
    extract_frames,
    parse_frames,
    replace_frames,
    title_block,
)
from app.main import _run_batch
from tests.conftest import FakeLlm

_TEX = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\title[Short]{Old title}
% the subtitle stays optional
\subtitle{}
\author{Ann \and
  Bob}
\institute{}
\date{\today}
\begin{document}
\maketitle
\begin{frame}{A}
one
\end{frame}
\end{document}
"""


def test_title_block_finds_the_commands_across_lines_and_comments() -> None:
    block = title_block(_TEX)
    assert block is not None
    assert block.spans == [(3, 3), (5, 5), (6, 7), (8, 8), (9, 9)]
    assert "% the subtitle" not in block.text and "Bob}" in block.text
    assert title_block("\\documentclass{beamer}\n\\begin{document}\n\\titlepage\n\\end{document}") is None


def test_title_travels_with_frames_and_replaces_the_preamble() -> None:
    frames = parse_frames(_TEX)
    block = title_block(_TEX)
    messages = build_frames_edit_messages([(frames[0], "x")], "tpl", "", (block.text, "加上副标题"))
    assert messages[2]["content"].startswith("%% TITLE\n\\title[Short]{Old title}")
    assert "%% TITLE" in messages[0]["content"]
    reply = (
        "%% TITLE\n\\title[Short]{New title}\n\\subtitle{A subtitle}\n\\author{Ann \\and Bob}\n"
        "\\institute{}\n\\date{\\today}\n%% FRAME 1\n\\begin{frame}{A}\nONE\n\\end{frame}\n"
    )
    new = extract_frames(reply, [TITLE_INDEX, 1])
    out = replace_frames(_TEX, frames, new)
    assert "\\title[Short]{New title}\n\\subtitle{A subtitle}" in out
    assert "Old title" not in out and "ONE" in out and "% the subtitle stays optional" in out
    assert out.index("\\date") < out.index("\\begin{document}")


def test_title_change_may_not_smuggle_other_code() -> None:
    frames = parse_frames(_TEX)
    with pytest.raises(EditError, match="only contain"):
        replace_frames(_TEX, frames, {TITLE_INDEX: "\\title{X}\n\\usepackage{evil}"})


def test_batch_with_a_title_request(
    artifact_root: Path, fake_xelatex_ok: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = store.new_generation(title="T", template_key="navy", template_name="Navy",
                                instruction="", source_kind="text", source_names=[])
    (store.generation_dir(meta.id) / "main.tex").write_text(_TEX, encoding="utf-8")
    fake = FakeLlm(["%% TITLE\n\\title[Short]{更好的标题}\n\\subtitle{}\n\\author{Ann}\n\\institute{}\n\\date{2026}\n"])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_batch(meta.id, [], [(TITLE_INDEX, "标题改得更好，日期写 2026")], lambda *_: None)
    out = (store.generation_dir(meta.id) / "main.tex").read_text(encoding="utf-8")
    assert "更好的标题" in out and "\\date{2026}" in out and "Old title" not in out
    assert "%% FRAME" not in fake.calls[0][2]["content"]
