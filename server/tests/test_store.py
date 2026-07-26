"""Real tests for generation artifact store (isolated artifact_root)."""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest

from app.store import (
    GenerationMeta,
    bundle_zip,
    generation_dir,
    list_generations,
    load_meta,
    new_generation,
    next_edit_dir,
    save_meta,
)


def _make_gen(**overrides: object) -> GenerationMeta:
    kwargs = dict(
        title="T",
        template_key="navy",
        template_name="Navy",
        instruction="",
        source_kind="text",
        source_names=[],
    )
    kwargs.update(overrides)
    return new_generation(**kwargs)  # type: ignore[arg-type]


def test_new_save_load_meta_roundtrip(artifact_root: Path) -> None:
    meta = _make_gen(
        title="Roundtrip Deck",
        instruction="keep short",
        author="Jane Doe",
        institute="MIT",
    )
    assert meta.status == "running"
    assert meta.id
    assert (artifact_root / meta.id / "meta.json").is_file()

    meta.status = "succeeded"
    meta.frame_count = 5
    meta.error = ""
    save_meta(meta)

    loaded = load_meta(meta.id)
    assert loaded.id == meta.id
    assert loaded.title == "Roundtrip Deck"
    assert loaded.author == "Jane Doe"
    assert loaded.institute == "MIT"
    assert loaded.template_key == "navy"
    assert loaded.template_name == "Navy"
    assert loaded.instruction == "keep short"
    assert loaded.source_kind == "text"
    assert loaded.status == "succeeded"
    assert loaded.frame_count == 5
    assert loaded.created_at == meta.created_at
    # save_meta refreshes updated_at
    assert loaded.updated_at >= meta.created_at


def test_load_meta_missing_author_institute_defaults(artifact_root: Path) -> None:
    """Older meta.json without author/institute must still load (schema evolution)."""
    meta = _make_gen(title="Legacy")
    path = artifact_root / meta.id / "meta.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("author", None)
    data.pop("institute", None)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    loaded = load_meta(meta.id)
    assert loaded.author == ""
    assert loaded.institute == ""


def test_load_meta_legacy_source_name_becomes_source_names(artifact_root: Path) -> None:
    """Pre-multi-PDF meta.json carries scalar source_name; the load shim upgrades it."""
    meta = _make_gen(title="Legacy PDF", source_kind="pdf", source_names=["a.pdf"])
    path = artifact_root / meta.id / "meta.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("source_names", None)
    data["source_name"] = "a.pdf"
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    loaded = load_meta(meta.id)
    assert loaded.source_names == ["a.pdf"]

    # Legacy text mode: source_name "" maps to an empty list
    data["source_name"] = ""
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert load_meta(meta.id).source_names == []

    # The shim also applies to listing
    listed = {m.id: m for m in list_generations()}
    assert listed[meta.id].source_names == []


def test_list_generations_newest_first_skips_corrupt_meta(artifact_root: Path) -> None:
    older = _make_gen(title="Older")
    time.sleep(0.02)
    newer = _make_gen(title="Newer")

    # Corrupt meta must not crash the listing
    bad_dir = artifact_root / "20260101-000000-dead"
    bad_dir.mkdir()
    (bad_dir / "meta.json").write_text("{not valid json", encoding="utf-8")

    listed = list_generations()
    ids = [m.id for m in listed]
    assert newer.id in ids
    assert older.id in ids
    assert ids.index(newer.id) < ids.index(older.id)
    # Corrupt entry is skipped entirely
    assert all(m.id != "20260101-000000-dead" for m in listed)


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "../escape",
        "has/slash",
        "UPPERCASE!!",
        "short",
        "spaces not ok",
        "2026_01_01",  # underscore not allowed
    ],
)
def test_generation_dir_rejects_illegal_ids(artifact_root: Path, bad_id: str) -> None:
    with pytest.raises(ValueError, match="Invalid generation ID"):
        generation_dir(bad_id)


def test_next_edit_dir_increments(artifact_root: Path) -> None:
    meta = _make_gen()
    d1 = next_edit_dir(meta.id)
    d2 = next_edit_dir(meta.id)
    d3 = next_edit_dir(meta.id)
    assert d1.name == "001"
    assert d2.name == "002"
    assert d3.name == "003"
    assert d1.parent == d2.parent == generation_dir(meta.id) / "edits"
    assert d1.is_dir() and d2.is_dir() and d3.is_dir()


def test_bundle_zip_contents(artifact_root: Path) -> None:
    meta = _make_gen()
    gdir = generation_dir(meta.id)

    (gdir / "main.tex").write_text("% deck\n", encoding="utf-8")
    (gdir / "collegebeamer.sty").write_text("% sty\n", encoding="utf-8")
    (gdir / "beamerthemeSimplePlus.sty").write_text("% theme\n", encoding="utf-8")
    (gdir / "src" / "brand").mkdir(parents=True)
    (gdir / "src" / "brand" / "logo.png").write_bytes(b"\x89PNG")
    (gdir / "font").mkdir()
    (gdir / "font" / "face.otf").write_bytes(b"OTTO")

    # Artifacts that must NOT appear in the bundle
    (gdir / "main.pdf").write_bytes(b"%PDF")
    (gdir / "main.log").write_text("log", encoding="utf-8")
    (gdir / "llm").mkdir()
    (gdir / "llm" / "generate_response.txt").write_text("x", encoding="utf-8")
    (gdir / "pages").mkdir()
    (gdir / "pages" / "page_001.png").write_bytes(b"png")

    zpath = bundle_zip(meta.id)
    assert zpath.is_file()
    with zipfile.ZipFile(zpath) as zf:
        names = set(zf.namelist())

    assert "main.tex" in names
    assert "collegebeamer.sty" in names
    assert "src/brand/logo.png" in names
    assert "font/face.otf" in names

    assert "main.pdf" not in names
    assert "main.log" not in names
    assert not any(n.startswith("llm/") for n in names)
    assert not any(n.startswith("pages/") for n in names)
    assert "meta.json" not in names
