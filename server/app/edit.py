from __future__ import annotations

import difflib
import gzip
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class EditError(RuntimeError):
    """Single-frame edit failed; message is user-facing English."""


@dataclass(slots=True)
class Frame:
    index: int  # order of appearance in the tex, 1-based
    title: str  # title from \begin{frame}{title}; "" if absent
    start_line: int  # 1-based, line of \begin{frame}
    end_line: int  # 1-based, line of \end{frame}
    tex: str  # full frame source including the begin/end lines
    page: int  # PDF page from synctex; 0 if unresolved


# Match bare frame / [opt] / [opt]{title} / {title}
_BEGIN_FRAME_RE = re.compile(
    r"\\begin\{frame\}(?:\[[^\]]*\])?(?:\{([^}]*)\})?"
)
_END_FRAME = r"\end{frame}"

# Shared by single-slide edits and whole-deck revisions.
KEEP_NOTES_RULE = (
    "Keep each frame's \\note{...} speaker notes. When a change alters what a slide "
    "says, update its note to match; when asked for notes, write them as \\note{...} "
    "inside the frame, just before \\end{frame}."
)


def parse_frames(tex: str) -> list[Frame]:
    """Pair \\begin{frame} with \\end{frame} line by line; page starts as 0."""
    lines = tex.splitlines()
    frames: list[Frame] = []
    open_start: int | None = None
    open_title = ""
    index = 0

    for i, line in enumerate(lines):
        line_no = i + 1
        if _is_comment_line(line):
            continue

        if r"\begin{frame}" in line:
            if open_start is not None:
                raise EditError(f"Unclosed frame starting at line {open_start}")
            m = _BEGIN_FRAME_RE.search(line)
            if m is None:
                # Literal match but irregular form; still treat as frame start
                open_title = ""
                begin_end = line.index(r"\begin{frame}") + len(r"\begin{frame}")
            else:
                open_title = m.group(1) if m.group(1) is not None else ""
                begin_end = m.end()
            open_start = line_no
            # Compact frames close on the line they open; models emit these.
            if _END_FRAME in line[begin_end:]:
                index += 1
                frames.append(
                    Frame(
                        index=index,
                        title=open_title,
                        start_line=line_no,
                        end_line=line_no,
                        tex=line,
                        page=0,
                    )
                )
                open_start = None
                open_title = ""
            continue

        if _END_FRAME in line:
            if open_start is None:
                continue
            index += 1
            start = open_start
            end = line_no
            frame_tex = "\n".join(lines[start - 1 : end])
            frames.append(
                Frame(
                    index=index,
                    title=open_title,
                    start_line=start,
                    end_line=end,
                    tex=frame_tex,
                    page=0,
                )
            )
            open_start = None
            open_title = ""

    if open_start is not None:
        raise EditError(f"Unclosed frame starting at line {open_start}")

    return frames


def resolve_frame_pages(frames: list[Frame], tex_path: Path) -> list[Frame]:
    """Fill page via forward synctex for each frame; leave 0 if unresolved.

    Query uses end_line (\\end{frame}), not start_line:
    Beamer's \\begin{frame} line usually has no typeset content, so synctex
    lands on the previous page. Measured on real main.tex — not a heuristic.
    """
    tex_path = Path(tex_path)
    lines = read_synctex_lines(tex_path)
    for frame in frames:
        frame.page = _forward_page(lines, frame.end_line)
    return frames


def read_synctex_lines(tex_path: Path) -> dict[int, int]:
    """Map each line of tex_path that produced output to its first PDF page.

    Reads <stem>.synctex.gz directly, so neither TeX Live's synctex binary nor
    a full TeX install is needed (Tectonic writes the same format).
    """
    tex_path = Path(tex_path)
    sync = tex_path.with_suffix(".synctex.gz")
    if not sync.is_file():
        raise EditError("No synctex data next to the PDF; recompile the deck")
    try:
        with gzip.open(sync, "rt", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except (OSError, EOFError) as exc:
        raise EditError(f"Could not read synctex data: {exc}") from exc

    target = tex_path.name
    tags: set[str] = set()
    first_page: dict[int, int] = {}
    page = 0
    for raw in text.splitlines():
        if raw.startswith("Input:"):
            _, tag, name = raw.split(":", 2)
            if name and Path(name).name == target:
                tags.add(tag)
        elif raw.startswith("{"):
            page = int(raw[1:]) if raw[1:].isdigit() else page
        elif page and raw[:1] in _SYNCTEX_NODES:
            head = raw[1:].split(":", 1)[0]
            tag, _, line = head.partition(",")
            if tag in tags and line.isdigit():
                first_page.setdefault(int(line), page)
    return first_page


# Record kinds that carry "tag,line:" — boxes, glue, kerns, math, current point.
_SYNCTEX_NODES = frozenset("[(vhxkg$")


def _forward_page(lines: dict[int, int], line_no: int) -> int:
    """Page of the nearest line with output, preferring the exact line then later ones."""
    if line_no in lines:
        return lines[line_no]
    for offset in range(1, 50):
        for candidate in (line_no + offset, line_no - offset):
            if candidate in lines:
                return lines[candidate]
    return 0


_NOTE_RE = re.compile(r"\\note\s*(?:\[[^\]]*\])?\s*\{")
_TEXT_CMDS = re.compile(
    r"\\(?:textbf|textit|textsl|textsc|texttt|textrm|textsf|emph|alert|underline|"
    r"structure|mathrm|mathbf|mathit|text|mbox|url)\s*\{([^{}]*)\}"
)


def frame_notes(frame_tex: str) -> str:
    """Speaker notes of one frame: the \\note{...} bodies as plain text."""
    notes = []
    for m in _NOTE_RE.finditer(frame_tex):
        depth, i = 1, m.end()
        while i < len(frame_tex) and depth:
            if frame_tex[i] == "\\":
                i += 2
                continue
            depth += {"{": 1, "}": -1}.get(frame_tex[i], 0)
            i += 1
        if depth == 0:
            notes.append(latex_to_text(frame_tex[m.end() : i - 1]))
    return "\n\n".join(n for n in notes if n)


_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ",
    "phi": "φ", "varphi": "φ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "cdot": "·", "approx": "≈", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "neq": "≠", "pm": "±", "to": "→", "rightarrow": "→", "leftarrow": "←", "infty": "∞",
    "in": "∈", "sum": "∑", "ldots": "…", "dots": "…", "%": "%",
}


def latex_to_text(src: str) -> str:
    """Good-enough plain text for prose notes (not a LaTeX interpreter)."""
    text = "\n".join(line for line in src.splitlines() if not _is_comment_line(line))
    for _ in range(4):  # nested \textbf{\emph{..}}
        text = _TEXT_CMDS.sub(r"\1", text)
    text = re.sub(r"\\\\(\[[^\]]*\])?", "\n", text)
    text = re.sub(r"\\par\b", "\n\n", text)
    text = re.sub(r"\\([a-zA-Z]+)\b ?", lambda m: _SYMBOLS.get(m.group(1), m.group(0)), text)
    text = re.sub(r"\\[a-zA-Z]+\*?\s*", "", text)  # remaining commands
    # Unescaped markup first, then the escaped literals it would have eaten.
    text = re.sub(r"(?<!\\)[${}]", "", text)
    text = re.sub(r"(?<!\\)~", " ", text)
    text = re.sub(r"\\([%&_#${}])", r"\1", text)
    text = text.replace("---", "\u2014").replace("--", "\u2013")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def build_frame_edit_messages(
    frame: Frame,
    instruction: str,
    template_main_tex: str,
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a single-frame Beamer editor. Output only one complete "
                "\\begin{frame}...\\end{frame} block. "
                "No explanation, no markdown fences, and do not change anything outside this frame. "
                + KEEP_NOTES_RULE
            ),
        },
        {
            "role": "user",
            "content": template_main_tex,
        },
        {
            "role": "user",
            "content": (
                f"Full source of the current frame:\n{frame.tex}\n\n"
                f"User edit request:\n{instruction}"
            ),
        },
    ]


def extract_frame(response: str) -> str:
    """After stripping fences, require exactly one full frame block; else raise."""
    s = _strip_markdown_fences(response)
    begins = list(re.finditer(r"\\begin\{frame\}", s))
    ends = list(re.finditer(r"\\end\{frame\}", s))
    if len(begins) != 1 or len(ends) != 1 or begins[0].start() >= ends[0].start():
        raise EditError("Model did not return a complete slide frame block")
    return s[begins[0].start() : ends[0].end()]


def replace_frame(tex: str, frame: Frame, new_frame_tex: str) -> str:
    """Replace lines [start_line, end_line]; leave everything else unchanged."""
    lines = tex.splitlines()
    n = len(lines)
    if frame.start_line < 1 or frame.end_line > n or frame.start_line > frame.end_line:
        raise EditError(
            f"Invalid frame line range: {frame.start_line}-{frame.end_line} "
            f"(document has {n} lines)"
        )
    new_lines = new_frame_tex.splitlines()
    out = lines[: frame.start_line - 1] + new_lines + lines[frame.end_line :]
    result = "\n".join(out)
    if tex.endswith("\n"):
        result += "\n"
    return result


def _is_comment_line(line: str) -> bool:
    return line.lstrip().startswith("%")


def _strip_markdown_fences(text: str) -> str:
    s = text.strip()
    if not s.startswith("```"):
        return s
    nl = s.find("\n")
    s = s[nl + 1 :] if nl != -1 else s[3:]
    s = s.rstrip()
    if s.endswith("```"):
        s = s[: -3].rstrip()
    return s


def _flat(s: str) -> str:
    return re.sub(r"\s+", "", latex_to_text(s))


def locate_line(tex: str, text: str, frame: Frame | None = None) -> tuple[int, str] | None:
    """Source line (1-based number, raw text) that typesets `text`, clicked on a slide.

    Lines are compared as plain text (markup stripped, whitespace ignored) by
    their longest common run with the clicked text, so a PDF line that is
    only part of a wrapped source line, or a line with inline math, still
    matches. The frame shown on the page is searched first; the whole file
    is the fallback (title page fields live in the preamble).
    """
    target = re.sub(r"\s+", "", text)
    if not target:
        return None
    lines = tex.splitlines()
    need = min(len(target), max(2, int(len(target) * 0.6)))

    def best(numbers: range) -> tuple[int, int] | None:
        found: tuple[int, int] | None = None
        for n in numbers:
            line = lines[n - 1]
            if not line.strip() or _is_comment_line(line):
                continue
            flat = _flat(line)
            if not flat:
                continue
            m = difflib.SequenceMatcher(None, target, flat, autojunk=False)
            size = m.find_longest_match(0, len(target), 0, len(flat)).size
            if size >= need and (found is None or size > found[1]):
                found = (n, size)
        return found

    hit = best(range(frame.start_line, frame.end_line + 1)) if frame else None
    if hit is None:
        hit = best(range(1, len(lines) + 1))
    return (hit[0], lines[hit[0] - 1]) if hit else None


def replace_line(tex: str, line_no: int, old: str, new: str) -> str:
    """Swap one source line, refusing if it is no longer what the user saw."""
    lines = tex.split("\n")
    if not 1 <= line_no <= len(lines) or lines[line_no - 1] != old:
        raise EditError("The source changed since you clicked; click the text again")
    lines[line_no - 1] = new
    return "\n".join(lines)
