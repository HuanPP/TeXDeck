"""What the model remembers about a user across decks.

Two lists of short lines: habits (lasting preferences: length, tone, layout
taste) and mistakes (kinds of error the user had to fix, stated as rules).
The model adds to them on its own: edit and revision prompts invite a
<memory> block after the reply when a request shows something lasting, and
the server cuts that block out and keeps the new lines. Every generation and
edit prompt carries the lists. The user can rewrite them on the page.

Kept per account (accounts/<name>/memory.json), or in settings.memory_file
without accounts.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any

from . import auth
from .config import settings

KINDS = ("habits", "mistakes")
MAX_LINES = 40  # per kind; the oldest go first
MAX_LINE_CHARS = 200

RULE = (
    "Memory: after your output you may add one block, <memory> ... </memory>, on its own "
    "lines at the very end, with one entry per line, each starting with `habit:` or `mistake:`. "
    "Add a habit when this request shows a lasting preference of this user that should apply "
    "to their future decks too (length, tone, wording, layout taste), not something about this "
    "slide only. Add a mistake when the user had to fix a kind of error that slide generators "
    "make, written as a short general rule that prevents it next time (for example: chart "
    "labels must not overlap). Do not repeat anything already remembered. Most requests show "
    "nothing new; then leave the block out. Write each entry in the user's language, in under "
    "120 characters."
)

_BLOCK_RE = re.compile(r"\s*<memory>(.*?)</memory>\s*", re.DOTALL | re.IGNORECASE)
_ENTRY_RE = re.compile(r"^\s*[-*]?\s*(habit|mistake)s?\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_lock = threading.Lock()


def _path() -> Path:
    account = auth.user_dir()
    return account / "memory.json" if account is not None else settings.memory_file


def load() -> dict[str, list[str]]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {kind: [str(x) for x in data.get(kind, []) if str(x).strip()] for kind in KINDS}


def _clean(lines: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = " ".join(str(line).split())[:MAX_LINE_CHARS]
        key = _key(text)
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out[-MAX_LINES:]


def _key(text: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def save(data: dict[str, Any]) -> dict[str, list[str]]:
    """Replace the lists (the page's editor); returns what was kept."""
    clean = {kind: _clean(list(data.get(kind) or [])) for kind in KINDS}
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return clean


def remember(new: dict[str, list[str]]) -> None:
    """Add the model's new entries, skipping ones already kept (in any wording case)."""
    if not any(new.get(kind) for kind in KINDS):
        return
    with _lock:
        data = load()
        for kind in KINDS:
            data[kind] = data[kind] + list(new.get(kind) or [])
        save(data)


def note() -> str:
    """The lists as prompt text; "" when nothing is remembered."""
    data = load()
    parts = []
    if data["habits"]:
        parts.append("This user's habits (follow them unless the request says otherwise):\n"
                     + "\n".join(f"- {h}" for h in data["habits"]))
    if data["mistakes"]:
        parts.append("Mistakes this user had to fix before (avoid them):\n"
                     + "\n".join(f"- {m}" for m in data["mistakes"]))
    return "\n\n".join(parts)


def edit_context() -> str:
    """For prompts that may add to memory: what is remembered, then the rule."""
    known = note()
    return (known + "\n\n" if known else "Nothing is remembered about this user yet.\n\n") + RULE


def split_reply(response: str) -> tuple[str, dict[str, list[str]]]:
    """Cut the <memory> block(s) out of a reply; returns (reply, new entries)."""
    found: dict[str, list[str]] = {kind: [] for kind in KINDS}

    def take(match: re.Match[str]) -> str:
        for line in match.group(1).splitlines():
            entry = _ENTRY_RE.match(line)
            if entry:
                kind = "habits" if entry.group(1).lower() == "habit" else "mistakes"
                found[kind].append(entry.group(2))
        return "\n"

    if not _BLOCK_RE.search(response):
        return response, found
    return _BLOCK_RE.sub(take, response).strip() + "\n", found
