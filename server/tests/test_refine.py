"""Whole-deck refine: job semantics (success/retry/rollback) and endpoint contract."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, tasks
from app.config import settings
from app.edit import numbered_deck
from app.main import _run_refine, app
from tests.conftest import FakeLlm


_ORIGINAL = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\begin{document}
\begin{frame}{Hello}
Body text.
\end{frame}
\end{document}
"""

# Two frames so a successful refine visibly changes frame_count (1 -> 2).
_REVISED = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\begin{document}
\begin{frame}{Hello}
REVISED-MARKER body.
\end{frame}
\begin{frame}{Extra}
Second frame.
\end{frame}
\end{document}
"""


def _make_finished_generation(artifact_root: Path) -> store.GenerationMeta:
    meta = store.new_generation(
        title="Refine Target",
        template_key="navy",
        template_name="Navy",
        instruction="",
        source_kind="text",
        source_names=[],
    )
    meta.status, meta.error, meta.frame_count = "succeeded", "", 1
    store.save_meta(meta)
    gdir = store.generation_dir(meta.id)
    (gdir / "main.tex").write_text(_ORIGINAL, encoding="utf-8")
    (gdir / "source.txt").write_text("notes", encoding="utf-8")
    return meta


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[str],
    usage: tuple[int, int] = (100, 50),
) -> dict[str, FakeLlm]:
    """Route app.main.create_client to a FakeLlm; holder['client'] is the instance."""
    holder: dict[str, FakeLlm] = {}

    def fake_create_client(on_usage=None) -> FakeLlm:
        client = FakeLlm(responses, on_usage=on_usage, usage=usage)
        holder["client"] = client
        return client

    monkeypatch.setattr("app.main.create_client", fake_create_client)
    return holder


# ---- job -----------------------------------------------------------------------


def test_run_refine_success(
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    meta = _make_finished_generation(artifact_root)
    gdir = store.generation_dir(meta.id)
    holder = _patch_llm(monkeypatch, [_REVISED])

    _run_refine(meta.id, "Add a second slide", lambda _s, _p: None)

    # Deck replaced, edits record written, backup keeps the pre-refine tex
    assert (gdir / "main.tex").read_text(encoding="utf-8") == _REVISED.strip()
    assert (gdir / "main.pdf").is_file()
    record = json.loads((gdir / "edits" / "001" / "refine.json").read_text(encoding="utf-8"))
    assert record["instruction"] == "Add a second slide"
    assert record["time"]
    assert (gdir / "edits" / "001" / "main.tex.bak").read_text(encoding="utf-8") == _ORIGINAL

    loaded = store.load_meta(meta.id)
    assert loaded.status == "succeeded"
    assert loaded.error == ""
    assert loaded.refine_count == 1
    assert loaded.frame_count == 2
    assert loaded.llm_model == settings.llm_model
    assert loaded.llm_calls == 1
    assert loaded.llm_prompt_tokens == 100
    assert loaded.llm_completion_tokens == 50

    # LLM traffic dumped under the refine_N stem
    assert (gdir / "llm" / "refine_1_prompt.json").is_file()
    assert (gdir / "llm" / "refine_1_response.txt").read_text(encoding="utf-8") == _REVISED

    # Prompt contract: system + template style contract + current deck + request
    client = holder["client"]
    messages = client.calls[0]
    assert len(messages) == 4
    assert messages[0]["role"] == "system"
    assert "revising a XeLaTeX Beamer deck" in messages[0]["content"]
    assert "%% DELETE <n>" in messages[0]["content"]  # may answer with changed slides only
    assert "No explanation, no markdown fences." in messages[0]["content"]
    assert r"\documentclass" in messages[1]["content"]  # template main.tex
    assert messages[2]["content"] == numbered_deck(_ORIGINAL)
    assert "%% FRAME 1\n\\begin{frame}" in messages[2]["content"]
    assert "Revision request for the whole deck:\nAdd a second slide" in messages[3]["content"]
    assert "<memory>" in messages[3]["content"]  # may add to what is remembered


def test_run_refine_retries_once_on_invalid_output(
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    meta = _make_finished_generation(artifact_root)
    gdir = store.generation_dir(meta.id)
    holder = _patch_llm(monkeypatch, ["not latex at all", _REVISED])

    _run_refine(meta.id, "Fix it", lambda _s, _p: None)

    client = holder["client"]
    assert len(client.calls) == 2
    # Retry appends the invalid output and a correction request
    retry = client.calls[1]
    assert retry[4]["role"] == "assistant"
    assert retry[4]["content"] == "not latex at all"
    assert "could not be used" in retry[5]["content"]
    assert (gdir / "llm" / "refine_1_retry_prompt.json").is_file()
    assert store.load_meta(meta.id).refine_count == 1


def test_run_refine_rolls_back_on_compile_failure(
    artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _make_finished_generation(artifact_root)
    gdir = store.generation_dir(meta.id)
    _patch_llm(monkeypatch, [_REVISED])

    def fake_run(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
        src = tex_path.read_text(encoding="utf-8")
        if "REVISED-MARKER" in src:
            return False, "! Broken revision.\nl.4\n"
        (work_dir / f"{tex_path.stem}.pdf").write_bytes(b"%PDF-fake")
        return True, "ok"

    monkeypatch.setattr("app.deck._run_xelatex", fake_run)
    monkeypatch.setattr("app.deck.shutil.which", lambda _cmd: "/usr/bin/xelatex")

    with pytest.raises(Exception):
        _run_refine(meta.id, "Break the deck", lambda _s, _p: None)

    # Original restored and recompiled; meta records the rollback without
    # flipping the status of the still-usable finished deck.
    assert (gdir / "main.tex").read_text(encoding="utf-8") == _ORIGINAL
    assert (gdir / "main.pdf").is_file()
    loaded = store.load_meta(meta.id)
    assert loaded.status == "succeeded"
    assert loaded.error.startswith("This refine failed and was rolled back:")
    assert loaded.refine_count == 0
    assert loaded.frame_count == 1
    # The failed call still burned tokens; usage must not be lost
    assert loaded.llm_calls >= 1


def test_run_refine_second_pass_uses_next_stem_and_accumulates(
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    meta = _make_finished_generation(artifact_root)
    gdir = store.generation_dir(meta.id)
    _patch_llm(monkeypatch, [_REVISED])
    _run_refine(meta.id, "First", lambda _s, _p: None)
    _patch_llm(monkeypatch, [_REVISED])
    _run_refine(meta.id, "Second", lambda _s, _p: None)

    loaded = store.load_meta(meta.id)
    assert loaded.refine_count == 2
    assert loaded.llm_calls == 2
    assert loaded.llm_prompt_tokens == 200
    assert loaded.llm_completion_tokens == 100
    assert (gdir / "llm" / "refine_2_prompt.json").is_file()
    assert (gdir / "edits" / "002" / "refine.json").is_file()


# ---- endpoint ------------------------------------------------------------------


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    assert settings.artifact_root == artifact_root
    with TestClient(app) as c:
        yield c


def test_refine_endpoint_empty_instruction_400(
    client: TestClient, artifact_root: Path
) -> None:
    meta = _make_finished_generation(artifact_root)
    r = client.post(f"/api/generations/{meta.id}/refine", json={"instruction": "   "})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_refine_endpoint_no_main_tex_404(client: TestClient, artifact_root: Path) -> None:
    meta = _make_finished_generation(artifact_root)
    (store.generation_dir(meta.id) / "main.tex").unlink()
    r = client.post(f"/api/generations/{meta.id}/refine", json={"instruction": "go"})
    assert r.status_code == 404
    assert "TeX" in r.json()["detail"]


def test_refine_endpoint_unknown_generation_404(client: TestClient) -> None:
    r = client.post(
        "/api/generations/20260101-000000-abcd/refine", json={"instruction": "go"}
    )
    assert r.status_code == 404


def test_refine_endpoint_409_while_busy(client: TestClient, artifact_root: Path) -> None:
    meta = _make_finished_generation(artifact_root)
    release = threading.Event()
    started = threading.Event()

    def blocking_job(_progress) -> None:
        started.set()
        assert release.wait(timeout=10)

    try:
        tasks.start(meta.id, blocking_job)
        assert started.wait(timeout=10)
        r = client.post(f"/api/generations/{meta.id}/refine", json={"instruction": "go"})
        assert r.status_code == 409
        assert "already running" in r.json()["detail"]
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while tasks.is_running(meta.id):
        assert time.monotonic() < deadline, "job never released the running flag"
        time.sleep(0.01)


def test_refine_endpoint_202_starts_job(
    client: TestClient, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = _make_finished_generation(artifact_root)
    started: list[str] = []
    monkeypatch.setattr(tasks, "start", lambda gen_id, _job: started.append(gen_id))

    r = client.post(f"/api/generations/{meta.id}/refine", json={"instruction": "polish"})
    assert r.status_code == 202
    assert started == [meta.id]
