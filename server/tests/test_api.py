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
        assert set(row.keys()) == {"key", "name", "customizable"}
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


def test_create_generation_multi_pdf_happy_path(
    client: TestClient, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple PDFs: 202, source_names in upload order, source_XX.pdf bytes on disk."""
    from app import tasks

    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)

    templates = client.get("/api/templates").json()
    assert templates, "need at least one real template"
    key = templates[0]["key"]
    bytes_a, bytes_b = b"%PDF-1.7 fake A", b"%PDF-1.7 fake B"
    r = client.post(
        "/api/generations",
        data={"title": "Two PDF Deck", "template_key": key, "instruction": ""},
        files=[
            ("files", ("a.pdf", bytes_a, "application/pdf")),
            ("files", ("b.pdf", bytes_b, "application/pdf")),
        ],
    )
    assert r.status_code == 202, r.text
    gen_id = r.json()["id"]
    meta = client.get(f"/api/generations/{gen_id}").json()
    assert meta["source_kind"] == "pdf"
    assert meta["source_names"] == ["a.pdf", "b.pdf"]
    gdir = artifact_root / gen_id
    assert (gdir / "source_01.pdf").read_bytes() == bytes_a
    assert (gdir / "source_02.pdf").read_bytes() == bytes_b
    assert not (gdir / "source.pdf").exists()


def test_create_generation_empty_pdf_among_several_names_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import tasks

    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)

    templates = client.get("/api/templates").json()
    key = templates[0]["key"]
    r = client.post(
        "/api/generations",
        data={"title": "Empty Member Deck", "template_key": key},
        files=[
            ("files", ("a.pdf", b"%PDF-1.7 fake A", "application/pdf")),
            ("files", ("hollow.pdf", b"", "application/pdf")),
        ],
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "empty" in detail.lower()
    assert "hollow.pdf" in detail


def test_create_generation_rejects_more_than_ten_files(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import tasks

    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)

    templates = client.get("/api/templates").json()
    key = templates[0]["key"]
    uploads = [
        ("files", (f"doc_{i:02d}.pdf", b"%PDF-1.7 fake", "application/pdf"))
        for i in range(1, 12)  # 11 files
    ]
    r = client.post(
        "/api/generations",
        data={"title": "Too Many Deck", "template_key": key},
        files=uploads,
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "10" in detail


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


def test_api_responses_are_not_cached(client: TestClient) -> None:
    # PDF/TeX/frames are rewritten in place after edits; a reload must revalidate.
    r = client.get("/api/templates")
    assert r.headers["cache-control"] == "no-cache"


def test_pptx_endpoint_caches_and_rebuilds(client: TestClient) -> None:
    import os

    import fitz

    from app.pptx_export import EXPORT_VERSION
    from app.store import generation_dir, new_generation

    meta = new_generation(
        title="My/Deck", template_key="navy", template_name="Navy",
        instruction="", source_kind="text", source_names=[],
    )
    gdir = generation_dir(meta.id)
    doc = fitz.open()
    page = doc.new_page(width=453.54, height=255.12)
    page.insert_text((30, 60), "Slide", fontsize=14)
    doc.save(gdir / "main.pdf")
    (gdir / "main.v0.pptx").write_bytes(b"stale")  # left by an older exporter
    r = client.get(f"/api/generations/{meta.id}/deck.pptx")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "My_Deck.pptx" in r.headers["content-disposition"]
    cached = gdir / f"main.v{EXPORT_VERSION}.pptx"
    assert cached.is_file() and not (gdir / "main.v0.pptx").exists()

    # Unchanged PDF: served from cache.
    before = cached.stat().st_mtime_ns
    assert client.get(f"/api/generations/{meta.id}/deck.pptx").status_code == 200
    assert cached.stat().st_mtime_ns == before

    # Recompiled PDF (newer mtime): rebuilt.
    os.utime(gdir / "main.pdf", ns=(before + 10**9, before + 10**9))
    assert client.get(f"/api/generations/{meta.id}/deck.pptx").status_code == 200
    assert cached.stat().st_mtime_ns > before

    assert client.get("/api/generations/20000101-000000-0000/deck.pptx").status_code == 404
