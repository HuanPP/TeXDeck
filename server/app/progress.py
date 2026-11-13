"""Learn: the parts of the notes a reader has marked as learnt.

Keys come from the browser (the heading titles of the notes); the server only
stores them, in progress.json beside the generation.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PROGRESS_FILE = "progress.json"
MAX_KEYS = 500
MAX_KEY = 300


def load(gdir: Path) -> dict[str, Any]:
    try:
        data = json.loads((gdir / PROGRESS_FILE).read_text(encoding="utf-8"))
        done = [k for k in data.get("done", []) if isinstance(k, str)]
    except (OSError, ValueError, AttributeError):
        done = []
    return {"done": done}


def save(gdir: Path, done: list[str]) -> dict[str, Any]:
    keys = list(dict.fromkeys(k[:MAX_KEY] for k in done if k.strip()))[:MAX_KEYS]
    path = gdir / PROGRESS_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": keys, "at": round(time.time(), 3)}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return {"done": keys}
