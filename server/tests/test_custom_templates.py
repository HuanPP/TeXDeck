"""Custom templates: validation, storage, the editor API and the bundle they produce."""

from __future__ import annotations

import os
import re
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app import auth, custom_templates, deck
from app.config import settings
from app.custom_templates import CustomTemplateError
from app.main import app


def _png(width: int, height: int, alpha: bool = False) -> bytes:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), alpha)
    pixmap.clear_with(200)
    return pixmap.tobytes("png")


@pytest.fixture
def custom_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "custom"
    monkeypatch.setattr(settings, "custom_templates_dir", root)
    return root


@pytest.fixture
def client(artifact_root: Path, custom_root: Path) -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-page PDF with a title line instead of xelatex."""

    def fake(_tex_path: Path, work_dir: Path, passes: int = 0) -> None:
        doc = fitz.open()
        page = doc.new_page(width=455, height=256)
        page.insert_text((40, 120), "Title of the deck", fontsize=20)
        doc.save(str(work_dir / "main.pdf"))
        doc.close()

    monkeypatch.setattr("app.deck._compile_preview", fake)


# ---- the packs themselves ---------------------------------------------------------


def _sty_rgb(sty: str, name: str) -> tuple[int, int, int]:
    if name == "maincolor":  # set by the pack's institution option
        value = re.findall(r"\\renewcommand\{\\maincolorRGB\}\{([^}]*)\}", sty)[-1]
        return tuple(int(v) for v in value.split(","))  # type: ignore[return-value]
    match = re.search(r"\\definecolor\{" + name + r"\}\{(RGB|rgb)\}\{([^}]*)\}", sty)
    assert match, name
    values = [float(v) for v in match.group(2).split(",")]
    if match.group(1) == "rgb":
        values = [v * 255 for v in values]
    return tuple(round(v) for v in values)  # type: ignore[return-value]


def test_colour_defaults_match_the_packs() -> None:
    bases = custom_templates.bases()
    assert {b.key for b in bases} == {"academic", "emerald", "graphite", "navy", "scholar"}
    for base in bases:
        sty = (base.dir / "collegebeamer.sty").read_text(encoding="utf-8")
        assert "\\InputIfFileExists{brand-colors.tex}" in sty
        for slot in base.meta["customize"]["colors"]:
            hex_value = slot["default"]
            rgb = tuple(int(hex_value[i : i + 2], 16) for i in (1, 3, 5))
            expected = _sty_rgb(sty, slot["name"])
            assert all(abs(a - b) <= 1 for a, b in zip(rgb, expected)), (base.key, slot, expected)
        for image in base.meta["customize"]["images"]:
            assert image in custom_templates.IMAGE_FILES


# ---- images ----------------------------------------------------------------------


def test_asset_is_reencoded_and_content_addressed(custom_root: Path) -> None:
    first = custom_templates.save_asset(_png(40, 20))
    again = custom_templates.save_asset(_png(40, 20))
    assert first == again
    assert re.fullmatch(r"[0-9a-f]{20}\.png", first["id"])
    assert (first["width"], first["height"]) == (40, 20)
    assert (custom_root / "assets" / first["id"]).is_file()


@pytest.mark.parametrize("data", [b"", b"GIF89a....", b"\x89PNG\r\n\x1a\nbroken"])
def test_asset_rejects_non_images(custom_root: Path, data: bytes) -> None:
    with pytest.raises(CustomTemplateError):
        custom_templates.save_asset(data)


def test_unused_uploads_are_pruned_after_a_day(custom_root: Path) -> None:
    old = custom_templates.save_asset(_png(10, 10))["id"]
    kept = custom_templates.save_asset(_png(1920, 1080))["id"]
    custom_templates.create(custom_templates.parse_draft(
        {"base": "navy", "images": {"background": kept}}
    ))
    for name in (old, kept):
        os.utime(custom_root / "assets" / name, (0, 0))
    custom_templates.save_asset(_png(12, 12))
    assert not (custom_root / "assets" / old).exists()
    assert (custom_root / "assets" / kept).exists()


# ---- drafts ----------------------------------------------------------------------


def test_draft_keeps_only_changed_colours(custom_root: Path) -> None:
    draft = custom_templates.parse_draft({
        "base": "navy",
        "name": "  Lab   deck ",
        "colors": {"maincolor": "#aa0000", "airforceblue": "#5e7691"},
    })
    assert draft.name == "Lab deck"
    assert draft.colors == {"maincolor": "#AA0000"}
    assert custom_templates.colors_tex(draft.colors).decode().splitlines()[-1] == (
        "\\definecolor{maincolor}{RGB}{170,0,0}"
    )


@pytest.mark.parametrize("body", [
    {"base": "simpleplus"},  # not customisable
    {"base": "../navy"},
    {"base": "navy", "colors": {"maincolor": "#12345"}},
    {"base": "navy", "colors": {"maincolor": "#123456}\\input{x}"}},
    {"base": "navy", "colors": {"codegreen": "#123456"}},
    {"base": "navy", "images": {"logo": None}},  # navy only shows the dark logo
    {"base": "navy", "images": {"background": "0123456789abcdef0123.png"}},
    {"base": "navy", "images": {"background": "../../etc/passwd"}},
    {"base": "navy", "editor": {"source": "0123456789abcdef0123.jpg"}},
])
def test_draft_rejects_bad_input(custom_root: Path, body: dict) -> None:
    with pytest.raises(CustomTemplateError):
        custom_templates.parse_draft(body)


def test_background_must_be_the_exported_size(custom_root: Path) -> None:
    wrong = custom_templates.save_asset(_png(1600, 1200))["id"]
    with pytest.raises(CustomTemplateError, match="1920x1080"):
        custom_templates.parse_draft({"base": "navy", "images": {"background": wrong}})


def test_bundle_lays_custom_files_over_the_base(custom_root: Path, tmp_path: Path) -> None:
    bg = custom_templates.save_asset(_png(1920, 1080))["id"]
    logo = custom_templates.save_asset(_png(300, 100, alpha=True))["id"]
    spec = custom_templates.create(custom_templates.parse_draft({
        "base": "graphite",
        "colors": {"maincolor": "#112233"},
        "images": {"background": bg, "trans_logo": logo},
    }))
    template = deck.get_template(spec["key"])
    out = tmp_path / "bundle"
    deck.copy_template_bundle(template, out)
    assert (out / "collegebeamer.sty").is_file()
    assert "{RGB}{17,34,51}" in (out / "brand-colors.tex").read_text()
    assert (out / "src/brand/background.png").read_bytes() == (custom_root / "assets" / bg).read_bytes()
    assert (out / "src/brand/trans-logo.png").read_bytes() == (custom_root / "assets" / logo).read_bytes()
    base_logo = deck.builtin_template("graphite").dir / "src/brand/color-logo.png"
    assert (out / "src/brand/color-logo.png").read_bytes() == base_logo.read_bytes()
    assert template.main_tex == deck.builtin_template("graphite").main_tex
    assert deck.template_fingerprint(template) != deck.template_fingerprint(deck.builtin_template("graphite"))


def test_templates_are_per_account(
    custom_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "users_file", tmp_path / "users.json")
    spec = auth.run_as("alice", custom_templates.create, custom_templates.parse_draft({"base": "navy"}))
    assert (tmp_path / "accounts/alice/templates" / f"{spec['key']}.json").is_file()
    assert [t["key"] for t in auth.run_as("alice", custom_templates.list_custom)] == [spec["key"]]
    assert auth.run_as("bob", custom_templates.list_custom) == []
    with pytest.raises(ValueError):
        auth.run_as("bob", deck.get_template, spec["key"])


# ---- API ---------------------------------------------------------------------------


def _upload(client: TestClient, data: bytes) -> str:
    r = client.post("/api/custom-templates/assets", files={"file": ("x.png", data, "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_editor_flow(client: TestClient, fake_compile: None) -> None:
    bases = client.get("/api/template-bases").json()
    navy = next(b for b in bases if b["key"] == "navy")
    assert navy["images"] == {"background": "Title page background", "trans_logo": "Logo on the title band"}

    bg = _upload(client, _png(1920, 1080))
    body = {"base": "navy", "name": "Lab", "colors": {"maincolor": "#AA0000"},
            "images": {"background": bg}, "editor": {"background": {"source": bg, "rotation": 15}}}

    r = client.post("/api/custom-templates/preview", json=body)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")

    r = client.post("/api/custom-templates", json=body)
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    listed = client.get("/api/templates").json()
    assert {"key": key, "name": "Lab", "base": "navy", "custom": True, "kind": "deck"} in listed
    assert client.get(f"/api/custom-templates/{key}").json()["editor"]["background"]["rotation"] == 15

    r = client.get(f"/api/templates/{key}/preview.png")
    assert r.status_code == 200, r.text
    layout = client.get(f"/api/templates/{key}/layout").json()
    assert layout and layout[0]["kind"] == "text" and 0 < layout[0]["x"] < 1
    assert client.get(f"/api/templates/{key}/images/background").content == (
        client.get(f"/api/custom-templates/assets/{bg}").content
    )
    base_logo = (deck.builtin_template("navy").dir / "src/brand/trans-logo.png").read_bytes()
    assert client.get(f"/api/templates/{key}/images/trans_logo").content == base_logo

    r = client.put(f"/api/custom-templates/{key}", json={**body, "name": "Lab 2"})
    assert r.status_code == 200 and r.json()["name"] == "Lab 2"
    r = client.put(f"/api/custom-templates/{key}", json={**body, "base": "academic"})
    assert r.status_code == 400

    assert client.delete(f"/api/custom-templates/{key}").status_code == 204
    assert key not in {t["key"] for t in client.get("/api/templates").json()}
    assert client.get(f"/api/custom-templates/{key}").status_code == 404
    # Decks made with it keep working ...
    assert deck.get_template(key).name == "Lab 2"
    # ... but new ones cannot pick it.
    r = client.post("/api/generations", data={"template_key": key, "source_text": "x"})
    assert r.status_code == 400


def test_unknown_custom_template_is_404(client: TestClient) -> None:
    assert client.get("/api/custom-templates/my-0123456789").status_code == 404
    assert client.get("/api/templates/my-0123456789/preview.png").status_code == 404
    assert client.put("/api/custom-templates/my-0123456789", json={"base": "navy"}).status_code == 404


# ---- real TeX ------------------------------------------------------------------------


@pytest.mark.slow
def test_custom_colours_and_odd_logo_compile(custom_root: Path) -> None:
    """A wide logo stays inside its slot and the palette reaches the PDF."""
    wide = custom_templates.save_asset(_png(800, 100, alpha=True))["id"]
    draft = custom_templates.parse_draft({
        "base": "scholar",
        "colors": {"maincolor": "#CC0000"},
        "images": {"logo": wide, "trans_logo": wide},
    })
    pdf = custom_templates.preview_pdf(draft)
    doc = fitz.open(stream=pdf, filetype="pdf")
    content_page = doc[2]
    images = content_page.get_image_info()
    logo = min(images, key=lambda i: fitz.Rect(i["bbox"]).get_area())
    rect = fitz.Rect(logo["bbox"])
    assert rect.width / rect.height == pytest.approx(8, rel=0.05)
    assert rect.width <= 2.5 * 0.09 * doc[0].rect.width + 0.5
    colours = {span["color"] for b in doc[0].get_text("dict")["blocks"] for line in b.get("lines", [])
               for span in line["spans"]}
    assert 0xCC0000 in colours
