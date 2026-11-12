"""Learn: questions about a notebook, asked of the connected model.

A thread starts when text quoted from the source PDF or the notes is put into
the question box (or with a plain question); follow-ups stay in that thread.
Threads that quote something get a number, drawn next to the quoted text.
They live in threads.json beside the generation. Answers run in a background
thread, so a slow model never holds an HTTP request open.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import fitz

from .llm import LlmError, create_client, image_block, text_block

THREADS_FILE = "threads.json"
CACHE_DIR = "chat"
MAX_QUOTE = 4000
MAX_RECTS = 80
# Whole-source text up to this size goes with every question; above it, the
# pages around the quotes and the pages the notes cite nearby.
MAX_SOURCE_CHARS = 60_000
MAX_NOTES_CHARS = 40_000
MAX_PAGE_IMAGES = 3

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="texdeck-chat")

SYSTEM_PROMPT = (
    "You help a student study. They are reading a source document and study notes "
    "made from it, and ask about passages they quote or about the material in "
    "general. Answer the question directly, in the language the question is "
    "written in. Ground the answer in the material and cite source pages as "
    "(p. N); when you add knowledge the material does not contain, say so in a "
    "few words. Be concise: no preamble, no restating the question, no closing "
    "offers. Format with Markdown (short paragraphs, lists only when listing). "
    "Write maths as LaTeX: $...$ inline, $$...$$ for a display on its own lines; "
    "math-mode commands only, never \\begin{document}, \\section or text-mode macros."
)


class ChatError(RuntimeError):
    """User-facing English."""


def _now() -> float:
    return round(time.time(), 3)


def load(gdir: Path) -> dict[str, Any]:
    path = gdir / THREADS_FILE
    if not path.is_file():
        return {"threads": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"threads": []}
    return data if isinstance(data.get("threads"), list) else {"threads": []}


def _save(gdir: Path, data: dict[str, Any]) -> None:
    path = gdir / THREADS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _notes_stamp(gdir: Path) -> int:
    pdf = gdir / "main.pdf"
    return pdf.stat().st_mtime_ns if pdf.is_file() else 0


def clean_anchor(raw: dict[str, Any], sources: int, notes_stamp: int) -> dict[str, Any]:
    where = raw.get("where")
    if where not in ("source", "notes"):
        raise ChatError("A quote comes from the source or the notes")
    text = re.sub(r"\s+", " ", str(raw.get("text") or "")).strip()[:MAX_QUOTE]
    rects = []
    for r in list(raw.get("rects") or [])[:MAX_RECTS]:
        try:
            box = {k: round(min(1.0, max(0.0, float(r[k]))), 5) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError):
            continue
        rects.append(box)
    page = int(raw.get("page") or 0)
    doc = int(raw.get("doc") or 1) if where == "source" else 0
    if page < 1 or (where == "source" and not 1 <= doc <= sources):
        raise ChatError("The quote's page is not in this notebook")
    if not text and not rects:
        raise ChatError("The quote is empty")
    anchor = {"where": where, "doc": doc, "page": page, "text": text, "rects": rects}
    if where == "notes":
        anchor["stamp"] = notes_stamp
    return anchor


def ask(gdir: Path, question: str, thread_id: str | None, anchors: list[dict[str, Any]],
        sources: int) -> dict[str, Any]:
    """Record the question and an empty answer, then answer in the background."""
    question = question.strip()
    if not question:
        raise ChatError("Write a question")
    stamp = _notes_stamp(gdir)
    question_id = uuid.uuid4().hex[:10]
    quotes = [{**clean_anchor(a, sources, stamp), "msg": question_id} for a in anchors]
    with _lock:
        data = load(gdir)
        threads = data["threads"]
        if thread_id:
            thread = next((t for t in threads if t["id"] == thread_id), None)
            if thread is None:
                raise ChatError("That conversation no longer exists")
            if any(m["status"] == "pending" for m in thread["messages"]):
                raise ChatError("Wait for the answer before asking again")
            thread["anchors"].extend(quotes)
            if quotes and not thread["number"]:
                thread["number"] = 1 + max((t["number"] for t in threads), default=0)
        else:
            thread = {
                "id": uuid.uuid4().hex[:10],
                "number": 1 + max((t["number"] for t in threads), default=0) if quotes else 0,
                "anchors": quotes,
                "messages": [],
                "created_at": _now(),
            }
            threads.append(thread)
        thread["messages"].append({"id": question_id, "role": "user", "content": question,
                                   "status": "done", "at": _now()})
        answer_id = uuid.uuid4().hex[:10]
        thread["messages"].append({"id": answer_id, "role": "assistant", "content": "",
                                   "status": "pending", "at": _now()})
        _save(gdir, data)
    _pool.submit(contextvars.copy_context().run, _answer, gdir, thread["id"], answer_id)
    return thread


def delete(gdir: Path, thread_id: str) -> None:
    with _lock:
        data = load(gdir)
        data["threads"] = [t for t in data["threads"] if t["id"] != thread_id]
        _save(gdir, data)


def threads_for_view(gdir: Path) -> list[dict[str, Any]]:
    """The threads, with quotes from the notes moved to where their text now
    is after the notes were edited (or marked stale when it is gone)."""
    stamp = _notes_stamp(gdir)
    with _lock:
        data = load(gdir)
        stale = [a for t in data["threads"] for a in t["anchors"]
                 if a["where"] == "notes" and a.get("stamp") != stamp]
        if stale and stamp:
            with fitz.open(gdir / "main.pdf") as pdf:
                for anchor in stale:
                    _relocate(anchor, pdf)
                    anchor["stamp"] = stamp
            _save(gdir, data)
        # An answer whose worker died with the server would wait forever.
        for t in data["threads"]:
            for m in t["messages"]:
                if m["status"] == "pending" and time.time() - m["at"] > 1800:
                    m.update(status="error", content="No answer arrived; ask again.")
        return data["threads"]


def _relocate(anchor: dict[str, Any], pdf: fitz.Document) -> None:
    text = anchor["text"]
    old_y = anchor["rects"][0]["y"] if anchor["rects"] else 0.0
    pages = sorted(range(1, pdf.page_count + 1), key=lambda n: abs(n - anchor["page"]))
    for needle in (text[:60], text[:24], text[:12]):
        if len(needle.strip()) < 4:
            continue
        for n in pages:
            page = pdf.load_page(n - 1)
            hits = page.search_for(needle)
            if hits:
                w, h = page.rect.width or 1.0, page.rect.height or 1.0
                best = min(hits, key=lambda r: abs(r.y0 / h - old_y) if n == anchor["page"] else 0)
                anchor.update(page=n, gone=False, rects=[{
                    "x": round(best.x0 / w, 5), "y": round(best.y0 / h, 5),
                    "w": round(best.width / w, 5), "h": round(best.height / h, 5),
                }])
                return
    anchor["gone"] = True
    anchor["rects"] = []


# ---- answering ---------------------------------------------------------------


def _answer(gdir: Path, thread_id: str, answer_id: str) -> None:
    try:
        thread = next(t for t in load(gdir)["threads"] if t["id"] == thread_id)
        messages = build_messages(gdir, thread)
        content = create_client().complete(messages).strip()
        status = "done"
    except (LlmError, ChatError) as exc:
        content, status = str(exc), "error"
    except StopIteration:
        return  # deleted meanwhile
    except Exception as exc:  # noqa: BLE001 - a failed answer must still end
        content, status = f"Could not answer: {exc}", "error"
    with _lock:
        data = load(gdir)
        for t in data["threads"]:
            for m in t["messages"]:
                if m["id"] == answer_id:
                    m.update(content=content, status=status, at=_now())
        _save(gdir, data)


def build_messages(gdir: Path, thread: dict[str, Any]) -> list[dict[str, Any]]:
    """System rules; the material (notes source, source text, quoted pages as
    images); then this thread's turns, quotes attached to the turn they came with."""
    material: list[dict[str, Any]] = []
    notes = _notes_body(gdir)
    if notes:
        material.append(text_block(f"--- The study notes (LaTeX source) ---\n{notes}"))
    quoted_pages = sorted({(a["doc"], a["page"]) for a in thread["anchors"] if a["where"] == "source"})
    material.append(text_block(_source_text(gdir, quoted_pages)))
    for doc, page in quoted_pages[:MAX_PAGE_IMAGES]:
        image = _page_image(gdir, doc, page)
        if image is not None:
            material.append(text_block(f"Image of source page {page}" + (f" (document {doc})" if doc > 1 else "")))
            material.append(image_block(image))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": material},
    ]
    for m in thread["messages"]:
        if m["role"] == "user":
            quotes = [a for a in thread["anchors"] if a.get("msg") == m["id"]]
            messages.append({"role": "user", "content": _with_quotes(m["content"], quotes)})
        elif m["status"] == "done" and m["content"]:
            messages.append({"role": "assistant", "content": m["content"]})
    return messages


def _with_quotes(question: str, quotes: list[dict[str, Any]]) -> str:
    if not quotes:
        return question
    parts = []
    for q in quotes:
        where = (f"the source, page {q['page']}" + (f" of document {q['doc']}" if q["doc"] > 1 else "")
                 if q["where"] == "source" else f"the notes, page {q['page']}")
        parts.append(f"Quoted from {where}:\n> {q['text']}")
    return "\n\n".join(parts) + f"\n\nQuestion: {question}"


def _notes_body(gdir: Path) -> str:
    path = gdir / "main.tex"
    if not path.is_file():
        return ""
    tex = path.read_text(encoding="utf-8")
    body = tex.split("\\begin{document}", 1)[-1].split("\\end{document}", 1)[0]
    body = re.sub(r"\\src\{[^}]*\}", "", body)
    return body.strip()[:MAX_NOTES_CHARS]


def _source_docs(gdir: Path) -> list[Path]:
    return sorted(gdir.glob("source_*.pdf"))


def _source_text(gdir: Path, quoted: list[tuple[int, int]]) -> str:
    text_path = gdir / "source.txt"
    if text_path.is_file():
        body = text_path.read_text(encoding="utf-8")
        return f"--- The source (pasted text) ---\n{body[:MAX_SOURCE_CHARS]}"
    pages: list[tuple[int, int, str]] = []
    for d, path in enumerate(_source_docs(gdir), start=1):
        with fitz.open(path) as pdf:
            for n in range(1, pdf.page_count + 1):
                pages.append((d, n, pdf.load_page(n - 1).get_text("text").strip()))
    total = sum(len(t) for _, _, t in pages)
    if total > MAX_SOURCE_CHARS:
        near = {(d, n + k) for d, n in quoted for k in (-1, 0, 1)}
        kept = [p for p in pages if (p[0], p[1]) in near]
        budget = MAX_SOURCE_CHARS - sum(len(t) for _, _, t in kept)
        for p in pages:  # then the opening pages, which set up the terms
            if p in kept or budget < len(p[2]):
                continue
            kept.append(p)
            budget -= len(p[2])
        pages = sorted(kept)
        head = "--- The source text (extract: the quoted pages and the opening pages) ---"
    else:
        head = "--- The source text ---"
    multi = len({d for d, _, _ in pages}) > 1
    blocks = [f"[{'document %d, ' % d if multi else ''}page {n}]\n{t or '(no text layer: scanned page)'}"
              for d, n, t in pages]
    return head + "\n" + "\n\n".join(blocks)


def _page_image(gdir: Path, doc: int, page: int) -> Path | None:
    path = gdir / f"source_{doc:02d}.pdf"
    if not path.is_file():
        return None
    out = gdir / CACHE_DIR / f"page_{doc:02d}_{page:04d}.png"
    if not out.is_file():
        with fitz.open(path) as pdf:
            if not 1 <= page <= pdf.page_count:
                return None
            out.parent.mkdir(exist_ok=True)
            pdf.load_page(page - 1).get_pixmap(dpi=110).save(out)
    return out
