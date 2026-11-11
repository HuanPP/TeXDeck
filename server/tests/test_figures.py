"""User images: upload checks, \\includegraphics allow-list, prompts, layout fixes."""

from __future__ import annotations

from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app import deck, figures, store, tasks
from app.config import settings
from app.deck import DeckError
from app.main import _run_generation, app
from tests.conftest import FakeLlm


def _png(width: int = 40, height: int = 20) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), 0)
    pix.clear_with(200)
    return pix.tobytes("png")


def _jpg(width: int = 30, height: int = 60) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, width, height), 0)
    pix.clear_with(90)
    return pix.tobytes("jpg")


def _deck(*frames: str) -> str:
    body = "\n".join(frames)
    return (
        "\\documentclass[aspectratio=169,10pt]{beamer}\n"
        "\\usepackage[zh,bjtu]{collegebeamer}\n"
        "\\begin{document}\n" + body + "\n\\end{document}"
    )


def _frame(title: str, body: str) -> str:
    return f"\\begin{{frame}}{{{title}}}\n{body}\n\\end{{frame}}"


_FIG1 = r"\includegraphics[width=0.8\linewidth,height=0.6\textheight,keepaspectratio]{figures/fig-01.png}"
_FIG2 = r"\includegraphics[width=0.4\linewidth,height=0.6\textheight,keepaspectratio]{figures/fig-02.jpg}"


# ---- storing uploads ---------------------------------------------------------------


def test_store_keeps_order_size_and_note(tmp_path: Path) -> None:
    records = figures.store(
        tmp_path, [("chart.png", _png(), " put on results "), ("photo.jpeg", _jpg(), "")]
    )
    assert [r["file"] for r in records] == ["figures/fig-01.png", "figures/fig-02.jpg"]
    assert records[0] == {
        "file": "figures/fig-01.png", "name": "chart.png", "note": "put on results",
        "width": 40, "height": 20,
    }
    assert (records[1]["width"], records[1]["height"]) == (30, 60)
    assert deck.figure_files(tmp_path) == ["figures/fig-01.png", "figures/fig-02.jpg"]


@pytest.mark.parametrize(
    "data, message",
    [(b"", "empty"), (b"GIF89a....", "PNG or JPEG"), (b"\x89PNG\r\n\x1a\nbroken", "could not be read")],
)
def test_store_rejects_bad_images(tmp_path: Path, data: bytes, message: str) -> None:
    with pytest.raises(figures.FigureError, match=message):
        figures.store(tmp_path, [("x.png", data, "")])


def test_store_rejects_too_many(tmp_path: Path) -> None:
    with pytest.raises(figures.FigureError, match="At most"):
        figures.store(tmp_path, [("x.png", _png(), "")] * (figures.MAX_FIGURES + 1))


# ---- what the model may reference ----------------------------------------------------


def test_extract_tex_allows_only_this_decks_figures() -> None:
    files = ["figures/fig-01.png"]
    ok = _deck(_frame("A", _FIG1))
    assert deck.extract_tex(ok, None, files) == ok
    # graphicx finds the file without its extension too
    deck.extract_tex(_deck(_frame("A", r"\includegraphics[width=3cm]{figures/fig-01}")), None, files)
    for bad in (
        r"\includegraphics{figures/fig-02.png}",
        r"\includegraphics{../20260101-000000-abcd/figures/fig-01.png}",
        r"\includegraphics{src/logo.png}",
    ):
        with pytest.raises(DeckError, match="does not exist"):
            deck.extract_tex(_deck(_frame("A", bad)), None, files)
    with pytest.raises(DeckError, match="no image files"):
        deck.extract_tex(ok, None, [])


def test_extract_tex_rejects_unverifiable_image_loading() -> None:
    files = ["figures/fig-01.png"]
    for bad in (
        r"\def\f{figures/fig-01.png}\includegraphics\f",
        r"\graphicspath{{../}}" + _FIG1,
        r"\pgfimage{figures/fig-01.png}",
    ):
        with pytest.raises(DeckError):
            deck.extract_tex(_deck(_frame("A", bad)), None, files)


def test_commented_includegraphics_is_ignored() -> None:
    deck.extract_tex(_deck(_frame("A", "% " + _FIG1)), None, [])


# ---- prompts ----------------------------------------------------------------------------


def test_generation_prompt_shows_images_after_text_material(tmp_path: Path) -> None:
    records = figures.store(tmp_path, [("chart.png", _png(2000, 500), "results slide")])
    template = deck.builtin_template("navy")
    request = deck.DeckRequest(template_key="navy", title="T", source_text="Notes.")
    messages = deck.build_generation_messages(
        template, request, [], figures.prompt_blocks(tmp_path, records)
    )
    material = messages[3]["content"]
    assert material[0] == {"type": "text", "text": "Notes."}
    described = " ".join(p.get("text", "") for p in material)
    assert "figures/fig-01.png" in described and "wide" in described and "results slide" in described
    assert sum(p["type"] == "image_url" for p in material) == 1
    # The model sees a downsized copy, not the full upload
    preview = tmp_path / figures.PREVIEW_DIR / "fig-01.png"
    assert fitz.Pixmap(str(preview)).width == 1024
    assert deck.PLACEMENT_RULE in messages[2]["content"]
    assert deck.PLACEMENT_RULE in messages[4]["content"]


def test_generation_prompt_without_images_forbids_them() -> None:
    template = deck.builtin_template("navy")
    request = deck.DeckRequest(template_key="navy", title="T", source_text="Notes.")
    messages = deck.build_generation_messages(template, request, [])
    assert messages[3]["content"] == "Notes."
    assert deck.PLACEMENT_RULE not in messages[2]["content"]
    assert "do not use \\includegraphics" in messages[2]["content"]
    assert deck.SOURCE_IMAGES_RULE in messages[2]["content"]


def test_jpeg_previews_are_labelled_jpeg(tmp_path: Path) -> None:
    records = figures.store(tmp_path, [("photo.jpg", _jpg(), "")])
    block = figures.prompt_blocks(tmp_path, records)[-1]
    assert block["image_url"]["url"].startswith("data:image/jpeg;base64,")


# ---- end to end through the API and the generation job ---------------------------


@pytest.fixture
def client(artifact_root: Path) -> TestClient:
    assert settings.artifact_root == artifact_root
    with TestClient(app) as c:
        yield c


def _create(client: TestClient) -> str:
    r = client.post(
        "/api/generations",
        data={"template_key": "navy", "source_text": "Notes.", "image_notes": ["on the title", ""]},
        files=[
            ("images", ("a.png", _png(), "image/png")),
            ("images", ("b.jpg", _jpg(), "image/jpeg")),
        ],
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_create_stores_images_and_regenerate_copies_them(
    client: TestClient, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)
    gen_id = _create(client)
    meta = client.get(f"/api/generations/{gen_id}").json()
    assert [f["name"] for f in meta["figures"]] == ["a.png", "b.jpg"]
    assert meta["figures"][0]["note"] == "on the title"
    assert (artifact_root / gen_id / "figures" / "fig-02.jpg").is_file()

    new_id = client.post(f"/api/generations/{gen_id}/regenerate").json()["id"]
    assert store.load_meta(new_id).figures == store.load_meta(gen_id).figures
    assert deck.figure_files(artifact_root / new_id) == ["figures/fig-01.png", "figures/fig-02.jpg"]


def test_create_rejects_a_bad_image_and_leaves_nothing(
    client: TestClient, artifact_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)
    r = client.post(
        "/api/generations",
        data={"template_key": "navy", "source_text": "Notes."},
        files=[("images", ("a.gif", b"GIF89a....", "image/gif"))],
    )
    assert r.status_code == 400 and "PNG or JPEG" in r.json()["detail"]
    assert list(artifact_root.iterdir()) == []


def test_generation_asks_again_for_an_unused_image(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)
    gen_id = _create(client)
    first = _deck(_frame("A", _FIG1))
    fixed = _deck(_frame("A", _FIG1), _frame("B", _FIG2))
    fake = FakeLlm([first, fixed])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)

    _run_generation(gen_id, lambda _s, _p: None)

    meta = store.load_meta(gen_id)
    assert meta.status == "succeeded" and meta.frame_count == 2
    assert (settings.artifact_root / gen_id / "main.tex").read_text(encoding="utf-8") == fixed
    fix_request = fake.calls[1][2]["content"]
    assert "figures/fig-02.jpg" in fix_request[0]["text"]
    assert [p["type"] for p in fix_request[2:]] == ["image_url"]  # only the unused one
    assert not (settings.artifact_root / gen_id / figures.PREVIEW_DIR).exists()


def test_layout_fix_that_breaks_compilation_is_dropped(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    fake_xelatex_ok: dict[str, int],
) -> None:
    monkeypatch.setattr(tasks, "start", lambda _gen_id, _job: None)
    gen_id = _create(client)
    first = _deck(_frame("A", _FIG1))
    broken = _deck(_frame("A", _FIG1 + r"\broken"), _frame("B", _FIG2))
    fake = FakeLlm([first, broken])
    monkeypatch.setattr("app.main.create_client", lambda on_usage=None: fake)

    def run(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
        if r"\broken" in tex_path.read_text(encoding="utf-8"):
            return False, "! Undefined control sequence."
        (work_dir / "main.pdf").write_bytes(b"%PDF-fake")
        return True, "ok"

    monkeypatch.setattr("app.deck._run_xelatex", run)
    monkeypatch.setattr(settings, "max_compile_repairs", 0)

    _run_generation(gen_id, lambda _s, _p: None)

    assert store.load_meta(gen_id).status == "succeeded"
    assert (settings.artifact_root / gen_id / "main.tex").read_text(encoding="utf-8") == first
