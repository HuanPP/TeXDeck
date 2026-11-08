"""Direct text edits: click text on the preview, fix its source line, recompile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, tasks
from app.edit import EditError, locate_line, parse_frames, replace_line, replace_text_span, text_span
from app.main import _run_line_edit, app

_TEX = r"""\documentclass[aspectratio=169,10pt]{beamer}
\title{旧的论文标题}
\begin{document}
\begin{frame}{训练流程}
\begin{itemize}
\item 先模仿启发式专家，再用 \textbf{PPO} 优化。
\item 学习率 $3\times10^{-4}$，裁剪参数 0.2。
\end{itemize}
\end{frame}
\begin{frame}{结论}
\item 先模仿启发式专家的另一处说法。
\end{frame}
\end{document}
"""


def test_locate_prefers_the_frame_on_the_page() -> None:
    frames = parse_frames(_TEX)
    # Same phrase in both frames: the clicked page's frame wins.
    assert locate_line(_TEX, "先模仿启发式专家", frames[1]) == (11, r"\item 先模仿启发式专家的另一处说法。")
    assert locate_line(_TEX, "先模仿启发式专家，再用 PPO 优化。", frames[0])[0] == 6


def test_locate_matches_part_of_a_line_with_markup_and_math() -> None:
    frames = parse_frames(_TEX)
    # The PDF shows "3 × 10−4" for the math; the Chinese part still matches.
    assert locate_line(_TEX, "学习率 3 × 10−4，裁剪参数 0.2。", frames[0])[0] == 7
    # A wrapped PDF line is only the end of the source line.
    assert locate_line(_TEX, "再用 PPO 优化。", frames[0])[0] == 6


def test_locate_falls_back_to_the_preamble_for_the_title_page() -> None:
    assert locate_line(_TEX, "旧的论文标题", None) == (2, r"\title{旧的论文标题}")
    assert locate_line(_TEX, "完全不存在的文字", None) is None


def test_replace_line_refuses_a_stale_line() -> None:
    assert replace_line(_TEX, 2, r"\title{旧的论文标题}", r"\title{新标题}").splitlines()[1] == r"\title{新标题}"
    with pytest.raises(EditError):
        replace_line(_TEX, 2, r"\title{别的}", r"\title{新标题}")


def _finished(artifact_root: Path) -> store.GenerationMeta:
    meta = store.new_generation(
        title="T", template_key="navy", template_name="Navy",
        instruction="", source_kind="text", source_names=[],
    )
    meta.status = "succeeded"
    store.save_meta(meta)
    (store.generation_dir(meta.id) / "main.tex").write_text(_TEX, encoding="utf-8")
    return meta


def test_run_line_edit_recompiles_and_records(artifact_root: Path, fake_xelatex_ok: dict[str, int]) -> None:
    meta = _finished(artifact_root)
    gdir = store.generation_dir(meta.id)
    _run_line_edit(meta.id, 2, r"\title{旧的论文标题}", r"\title{新标题}", lambda _s, _p: None)
    assert (gdir / "main.tex").read_text(encoding="utf-8").splitlines()[1] == r"\title{新标题}"
    assert fake_xelatex_ok["runs"] >= 1
    record = json.loads(next(gdir.glob("edits/*/edit.json")).read_text(encoding="utf-8"))
    assert record["kind"] == "direct" and record["new"] == r"\title{新标题}"
    loaded = store.load_meta(meta.id)
    assert loaded.edit_count == 1 and loaded.error == ""


def test_run_line_edit_rolls_back_when_it_does_not_compile(
    artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _finished(artifact_root)
    gdir = store.generation_dir(meta.id)

    def fake_run(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
        if "{未闭合" in tex_path.read_text(encoding="utf-8"):
            return False, "! Missing } inserted.\n"
        (work_dir / f"{tex_path.stem}.pdf").write_bytes(b"%PDF-fake")
        return True, "ok"

    monkeypatch.setattr("app.deck._run_xelatex", fake_run)
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")
    with pytest.raises(RuntimeError):
        _run_line_edit(meta.id, 2, r"\title{旧的论文标题}", r"\title{未闭合", lambda _s, _p: None)
    assert (gdir / "main.tex").read_text(encoding="utf-8") == _TEX
    loaded = store.load_meta(meta.id)
    assert loaded.status == "succeeded" and loaded.edit_count == 0
    assert loaded.error.startswith("This edit failed and was rolled back:")


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    with TestClient(app) as c:
        yield c


def test_locate_and_edit_endpoints(
    client: TestClient, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _finished(artifact_root)
    started: list[str] = []
    monkeypatch.setattr(tasks, "start", lambda gen_id, _job: started.append(gen_id))

    r = client.post(f"/api/generations/{meta.id}/locate", json={"page": 1, "text": "旧的论文标题"})
    assert r.status_code == 200, r.text
    assert r.json() == {"line": 2, "source": r"\title{旧的论文标题}", "start": 7, "end": 13,
                        "text": "旧的论文标题", "raw": False}
    r = client.post(f"/api/generations/{meta.id}/locate", json={"page": 1, "text": "没有这段字"})
    assert r.status_code == 404

    url = f"/api/generations/{meta.id}/source-line"
    assert client.post(url, json={"line": 2, "old": r"\title{旧的论文标题}", "new": r"\title{新}"}).status_code == 202
    assert started == [meta.id]
    assert client.post(url, json={"line": 2, "old": r"\title{别的}", "new": r"\title{新}"}).status_code == 409
    assert client.post(url, json={"line": 2, "old": r"\title{旧的论文标题}", "new": r"\title{旧的论文标题}"}).status_code == 400
    assert client.post(url, json={"line": 2, "old": r"\title{旧的论文标题}", "start": 7, "end": 13,
                                  "text": "新 50%"}).status_code == 202
    assert started == [meta.id, meta.id]
    assert client.post(url, json={"line": 2, "old": r"\title{旧的论文标题}", "start": 7, "end": 13,
                                  "text": "旧的论文标题"}).status_code == 400


def _plain(line: str, clicked: str) -> tuple[str, bool]:
    span = text_span(line, clicked)
    assert line[span.start:span.end] == line[span.start:span.end]  # offsets are in range
    return span.text, span.raw


def test_text_span_hides_the_markup_around_the_text() -> None:
    assert _plain(r"\item 跨型号验证：检验配置策略的迁移能力。", "跨型号验证：检验配置策略的迁移能力。") == (
        "跨型号验证：检验配置策略的迁移能力。", False)
    # The slide shows the long title, in small caps: only that argument is edited.
    line = r"\title[PSRONet：转辙机传感配置优化]{PSRONet：基于深度强化学习的铁路转辙机故障诊断传感配置框架}"
    span = text_span(line, "PSRONET：基于深度强化学习")
    assert (span.text, span.raw) == ("PSRONet：基于深度强化学习的铁路转辙机故障诊断传感配置框架", False)
    assert line[span.end:] == "}"
    # Emphasis inside the sentence is invisible; escaped characters show as themselves.
    assert _plain(r"  \item 先用 \textbf{PPO} 优化 50\% 的参数。 % note", "先用 PPO 优化") == (
        "先用 PPO 优化 50% 的参数。", False)


def test_text_span_falls_back_to_latex_for_math() -> None:
    line = r"\item 学习率 $3\times10^{-4}$，裁剪参数 $0.2$。"
    # Only this sentence's source is shown, without \item.
    assert _plain(line, "学习率 3 × 10−4，裁剪参数") == (line[6:], True)
    assert _plain(r"\item \textbf{优化对象：}通道子集 $r$。", "优化对象：通道子集") == (r"\textbf{优化对象：}通道子集 $r$。", True)
    # A table cell is edited on its own.
    assert _plain(r"训练／验证／测试 & $72\%$\\", "训练／验证／测试") == ("训练／验证／测试", False)
    assert _plain(r"\vspace{1em}", "完全不同") == (r"\vspace{1em}", True)


def test_replace_text_span_keeps_markup_and_escapes() -> None:
    line = r"\item 先用 \textbf{PPO} 优化 50\% 的参数。"
    span = text_span(line, "先用 PPO 优化")
    assert replace_text_span(line, span.start, span.end, "先用 PPO 微调 60% 的参数。") == (
        r"\item 先用 \textbf{PPO} 微调 60\% 的参数。")
    assert replace_text_span(line, span.start, span.end, "先用 PPO2 优化 50% 的参数 & 权重。") == (
        r"\item 先用 \textbf{PPO2} 优化 50\% 的参数 \& 权重。")
    # Deleting a whole emphasised word leaves an empty wrapper, which still compiles.
    out = replace_text_span(line, span.start, span.end, "先用 优化 50% 的参数。")
    assert r"\textbf{}" in out and text_span(out, "先用 优化").text == "先用 优化 50% 的参数。"
    title = r"\title[短]{长标题}"
    span = text_span(title, "长标题")
    assert replace_text_span(title, span.start, span.end, "新的_标题{x}") == r"\title[短]{新的\_标题\{x\}}"
