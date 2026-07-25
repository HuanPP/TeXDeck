"""HTTP API tests with TestClient; no real LLM calls."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.deck import list_templates
from app.main import app


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    # artifact_root fixture already monkeypatches settings.artifact_root
    assert settings.artifact_root == artifact_root
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_list_templates_matches_real_disk(client: TestClient) -> None:
    r = client.get("/api/templates")
    assert r.status_code == 200
    body = r.json()
    expected = list_templates()
    assert len(body) == len(expected)
    assert {row["key"] for row in body} == {t.key for t in expected}
    for row in body:
        assert set(row.keys()) == {"key", "name"}
        assert isinstance(row["key"], str) and row["key"]
        assert isinstance(row["name"], str) and row["name"]


def test_list_generations_empty(client: TestClient, artifact_root: Path) -> None:
    assert list(artifact_root.iterdir()) == []
    r = client.get("/api/generations")
    assert r.status_code == 200
    assert r.json() == []


def test_get_generation_missing_valid_id_returns_404_english(
    client: TestClient,
) -> None:
    # Valid shape per store._GEN_ID_RE, but not on disk
    missing_id = "20260101-000000-abcd"
    r = client.get(f"/api/generations/{missing_id}")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "Generation not found" in detail
    # English, not Chinese
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in detail)


def test_get_generation_illegal_id_returns_400(client: TestClient) -> None:
    r = client.get("/api/generations/not-a-valid-id!!")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "Invalid" in detail


def test_create_generation_requires_material(client: TestClient) -> None:
    templates = client.get("/api/templates").json()
    assert templates, "need at least one real template"
    key = templates[0]["key"]
    r = client.post(
        "/api/generations",
        data={
            "title": "No Material Deck",
            "template_key": key,
            "instruction": "",
            # neither file nor source_text
        },
    )
    assert 400 <= r.status_code < 500
    detail = r.json()["detail"]
    assert isinstance(detail, str)
    assert "PDF" in detail or "text" in detail.lower() or "Upload" in detail


def test_create_generation_accepts_author_and_institute(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Form fields author/institute are parsed and persisted; no real LLM needed."""
    # Avoid background job hitting a real LLM after 202 accept
    from app import tasks

    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)

    templates = client.get("/api/templates").json()
    assert templates, "need at least one real template"
    key = templates[0]["key"]
    r = client.post(
        "/api/generations",
        data={
            "title": "Author Field Deck",
            "template_key": key,
            "instruction": "short",
            "source_text": "Some research notes for the deck.",
            "author": "Jane Doe, John Smith",
            "institute": "Dept. of Physics, MIT",
        },
    )
    assert r.status_code == 202, r.text
    gen_id = r.json()["id"]
    meta = client.get(f"/api/generations/{gen_id}").json()
    assert meta["author"] == "Jane Doe, John Smith"
    assert meta["institute"] == "Dept. of Physics, MIT"
    assert meta["title"] == "Author Field Deck"
