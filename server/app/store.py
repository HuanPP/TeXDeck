from __future__ import annotations

import json
import re
import secrets
import shutil
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

# gen_id like 20260725-143020-a1b2; hex + hyphen only, blocks path traversal
_GEN_ID_RE = re.compile(r"^[0-9a-f-]{10,40}$")


@dataclass(slots=True)
class GenerationMeta:
    id: str  # "20260725-143020-a1b2"
    title: str
    template_key: str
    template_name: str
    instruction: str
    source_kind: str  # "pdf" | "text"
    source_name: str  # original filename; "" for text mode
    status: str  # "running" | "succeeded" | "failed"
    error: str  # failure reason (English); "" on success
    created_at: str  # ISO8601
    updated_at: str
    page_count: int  # source PDF pages; 0 for text mode
    frame_count: int  # finished slide count; 0 while incomplete
    edit_count: int
    author: str = ""  # optional; missing in older meta.json
    institute: str = ""  # optional; missing in older meta.json


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def generation_dir(gen_id: str) -> Path:
    if not _GEN_ID_RE.fullmatch(gen_id):
        raise ValueError("Invalid generation ID")
    return settings.artifact_root / gen_id


def new_generation(
    *,
    title: str,
    template_key: str,
    template_name: str,
    instruction: str,
    source_kind: str,
    source_name: str,
    author: str = "",
    institute: str = "",
) -> GenerationMeta:
    gen_id = _gen_id()
    now = _now_iso()
    meta = GenerationMeta(
        id=gen_id,
        title=title,
        template_key=template_key,
        template_name=template_name,
        instruction=instruction,
        source_kind=source_kind,
        source_name=source_name,
        status="running",
        error="",
        created_at=now,
        updated_at=now,
        page_count=0,
        frame_count=0,
        edit_count=0,
        author=author,
        institute=institute,
    )
    directory = generation_dir(gen_id)
    directory.mkdir(parents=True, exist_ok=False)
    _write_meta_file(directory / "meta.json", meta)
    return meta


def load_meta(gen_id: str) -> GenerationMeta:
    path = generation_dir(gen_id) / "meta.json"
    if not path.is_file():
        raise KeyError(gen_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    return GenerationMeta(**data)


def save_meta(meta: GenerationMeta) -> None:
    meta.updated_at = _now_iso()
    path = generation_dir(meta.id) / "meta.json"
    _write_meta_file(path, meta)


def _write_meta_file(path: Path, meta: GenerationMeta) -> None:
    path.write_text(
        json.dumps(asdict(meta), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def list_generations() -> list[GenerationMeta]:
    root = settings.artifact_root
    if not root.is_dir():
        return []
    result: list[GenerationMeta] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            result.append(GenerationMeta(**data))
        except Exception as exc:
            # Listing: skip corrupted meta with a warning; do not block other entries
            print(f"Warning: skipping corrupted meta.json: {meta_path} ({exc})")
    result.sort(key=lambda m: m.created_at, reverse=True)
    return result


def delete_generation(gen_id: str) -> None:
    directory = generation_dir(gen_id)
    if not directory.exists():
        raise KeyError(gen_id)
    shutil.rmtree(directory)


def next_edit_dir(gen_id: str) -> Path:
    edits_root = generation_dir(gen_id) / "edits"
    edits_root.mkdir(parents=True, exist_ok=True)
    max_n = 0
    for child in edits_root.iterdir():
        if child.is_dir() and re.fullmatch(r"\d{3}", child.name):
            max_n = max(max_n, int(child.name))
    next_n = max_n + 1
    path = edits_root / f"{next_n:03d}"
    path.mkdir(parents=False, exist_ok=False)
    return path


def bundle_zip(gen_id: str) -> Path:
    directory = generation_dir(gen_id)
    if not directory.is_dir():
        raise KeyError(gen_id)
    zip_path = directory / "bundle.zip"
    names = ("main.tex", "collegebeamer.sty")
    dirs = ("src", "font")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            src = directory / name
            if src.is_file():
                zf.write(src, arcname=name)
        for dirname in dirs:
            src_dir = directory / dirname
            if not src_dir.is_dir():
                continue
            for file_path in sorted(src_dir.rglob("*")):
                if file_path.is_file():
                    arcname = file_path.relative_to(directory).as_posix()
                    zf.write(file_path, arcname=arcname)
    return zip_path
