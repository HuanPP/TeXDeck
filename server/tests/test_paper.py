"""Papering projects: paths, links, imports, saves and the HTTP routes."""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import paper, store
from app.main import app


def _zip(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name, target in (links or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            z.writestr(info, target)
    return buf.getvalue()


def test_create_blank_project(artifact_root: Path) -> None:
    meta = paper.create("Fish & Chips_1", owner="")
    assert meta.kind == "paper" and meta.status == "succeeded"
    text = (paper.root(meta.id) / "main.tex").read_text()
    assert r"\title{Fish \& Chips\_1}" in text
    assert paper.load_settings(meta.id) == {"engine": "xelatex", "main": "main.tex"}


@pytest.mark.parametrize("bad", ["../x.tex", "a/../../x", ".git/config", "a/.hidden", "/", "", "a\\b", "c:x"])
def test_clean_path_refuses_escapes_and_hidden(bad: str) -> None:
    with pytest.raises(paper.PaperError):
        paper.clean_path(bad)


def test_symlinks_are_never_followed(artifact_root: Path, tmp_path: Path) -> None:
    meta = paper.create("Links", owner="")
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    os.symlink(secret, paper.root(meta.id) / "leak.tex")
    os.symlink(tmp_path, paper.root(meta.id) / "dir")
    with pytest.raises(paper.PaperError):
        paper.read_source(meta.id, "leak.tex")
    with pytest.raises(paper.PaperError):
        paper.read_source(meta.id, "dir/secret.txt")
    assert [e["path"] for e in paper.entries(meta.id)] == ["main.tex"]
    names = zipfile.ZipFile(io.BytesIO(paper.export_zip(meta.id))).namelist()
    assert names == ["main.tex"]


def test_import_zip_strips_top_folder_and_skips_links(artifact_root: Path) -> None:
    data = _zip(
        {
            "thesis/chapters/intro.tex": b"\\section{Intro}",
            "thesis/thesis.tex": b"\\documentclass{article}\\begin{document}\\input{chapters/intro}\\end{document}",
            "thesis/.latexmkrc": b"$x=1;",
            "__MACOSX/thesis/._thesis.tex": b"junk",
        },
        links={"thesis/evil.tex": "/etc/passwd"},
    )
    meta = paper.create("Thesis", owner="", archive=data)
    paths = {e["path"] for e in paper.entries(meta.id)}
    assert paths == {"chapters", "chapters/intro.tex", "thesis.tex"}
    assert paper.load_settings(meta.id)["main"] == "thesis.tex"


def test_import_without_tex_removes_the_project(artifact_root: Path) -> None:
    with pytest.raises(paper.PaperError):
        paper.create("Empty", owner="", archive=_zip({"a.png": b"x"}))
    assert store.list_generations() == []


def test_save_checks_the_revision(artifact_root: Path) -> None:
    meta = paper.create("Save", owner="")
    src = paper.read_source(meta.id, "main.tex")
    rev = paper.save_source(meta.id, "main.tex", "one", src["revision"])
    with pytest.raises(paper.PaperError) as stale:
        paper.save_source(meta.id, "main.tex", "two", src["revision"])
    assert stale.value.status == 409
    paper.save_source(meta.id, "main.tex", "two", rev)
    assert paper.read_source(meta.id, "main.tex")["text"] == "two"


def test_rename_moves_the_main_file_setting(artifact_root: Path) -> None:
    meta = paper.create("Rename", owner="")
    paper.rename_path(meta.id, "main.tex", "src/paper.tex")
    assert paper.load_settings(meta.id)["main"] == "src/paper.tex"
    with pytest.raises(paper.PaperError):
        paper.rename_path(meta.id, "src", "src/inner")


def test_fingerprint_changes_with_files(artifact_root: Path) -> None:
    meta = paper.create("Watch", owner="")
    before = paper.fingerprint(meta.id)
    paper.new_file(meta.id, "refs.bib")
    assert paper.fingerprint(meta.id) != before


def test_first_error_prefers_the_earliest() -> None:
    log = "./main.tex:3: Undefined control sequence.\n! Emergency stop.\n"
    assert paper.first_error(log) == "main.tex:3: Undefined control sequence."
    assert paper.first_error("! LaTeX Error: File `x.sty' not found.\n./a.tex:9: Emergency stop.") == \
        "LaTeX Error: File `x.sty' not found."


def test_routes_round_trip(artifact_root: Path) -> None:
    client = TestClient(app)
    r = client.post("/api/papers", data={"title": "Routes"})
    assert r.status_code == 201
    gid = r.json()["id"]
    project = client.get(f"/api/papers/{gid}").json()
    assert project["settings"]["main"] == "main.tex" and project["build"]["pdf"] == ""
    src = client.get(f"/api/papers/{gid}/source", params={"path": "main.tex"}).json()
    r = client.put(f"/api/papers/{gid}/source", params={"path": "main.tex"}, json={"text": "x", "revision": src["revision"]})
    assert r.status_code == 200
    r = client.post(f"/api/papers/{gid}/upload", data={"folder": "figs"}, files={"files": ("a.png", b"png")})
    assert r.json()["saved"] == ["figs/a.png"]
    assert client.get(f"/api/papers/{gid}/asset", params={"path": "figs/a.png"}).content == b"png"
    assert client.get(f"/api/papers/{gid}/source", params={"path": "../meta.json"}).status_code == 400
    # A deck is not a paper project.
    deck = store.new_generation(title="d", template_key="k", template_name="k", instruction="", source_kind="text",
                                source_names=[])
    assert client.get(f"/api/papers/{deck.id}").status_code == 404


@pytest.mark.slow
@pytest.mark.skipif(not (shutil.which("bwrap") and shutil.which("latexmk") and shutil.which("synctex")),
                    reason="needs bwrap, latexmk and synctex")
def test_compile_in_sandbox_and_synctex(artifact_root: Path) -> None:
    meta = paper.create("Compile", owner="")
    state = paper.compile_project(meta.id)
    assert state.ok and state.pdf
    at = paper.forward(meta.id, "main.tex", 15)
    back = paper.inverse(meta.id, int(at["page"]), at["x"], at["y"])
    assert back == {"path": "main.tex", "line": 15}
    src = paper.read_source(meta.id, "main.tex")
    paper.save_source(meta.id, "main.tex", src["text"].replace("Start writing here.", r"\input{/etc/hostname}"),
                      src["revision"])
    state = paper.compile_project(meta.id)
    assert not state.ok and "not found" in state.error
