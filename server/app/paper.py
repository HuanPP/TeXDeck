"""Papering: LaTeX projects the reader writes with an agent beside them.

A project is a generation of kind "paper". Its files live in `project/`, the
latest build in `build/`, and settings (main file, engine) in `paper.json`.

Anything in `project/` may have been written by an agent or uploaded, so the
server never follows a symlink there (an agent could plant one pointing at
another account's data), and LaTeX runs inside bubblewrap where it sees the
system TeX tree, the sources read-only and the build folder, nothing else.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import store
from .config import settings
from .deck import _engine_runs

ENGINES = ("pdflatex", "xelatex", "lualatex")
DEFAULT_ENGINE = "xelatex"
SOURCE_LIMIT = 2 * 1024 * 1024  # editable text files
UPLOAD_LIMIT = 40 * 1024 * 1024  # one uploaded file
PROJECT_LIMIT = 200 * 1024 * 1024  # imported zip, unpacked
MAX_ENTRIES = 5000
COMPILE_TIMEOUT = {"pdflatex": 180, "xelatex": 180, "lualatex": 360}

_SOURCE_IN_JAIL = "/source"
_BUILD_IN_JAIL = "/build"

STARTER = r"""\documentclass[11pt]{article}
\usepackage{fontspec}
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage[margin=2.5cm]{geometry}

\title{%s}
\author{}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}
Start writing here.

\end{document}
"""

STARTER_ZH = r"""\documentclass[11pt]{ctexart}
\usepackage{amsmath, amssymb}
\usepackage{graphicx}
\usepackage[margin=2.5cm]{geometry}

\title{%s}
\author{}
\date{\today}

\begin{document}
\maketitle

\section{引言}
在这里开始写作。

\end{document}
"""


class PaperError(RuntimeError):
    """User-facing English message; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Layout and settings
# --------------------------------------------------------------------------


def root(gen_id: str) -> Path:
    return store.generation_dir(gen_id) / "project"


def build_dir(gen_id: str) -> Path:
    return store.generation_dir(gen_id) / "build"


def output_pdf(gen_id: str) -> Path:
    return store.generation_dir(gen_id) / "output.pdf"


def _settings_path(gen_id: str) -> Path:
    return store.generation_dir(gen_id) / "paper.json"


def load_settings(gen_id: str) -> dict[str, str]:
    try:
        data = json.loads(_settings_path(gen_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    engine = data.get("engine") if data.get("engine") in ENGINES else DEFAULT_ENGINE
    main = data.get("main") if isinstance(data.get("main"), str) else "main.tex"
    return {"engine": engine, "main": main}


def save_settings(gen_id: str, engine: str, main: str) -> dict[str, str]:
    if engine not in ENGINES:
        raise PaperError(f"Unknown compiler: {engine}")
    if not main.lower().endswith(".tex") or not resolve(gen_id, main).is_file():
        raise PaperError("Choose a .tex file in the project as the main file")
    value = {"engine": engine, "main": main}
    _write_json(_settings_path(gen_id), value)
    return value


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Paths: relative, visible, no symlinks
# --------------------------------------------------------------------------


def clean_path(path: str) -> str:
    """A project-relative POSIX path, or PaperError. Hidden names are refused
    (they hold agent state) and so is anything that climbs out."""
    if not isinstance(path, str) or not path or len(path) > 1024 or re.search(r"[\\:\x00-\x1f]", path):
        raise PaperError("Invalid file path")
    parts = path.strip("/").split("/")
    if len(parts) > 20 or any(not p or p in (".", "..") or p.startswith(".") for p in parts):
        raise PaperError("Invalid file path")
    return "/".join(parts)


def resolve(gen_id: str, path: str, *, parents: bool = False) -> Path:
    """Absolute path of `path` in the project. No component may be a symlink;
    with `parents`, missing folders are made."""
    base = root(gen_id)
    rel = clean_path(path)
    current = base
    parts = rel.split("/")
    for name in parts[:-1]:
        current = current / name
        if parents and not current.exists() and not current.is_symlink():
            current.mkdir(mode=0o700)
        if current.is_symlink() or not current.is_dir():
            raise PaperError("Invalid project folder")
    target = current / parts[-1]
    if target.is_symlink():
        raise PaperError("Links are not supported in a project")
    return target


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError as exc:
        raise PaperError("File no longer exists", 404) from exc
    except OSError as exc:
        raise PaperError("Choose a regular file") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not os.path.isfile(path) or info.st_nlink != 1:
            raise PaperError("Choose a regular file")
        if info.st_size > limit:
            raise PaperError("File exceeds the size limit", 413)
        return handle.read(limit + 1)[:limit]


def entries(gen_id: str) -> list[dict[str, Any]]:
    """Folders first, then files, depth-first; hidden names and links skipped."""
    out: list[dict[str, Any]] = []

    def walk(folder: Path, prefix: str, depth: int) -> None:
        if depth > 20:
            return
        try:
            children = list(os.scandir(folder))
        except FileNotFoundError:
            return
        children.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        for child in children:
            if child.name.startswith(".") or child.is_symlink():
                continue
            if len(out) >= MAX_ENTRIES:
                raise PaperError("The project has too many files", 413)
            path = prefix + child.name
            if child.is_dir(follow_symlinks=False):
                out.append({"path": path, "directory": True})
                walk(Path(child.path), path + "/", depth + 1)
            elif child.is_file(follow_symlinks=False):
                out.append({"path": path, "directory": False, "size": child.stat(follow_symlinks=False).st_size})

    walk(root(gen_id), "", 0)
    return out


def fingerprint(gen_id: str) -> str:
    """Changes whenever a visible file is added, removed or written."""
    h = hashlib.sha1()
    for dirpath, dirnames, filenames in os.walk(root(gen_id)):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            h.update(f"{p}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source(gen_id: str, path: str) -> dict[str, str]:
    data = _read_regular(resolve(gen_id, path), SOURCE_LIMIT)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaperError("This file is not editable text", 415) from exc
    if "\0" in text:
        raise PaperError("This file is not editable text", 415)
    return {"text": text, "revision": revision(data)}


_save_lock = threading.Lock()


def save_source(gen_id: str, path: str, text: str, base_revision: str) -> str:
    """Write `text` if the file is still at `base_revision`; the new revision."""
    data = text.encode("utf-8")
    if len(data) > SOURCE_LIMIT:
        raise PaperError("File exceeds 2 MB", 413)
    target = resolve(gen_id, path)
    with _save_lock:
        if target.exists():
            if revision(_read_regular(target, SOURCE_LIMIT)) != base_revision:
                raise PaperError("The file changed on disk. Reload it or keep your edits.", 409)
        elif base_revision:
            raise PaperError("The file was removed on disk", 409)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        tmp.replace(target)
    return revision(data)


def new_file(gen_id: str, path: str, directory: bool = False) -> None:
    target = resolve(gen_id, path, parents=True)
    if target.exists():
        raise PaperError("A file with this name already exists", 409)
    if directory:
        target.mkdir(mode=0o700)
    else:
        with open(target, "xb"):
            pass


def write_upload(gen_id: str, path: str, data: bytes, *, replace: bool = False) -> None:
    if len(data) > UPLOAD_LIMIT:
        raise PaperError("A file may be at most 40 MB", 413)
    target = resolve(gen_id, path, parents=True)
    if target.exists() and (not replace or target.is_dir()):
        raise PaperError(f"{path} already exists", 409)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.upload")
    tmp.write_bytes(data)
    tmp.replace(target)


def delete_path(gen_id: str, path: str) -> None:
    target = resolve(gen_id, path)
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()
    else:
        raise PaperError("File no longer exists", 404)


def rename_path(gen_id: str, path: str, new_path: str) -> None:
    source = resolve(gen_id, path)
    target = resolve(gen_id, new_path, parents=True)
    if not source.exists():
        raise PaperError("File no longer exists", 404)
    if target.exists():
        raise PaperError("A file with this name already exists", 409)
    if source.is_dir() and target.is_relative_to(source):
        raise PaperError("A folder cannot move into itself")
    source.rename(target)
    current = load_settings(gen_id)
    if current["main"] == clean_path(path):
        _write_json(_settings_path(gen_id), {**current, "main": clean_path(new_path)})


# --------------------------------------------------------------------------
# Creating and exporting
# --------------------------------------------------------------------------


def create(title: str, *, owner: str, language: str = "en", archive: bytes | None = None) -> store.GenerationMeta:
    title = title.strip()
    if not title or len(title) > 120:
        raise PaperError("Enter a project name (up to 120 characters)")
    meta = store.new_generation(
        title=title, template_key="paper", template_name="Paper", instruction="",
        source_kind="text", source_names=[], language=language, owner=owner, kind="paper",
    )
    meta.status = "succeeded"
    store.save_meta(meta)
    gen_id = meta.id
    root(gen_id).mkdir(mode=0o700)
    try:
        if archive:
            main = import_zip(gen_id, archive)
        else:
            main = "main.tex"
            starter = STARTER_ZH if language == "zh" else STARTER
            (root(gen_id) / main).write_text(starter % _tex_escape(title), encoding="utf-8")
        _write_json(_settings_path(gen_id), {"engine": DEFAULT_ENGINE, "main": main})
    except BaseException:
        store.delete_generation(gen_id)
        raise
    return meta


def _tex_escape(text: str) -> str:
    return re.sub(r"([#$%&_{}])", r"\\\1", text).replace("~", r"\textasciitilde{}").replace("^", r"\^{}")


def import_zip(gen_id: str, data: bytes) -> str:
    """Unpack an Overleaf-style zip into the project; returns the main file.
    A single top folder is stripped. Links and hidden files are skipped."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PaperError("This is not a zip file") from exc
    members, names = [], []
    for m in archive.infolist():
        name = m.filename.replace("\\", "/").lstrip("/")
        if m.is_dir() or name.startswith("__MACOSX/") or any(p.startswith(".") for p in name.split("/")):
            continue
        members.append(m)
        names.append(name)
    tops = {n.split("/", 1)[0] for n in names}
    strip = len(tops) == 1 and all("/" in n for n in names)
    total = 0
    written: list[str] = []
    for member, name in zip(members, names):
        if strip:
            name = name.split("/", 1)[1]
        mode = (member.external_attr >> 16) & 0o170000
        if mode == 0o120000:  # symlink
            continue
        total += member.file_size
        if total > PROJECT_LIMIT or len(written) >= MAX_ENTRIES:
            raise PaperError("The zip is too large to import", 413)
        try:
            path = clean_path(name)
        except PaperError:
            continue
        target = resolve(gen_id, path, parents=True)
        with archive.open(member) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        written.append(path)
    texs = [p for p in written if p.lower().endswith(".tex")]
    if not texs:
        raise PaperError("The zip has no .tex file")
    return _guess_main(gen_id, texs)


def _guess_main(gen_id: str, texs: list[str]) -> str:
    def score(path: str) -> tuple[int, int, int]:
        try:
            head = (root(gen_id) / path).read_text(encoding="utf-8", errors="replace")[:20000]
        except OSError:
            head = ""
        has_doc = "\\documentclass" in head and "\\begin{document}" in head
        return (0 if has_doc else 1, 0 if PurePosixPath(path).name.lower() == "main.tex" else 1, path.count("/"))

    return min(texs, key=score)


def export_zip(gen_id: str) -> bytes:
    buffer = io.BytesIO()
    remaining = PROJECT_LIMIT
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in entries(gen_id):
            if item["directory"]:
                continue
            data = _read_regular(resolve(gen_id, item["path"]), remaining)
            remaining -= len(data)
            archive.writestr(item["path"], data)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Compiling (latexmk inside bubblewrap)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class BuildState:
    running: bool = False
    ok: bool = False
    error: str = ""
    log: str = ""
    pdf: str = ""  # mtime of output.pdf as a version tag; "" before the first success
    finished_at: float = 0.0


_builds: dict[str, BuildState] = {}
_builds_lock = threading.Lock()


def build_state(gen_id: str) -> BuildState:
    with _builds_lock:
        state = _builds.get(gen_id)
        if state is not None:
            return BuildState(**asdict(state))
    pdf = output_pdf(gen_id)
    saved = BuildState(pdf=str(pdf.stat().st_mtime_ns) if pdf.is_file() else "")
    try:
        data = json.loads((store.generation_dir(gen_id) / "build.json").read_text(encoding="utf-8"))
        saved.ok, saved.error, saved.log = bool(data.get("ok")), str(data.get("error", "")), str(data.get("log", ""))
    except (OSError, ValueError):
        pass
    return saved


def stale(gen_id: str) -> bool:
    """A source file is newer than the PDF (edited while nobody compiled)."""
    pdf = output_pdf(gen_id)
    if not pdf.is_file():
        return True
    built = pdf.stat().st_mtime
    for dirpath, dirnames, filenames in os.walk(root(gen_id)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if not name.startswith(".") and os.lstat(os.path.join(dirpath, name)).st_mtime > built:
                return True
    return False


def _set_build(gen_id: str, state: BuildState) -> None:
    with _builds_lock:
        _builds[gen_id] = state


def find_bwrap() -> str | None:
    return shutil.which(settings.bwrap_command) if os.name == "posix" else None


def compile_args(gen_id: str, engine: str, main: str, force: bool) -> list[str]:
    flag = {"pdflatex": "-pdf", "xelatex": "-xelatex", "lualatex": "-lualatex"}[engine]
    latexmk = [
        "latexmk", "-norc", flag, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
        "-synctex=1", "-latexoption=-no-shell-escape", "-jobname=document", f"-outdir={_BUILD_IN_JAIL}",
    ]
    if force:
        latexmk.append("-g")
    latexmk.append(main)
    bwrap = find_bwrap()
    if bwrap is None:
        raise PaperError("Compiling needs bubblewrap on the server", 503)
    args = [bwrap, "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        top = Path("/") / name
        if top.is_symlink():
            args += ["--symlink", os.readlink(top), str(top)]
        elif top.is_dir():
            args += ["--ro-bind", str(top), str(top)]
    for extra in ("/etc/texmf", "/etc/fonts", "/var/lib/texmf", "/var/cache/fontconfig", "/etc/alternatives",
                  "/etc/localtime"):
        args += ["--ro-bind-try", extra, extra]
    args += [
        "--ro-bind", str(root(gen_id)), _SOURCE_IN_JAIL,
        "--bind", str(build_dir(gen_id)), _BUILD_IN_JAIL,
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        "--setenv", "HOME", _BUILD_IN_JAIL,
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "openout_any", "p",
        "--setenv", "shell_escape", "f",
        "--setenv", "TEXMFVAR", f"{_BUILD_IN_JAIL}/.texmf-var",
        "--chdir", _SOURCE_IN_JAIL,
    ]
    return args + latexmk


def compile_project(gen_id: str) -> BuildState:
    """Build the main file once; a second request while one runs is a no-op."""
    with _builds_lock:
        current = _builds.get(gen_id)
        if current is not None and current.running:
            return BuildState(**asdict(current))
    previous = build_state(gen_id)
    conf = load_settings(gen_id)
    main = resolve(gen_id, conf["main"])
    if not main.is_file():
        raise PaperError("The main file is missing; choose one in Settings")
    try:
        head = main.read_text(encoding="utf-8", errors="replace")
    except OSError:
        head = ""
    if "\\documentclass" not in head:
        state = BuildState(pdf=previous.pdf, error=f"{conf['main']} has no \\documentclass, so it cannot be built "
                           "as the main file. Pick the file that starts the document with the star in Files.",
                           finished_at=time.time())
        _set_build(gen_id, state)
        return BuildState(**asdict(state))
    _set_build(gen_id, BuildState(running=True, pdf=previous.pdf, log=previous.log))
    state = BuildState(pdf=previous.pdf)
    try:
        out = build_dir(gen_id)
        out.mkdir(mode=0o700, exist_ok=True)
        args = compile_args(gen_id, conf["engine"], clean_path(conf["main"]), force=not previous.ok)
        try:
            with _engine_runs:
                proc = subprocess.run(args, capture_output=True, timeout=COMPILE_TIMEOUT[conf["engine"]], check=False)
            output = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            output, ok = "Compilation timed out.", False
        log_file = out / "document.log"
        log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else output
        state.log = log[-200_000:]
        built = out / "document.pdf"
        if ok and built.is_file() and not built.is_symlink():
            pdf = output_pdf(gen_id)
            tmp = pdf.with_name("output.pdf.tmp")
            shutil.copyfile(built, tmp)
            tmp.replace(pdf)
            synctex = out / "document.synctex.gz"
            if synctex.is_file() and not synctex.is_symlink():
                shutil.copyfile(synctex, store.generation_dir(gen_id) / "output.synctex.gz")
            state.ok, state.pdf = True, str(pdf.stat().st_mtime_ns)
        else:
            state.error = first_error(log) or "Compilation failed"
    except PaperError as exc:
        state.error = str(exc)
    except Exception as exc:  # noqa: BLE001 - reported to the page, never fatal
        state.error = f"Compilation failed: {exc}"
    state.finished_at = time.time()
    _set_build(gen_id, state)
    _write_json(store.generation_dir(gen_id) / "build.json",
                {"ok": state.ok, "error": state.error, "log": state.log[-50_000:]})
    return BuildState(**asdict(state))


_ERROR_RE = re.compile(r"^(?:\./)?(?:/source/)?([^:\n]+\.\w+):(\d+): (.+)$", re.M)


def first_error(log: str) -> str:
    """The earliest error in the log: a file:line: message, or a bare `! ...` line
    (some errors skip the file-line form even with -file-line-error)."""
    found = [m for m in (_ERROR_RE.search(log), re.search(r"^! (.+)$", log, re.M)) if m]
    if not found:
        return ""
    match = min(found, key=lambda m: m.start())
    if match.re is _ERROR_RE:
        return f"{match.group(1)}:{match.group(2)}: {match.group(3).strip()}"
    return match.group(1).strip()


# --------------------------------------------------------------------------
# SyncTeX
# --------------------------------------------------------------------------


def _synctex(args: list[str]) -> str:
    if not shutil.which("synctex"):
        raise PaperError("SyncTeX is not installed on the server", 503)
    try:
        proc = subprocess.run(["synctex", *args], capture_output=True, timeout=5, check=False)
    except subprocess.TimeoutExpired as exc:
        raise PaperError("No matching position in the PDF", 404) from exc
    return proc.stdout.decode("utf-8", errors="replace")


def _synctex_files(gen_id: str) -> tuple[Path, Path]:
    gdir = store.generation_dir(gen_id)
    pdf, sync = gdir / "output.pdf", gdir / "output.synctex.gz"
    if not pdf.is_file() or not sync.is_file():
        raise PaperError("Compile the project first", 404)
    # synctex looks for <pdf stem>.synctex.gz beside the PDF.
    return pdf, sync


def forward(gen_id: str, path: str, line: int) -> dict[str, float]:
    """Source line -> {page, x, y} (PDF points from the top left)."""
    rel = clean_path(path)
    pdf, _ = _synctex_files(gen_id)
    out = _synctex(["view", "-i", f"{max(1, line)}:0:{_SOURCE_IN_JAIL}/./{rel}", "-o", str(pdf)])
    if "Page:" not in out:
        out = _synctex(["view", "-i", f"{max(1, line)}:0:{_SOURCE_IN_JAIL}/{rel}", "-o", str(pdf)])
    page, y, x = _field(out, "Page"), _field(out, "y"), _field(out, "x")
    if page is None or y is None:
        raise PaperError("No matching position in the PDF", 404)
    return {"page": int(page), "x": x or 0.0, "y": y}


def inverse(gen_id: str, page: int, x: float, y: float) -> dict[str, Any]:
    """PDF position (points from the top left) -> {path, line}."""
    pdf, _ = _synctex_files(gen_id)
    out = _synctex(["edit", "-o", f"{page}:{x:.2f}:{y:.2f}:{pdf}"])
    match = re.search(r"^Input:(.*)$", out, re.M)
    line = _field(out, "Line")
    if not match or line is None or line < 1:
        raise PaperError("No matching source line", 404)
    name = match.group(1).strip()
    for prefix in (_SOURCE_IN_JAIL + "/./", _SOURCE_IN_JAIL + "/", "./"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    else:
        raise PaperError("No matching source line", 404)
    try:
        if not resolve(gen_id, name).is_file():
            raise PaperError("No matching source line", 404)
    except PaperError as exc:
        raise PaperError("No matching source line", 404) from exc
    return {"path": clean_path(name), "line": int(line)}


def _field(text: str, key: str) -> float | None:
    match = re.search(rf"^{key}:([-\d.]+)$", text, re.M)
    try:
        return float(match.group(1)) if match else None
    except ValueError:
        return None
