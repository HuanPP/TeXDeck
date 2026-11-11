from __future__ import annotations

import json
import re
import secrets
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    source_names: list[str]  # original filenames, upload order = reading order; [] for text mode
    status: str  # "running" | "succeeded" | "failed"
    error: str  # failure reason (English); "" on success
    created_at: str  # ISO8601
    updated_at: str
    page_count: int  # total source pages across ALL PDFs; 0 for text mode
    frame_count: int  # finished slide count; 0 while incomplete
    edit_count: int
    author: str = ""  # optional; missing in older meta.json
    institute: str = ""  # optional; missing in older meta.json
    refine_count: int = 0  # whole-deck refine passes; missing in older meta.json
    llm_model: str = ""  # model used by the most recent job; "" in older meta.json
    llm_prompt_tokens: int = 0  # cumulative across all jobs on this generation
    llm_completion_tokens: int = 0
    llm_calls: int = 0  # every LLM call, including repairs and page selection
    language: str = "source"  # slide language: "source" (match the material) | "en" | "zh"
    error_kind: str = ""  # "model" when `error` came from the model provider; read only with `error`
    owner: str = ""  # account that made it; "" without accounts (and before them: the admin's)
    # User images to place on the slides, in upload order:
    # {"file": "figures/fig-01.png", "name", "note", "width", "height"}
    figures: list[dict[str, Any]] = field(default_factory=list)


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
    source_names: list[str],
    author: str = "",
    institute: str = "",
    language: str = "source",
    owner: str = "",
    figures: list[dict[str, Any]] | None = None,
) -> GenerationMeta:
    gen_id, directory = _claim_generation_dir()
    now = _now_iso()
    meta = GenerationMeta(
        id=gen_id,
        title=title,
        template_key=template_key,
        template_name=template_name,
        instruction=instruction,
        source_kind=source_kind,
        source_names=list(source_names),
        status="running",
        error="",
        created_at=now,
        updated_at=now,
        page_count=0,
        frame_count=0,
        edit_count=0,
        author=author,
        institute=institute,
        language=language,
        owner=owner,
        figures=list(figures or []),
    )
    _write_meta_file(directory / "meta.json", meta)
    return meta


def _claim_generation_dir() -> tuple[str, Path]:
    """Mint an id and atomically create its directory; retry on same-second collision."""
    last_exc: OSError | None = None
    for _ in range(5):
        gen_id = _gen_id()
        directory = generation_dir(gen_id)
        try:
            directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            last_exc = exc
            continue
        return gen_id, directory
    raise RuntimeError("Could not allocate a unique generation ID") from last_exc


def _meta_from_dict(data: dict[str, Any]) -> GenerationMeta:
    """Build GenerationMeta from a meta.json dict, upgrading legacy schemas.

    Pre-multi-PDF meta.json carries a scalar source_name instead of source_names.
    """
    if "source_names" not in data:
        legacy = data.pop("source_name", "")
        data["source_names"] = [legacy] if legacy else []
    data.pop("source_name", None)
    return GenerationMeta(**data)


def load_meta(gen_id: str) -> GenerationMeta:
    path = generation_dir(gen_id) / "meta.json"
    if not path.is_file():
        raise KeyError(gen_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    return _meta_from_dict(data)


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
            result.append(_meta_from_dict(data))
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


def mark_interrupted_generations() -> int:
    """Fail any meta still 'running' (a restart killed its in-process job)."""
    n = 0
    for meta in list_generations():
        if meta.status == "running":
            meta.status = "failed"
            meta.error = "Interrupted by server restart"
            save_meta(meta)
            n += 1
    return n


def bundle_zip(gen_id: str) -> Path:
    directory = generation_dir(gen_id)
    if not directory.is_dir():
        raise KeyError(gen_id)
    zip_path = directory / "bundle.zip"
    tmp_path = directory / "bundle.zip.tmp"
    dirs = ("src", "font", "figures")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        main_tex = directory / "main.tex"
        if main_tex.is_file():
            zf.write(main_tex, arcname="main.tex")
        for sty in sorted(directory.glob("*.sty")):
            zf.write(sty, arcname=sty.name)
        for dirname in dirs:
            src_dir = directory / dirname
            if not src_dir.is_dir():
                continue
            for file_path in sorted(src_dir.rglob("*")):
                if file_path.is_file():
                    arcname = file_path.relative_to(directory).as_posix()
                    zf.write(file_path, arcname=arcname)
    # Atomic publish so a concurrent download never streams a half-written zip
    tmp_path.replace(zip_path)
    return zip_path
