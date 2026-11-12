"""Queued changes: text fixes and slide requests applied in one job with one compile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, tasks
from app.edit import EditError, TextChange, apply_text_changes
from app.main import _run_batch, app
from tests.conftest import FakeLlm

_TEX = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\title{旧标题}
\begin{document}
\begin{frame}{训练流程}
\item 先模仿专家，再用 \textbf{PPO} 优化。
\item 学习率 0.1。
\end{frame}
\begin{frame}{结论}
\item 结论一。
\end{frame}
\end{document}
"""
_L6 = r"\item 先模仿专家，再用 \textbf{PPO} 优化。"


def _span(line: str, text: str) -> tuple[int, int]:
    start = line.index(text)
    return start, start + len(text)


def test_two_fixes_in_one_line_and_another_line() -> None:
    a, b = _span(_L6, "先模仿专家"), _span(_L6, "优化")
    out = apply_text_changes(_TEX, [
        TextChange(6, _L6, *a, "先学专家"),
        TextChange(6, _L6, *b, "微调"),
        TextChange(3, r"\title{旧标题}", 7, 10, "新标题"),
    ])
    lines = out.splitlines()
    assert lines[5] == r"\item 先学专家，再用 \textbf{PPO} 微调。"
    assert lines[2] == r"\title{新标题}"


def test_raw_fix_and_escaping() -> None:
    line = r"\item 学习率 0.1。"
    out = apply_text_changes(_TEX, [TextChange(7, line, *_span(line, "0.1"), "10% & more")])
    assert out.splitlines()[6] == r"\item 学习率 10\% \& more。"
    out = apply_text_changes(_TEX, [TextChange(7, line, *_span(line, "0.1"), r"$10^{-2}$", raw=True)])
    assert out.splitlines()[6] == r"\item 学习率 $10^{-2}$。"


def test_conflicts_and_stale_source_are_refused() -> None:
    a = _span(_L6, "先模仿专家")
    with pytest.raises(EditError, match="overlap"):
        apply_text_changes(_TEX, [TextChange(6, _L6, a[0], a[1], "x"), TextChange(6, _L6, a[0] + 1, a[1], "y")])
    with pytest.raises(EditError, match="changed"):
        apply_text_changes(_TEX, [TextChange(6, "something else", 0, 3, "x")])
    two = _L6 + "\n" + r"\item 学习率 0.1。"
    with pytest.raises(EditError, match="same source lines"):
        apply_text_changes(_TEX, [TextChange(6, two, 0, 3, "x"), TextChange(7, r"\item 学习率 0.1。", 0, 3, "y")])


# ---- the job ---------------------------------------------------------------------------


def _finished() -> store.GenerationMeta:
    meta = store.new_generation(
        title="T", template_key="navy", template_name="Navy",
        instruction="", source_kind="text", source_names=[],
    )
    meta.status = "succeeded"
    store.save_meta(meta)
    (store.generation_dir(meta.id) / "main.tex").write_text(_TEX, encoding="utf-8")
    return meta


def test_text_only_batch_compiles_once_without_the_model(
    artifact_root: Path, fake_xelatex_ok: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.main.create_client", lambda **_: pytest.fail("no model for text fixes"))
    meta = _finished()
    a = _span(_L6, "先模仿专家")
    _run_batch(meta.id, [TextChange(6, _L6, *a, "先学专家")], [], lambda *_: None)
    out = (store.generation_dir(meta.id) / "main.tex").read_text(encoding="utf-8")
    assert "先学专家" in out
    assert fake_xelatex_ok["runs"] == 1  # one compile; the aux files did not change, so one pass
    loaded = store.load_meta(meta.id)
    assert loaded.edit_count == 1 and loaded.error == ""
    record = json.loads((store.generation_dir(meta.id) / "edits" / "001" / "batch.json").read_text())
    assert record["texts"][0]["text"] == "先学专家"


def test_slide_requests_go_out_in_one_call_after_the_text_fixes(
    artifact_root: Path, fake_xelatex_ok: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _finished()
    fake = FakeLlm([
        "%% FRAME 1\n\\begin{frame}{训练流程}\n\\item 先学专家，再微调。\n\\end{frame}\n"
        "%% FRAME 2\n\\begin{frame}{结论}\n\\item 新结论。\n\\end{frame}"
    ])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    a = _span(_L6, "先模仿专家")
    _run_batch(meta.id, [TextChange(6, _L6, *a, "先学专家")], [(1, "合并成一句"), (2, "改写结论")],
               lambda *_: None)
    assert len(fake.calls) == 1
    sent = fake.calls[0][2]["content"]
    assert "先学专家" in sent  # the slide request sees the text fix
    assert "合并成一句" in sent and "改写结论" in sent
    out = (store.generation_dir(meta.id) / "main.tex").read_text(encoding="utf-8")
    assert "新结论" in out and "先学专家，再微调" in out
    assert fake_xelatex_ok["runs"] == 1  # one compile, one pass (aux unchanged)


def test_failed_batch_leaves_the_deck_alone(
    artifact_root: Path, fake_xelatex_ok: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _finished()
    fake = FakeLlm(["%% FRAME 1\n\\begin{frame}{x}\nonly one\n\\end{frame}"])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    with pytest.raises(EditError):
        _run_batch(meta.id, [], [(1, "a"), (2, "b")], lambda *_: None)
    assert (store.generation_dir(meta.id) / "main.tex").read_text(encoding="utf-8") == _TEX
    assert "rolled back" in store.load_meta(meta.id).error


# ---- the endpoint ------------------------------------------------------------------------


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    with TestClient(app) as c:
        yield c


def test_endpoint_checks_before_starting(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(tasks, "start", lambda gen_id, _job: started.append(gen_id))
    meta = _finished()
    url = f"/api/generations/{meta.id}/batch"
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"pages": [{"index": 9, "instruction": "x"}]}).status_code == 400
    stale = {"line": 6, "source": "not this", "start": 0, "end": 2, "text": "x"}
    assert client.post(url, json={"texts": [stale]}).status_code == 409
    a = _span(_L6, "先模仿专家")
    ok = {"line": 6, "source": _L6, "start": a[0], "end": a[1], "text": "先学专家"}
    assert client.post(url, json={"texts": [ok]}).status_code == 202
    assert started == [meta.id]
