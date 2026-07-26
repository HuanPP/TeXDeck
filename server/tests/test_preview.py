"""Template preview endpoint: cache hit/miss, invalidation, and failure modes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.deck import CompileError, get_template, template_fingerprint
from app.main import app


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    assert settings.artifact_root == artifact_root
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_compile(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace the xelatex preview build with a tiny real PDF written by fitz."""
    counter = {"n": 0}

    def fake(_tex_path: Path, work_dir: Path) -> None:
        counter["n"] += 1
        doc = fitz.open()
        try:
            doc.new_page(width=320, height=180)
            doc.save(str(work_dir / "main.pdf"))
        finally:
            doc.close()

    monkeypatch.setattr("app.deck._compile_preview", fake)
    return counter


def _preview_files(artifact_root: Path, key: str) -> list[Path]:
    d = artifact_root / "_previews"
    if not d.is_dir():
        return []
    return sorted(
        p
        for p in d.glob(f"{key}-*.png")
        if re.fullmatch(r"[0-9a-f]{12}", p.stem[len(key) + 1 :])
    )


def test_preview_builds_caches_and_reuses(
    client: TestClient, artifact_root: Path, fake_compile: dict[str, int]
) -> None:
    r = client.get("/api/templates/navy/preview.png")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-cache"
    assert r.content.startswith(b"\x89PNG")
    assert fake_compile["n"] == 1

    files = _preview_files(artifact_root, "navy")
    assert len(files) == 1
    expected = template_fingerprint(get_template("navy"))
    assert files[0].name == f"navy-{expected}.png"
    # No half-written temp file left behind
    assert not list((artifact_root / "_previews").glob("*.tmp"))

    # Second request is served from cache: same bytes, no rebuild
    r2 = client.get("/api/templates/navy/preview.png")
    assert r2.status_code == 200
    assert r2.content == r.content
    assert fake_compile["n"] == 1


def test_preview_cache_dir_does_not_pollute_generations(
    client: TestClient, artifact_root: Path, fake_compile: dict[str, int]
) -> None:
    assert client.get("/api/templates/navy/preview.png").status_code == 200
    assert client.get("/api/generations").json() == []


def test_preview_unknown_template_404(client: TestClient) -> None:
    r = client.get("/api/templates/no-such-template/preview.png")
    assert r.status_code == 404


def test_preview_compile_failure_503_with_log_tail(
    client: TestClient, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_tex_path: Path, _work_dir: Path) -> None:
        raise CompileError(
            "Template preview compilation failed", "...\n! Undefined control sequence.\nl.7"
        )

    monkeypatch.setattr("app.deck._compile_preview", broken)
    r = client.get("/api/templates/navy/preview.png")
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "Undefined control sequence" in detail
    assert _preview_files(artifact_root, "navy") == []


def test_preview_template_change_makes_new_hash_and_drops_stale(
    client: TestClient,
    artifact_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_compile: dict[str, int],
) -> None:
    # Isolated template pack so we can edit it without touching real templates
    templates_root = tmp_path / "templates"
    pack = templates_root / "fake"
    pack.mkdir(parents=True)
    (pack / "template.json").write_text(
        json.dumps({"key": "fake", "name": "Fake"}), encoding="utf-8"
    )
    (pack / "main.tex").write_text("\\documentclass{beamer} v1", encoding="utf-8")
    monkeypatch.setattr(settings, "templates_dir", templates_root)

    assert client.get("/api/templates/fake/preview.png").status_code == 200
    first = _preview_files(artifact_root, "fake")
    assert len(first) == 1
    assert fake_compile["n"] == 1

    # Any byte change in the pack must produce a new hash and a rebuild
    (pack / "main.tex").write_text("\\documentclass{beamer} v2", encoding="utf-8")
    assert client.get("/api/templates/fake/preview.png").status_code == 200
    second = _preview_files(artifact_root, "fake")
    assert len(second) == 1
    assert second[0].name != first[0].name
    assert not first[0].exists()  # stale sibling deleted
    assert fake_compile["n"] == 2
