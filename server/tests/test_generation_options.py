"""Slide language, optional title, model-error tagging and the submit-time model check."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, tasks
from app.config import settings
from app.deck import DeckRequest, build_generation_messages, extract_title, get_template
from app.llm import LlmError
from app.main import _run_generation, app

from .conftest import FakeLlm

_DECK = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\title[Short]{Gold \textbf{Prices} in 2024\\ A Review}
\begin{document}
\begin{frame}{Hello}
Body text.
\end{frame}
\end{document}
"""


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ids: list[str] = []
    monkeypatch.setattr(tasks, "start", lambda gen_id, _job: ids.append(gen_id))
    return ids


def _create(client: TestClient, **data: str):
    return client.post("/api/generations", data={
        "template_key": "navy", "source_text": "Notes about gold.", **data,
    })


@pytest.mark.parametrize(("language", "expected"), [
    ("source", "the language of the source material"),
    ("en", "Output language: English"),
    ("zh", "Output language: Simplified Chinese"),
])
def test_language_rule_reaches_the_prompt(language: str, expected: str) -> None:
    template = get_template("navy")
    request = DeckRequest(template_key="navy", title="T", source_text="x", language=language)
    assert expected in build_generation_messages(template, request, documents=[])[2]["content"]


def test_empty_title_asks_the_model_for_one() -> None:
    template = get_template("navy")
    title_msg = build_generation_messages(
        template, DeckRequest(template_key="navy", title="", source_text="x"), documents=[]
    )[2]["content"]
    assert "No title was provided" in title_msg and "Title:" not in title_msg


def test_extract_title_strips_latex() -> None:
    assert extract_title(_DECK) == "Gold Prices in 2024 A Review"
    assert extract_title(r"\title{A {nested} title}") == "A nested title"
    assert extract_title("no title here") == ""


def test_language_and_blank_title_are_stored_and_regenerated(client: TestClient, started: list[str]) -> None:
    r = _create(client, language="zh")
    assert r.status_code == 202, r.text
    meta = client.get(f"/api/generations/{r.json()['id']}").json()
    assert meta["language"] == "zh" and meta["title"] == ""
    again = client.post(f"/api/generations/{meta['id']}/regenerate").json()["id"]
    assert store.load_meta(again).language == "zh"
    assert _create(client, language="fr").status_code == 400


def test_blank_title_is_filled_from_the_deck(
    client: TestClient, started: list[str], monkeypatch: pytest.MonkeyPatch, fake_xelatex_ok: dict[str, int]
) -> None:
    gen_id = _create(client).json()["id"]
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: FakeLlm([_DECK], on_usage=on_usage))
    _run_generation(gen_id, lambda _s, _p: None)
    loaded = store.load_meta(gen_id)
    assert loaded.status == "succeeded" and loaded.title == "Gold Prices in 2024 A Review"


def test_model_failures_are_tagged(client: TestClient, started: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    gen_id = _create(client, title="T").json()["id"]

    def broken(on_usage=None):
        raise LlmError("Claude Code: Not logged in")

    monkeypatch.setattr("app.main.create_client", broken)
    with pytest.raises(LlmError):
        _run_generation(gen_id, lambda _s, _p: None)
    loaded = store.load_meta(gen_id)
    assert loaded.status == "failed" and loaded.error_kind == "model"

    (store.generation_dir(gen_id) / "source.txt").unlink()
    with pytest.raises(RuntimeError):
        _run_generation(gen_id, lambda _s, _p: None)
    assert store.load_meta(gen_id).error_kind == ""


def test_submit_is_refused_without_a_model(
    client: TestClient, started: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import llm

    monkeypatch.setattr("app.main.ensure_model_ready", llm.ensure_model_ready)
    monkeypatch.setattr(settings, "llm_provider", "auto")
    monkeypatch.setattr(settings, "llm_api_key", "")
    r = _create(client, title="T")
    assert r.status_code == 400 and "No model connected" in r.json()["detail"]
    assert started == []
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    assert _create(client, title="T").status_code == 202
