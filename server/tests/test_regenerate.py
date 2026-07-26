"""Regenerate endpoint + pasted-text persistence (source.txt read from disk)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import store, tasks
from app.config import settings
from app.main import _run_generation, app
from tests.conftest import FakeLlm


_DECK = r"""\documentclass[aspectratio=169,10pt]{beamer}
\usepackage[zh,bjtu]{collegebeamer}
\begin{document}
\begin{frame}{Hello}
Body text.
\end{frame}
\end{document}
"""


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    assert settings.artifact_root == artifact_root
    with TestClient(app) as c:
        yield c


@pytest.fixture
def no_jobs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Swallow background jobs; returns the list of started generation ids."""
    started: list[str] = []
    monkeypatch.setattr(tasks, "start", lambda gen_id, _job: started.append(gen_id))
    return started


def _template_key(client: TestClient) -> str:
    # navy is a real shipped template whose theme directive matches _DECK
    templates = client.get("/api/templates").json()
    assert any(t["key"] == "navy" for t in templates), "need the navy template"
    return "navy"


def _create_text_generation(client: TestClient, key: str) -> str:
    r = client.post(
        "/api/generations",
        data={
            "title": "Text Deck",
            "template_key": key,
            "instruction": "keep short",
            "source_text": "Pasted research notes.",
            "author": "Jane",
            "institute": "MIT",
        },
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


# ---- pasted text persisted at creation -------------------------------------------


def test_create_text_generation_writes_source_txt(
    client: TestClient, artifact_root: Path, no_jobs: list[str]
) -> None:
    gen_id = _create_text_generation(client, _template_key(client))
    source = artifact_root / gen_id / "source.txt"
    assert source.read_text(encoding="utf-8") == "Pasted research notes."


def test_generation_job_reads_source_txt_from_disk(
    client: TestClient,
    artifact_root: Path,
    no_jobs: list[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    """The job must use the on-disk text, not anything captured in memory."""
    gen_id = _create_text_generation(client, _template_key(client))
    # Overwrite the persisted text AFTER creation; the job must pick this up.
    (artifact_root / gen_id / "source.txt").write_text(
        "EDITED-ON-DISK notes.", encoding="utf-8"
    )

    holder: dict[str, FakeLlm] = {}

    def fake_create_client(on_usage=None) -> FakeLlm:
        holder["client"] = FakeLlm([_DECK], on_usage=on_usage, usage=(11, 7))
        return holder["client"]

    monkeypatch.setattr("app.main.create_client", fake_create_client)
    _run_generation(gen_id, lambda _s, _p: None)

    material = holder["client"].calls[0][3]["content"]
    assert material == "EDITED-ON-DISK notes."
    loaded = store.load_meta(gen_id)
    assert loaded.status == "succeeded"
    assert loaded.frame_count == 1
    # Usage accounting rides along with the generation job
    assert loaded.llm_calls == 1
    assert loaded.llm_prompt_tokens == 11
    assert loaded.llm_completion_tokens == 7
    assert loaded.llm_model == settings.llm_model


def test_generation_job_fails_when_source_txt_missing(
    client: TestClient, artifact_root: Path, no_jobs: list[str]
) -> None:
    gen_id = _create_text_generation(client, _template_key(client))
    (artifact_root / gen_id / "source.txt").unlink()
    with pytest.raises(RuntimeError, match="Pasted text source is missing"):
        _run_generation(gen_id, lambda _s, _p: None)
    assert store.load_meta(gen_id).status == "failed"


# ---- regenerate -------------------------------------------------------------------


def test_regenerate_text_happy_path(
    client: TestClient, artifact_root: Path, no_jobs: list[str]
) -> None:
    old_id = _create_text_generation(client, _template_key(client))

    r = client.post(f"/api/generations/{old_id}/regenerate")
    assert r.status_code == 202, r.text
    new_id = r.json()["id"]
    assert new_id != old_id
    assert no_jobs[-1] == new_id

    old_meta = client.get(f"/api/generations/{old_id}").json()
    new_meta = client.get(f"/api/generations/{new_id}").json()
    for field in (
        "title",
        "template_key",
        "template_name",
        "instruction",
        "author",
        "institute",
        "source_kind",
        "source_names",
    ):
        assert new_meta[field] == old_meta[field], field
    assert new_meta["status"] == "running"
    assert (artifact_root / new_id / "source.txt").read_text(
        encoding="utf-8"
    ) == "Pasted research notes."
    # Old generation untouched
    assert (artifact_root / old_id / "source.txt").is_file()


def test_regenerate_pdf_happy_path(
    client: TestClient, artifact_root: Path, no_jobs: list[str]
) -> None:
    key = _template_key(client)
    bytes_a, bytes_b = b"%PDF-1.7 fake A", b"%PDF-1.7 fake B"
    r = client.post(
        "/api/generations",
        data={"title": "Two PDF Deck", "template_key": key},
        files=[
            ("files", ("a.pdf", bytes_a, "application/pdf")),
            ("files", ("b.pdf", bytes_b, "application/pdf")),
        ],
    )
    assert r.status_code == 202, r.text
    old_id = r.json()["id"]

    r = client.post(f"/api/generations/{old_id}/regenerate")
    assert r.status_code == 202, r.text
    new_id = r.json()["id"]
    assert new_id != old_id
    assert no_jobs[-1] == new_id

    new_dir = artifact_root / new_id
    assert (new_dir / "source_01.pdf").read_bytes() == bytes_a
    assert (new_dir / "source_02.pdf").read_bytes() == bytes_b
    new_meta = client.get(f"/api/generations/{new_id}").json()
    assert new_meta["source_kind"] == "pdf"
    assert new_meta["source_names"] == ["a.pdf", "b.pdf"]
    # Old sources still in place
    assert (artifact_root / old_id / "source_01.pdf").read_bytes() == bytes_a


def test_regenerate_missing_text_source_400(
    client: TestClient, artifact_root: Path, no_jobs: list[str]
) -> None:
    old_id = _create_text_generation(client, _template_key(client))
    (artifact_root / old_id / "source.txt").unlink()
    r = client.post(f"/api/generations/{old_id}/regenerate")
    assert r.status_code == 400
    assert "missing" in r.json()["detail"].lower()
    # No half-created sibling generation was started
    assert no_jobs.count(old_id) <= 1


def test_regenerate_missing_pdf_source_400(
    client: TestClient, artifact_root: Path, no_jobs: list[str]
) -> None:
    key = _template_key(client)
    r = client.post(
        "/api/generations",
        data={"title": "One PDF Deck", "template_key": key},
        files=[("files", ("a.pdf", b"%PDF-1.7 fake A", "application/pdf"))],
    )
    old_id = r.json()["id"]
    (artifact_root / old_id / "source_01.pdf").unlink()
    r = client.post(f"/api/generations/{old_id}/regenerate")
    assert r.status_code == 400
    assert "missing" in r.json()["detail"].lower()


def test_regenerate_unknown_generation_404(client: TestClient) -> None:
    r = client.post("/api/generations/20260101-000000-abcd/regenerate")
    assert r.status_code == 404
