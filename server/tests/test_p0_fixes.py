"""Regression tests for the P0 robustness batch (2026-07-26 audit)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app import store, tasks
from app.deck import DeckError, extract_tex
from app.edit import parse_frames


# ---- parse_frames: compact single-line frames -------------------------------

_SINGLE_LINE_DOC = r"""\documentclass{beamer}
\begin{document}
\begin{frame}{Compact} one-liner body \end{frame}
\begin{frame}{Normal}
Body.
\end{frame}
\end{document}
"""


def test_parse_frames_accepts_single_line_frame() -> None:
    frames = parse_frames(_SINGLE_LINE_DOC)
    assert [f.title for f in frames] == ["Compact", "Normal"]
    compact = frames[0]
    assert compact.start_line == compact.end_line == 3
    assert "one-liner body" in compact.tex


def test_parse_frames_single_line_frame_then_unclosed_still_detected() -> None:
    bad = _SINGLE_LINE_DOC.replace("\\end{frame}\n\\end{document}", "\\end{document}")
    with pytest.raises(Exception, match="Unclosed frame"):
        parse_frames(bad)


# ---- extract_tex: local-file / shell escape guard ----------------------------

_OK_DOC = (
    "\\documentclass{beamer}\n"
    "\\usepackage[zh,bjtu]{collegebeamer}\n"
    "\\begin{document}\n"
    "\\begin{frame}{Hello}\nBody text.\n\\end{frame}\n"
    "\\end{document}\n"
)


@pytest.mark.parametrize(
    "payload",
    [r"\input{/etc/passwd}", r"\include{secrets}", r"\write18{rm -rf /}", r"\openin1=key"],
)
def test_extract_tex_rejects_local_file_escapes(payload: str) -> None:
    bad = _OK_DOC.replace("Body text.", payload)
    with pytest.raises(DeckError, match="not allowed"):
        extract_tex(bad, None)


def test_extract_tex_escape_in_comment_is_fine() -> None:
    ok = _OK_DOC.replace("Body text.", "Body text. % \\input{note}")
    assert extract_tex(ok, None)


# ---- tasks: per-generation mutual exclusion ----------------------------------

def test_start_rejects_second_job_for_same_generation() -> None:
    release = threading.Event()
    started = threading.Event()

    def job(_progress) -> None:
        started.set()
        assert release.wait(timeout=10)

    gen_id = "aaaaaaaa-9999"
    try:
        tasks.start(gen_id, job)
        assert started.wait(timeout=10)
        assert tasks.is_running(gen_id)
        with pytest.raises(tasks.TaskBusy):
            tasks.start(gen_id, job)
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while tasks.is_running(gen_id):
        assert time.monotonic() < deadline, "job never released the running flag"
        time.sleep(0.01)


def test_running_flag_cleared_after_failure() -> None:
    def job(_progress) -> None:
        raise RuntimeError("boom")

    gen_id = "aaaaaaaa-9998"
    tasks.start(gen_id, job)
    deadline = time.monotonic() + 10
    while tasks.is_running(gen_id):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    state = tasks.get(gen_id)
    assert state is not None and state.status == "failed"


# ---- store: restart sweep and id-collision retry ------------------------------

def _new(title: str = "T") -> store.GenerationMeta:
    return store.new_generation(
        title=title,
        template_key="navy",
        template_name="Navy",
        instruction="",
        source_kind="text",
        source_names=[],
    )


def test_mark_interrupted_generations(artifact_root: Path) -> None:
    running = _new("interrupted")
    done = _new("finished")
    done.status = "succeeded"
    store.save_meta(done)

    assert store.mark_interrupted_generations() == 1
    swept = store.load_meta(running.id)
    assert swept.status == "failed"
    assert "restart" in swept.error.lower()
    assert store.load_meta(done.id).status == "succeeded"
    # Second sweep is a no-op
    assert store.mark_interrupted_generations() == 0


def test_new_generation_retries_on_id_collision(
    artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(["aaaaaaaa-0001", "aaaaaaaa-0001", "aaaaaaaa-0002"])
    monkeypatch.setattr(store, "_gen_id", lambda: next(ids))
    first = _new("one")
    second = _new("two")
    assert first.id == "aaaaaaaa-0001"
    assert second.id == "aaaaaaaa-0002"


def test_new_generation_gives_up_after_exhausted_retries(
    artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "_gen_id", lambda: "aaaaaaaa-0003")
    _new("claims-the-id")
    with pytest.raises(RuntimeError, match="unique generation ID"):
        _new("collides-forever")
