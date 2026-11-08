"""Custom templates: a built-in pack with its own colours, background and logos.

A custom template is a small JSON spec (base pack, colours, image ids, the
editor's own state) plus images in a content-addressed store. Nothing of the
base is copied: deck.copy_template_bundle copies the base pack and lays the
custom files over it (Template.extra), so a fix to the base reaches every
template derived from it.

Each account keeps its own under accounts/<name>/templates/; without accounts
they live in settings.custom_templates_dir. Deleting only hides a template, so
decks made with it can still be edited and regenerated.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fitz

from . import auth, deck
from .config import settings

KEY_PREFIX = "my-"
_KEY_RE = re.compile(r"my-[0-9a-f]{10}")
_ASSET_RE = re.compile(r"[0-9a-f]{20}\.(png|jpg)")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")

# Image slot -> where the base pack keeps that image.
IMAGE_FILES = {
    "background": "src/brand/background.png",
    "logo": "src/brand/color-logo.png",
    "trans_logo": "src/brand/trans-logo.png",
}
BACKGROUND_SIZE = (1920, 1080)  # the editor exports exactly this
MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_ASSET_SIDE = 4096
MAX_EDITOR_STATE = 64 * 1024  # bytes of JSON
MAX_NAME = 60
_STALE_UPLOAD_SECONDS = 24 * 3600


class CustomTemplateError(ValueError):
    """Bad input; the message is shown to the user."""


class TemplateNotFound(CustomTemplateError):
    """No such custom template for this user (or it was deleted)."""


@dataclass(slots=True)
class Draft:
    base: str
    name: str
    colors: dict[str, str]  # slot -> "#RRGGBB", only slots the base offers
    images: dict[str, str]  # slot -> asset id, only replaced images
    editor: dict[str, Any]  # the editor's own state (sources, layer, fill), opaque here


def is_custom_key(key: str) -> bool:
    return key.startswith(KEY_PREFIX)


def root() -> Path:
    account = auth.user_dir()
    return account / "templates" if account else settings.custom_templates_dir


def _assets() -> Path:
    return root() / "assets"


def asset_path(asset_id: str) -> Path:
    if not _ASSET_RE.fullmatch(asset_id):
        raise CustomTemplateError("Unknown image")
    path = _assets() / asset_id
    if not path.is_file():
        raise CustomTemplateError("Unknown image; upload it again")
    return path


# ---- bases ----------------------------------------------------------------------


def bases() -> list[deck.Template]:
    """Built-in packs that can be customised (their template.json says how)."""
    return [t for t in deck.list_templates() if t.meta.get("customize")]


def base_info(template: deck.Template) -> dict[str, Any]:
    customize = template.meta["customize"]
    return {
        "key": template.key,
        "name": template.name,
        "colors": customize.get("colors", []),
        "images": customize.get("images", {}),
    }


def _base(key: str) -> deck.Template:
    try:
        template = deck.builtin_template(key)
    except ValueError as exc:
        raise CustomTemplateError(f"Unknown base template: {key}") from exc
    if not template.meta.get("customize"):
        raise CustomTemplateError(f"{template.name} cannot be customised")
    return template


# ---- images ---------------------------------------------------------------------


def save_asset(data: bytes) -> dict[str, Any]:
    """Check an uploaded PNG/JPEG, re-encode it (dropping metadata) and store it."""
    if len(data) > MAX_ASSET_BYTES:
        raise CustomTemplateError(f"Image is larger than {MAX_ASSET_BYTES // (1024 * 1024)} MB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    else:
        raise CustomTemplateError("Use a PNG or JPEG image")
    try:
        pixmap = fitz.Pixmap(data)
    except Exception as exc:  # MuPDF raises its own error types
        raise CustomTemplateError("The image could not be read") from exc
    if max(pixmap.width, pixmap.height) > MAX_ASSET_SIDE:
        raise CustomTemplateError(f"Image is larger than {MAX_ASSET_SIDE} pixels on a side")
    if ext == "jpg" and pixmap.alpha:
        pixmap = fitz.Pixmap(pixmap, 0)
    clean = pixmap.tobytes("png") if ext == "png" else pixmap.tobytes("jpg", jpg_quality=92)

    asset_id = f"{hashlib.sha256(clean).hexdigest()[:20]}.{ext}"
    folder = _assets()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / asset_id
    if not path.is_file():
        _write_atomic(path, clean)
    else:
        os.utime(path)  # fresh again: not pruned while a draft still uses it
    _prune_assets()
    return {"id": asset_id, "width": pixmap.width, "height": pixmap.height}


def _referenced_assets() -> set[str]:
    used: set[str] = set()
    for spec in _all_specs():
        used.update(spec.get("images", {}).values())
        used.update(_editor_assets(spec.get("editor", {})))
    return used


def _editor_assets(editor: Any) -> set[str]:
    """Asset ids anywhere in the editor state (it keeps the original uploads)."""
    found: set[str] = set()
    if isinstance(editor, dict):
        for value in editor.values():
            found |= _editor_assets(value)
    elif isinstance(editor, list):
        for value in editor:
            found |= _editor_assets(value)
    elif isinstance(editor, str) and _ASSET_RE.fullmatch(editor):
        found.add(editor)
    return found


def _prune_assets() -> None:
    """Drop uploads no template uses once they are a day old (abandoned drafts)."""
    folder = _assets()
    cutoff = time.time() - _STALE_UPLOAD_SECONDS
    stale = [p for p in folder.iterdir() if p.is_file() and p.stat().st_mtime < cutoff]
    if not stale:
        return
    used = _referenced_assets()
    for path in stale:
        if path.name not in used:
            path.unlink(missing_ok=True)


# ---- drafts and templates -------------------------------------------------------


def parse_draft(body: dict[str, Any]) -> Draft:
    """Validate what the editor sends; every value that reaches TeX is checked here."""
    base = _base(str(body.get("base") or ""))
    customize = base.meta["customize"]

    name = " ".join(str(body.get("name") or "").split())[:MAX_NAME] or f"My {base.name}"

    slots = {c["name"]: c["default"] for c in customize.get("colors", [])}
    colors: dict[str, str] = {}
    raw_colors = body.get("colors") or {}
    if not isinstance(raw_colors, dict):
        raise CustomTemplateError("colors must be an object")
    for slot, value in raw_colors.items():
        if slot not in slots:
            raise CustomTemplateError(f"{base.name} has no colour named {slot}")
        if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
            raise CustomTemplateError(f"Colour {slot} must look like #1A2B3C")
        if value.upper() != slots[slot].upper():  # defaults stay with the base
            colors[slot] = value.upper()

    images: dict[str, str] = {}
    raw_images = body.get("images") or {}
    if not isinstance(raw_images, dict):
        raise CustomTemplateError("images must be an object")
    offered = customize.get("images", {})
    for slot, asset_id in raw_images.items():
        if slot not in offered:
            raise CustomTemplateError(f"{base.name} has no image named {slot}")
        if asset_id is None:
            continue
        path = asset_path(str(asset_id))
        if slot == "background":
            size = _image_size(path)
            if size != BACKGROUND_SIZE:
                raise CustomTemplateError(
                    f"The background must be {BACKGROUND_SIZE[0]}x{BACKGROUND_SIZE[1]} pixels"
                )
            if path.suffix != ".png":
                raise CustomTemplateError("The background must be a PNG image")
        elif path.suffix != ".png":
            raise CustomTemplateError("Logos must be PNG images")
        images[slot] = path.name

    editor = body.get("editor") or {}
    if not isinstance(editor, dict):
        raise CustomTemplateError("editor must be an object")
    if len(json.dumps(editor)) > MAX_EDITOR_STATE:
        raise CustomTemplateError("Editor state is too large")
    for asset_id in _editor_assets(editor):
        asset_path(asset_id)
    return Draft(base=base.key, name=name, colors=colors, images=images, editor=editor)


def _image_size(path: Path) -> tuple[int, int]:
    pixmap = fitz.Pixmap(str(path))
    return pixmap.width, pixmap.height


def colors_tex(colors: dict[str, str]) -> bytes:
    lines = ["% Written by the TeXDeck template editor."]
    for slot, value in sorted(colors.items()):
        r, g, b = (int(value[i : i + 2], 16) for i in (1, 3, 5))
        lines.append(f"\\definecolor{{{slot}}}{{RGB}}{{{r},{g},{b}}}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_template(draft: Draft, key: str) -> deck.Template:
    """The base pack with the draft's colours and images laid over it."""
    base = _base(draft.base)
    extra: dict[str, bytes | Path] = {}
    if draft.colors:
        extra["brand-colors.tex"] = colors_tex(draft.colors)
    for slot, asset_id in draft.images.items():
        extra[IMAGE_FILES[slot]] = asset_path(asset_id)
    return deck.Template(
        key=key,
        name=draft.name,
        dir=base.dir,
        main_tex=base.main_tex,
        meta=base.meta,
        extra=extra,
        base=base.key,
    )


def preview_pdf(draft: Draft) -> bytes:
    return deck.render_template_pdf(build_template(draft, "draft"))


def _spec_path(key: str) -> Path:
    if not _KEY_RE.fullmatch(key):
        raise TemplateNotFound(f"Unknown template: {key}")
    return root() / f"{key}.json"


def _read_spec(key: str) -> dict[str, Any]:
    path = _spec_path(key)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TemplateNotFound(f"Unknown template: {key}") from exc


def _all_specs() -> list[dict[str, Any]]:
    folder = root()
    if not folder.is_dir():
        return []
    specs = []
    for path in sorted(folder.glob(f"{KEY_PREFIX}*.json")):
        try:
            specs.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return specs


def _draft_of(spec: dict[str, Any]) -> Draft:
    return Draft(
        base=spec["base"],
        name=spec["name"],
        colors=dict(spec.get("colors", {})),
        images=dict(spec.get("images", {})),
        editor=dict(spec.get("editor", {})),
    )


def load(key: str) -> deck.Template:
    """The template for generation and editing; deleted ones still load.
    Raises ValueError (TemplateNotFound) for keys this user does not have."""
    spec = _read_spec(key)
    return build_template(_draft_of(spec), key)


def list_custom() -> list[dict[str, Any]]:
    return [
        {"key": s["key"], "name": s["name"], "base": s["base"], "custom": True}
        for s in sorted(_all_specs(), key=lambda s: s.get("created_at", ""))
        if not s.get("deleted")
    ]


def get(key: str) -> dict[str, Any]:
    spec = _read_spec(key)
    if spec.get("deleted"):
        raise TemplateNotFound(f"Unknown template: {key}")
    return spec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _spec(draft: Draft, key: str, created_at: str) -> dict[str, Any]:
    return {
        "key": key,
        "name": draft.name,
        "base": draft.base,
        "colors": draft.colors,
        "images": draft.images,
        "editor": draft.editor,
        "created_at": created_at,
        "updated_at": _now(),
    }


def create(draft: Draft) -> dict[str, Any]:
    root().mkdir(parents=True, exist_ok=True)
    while True:
        key = KEY_PREFIX + secrets.token_hex(5)
        if not _spec_path(key).exists():
            break
    spec = _spec(draft, key, _now())
    _write_atomic(_spec_path(key), json.dumps(spec, indent=2).encode("utf-8"))
    return spec


def update(key: str, draft: Draft) -> dict[str, Any]:
    old = get(key)
    if draft.base != old["base"]:
        raise CustomTemplateError("The base of a saved template cannot change")
    spec = _spec(draft, key, old.get("created_at", _now()))
    _write_atomic(_spec_path(key), json.dumps(spec, indent=2).encode("utf-8"))
    return spec


def delete(key: str) -> None:
    spec = get(key)
    spec["deleted"] = True
    spec["updated_at"] = _now()
    _write_atomic(_spec_path(key), json.dumps(spec, indent=2).encode("utf-8"))


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
