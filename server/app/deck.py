from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz

from .config import settings
from .llm import LlmClient, ProgressCallback, image_block, text_block
from .pdf import PdfDocument

# Hard cap on actual repair API calls (including no-ops that echo the input).
# Effective write-back repairs are still limited by settings.max_compile_repairs.
_MAX_REPAIR_CALLS = settings.max_compile_repairs * 3


class DeckError(RuntimeError):
    """Generation/validation failed; message is user-facing English."""


class CompileError(RuntimeError):
    """LaTeX compilation failed."""

    def __init__(self, message: str, log: str) -> None:
        super().__init__(message)
        self.log = log


@dataclass(slots=True)
class Template:
    key: str
    name: str
    dir: Path
    main_tex: str
    # template.json as read from disk (a custom template carries its base's).
    meta: dict[str, Any] = field(default_factory=dict)
    # Files laid over the pack when it is copied (relative path -> bytes or a
    # file to copy); custom templates put their palette and images here.
    extra: dict[str, bytes | Path] = field(default_factory=dict)
    base: str = ""  # the built-in pack a custom template derives from


@dataclass(slots=True)
class DeckRequest:
    template_key: str
    title: str
    author: str = ""  # optional; "" if not provided
    institute: str = ""  # optional; "" if not provided
    instruction: str = ""  # free-form user request; may be ""
    source_text: str = ""  # paste-text body; "" in PDF mode
    language: str = "source"  # "source" | "en" | "zh"


# The page's "Slide language" choice. Requirements may still override it.
LANGUAGE_RULES = {
    "source": (
        "Output language: the language of the source material (the dominant one if it "
        "mixes languages), unless the Requirements explicitly ask for another language."
    ),
    "en": (
        "Output language: English. Write every slide in English even when the source "
        "material is in another language, unless the Requirements explicitly ask otherwise."
    ),
    "zh": (
        "Output language: Simplified Chinese. Write every slide in Simplified Chinese even "
        "when the source material is in another language, unless the Requirements "
        "explicitly ask otherwise. Keep technical terms, names and formulas as they are."
    ),
}

# Speaker notes ride along in the source as \note{...}: Beamer leaves them out
# of the PDF by default, the sidebar shows them and the PPTX carries them.
NOTES_RULE = (
    "Speaker notes: inside every content frame, just before \\end{frame}, add "
    "\\note{...} with what the presenter says on that slide: 2-4 natural spoken "
    "sentences in the output language that explain the slide rather than repeat its "
    "bullets. Plain prose only (no lists, environments or display math). The title page "
    "needs no note."
)

PLACEMENT_RULE = (
    "User images: the user supplied image files (listed and shown after the source "
    "material) that must appear in the deck. Put each one on the slide whose content it "
    "fits best, or where its user note says; use every image exactly once unless the "
    "Requirements say otherwise. Include an image only as "
    "\\includegraphics[width=<w>\\linewidth,height=<h>\\textheight,keepaspectratio]{<file>} "
    "with both bounds and the file name exactly as listed. Choose the layout from the "
    "image's shape: a wide image takes the full width with at most two short lines of "
    "text above or below it; a square or tall image goes in a columns layout with the "
    "image in a 0.4-0.5 wide column and the text beside it; a logo-like or decorative "
    "image may be small. On a slide with a frame title keep the image at most "
    "0.75\\textheight, less when text shares its column, so nothing runs off the slide. "
    "Never crop, stretch, rotate or redraw a user image, and never reference any other "
    "image file."
)

SOURCE_IMAGES_RULE = (
    "Images inside the source material: charts, diagrams and tables may be redrawn with "
    "tikz, pgfplots or booktabs when their data or structure is stated in the material. "
    "Never redraw photographs, micrographs, scans, maps, renders or dense plots whose "
    "content you cannot reproduce faithfully; state what they show in words instead."
)

# Users kept asking to fix these by hand: arrows too short to see, edge labels
# sitting on node text, chart labels running into each other.
DIAGRAM_RULE = (
    "Diagrams and charts: nothing may touch or overlap. In TikZ, place nodes far enough "
    "apart that every arrow shows at least 1cm of line between the node borders, and put "
    "edge labels beside the arrow (above, below or sloped) clear of every node and of other "
    "labels. In pgfplots, keep tick and category labels short; when they are long or many, "
    "use a smaller font, wrap them (text width with align=center) or rotate them, and widen "
    "the plot, so that neighbouring labels never run into each other."
)

# User images live here, relative to the deck's compile directory.
FIGURE_DIR = "figures"

_TITLE_RE = re.compile(r"\\title\s*(?:\[[^\]]*\])?\s*\{")


def extract_title(tex: str) -> str:
    """Plain-text \\title{...} of a deck (brace-balanced, commands stripped); "" if absent."""
    match = _TITLE_RE.search(tex)
    if not match:
        return ""
    depth, i = 1, match.end()
    while i < len(tex) and depth:
        depth += {"{": 1, "}": -1}.get(tex[i], 0)
        i += 1
    raw = tex[match.end() : i - 1]
    raw = re.sub(r"\\\\(\[[^\]]*\])?", " ", raw)  # line breaks
    raw = re.sub(r"\\[a-zA-Z@]+\*?(\[[^\]]*\])?", "", raw)  # commands, keep their text args
    raw = raw.replace("{", "").replace("}", "").replace("~", " ")
    return re.sub(r"\s+", " ", raw).strip()[:200]


@dataclass(slots=True)
class CompileResult:
    pdf_path: Path
    log: str
    attempts: int


def list_templates() -> list[Template]:
    root = Path(settings.templates_dir)
    if not root.is_dir():
        raise DeckError(f"Templates directory not found: {root}")

    templates: list[Template] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        meta_path = child / "template.json"
        if not meta_path.is_file():
            continue
        templates.append(_load_template_from_dir(child))

    templates.sort(key=lambda t: t.name)
    return templates


def get_template(key: str) -> Template:
    """A built-in pack, or one of the current user's custom templates."""
    from . import custom_templates  # imports this module

    if custom_templates.is_custom_key(key):
        return custom_templates.load(key)
    return builtin_template(key)


def builtin_template(key: str) -> Template:
    root = Path(settings.templates_dir)
    candidate = root / key
    meta_path = candidate / "template.json"
    if not candidate.is_dir() or not meta_path.is_file():
        raise ValueError(f"Unknown template: {key}")
    return _load_template_from_dir(candidate)


def _load_template_from_dir(directory: Path) -> Template:
    meta_path = directory / "template.json"
    main_path = directory / "main.tex"
    if not main_path.is_file():
        raise DeckError(f"Template is missing main.tex: {directory.name}")

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeckError(f"Cannot read template config: {directory.name}") from exc

    key = str(meta.get("key") or directory.name)
    name = str(meta.get("name") or key)
    main_tex = main_path.read_text(encoding="utf-8")
    return Template(key=key, name=name, dir=directory, main_tex=main_tex, meta=meta, base=key)


def copy_template_bundle(template: Template, target_dir: Path) -> None:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    # Every style file the sample deck loads must travel with it; SimplePlus and
    # Execushares keep their themes in beamertheme*.sty, not collegebeamer.sty.
    for sty in sorted(template.dir.glob("*.sty")):
        shutil.copy2(sty, target_dir / sty.name)

    for folder_name in ("src", "font"):
        src = template.dir / folder_name
        if src.is_dir():
            dest = target_dir / folder_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)

    for rel, content in template.extra.items():
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            shutil.copyfile(content, dest)
        else:
            dest.write_bytes(content)


def build_generation_messages(
    template: Template,
    request: DeckRequest,
    documents: list[PdfDocument],
    figure_blocks: list[dict[str, Any]] | None = None,
    memory_note: str = "",
) -> list[dict[str, Any]]:
    """figure_blocks: the user's images as figures.prompt_blocks renders them.
    memory_note: what is remembered about this user (memory.note()), if anything."""
    system = (
        "Output only one complete, compilable XeLaTeX Beamer document. "
        "No explanation, no markdown fences."
    )

    if request.title.strip():
        title_parts = [f"Title: {request.title}"]
    else:
        title_parts = [
            "No title was provided; use the title of the source material, or a short "
            "fitting title if it has none."
        ]
    if request.author.strip():
        title_parts.append(f"Author: {request.author}")
    else:
        title_parts.append(
            "No author was provided; leave the author field empty rather than inventing one."
        )
    if request.institute.strip():
        title_parts.append(f"Institute: {request.institute}")
    else:
        title_parts.append(
            "No institute was provided; leave the institute field empty rather than inventing one."
        )
    if request.instruction.strip():
        title_parts.append(f"Requirements: {request.instruction}")
    title_parts.append(LANGUAGE_RULES.get(request.language, LANGUAGE_RULES["source"]))
    title_parts.append(NOTES_RULE)
    title_parts.append(DIAGRAM_RULE)
    title_parts.append(SOURCE_IMAGES_RULE)
    if memory_note:
        title_parts.append(memory_note)
    if figure_blocks:
        title_parts.append(PLACEMENT_RULE)
    else:
        title_parts.append("There are no image files; do not use \\includegraphics.")
    title_message = "\n".join(title_parts)

    if documents:
        multi = len(documents) > 1
        material: list[dict[str, Any]] = []
        if multi:
            material.append(
                text_block(
                    f"Source material consists of {len(documents)} documents, in reading order. "
                    "When citing, use the document name and its own page number."
                )
            )
        for document in documents:
            if multi:
                material.append(
                    text_block(
                        f'--- Document {document.index} of {len(documents)}: "{document.name}" '
                        f"({len(document.pages)} pages, {len(document.selected_numbers)} shown) ---"
                    )
                )
            for page in document.selected_pages:
                material.append(image_block(page.image_path))
                material.append(
                    text_block(
                        f'Doc {document.index} ("{document.name}") page {page.number}'
                        if multi
                        else f"Page {page.number}"
                    )
                )
        if not any(part.get("type") == "image_url" for part in material):
            raise DeckError("No source material available for generation")
        material_content: str | list[dict[str, Any]] = material
    else:
        source = request.source_text
        if not source.strip():
            raise DeckError("No source material available for generation")
        material_content = source

    if figure_blocks:
        if isinstance(material_content, str):
            material_content = [text_block(material_content)]
        material_content = material_content + figure_blocks

    # Five segments: title+request is repeated after the material (W11 experiment:
    # baseline 4-msg produced 10 frames; 5-msg produced 8 — closer to "under 8").
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": template.main_tex},
        {"role": "user", "content": title_message},
        {"role": "user", "content": material_content},
        {"role": "user", "content": title_message},
    ]


def extract_tex(
    response: str, template: Template | None, figures: list[str] | tuple[str, ...] = ()
) -> str:
    """figures: the image files this deck may reference (see figure_files).

    template is required on the generation path; repair passes None.
    """
    tex = _strip_markdown_fences(response)

    if r"\documentclass" not in tex:
        raise DeckError("Incomplete model LaTeX: missing \\documentclass")
    if r"\begin{document}" not in tex:
        raise DeckError("Incomplete model LaTeX: missing \\begin{document}")
    if r"\end{document}" not in tex:
        raise DeckError("Incomplete model LaTeX: missing \\end{document}")

    end_idx = tex.rfind(r"\end{document}")
    after = tex[end_idx + len(r"\end{document}") :]
    if after.strip():
        raise DeckError("Incomplete model LaTeX: content after \\end{document}")

    theme_directive = _theme_directive(template)
    if theme_directive is not None and theme_directive not in strip_tex_comments(tex):
        raise DeckError(f"Incomplete model LaTeX: missing {theme_directive}")

    # Template header comments may keep [STYLE REF] text; only the de-commented
    # body is checked for leftovers.
    body = strip_tex_comments(tex)
    if "[STYLE REF]" in body:
        raise DeckError("Incomplete model LaTeX: leftover [STYLE REF] sample page")
    if "AI GENERATION INSTRUCTIONS" in body:
        raise DeckError(
            "Incomplete model LaTeX: leftover AI GENERATION INSTRUCTIONS page"
        )

    check_body(body, figures)
    return tex


_GRAPHICS_RE = re.compile(r"\\includegraphics\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")


def graphics_refs(tex: str) -> list[str]:
    """File arguments of every \\includegraphics outside comments."""
    return [ref.strip() for ref in _GRAPHICS_RE.findall(strip_tex_comments(tex))]


def ref_matches(file: str, refs: list[str]) -> bool:
    """graphicx finds figures/fig-01.png when written with or without the extension."""
    stem = file.rsplit(".", 1)[0]
    return any(ref in (file, stem) for ref in refs)


def figure_files(work_dir: Path) -> list[str]:
    """The user images a deck in work_dir may reference, as relative paths."""
    folder = Path(work_dir) / FIGURE_DIR
    if not folder.is_dir():
        return []
    return [f"{FIGURE_DIR}/{p.name}" for p in sorted(folder.iterdir()) if p.is_file()]


def check_body(body: str, figures: list[str] | tuple[str, ...] = ()) -> None:
    """Reject file access a model must not do; body is LaTeX without comments.

    A poisoned source document could steer the model into embedding local file
    contents (or running commands), or another deck's images, so images are
    limited to this deck's own files and the rest of file input is refused.
    """
    for forbidden in (
        r"\input", r"\include{", r"\write18", r"\openin",
        r"\graphicspath", r"\pgfimage", r"\pgfdeclareimage",
    ):
        if forbidden in body:
            raise DeckError(
                f"Model output uses {forbidden}, which is not allowed in generated decks"
            )
    refs = [ref.strip() for ref in _GRAPHICS_RE.findall(body)]
    if body.count(r"\includegraphics") != len(refs):
        raise DeckError("Model output uses \\includegraphics in a form that cannot be checked")
    for ref in refs:
        if not any(ref_matches(f, [ref]) for f in figures):
            if not figures:
                raise DeckError(
                    "Model output uses \\includegraphics, but this deck has no image files"
                )
            raise DeckError(
                f"Model output uses an image that does not exist: {ref} "
                f"(available: {', '.join(figures)})"
            )


_THEME_DIRECTIVE_RE = re.compile(
    r"\\usepackage(?:\[[^\]]*\])?\{[a-zA-Z]*beamer[a-zA-Z]*\}|\\usetheme\{[^}]+\}"
)


def _theme_directive(template: Template | None) -> str | None:
    """The line that loads the template's theme; the model must keep it verbatim."""
    if template is None:
        return None
    match = _THEME_DIRECTIVE_RE.search(strip_tex_comments(template.main_tex))
    return match.group(0) if match else None


def strip_tex_comments(tex: str) -> str:
    # Drop everything after an unescaped % on each line
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in tex.splitlines())


def _extract_compile_error_block(compile_log: str) -> str:
    """Pull the first !-prefixed error block through the l.<line> line after it."""
    lines = compile_log.splitlines()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("!"):
            start = i
            break
    if start is None:
        # Log-format fallback (not business degradation): last 3000 chars if no ! error
        return compile_log[-3000:] if len(compile_log) > 3000 else compile_log

    end = start
    line_no_seen = False
    for j in range(start, len(lines)):
        end = j
        if re.match(r"^l\.\d+", lines[j]):
            line_no_seen = True
            # one extra line of context
            if j + 1 < len(lines):
                end = j + 1
            break
    if not line_no_seen:
        # have ! but no l.N: take a short window
        end = min(start + 20, len(lines) - 1)
    return "\n".join(lines[start : end + 1])


def _tex_context_around_error(tex: str, compile_log: str, radius: int = 10) -> str:
    """From the first l.<line> in the log, show the enclosing frame (or ±radius).

    LaTeX alignment errors often report the line at \\end{frame}, not the real
    bad row. Giving the model the whole frame is far more useful than ±3 lines
    around that end marker.
    """
    m = re.search(r"^l\.(\d+)", compile_log, re.MULTILINE)
    if not m:
        return "(no error line number found in the log)"
    line_no = int(m.group(1))
    lines = tex.splitlines()
    if line_no < 1 or line_no > len(lines):
        return f"(error line l.{line_no} is outside the source, which has {len(lines)} lines)"

    begin_frame: int | None = None
    for n in range(line_no, 0, -1):
        if r"\begin{frame}" in lines[n - 1]:
            begin_frame = n
            break

    end_frame: int | None = None
    if begin_frame is not None:
        for n in range(max(line_no, begin_frame), len(lines) + 1):
            if r"\end{frame}" in lines[n - 1]:
                end_frame = n
                break

    if begin_frame is None or end_frame is None:
        # Not inside a frame (e.g. preamble error) — fall back to ±radius.
        lo = max(1, line_no - radius)
        hi = min(len(lines), line_no + radius)
    else:
        lo = begin_frame
        hi = end_frame
        # Cap oversized frames so the repair prompt stays bounded.
        if hi - lo + 1 > 200:
            lo = max(begin_frame, line_no - 60)
            hi = min(end_frame, line_no + 20)

    chunks: list[str] = []
    for n in range(lo, hi + 1):
        mark = ">>>" if n == line_no else "   "
        chunks.append(f"{mark} {n}: {lines[n - 1]}")
    return "\n".join(chunks)


def build_repair_messages(
    tex: str, compile_log: str, figures: list[str] | tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    error_block = _extract_compile_error_block(compile_log)
    tex_context = _tex_context_around_error(tex, compile_log)
    preamble_note = (
        "The document preamble is fixed and must not be changed. Allowed packages: "
        "collegebeamer / tikz / pgfplots / amsmath / amssymb / pifont / "
        "booktabs / colortbl / multirow. "
        + (
            "The only image files are: " + ", ".join(figures) + "; "
            "do not reference any other image file; "
            if figures
            else "There are no external image files; do not use \\includegraphics; "
        )
        + "do not add new \\usepackage lines."
    )
    return [
        {
            "role": "system",
            "content": (
                "You are a LaTeX repair assistant. Fix the document from the compile error. "
                "Output only the full repaired, compilable XeLaTeX source. "
                "No explanation, no markdown fences."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Compile error:\n{error_block}\n\n"
                f"Source near the error:\n{tex_context}\n\n"
                f"{preamble_note}\n\n"
                f"Full current tex:\n{tex}"
            ),
        },
    ]


def tex_effectively_equal(a: str, b: str) -> bool:
    """True if a and b match after stripping each line (ignore pure whitespace diffs)."""
    a_lines = [line.strip() for line in a.splitlines()]
    b_lines = [line.strip() for line in b.splitlines()]
    return a_lines == b_lines


def compile_tex(
    tex_path: Path,
    *,
    client: LlmClient | None = None,
    progress: ProgressCallback | None = None,
    llm_dump_dir: Path | None = None,
) -> CompileResult:
    tex_path = Path(tex_path)
    if not tex_path.is_file():
        raise CompileError(f"TeX file not found: {tex_path}", "")

    engine = tex_engine()

    work_dir = tex_path.parent
    pdf_path = work_dir / f"{tex_path.stem}.pdf"
    attempts = 0
    repairs_used = 0  # effective repairs that wrote a different tex
    repair_calls = 0  # every model call, including no-ops
    last_log = ""

    def _try_repair(*, progress_pct: int) -> None:
        """Issue repair call(s) until a non-noop change or hard call limit.

        No-op echoes do not consume settings.max_compile_repairs.
        Raises CompileError when no further repair is possible.
        """
        nonlocal repairs_used, repair_calls
        if client is None or repairs_used >= settings.max_compile_repairs:
            raise CompileError("LaTeX compilation failed", last_log)

        while True:
            if repair_calls >= _MAX_REPAIR_CALLS:
                raise CompileError("LaTeX compilation failed", last_log)

            repair_calls += 1
            if progress is not None:
                progress(
                    f"Compile failed; repair attempt {repairs_used + 1}",
                    progress_pct,
                )
            outcome = _repair_tex(
                tex_path,
                last_log,
                client,
                repair_index=repair_calls,
                llm_dump_dir=llm_dump_dir,
            )
            if outcome == "noop":
                # Echoed input: free retry; do not count against max_compile_repairs.
                continue
            # "changed" or "invalid" — both consume one effective repair slot.
            repairs_used += 1
            return

    while True:
        if progress is not None:
            progress("Compiling LaTeX (pass 1)", min(20 + attempts * 10, 80))

        before = _aux_state(work_dir, tex_path.stem)
        ok, last_log = _run_xelatex(tex_path, work_dir)
        attempts += 1
        if not ok:
            _try_repair(progress_pct=50)
            continue

        # Tectonic reruns to a fixed point by itself. xelatex needs a second
        # pass only when the first one changed what it reads back (page
        # labels, navigation, outline); after an edit that keeps the slide
        # structure, the files from the previous compile are already right.
        if engine == "xelatex" and (
            _aux_state(work_dir, tex_path.stem) != before or _RERUN_HINT.search(last_log)
        ):
            if progress is not None:
                progress("Compiling LaTeX (pass 2)", min(40 + attempts * 10, 90))

            ok, last_log = _run_xelatex(tex_path, work_dir)
            attempts += 1
            if not ok:
                _try_repair(progress_pct=60)
                continue

        if not pdf_path.is_file():
            raise CompileError("LaTeX compilation failed: no PDF produced", last_log)

        if progress is not None:
            progress("Compilation complete", 100)
        return CompileResult(pdf_path=pdf_path, log=last_log, attempts=attempts)


# Files a LaTeX run writes and the next run reads back.
_AUX_SUFFIXES = (".aux", ".nav", ".toc", ".snm", ".out", ".vrb")
_RERUN_HINT = re.compile(r"Rerun to get|Label\(s\) may have changed|Please rerun", re.I)


def _aux_state(work_dir: Path, stem: str) -> dict[str, bytes | None]:
    state: dict[str, bytes | None] = {}
    for suffix in _AUX_SUFFIXES:
        path = work_dir / f"{stem}{suffix}"
        state[suffix] = path.read_bytes() if path.is_file() else None
    return state


def tex_engine() -> str:
    """Resolve TEXDECK_TEX_ENGINE to "xelatex" or "tectonic", or raise CompileError."""
    if settings.tex_engine == "auto":
        if shutil.which(settings.xelatex_command):
            return "xelatex"
        if shutil.which(settings.tectonic_command):
            return "tectonic"
        raise CompileError("No TeX engine found; install TeX Live (xelatex) or Tectonic", "")
    command = settings.xelatex_command if settings.tex_engine == "xelatex" else settings.tectonic_command
    if not shutil.which(command):
        raise CompileError(f"{settings.tex_engine} not found ({command})", "")
    return settings.tex_engine


def _run_xelatex(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
    """One engine run: an xelatex pass, or a full Tectonic build. Returns (ok, log)."""
    if tex_engine() == "tectonic":
        cmd = [
            settings.tectonic_command,
            "-X",
            "compile",
            "--untrusted",
            "--synctex",
            "--keep-logs",
            tex_path.name,
        ]
        timeout = settings.tectonic_timeout_seconds
    else:
        cmd = [
            settings.xelatex_command,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-synctex=1",
            tex_path.name,
        ]
        timeout = settings.xelatex_timeout_seconds
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # The Windows desktop build has no console; keep TeX from flashing one.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        partial = ""
        if exc.stdout:
            partial += exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(
                "utf-8", errors="replace"
            )
        if exc.stderr:
            partial += "\n" + (
                exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(
                    "utf-8", errors="replace"
                )
            )
        log = _read_compile_log(work_dir, tex_path.stem, partial)
        raise CompileError(f"LaTeX compilation timed out (>{timeout}s)", log) from exc
    except FileNotFoundError as exc:
        raise CompileError(f"{cmd[0]} not found", "") from exc

    proc_output = (proc.stdout or "") + (proc.stderr or "")
    log = _read_compile_log(work_dir, tex_path.stem, proc_output)
    return proc.returncode == 0, log


def _read_compile_log(work_dir: Path, stem: str, fallback: str) -> str:
    log_path = work_dir / f"{stem}.log"
    if log_path.is_file():
        try:
            return log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return fallback


def _repair_tex(
    tex_path: Path,
    compile_log: str,
    client: LlmClient,
    *,
    repair_index: int,
    llm_dump_dir: Path | None = None,
) -> str:
    """One repair model call.

    Returns:
        "changed" — cleaned output differs from current tex and was written.
        "noop" — cleaned output is effectively identical to current (echo); not written.
        "invalid" — extract_tex rejected the response; file left alone.
    """
    current = tex_path.read_text(encoding="utf-8")
    figures = figure_files(tex_path.parent)
    messages = build_repair_messages(current, compile_log, figures)
    repaired = client.complete(messages)
    try:
        # Repair output must be structurally complete; on failure leave the file alone
        cleaned = extract_tex(repaired, None, figures)
    except DeckError:
        if llm_dump_dir is not None:
            _dump_repair_llm(llm_dump_dir, repair_index, messages, repaired)
        return "invalid"

    if tex_effectively_equal(cleaned, current):
        # Model echoed the input; not a real repair. Dump with _noop for debugging.
        if llm_dump_dir is not None:
            _dump_repair_llm(
                llm_dump_dir, repair_index, messages, repaired, noop=True
            )
        return "noop"

    if llm_dump_dir is not None:
        _dump_repair_llm(llm_dump_dir, repair_index, messages, repaired)
    tex_path.write_text(cleaned, encoding="utf-8")
    return "changed"


def _sanitize_messages_for_dump(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace base64 image payloads with placeholders before writing to disk."""
    out: list[dict[str, Any]] = []
    n = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            out.append(msg)
            continue
        parts: list[Any] = []
        for part in content:
            img = isinstance(part, dict) and part.get("type") == "image_url"
            if img and isinstance(part.get("image_url"), dict):
                n += 1
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"<image:page_{n:03d}>"},
                    }
                )
            else:
                parts.append(part)
        out.append({**msg, "content": parts})
    return out


def _dump_repair_llm(
    dump_dir: Path,
    repair_index: int,
    messages: list[dict[str, Any]],
    response: str,
    *,
    noop: bool = False,
) -> None:
    dump_dir = Path(dump_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    stem = f"repair_{repair_index}_noop" if noop else f"repair_{repair_index}"
    (dump_dir / f"{stem}_prompt.json").write_text(
        json.dumps(
            _sanitize_messages_for_dump(messages),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (dump_dir / f"{stem}_response.txt").write_text(response, encoding="utf-8")


# ---- template previews ---------------------------------------------------------

# Rendered title-page PNG width for the template picker.
_PREVIEW_WIDTH_PX = 640

# One build at a time per template key; a second request for the same key waits
# and then finds the cached file instead of compiling again.
_preview_locks: dict[str, threading.Lock] = {}
_preview_locks_guard = threading.Lock()


def _preview_lock(key: str) -> threading.Lock:
    with _preview_locks_guard:
        return _preview_locks.setdefault(key, threading.Lock())


def preview_cache_dir() -> Path:
    # Underscore name can never collide with generation ids (hex + hyphen only),
    # and it has no meta.json, so list_generations skips it.
    return settings.artifact_root / "_previews"


def template_fingerprint(template: Template) -> str:
    """First 12 hex chars of sha256 over the whole template pack (names + bytes)."""
    h = hashlib.sha256()
    files: list[Path] = [template.dir / "template.json", template.dir / "main.tex"]
    files.extend(sorted(template.dir.glob("*.sty")))
    for folder_name in ("src", "font"):
        folder = template.dir / folder_name
        if folder.is_dir():
            files.extend(sorted(p for p in folder.rglob("*") if p.is_file()))
    for path in files:
        h.update(path.relative_to(template.dir).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    for rel, content in sorted(template.extra.items()):
        h.update(b"extra:" + rel.encode("utf-8") + b"\0")
        h.update(content.read_bytes() if isinstance(content, Path) else content)
        h.update(b"\0")
    return h.hexdigest()[:12]


def _compile_preview(tex_path: Path, work_dir: Path, passes: int = 0) -> None:
    """Compile the template's own main.tex; raise CompileError on failure.

    passes=0 runs as many xelatex passes as page references need; drafts ask
    for one, since a live preview does not need "page 3 of 12" right.
    Kept as a small seam so tests can substitute a fake PDF writer.
    """
    passes = passes or (2 if tex_engine() == "xelatex" else 1)
    log = ""
    for _ in range(passes):
        ok, log = _run_xelatex(tex_path, work_dir)
        if not ok:
            raise CompileError("Template preview compilation failed", log)
    if not tex_path.with_suffix(".pdf").is_file():
        raise CompileError("Template preview compilation produced no PDF", log)


def render_template_preview(template: Template) -> Path:
    """Return a cached PNG of the template's own title page, building on miss.

    Cache key is the template-pack fingerprint, so editing any template file
    invalidates the old image; stale hashes for the same key are deleted.
    """
    cache_dir = preview_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    fingerprint = template_fingerprint(template)
    cache_path = cache_dir / f"{template.key}-{fingerprint}.png"
    if cache_path.is_file():
        return cache_path

    with _preview_lock(template.key):
        if cache_path.is_file():  # built while we waited for the lock
            return cache_path
        with tempfile.TemporaryDirectory(prefix="texdeck-preview-") as tmp:
            work_dir = Path(tmp)
            copy_template_bundle(template, work_dir)
            tex_path = work_dir / "main.tex"
            # The template's own main.tex verbatim: its title page IS the preview.
            tex_path.write_text(template.main_tex, encoding="utf-8")
            _compile_preview(tex_path, work_dir)

            doc = fitz.open(work_dir / "main.pdf")
            try:
                page = doc.load_page(0)
                layout = json.dumps(title_page_layout(page))
                zoom = _PREVIEW_WIDTH_PX / max(page.rect.width, 1.0)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                tmp_png = cache_dir / f"{template.key}-{fingerprint}.png.tmp"
                # Explicit output: the .tmp suffix must not confuse format detection
                pixmap.save(str(tmp_png), output="png")
            finally:
                doc.close()
            # Layout first: a published PNG always has its layout beside it.
            cache_path.with_suffix(".json").write_text(layout, encoding="utf-8")
            # Atomic publish; readers never see a half-written PNG.
            tmp_png.replace(cache_path)

        # Drop files of older template revisions (exact key + 12-hex-hash names
        # only, so a key that happens to be a prefix of another is never hit).
        for sibling in cache_dir.glob(f"{template.key}-*"):
            if sibling.suffix not in (".png", ".json") or sibling.stem == cache_path.stem:
                continue
            if re.fullmatch(r"[0-9a-f]{12}", sibling.stem[len(template.key) + 1 :]):
                sibling.unlink(missing_ok=True)
    return cache_path


def template_title_layout(template: Template) -> list[dict[str, Any]]:
    """Text and logo boxes on the template's title page (see title_page_layout)."""
    png = render_template_preview(template)
    layout = png.with_suffix(".json")
    if not layout.is_file():  # cached by an older version that kept no layout
        png.unlink(missing_ok=True)
        layout = render_template_preview(template).with_suffix(".json")
    return json.loads(layout.read_text(encoding="utf-8"))


def title_page_layout(page: fitz.Page) -> list[dict[str, Any]]:
    """Boxes (fractions of the page) of the text and images drawn over the
    background, so the background editor can show what will cover a picture."""
    width, height = page.rect.width, page.rect.height
    boxes: list[dict[str, Any]] = []

    def add(kind: str, rect: fitz.Rect) -> None:
        rect = rect & page.rect
        if rect.is_empty:
            return
        boxes.append({
            "kind": kind,
            "x": round(rect.x0 / width, 4),
            "y": round(rect.y0 / height, 4),
            "w": round(rect.width / width, 4),
            "h": round(rect.height / height, 4),
        })

    for block in page.get_text("blocks"):
        if block[6] == 0 and str(block[4]).strip():
            add("text", fitz.Rect(block[:4]))
    for info in page.get_image_info():
        rect = fitz.Rect(info["bbox"])
        if rect.get_area() < 0.5 * page.rect.get_area():  # the background itself is not a guide
            add("image", rect)
    return boxes


def render_template_pdf(template: Template) -> bytes:
    """Build the template's sample deck uncached (custom-template drafts)."""
    with tempfile.TemporaryDirectory(prefix="texdeck-draft-") as tmp:
        work_dir = Path(tmp)
        copy_template_bundle(template, work_dir)
        tex_path = work_dir / "main.tex"
        tex_path.write_text(template.main_tex, encoding="utf-8")
        _compile_preview(tex_path, work_dir, passes=1)
        return (work_dir / "main.pdf").read_bytes()


def _strip_markdown_fences(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop opening fence line (``` or ```latex)
    nl = s.find("\n")
    s = s[nl + 1 :] if nl != -1 else s[3:]
    s = s.rstrip()
    if s.endswith("```"):
        s = s[: -3].rstrip()
    return s
