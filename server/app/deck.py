from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(slots=True)
class DeckRequest:
    template_key: str
    title: str
    author: str = ""  # optional; "" if not provided
    institute: str = ""  # optional; "" if not provided
    instruction: str = ""  # free-form user request; may be ""
    source_text: str = ""  # paste-text body; "" in PDF mode


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
    return Template(key=key, name=name, dir=directory, main_tex=main_tex)


def copy_template_bundle(template: Template, target_dir: Path) -> None:
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    sty = template.dir / "collegebeamer.sty"
    if sty.is_file():
        shutil.copy2(sty, target_dir / "collegebeamer.sty")

    for folder_name in ("src", "font"):
        src = template.dir / folder_name
        if src.is_dir():
            dest = target_dir / folder_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)


def build_generation_messages(
    template: Template,
    request: DeckRequest,
    document: PdfDocument | None,
) -> list[dict[str, Any]]:
    system = (
        "Output only one complete, compilable XeLaTeX Beamer document. "
        "No explanation, no markdown fences."
    )

    title_parts = [f"Title: {request.title}"]
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
    title_message = "\n".join(title_parts)

    if document is not None:
        pages = list(document.selected_pages)
        if not pages:
            raise DeckError("No source material available for generation")
        material: list[dict[str, Any]] = []
        for page in pages:
            material.append(image_block(page.image_path))
            material.append(text_block(f"Page {page.number}"))
        material_content: str | list[dict[str, Any]] = material
    else:
        source = request.source_text
        if not source.strip():
            raise DeckError("No source material available for generation")
        material_content = source

    # Five segments: title+request is repeated after the material (W11 experiment:
    # baseline 4-msg produced 10 frames; 5-msg produced 8 — closer to "under 8").
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": template.main_tex},
        {"role": "user", "content": title_message},
        {"role": "user", "content": material_content},
        {"role": "user", "content": title_message},
    ]


def extract_tex(response: str, template: Template | None) -> str:
    # template is required on the generation path; repair passes None
    # (no template-field comparisons today).
    _ = template
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
    if theme_directive is not None and theme_directive not in _strip_tex_comments(tex):
        raise DeckError(f"Incomplete model LaTeX: missing {theme_directive}")

    # Template header comments may keep [STYLE REF] text; only the de-commented
    # body is checked for leftovers.
    body = _strip_tex_comments(tex)
    if "[STYLE REF]" in body:
        raise DeckError("Incomplete model LaTeX: leftover [STYLE REF] sample page")
    if "AI GENERATION INSTRUCTIONS" in body:
        raise DeckError(
            "Incomplete model LaTeX: leftover AI GENERATION INSTRUCTIONS page"
        )

    # Product hard rule: users supply no external images; \includegraphics always fails.
    if r"\includegraphics" in body:
        raise DeckError(
            "Model output uses \\includegraphics, but this product provides no external image files"
        )

    return tex


_THEME_DIRECTIVE_RE = re.compile(
    r"\\usepackage(?:\[[^\]]*\])?\{[a-zA-Z]*beamer[a-zA-Z]*\}|\\usetheme\{[^}]+\}"
)


def _theme_directive(template: Template | None) -> str | None:
    """The line that loads the template's theme; the model must keep it verbatim."""
    if template is None:
        return None
    match = _THEME_DIRECTIVE_RE.search(_strip_tex_comments(template.main_tex))
    return match.group(0) if match else None


def _strip_tex_comments(tex: str) -> str:
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


def build_repair_messages(tex: str, compile_log: str) -> list[dict[str, Any]]:
    error_block = _extract_compile_error_block(compile_log)
    tex_context = _tex_context_around_error(tex, compile_log)
    preamble_note = (
        "The document preamble is fixed and must not be changed. Allowed packages: "
        "collegebeamer / tikz / pgfplots / amsmath / amssymb / pifont / "
        "booktabs / colortbl / multirow. "
        "There are no external image files; do not use \\includegraphics; "
        "do not add new \\usepackage lines."
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


def _tex_effectively_equal(a: str, b: str) -> bool:
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

    xelatex = settings.xelatex_command
    if not shutil.which(xelatex):
        raise CompileError("xelatex not found; please install TeX Live", "")

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

        ok, last_log = _run_xelatex(tex_path, work_dir)
        attempts += 1
        if not ok:
            _try_repair(progress_pct=50)
            continue

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


def _run_xelatex(tex_path: Path, work_dir: Path) -> tuple[bool, str]:
    cmd = [
        settings.xelatex_command,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-synctex=1",
        tex_path.name,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=settings.xelatex_timeout_seconds,
            check=False,
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
        raise CompileError(
            f"LaTeX compilation timed out (>{settings.xelatex_timeout_seconds}s)",
            log,
        ) from exc
    except FileNotFoundError as exc:
        raise CompileError("xelatex not found; please install TeX Live", "") from exc

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
    messages = build_repair_messages(current, compile_log)
    repaired = client.complete(messages)
    try:
        # Repair output must be structurally complete; on failure leave the file alone
        cleaned = extract_tex(repaired, None)
    except DeckError:
        if llm_dump_dir is not None:
            _dump_repair_llm(llm_dump_dir, repair_index, messages, repaired)
        return "invalid"

    if _tex_effectively_equal(cleaned, current):
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
