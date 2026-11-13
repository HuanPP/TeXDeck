"""Learn: questions about a notebook, kept as numbered threads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz
import pytest

from app import chat


class _Now:
    """Runs answers inline instead of on the pool."""

    def submit(self, fn: Any, *args: Any) -> None:
        fn(*args)


class _FakeClient:
    def __init__(self, reply: str = "Answer with $x^2$.", fail: bool = False) -> None:
        self.reply, self.fail, self.seen = reply, fail, []

    def complete(self, messages: list[dict[str, Any]]) -> str:
        self.seen.append(messages)
        if self.fail:
            raise chat.LlmError("Model unavailable")
        return self.reply


def _pdf(path: Path, pages: list[str]) -> None:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 300), text, fontsize=12)
    doc.save(path)


@pytest.fixture()
def gdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _pdf(tmp_path / "source_01.pdf", ["Residual learning eases training.", "Shortcut connections add identity."])
    _pdf(tmp_path / "main.pdf", ["Notes: residual blocks", "Notes: training setup"])
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n\\begin{notesheet}{Res}\\src{1}\nbody\n"
        "\\end{notesheet}\n\\end{document}\n", encoding="utf-8")
    monkeypatch.setattr(chat, "_pool", _Now())
    return tmp_path


def _quote(where: str = "source", page: int = 1, text: str = "Residual learning") -> dict[str, Any]:
    return {"where": where, "doc": 1, "page": page, "text": text, "rects": [{"x": 0.1, "y": 0.3, "w": 0.4, "h": 0.02}]}


def test_quoted_questions_get_numbers_and_follow_ups_do_not(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "create_client", lambda: _FakeClient())
    first = chat.ask(gdir, "Why?", None, [_quote()], sources=1)
    chat.ask(gdir, "And then?", first["id"], [], sources=1)
    plain = chat.ask(gdir, "General question", None, [], sources=1)
    second = chat.ask(gdir, "What is this?", None, [_quote("notes", 2, "training setup")], sources=1)
    threads = chat.threads_for_view(gdir)
    assert [t["number"] for t in threads] == [1, 0, 2]
    assert first["number"] == 1 and plain["number"] == 0 and second["number"] == 2
    assert [m["role"] for m in threads[0]["messages"]] == ["user", "assistant", "user", "assistant"]
    assert all(m["status"] == "done" and m["content"] for m in threads[0]["messages"])


def test_numbers_are_not_reused_after_a_delete(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "create_client", lambda: _FakeClient())
    one = chat.ask(gdir, "a", None, [_quote()], sources=1)
    chat.ask(gdir, "b", None, [_quote()], sources=1)
    chat.delete(gdir, one["id"])
    assert chat.ask(gdir, "c", None, [_quote()], sources=1)["number"] == 3


def test_the_model_sees_the_material_and_the_quote_with_its_question(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(chat, "create_client", lambda: client)
    thread = chat.ask(gdir, "Why does it help?", None, [_quote()], sources=1)
    chat.ask(gdir, "Show the formula", thread["id"], [], sources=1)
    first, follow = client.seen
    material = " ".join(p.get("text", "") for p in first[1]["content"])
    assert "[page 1]" in material and "Shortcut connections add identity." in material
    assert "\\src" not in material and "\\begin{notesheet}{Res}" in material
    assert any(p["type"] == "image_url" for p in first[1]["content"])  # the quoted page itself
    assert "Quoted from the source, page 1" in first[-1]["content"] and first[-1]["content"].endswith("Why does it help?")
    # The follow-up carries the earlier turn; the quote stays with the first question.
    assert [m["role"] for m in follow[2:]] == ["user", "assistant", "user"]
    assert follow[-1]["content"] == "Show the formula"


def test_a_failed_answer_is_reported_in_the_thread(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "create_client", lambda: _FakeClient(fail=True))
    chat.ask(gdir, "Why?", None, [], sources=1)
    answer = chat.threads_for_view(gdir)[0]["messages"][-1]
    assert answer["status"] == "error" and "unavailable" in answer["content"]


def test_bad_quotes_are_refused(gdir: Path) -> None:
    with pytest.raises(chat.ChatError):
        chat.ask(gdir, "Why?", None, [_quote(page=0)], sources=1)
    with pytest.raises(chat.ChatError):
        chat.ask(gdir, "Why?", None, [{**_quote(), "doc": 2}], sources=1)
    with pytest.raises(chat.ChatError):
        chat.ask(gdir, "  ", None, [], sources=1)


def test_quotes_from_the_notes_follow_their_text_after_a_recompile(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "create_client", lambda: _FakeClient())
    chat.ask(gdir, "Why?", None, [_quote("notes", 2, "Notes: training setup")], sources=1)
    chat.ask(gdir, "And this?", None, [_quote("notes", 1, "Notes: residual blocks")], sources=1)
    # The edit moved the training part to page 1 and dropped the residual part.
    _pdf(gdir / "main.pdf", ["Notes: training setup", "Something else"])
    import os
    os.utime(gdir / "main.pdf", ns=(1, 1))
    moved, gone = (t["anchors"][0] for t in chat.threads_for_view(gdir))
    assert moved["page"] == 1 and not moved.get("gone") and moved["rects"]
    assert gone["gone"] is True and gone["rects"] == []


def test_an_area_quote_sends_a_crop_of_its_page(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient()
    monkeypatch.setattr(chat, "create_client", lambda: client)
    box = {"x": 0.05, "y": 0.3, "w": 0.6, "h": 0.1}
    chat.ask(gdir, "What does this figure show?", None,
             [{"where": "source", "doc": 1, "page": 2, "text": "ignored", "rects": [box], "area": True}], sources=1)
    anchor = chat.threads_for_view(gdir)[0]["anchors"][0]
    assert anchor["area"] and anchor["text"] == ""
    crop = chat.crop_path(gdir, anchor["image"])
    assert crop is not None
    with fitz.open(crop) as img:
        assert img[0].rect.width > img[0].rect.height  # the wide box, not the page
    question = client.seen[0][-1]["content"]
    assert question[0]["text"].startswith("Quoted an area of the source, page 2")
    assert question[1]["type"] == "image_url"
    assert chat.crop_path(gdir, "../threads.json") is None


def test_an_area_quote_is_one_box(gdir: Path) -> None:
    with pytest.raises(chat.ChatError):
        chat.ask(gdir, "Why?", None, [{**_quote(), "area": True, "rects": []}], sources=1)


def test_a_new_session_clears_the_conversation_and_restarts_numbers(gdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat, "create_client", lambda: _FakeClient())
    chat.ask(gdir, "Why?", None, [_quote()], 1)
    chat.ask(gdir, "And this?", None, [_quote(page=2, text="Shortcut")], 1)
    assert chat.clear(gdir) == 2
    assert chat.load(gdir)["threads"] == []
    assert (gdir / "threads.cleared.json").is_file()
    assert chat.ask(gdir, "Again", None, [_quote()], 1)["number"] == 1
