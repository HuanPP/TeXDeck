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
    short = "%% FRAME 1\n\\begin{frame}{x}\nonly one\n\\end{frame}"
    fake = FakeLlm([short, short])  # the retry is incomplete too
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    with pytest.raises(EditError):
        _run_batch(meta.id, [], [(1, "a"), (2, "b")], lambda *_: None)
    assert (store.generation_dir(meta.id) / "main.tex").read_text(encoding="utf-8") == _TEX
    assert "rolled back" in store.load_meta(meta.id).error
    assert len(fake.calls) == 2


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


# ---- whole-deck requests: change blocks ------------------------------------------------


def test_deck_reply_replaces_deletes_and_inserts_by_original_numbers() -> None:
    from app.edit import TITLE_INDEX, apply_deck_reply, numbered_deck, parse_frames

    assert numbered_deck(_TEX).count("%% FRAME ") == 2
    reply = (
        "%% INSERT AFTER 0\n\\section{开头}\n\n\\begin{frame}{新开头}\nhi\n\\end{frame}\n"
        "%% DELETE 1\n"
        "%% FRAME 2\n\\begin{frame}{结论}\n\\item 新结论。\n\\end{frame}\n"
        "%% INSERT AFTER 2\n\\begin{frame}{致谢}\nthanks\n\\end{frame}\n"
        "%% TITLE\n\\title{新标题}\n"
    )
    out = apply_deck_reply(_TEX, reply, [1, 2, TITLE_INDEX])
    assert [f.title for f in parse_frames(out)] == ["新开头", "结论", "致谢"]
    assert "\\section{开头}" in out and "训练流程" not in out and "新结论" in out
    assert "\\title{新标题}" in out and "%% FRAME" not in out
    # A full document is taken as it is, minus the numbering lines.
    full = numbered_deck(_TEX).replace("结论一", "结论二")
    assert apply_deck_reply(_TEX, full) == _TEX.replace("结论一", "结论二").strip()


def test_deck_reply_must_answer_every_slide_request() -> None:
    from app.edit import apply_deck_reply

    with pytest.raises(EditError, match="slide\\(s\\) 1"):
        apply_deck_reply(_TEX, "%% FRAME 2\n\\begin{frame}{x}\ny\n\\end{frame}", [1])
    with pytest.raises(EditError, match="has 2"):
        apply_deck_reply(_TEX, "%% DELETE 5", [])
    with pytest.raises(EditError, match="neither"):
        apply_deck_reply(_TEX, "Sure, here you go.", [])


def test_deck_request_goes_out_with_the_slide_requests_in_one_call(
    artifact_root: Path, fake_xelatex_ok: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.edit import DECK_INDEX

    meta = _finished()
    gdir = store.generation_dir(meta.id)
    png = _png()
    meta.figures = figures_add(gdir, [], [("chart.png", png, "result chart")])
    store.save_meta(meta)
    fake = FakeLlm(["%% FRAME 2\n\\begin{frame}{结论}\n\\includegraphics[width=0.5\\linewidth,"
                    "height=0.5\\textheight,keepaspectratio]{figures/fig-01.png}\n\\end{frame}"])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)
    _run_batch(meta.id, [], [(2, "放上这张图")], lambda *_: None, "全篇改成英文标点",
               {2: ["figures/fig-01.png"]})
    assert len(fake.calls) == 1
    sent = fake.calls[0]
    request = sent[3]["content"]
    assert isinstance(request, list)  # text plus the attached picture
    texts = "\n".join(b.get("text", "") for b in request)
    assert "全篇改成英文标点" in texts and "Change for frame 2:\n放上这张图" in texts
    assert "figures/fig-01.png" in texts and any(b.get("type") != "text" for b in request)
    assert "%% FRAME 1" in sent[2]["content"]
    out = (gdir / "main.tex").read_text(encoding="utf-8")
    assert "fig-01.png" in out and "训练流程" in out
    record = json.loads((gdir / "edits" / "001" / "batch.json").read_text(encoding="utf-8"))
    assert record["deck"] == "全篇改成英文标点" and record["figures"] == {"2": ["figures/fig-01.png"]}
    assert DECK_INDEX == -1


def test_images_for_edits_are_added_after_the_existing_ones(client: TestClient) -> None:
    meta = _finished()
    url = f"/api/generations/{meta.id}/figures"
    r = client.post(url, files=[("images", ("a.png", _png(), "image/png"))], data={"notes": ["左边"]})
    assert r.status_code == 200, r.text
    assert r.json()["figures"][0]["file"] == "figures/fig-01.png"
    r = client.post(url, files=[("images", ("b.png", _png(), "image/png"))])
    assert r.json()["figures"][0]["file"] == "figures/fig-02.png"
    assert [f["note"] for f in store.load_meta(meta.id).figures] == ["左边", ""]
    assert client.get(f"{url}/fig-02.png").status_code == 200
    assert client.get(f"{url}/fig-09.png").status_code == 404
    bad = {"pages": [{"index": 1, "instruction": "x", "figures": ["figures/nope.png"]}]}
    assert client.post(f"/api/generations/{meta.id}/batch", json=bad).status_code == 400


def _png() -> bytes:
    import fitz

    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 20), 0)
    pix.clear_with(200)
    return pix.tobytes("png")


def figures_add(gdir: Path, existing: list, uploads: list) -> list:
    from app import figures

    return figures.add(gdir, existing, uploads)
