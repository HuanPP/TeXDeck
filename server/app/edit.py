from __future__ import annotations

import difflib
import gzip
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .deck import DIAGRAM_RULE
from .llm import text_block


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
    "in": "∈", "sum": "∑", "ldots": "…", "dots": "…", "%": "%", "Vert": "∥", "parallel": "∥",
}


def latex_to_text(src: str) -> str:
    """Good-enough plain text for prose notes (not a LaTeX interpreter)."""
    text = "\n".join(line for line in src.splitlines() if not _is_comment_line(line))
    for _ in range(4):  # nested \textbf{\emph{..}}
        text = _TEXT_CMDS.sub(r"\1", text)
    text = re.sub(r"\\\\(\[[^\]]*\])?", "\n", text)
    text = re.sub(r"\\par\b", "\n\n", text)
    text = re.sub(r"\\([a-zA-Z]+)(?![a-zA-Z]) ?", lambda m: _SYMBOLS.get(m.group(1), m.group(0)), text)
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
    figure_catalog: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a single-frame Beamer editor. Output only one complete "
                "\\begin{frame}...\\end{frame} block. "
                "No explanation, no markdown fences, and do not change anything outside this frame. "
                + KEEP_NOTES_RULE + " " + DIAGRAM_RULE
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
                + (f"{figure_catalog}\n\n" if figure_catalog else "")
                + f"User edit request:\n{instruction}"
            ),
        },
    ]


_FRAME_MARK_RE = re.compile(r"^%+\s*(?:FRAME\s+(\d+)|(TITLE))\s*$", re.MULTILINE)


_TITLE_RULE = (
    "The title page comes from the preamble's title commands, marked %% TITLE: answer it "
    "with a line `%% TITLE` followed by the revised \\title, \\subtitle, \\author, "
    "\\institute and \\date commands only (keep any [short] arguments). "
)


def _with_attachments(
    parts: list[tuple[str, list[dict[str, Any]]]], context: str
) -> str | list[dict[str, Any]]:
    """Join (text, extra blocks) parts into one message; plain text when nothing is attached."""
    texts = [context] if context else []
    if not any(blocks for _, blocks in parts):
        return "\n\n".join(texts + [text for text, _ in parts])
    content: list[dict[str, Any]] = [text_block(t) for t in texts]
    for text, blocks in parts:
        content.append(text_block(text))
        content.extend(blocks)
    return content


def build_frames_edit_messages(
    requests: list[tuple[Frame, str]],
    template_main_tex: str,
    context: str = "",
    title: tuple[str, str] | None = None,
    attachments: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """One call that revises several frames; each comes back after a `%% FRAME n` line.

    context: anything every change needs to know (the deck's image files...).
    title: (the title commands, the change asked for the title page), if any.
    attachments: frame index (TITLE_INDEX for the title) -> content blocks shown
    right after that change, such as new images the user attached to it.
    """
    attachments = attachments or {}
    parts = [
        (
            f"%% FRAME {frame.index}\n{frame.tex}\n\nChange for frame {frame.index}:\n{instruction}",
            attachments.get(frame.index, []),
        )
        for frame, instruction in requests
    ]
    title_rule = ""
    if title is not None:
        parts.insert(
            0,
            (f"%% TITLE\n{title[0]}\n\nChange for the title page:\n{title[1]}", attachments.get(TITLE_INDEX, [])),
        )
        title_rule = _TITLE_RULE
    content = _with_attachments(parts, context)
    return [
        {
            "role": "system",
            "content": (
                "You are editing several frames of a Beamer deck at once. For every frame "
                "below, output the complete revised frame: first a line `%% FRAME <number>`, "
                "then its whole \\begin{frame}...\\end{frame} block. Output exactly these "
                "frames, in the given order, and nothing else: no explanation, no markdown "
                "fences, nothing outside the frames. " + title_rule + KEEP_NOTES_RULE + " " + DIAGRAM_RULE
            ),
        },
        {"role": "user", "content": template_main_tex},
        {"role": "user", "content": content},
    ]


def extract_frames(response: str, indices: list[int]) -> dict[int, str]:
    """The `%% FRAME n` blocks of a multi-frame reply; every requested frame must be there.

    TITLE_INDEX in `indices` asks for the `%% TITLE` block too (its text, unchecked).
    """
    text = _strip_markdown_fences(response)
    marks = list(_FRAME_MARK_RE.finditer(text))
    found: dict[int, str] = {}
    for k, mark in enumerate(marks):
        index = TITLE_INDEX if mark.group(2) else int(mark.group(1))
        end = marks[k + 1].start() if k + 1 < len(marks) else len(text)
        if index in indices and index not in found:
            chunk = text[mark.end() : end]
            found[index] = chunk.strip("\n") if index == TITLE_INDEX else extract_frame(chunk)
    missing = [i for i in indices if i not in found]
    if missing:
        raise EditError(f"Model did not return slide(s) {', '.join(map(str, missing))}")
    return found


def replace_frames(tex: str, frames: list[Frame], new: dict[int, str]) -> str:
    """Swap several frames, and the title commands for TITLE_INDEX.

    Bottom-up, so earlier line numbers stay valid; the title commands sit in
    the preamble above every frame, so they go last.
    """
    for frame in sorted((f for f in frames if f.index in new), key=lambda f: f.start_line, reverse=True):
        tex = replace_frame(tex, frame, new[frame.index])
    if TITLE_INDEX in new:
        block = title_block(tex)
        if block is None:
            raise EditError("This deck has no title commands to change")
        tex = replace_title_block(tex, block, new[TITLE_INDEX])
    return tex


DECK_INDEX = -1  # attachments key for the whole-deck request

_DECK_MARK_RE = re.compile(
    r"^%+\s*(?:FRAME\s+(\d+)|DELETE\s+(\d+)|INSERT\s+AFTER\s+(\d+)|(TITLE))\s*$", re.MULTILINE
)
_NUMBER_MARK_RE = re.compile(r"^%% FRAME \d+\n", re.MULTILINE)

DECK_REPLY_RULE = (
    "Every frame of the deck is preceded by a line `%% FRAME <number>`. Reply with only what "
    "changes, as blocks: `%% FRAME <n>` followed by the complete revised frame (or several "
    "frames, to split it); `%% DELETE <n>` to remove frame n; `%% INSERT AFTER <n>` followed by "
    "new frames and any \\section lines they need (n = 0 puts them before the first frame); "
    "`%% TITLE` followed by the revised \\title, \\subtitle, \\author, \\institute and \\date "
    "commands only. Leave out every frame that stays the same, and always use the numbers of "
    "the deck as given, not as they will be after your changes. Only when the preamble must "
    "change, or when most frames change, output the complete revised document from "
    "\\documentclass to \\end{document} instead. No explanation, no markdown fences."
)


def numbered_deck(tex: str) -> str:
    """The deck with a `%% FRAME n` line before every frame, as the model sees it."""
    lines = tex.split("\n")
    for frame in reversed(parse_frames(tex)):
        lines.insert(frame.start_line - 1, f"%% FRAME {frame.index}")
    return "\n".join(lines)


def build_deck_edit_messages(
    tex: str,
    template_main_tex: str,
    instruction: str,
    pages: list[tuple[int, str]] = (),  # type: ignore[assignment]
    context: str = "",
    attachments: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """One call for a whole-deck request plus any per-slide requests; the reply is
    applied by apply_deck_reply.

    pages: (frame index or TITLE_INDEX, change) pairs.
    attachments: request key (frame index, TITLE_INDEX, DECK_INDEX) -> extra blocks.
    """
    attachments = attachments or {}
    parts: list[tuple[str, list[dict[str, Any]]]] = []
    if instruction.strip():
        parts.append((f"Revision request for the whole deck:\n{instruction}", attachments.get(DECK_INDEX, [])))
    for index, change in pages:
        label = "the title page (answer with %% TITLE)" if index == TITLE_INDEX else f"frame {index}"
        parts.append((f"Change for {label}:\n{change}", attachments.get(index, [])))
    return [
        {
            "role": "system",
            "content": (
                "You are revising a XeLaTeX Beamer deck. " + DECK_REPLY_RULE + " "
                + KEEP_NOTES_RULE + " " + DIAGRAM_RULE
            ),
        },
        {"role": "user", "content": template_main_tex},
        {"role": "user", "content": numbered_deck(tex)},
        {"role": "user", "content": _with_attachments(parts, context)},
    ]


def _frames_text(chunk: str, what: str) -> list[str]:
    """Lines up to the last \\end{frame} (\\section lines may come first); at least
    one complete frame."""
    text = _strip_markdown_fences(chunk).strip("\n")
    try:
        frames = parse_frames(text)
    except EditError as e:
        raise EditError(f"{what}: {e}") from e
    if not frames:
        raise EditError(f"{what}: no complete \\begin{{frame}}...\\end{{frame}} block")
    return text.split("\n")[: frames[-1].end_line]


def apply_deck_reply(tex: str, reply: str, required: list[int] = ()) -> str:  # type: ignore[assignment]
    """Apply a reply written to DECK_REPLY_RULE: a full document, or change blocks.

    required: frame indices (TITLE_INDEX for the title) the user asked about; each
    must be answered by a FRAME, DELETE or TITLE block unless the whole document
    came back.
    """
    text = _strip_markdown_fences(reply)
    if r"\documentclass" in text:
        return _NUMBER_MARK_RE.sub("", text)
    marks = list(_DECK_MARK_RE.finditer(text))
    if not marks:
        raise EditError("The model returned neither changed slides nor a full document")
    frames = parse_frames(tex)
    count = len(frames)
    edits: list[tuple[int, int, list[str]]] = []  # 0-based [start, end) line ranges
    touched: set[int] = set()
    title_text: str | None = None
    for k, mark in enumerate(marks):
        chunk = text[mark.end() : marks[k + 1].start() if k + 1 < len(marks) else len(text)]
        replace, delete, after, title = mark.groups()
        if title:
            title_text = chunk.strip("\n")
            touched.add(TITLE_INDEX)
            continue
        n = int(replace or delete or after)
        if not (0 if after else 1) <= n <= count:
            raise EditError(f"The model referred to slide {n}, but the deck has {count}")
        if after:
            at = frames[n - 1].end_line if n else frames[0].start_line - 1 if frames else 0
            edits.append((at, at, _frames_text(chunk, f"New slides after {n}")))
            continue
        if n in touched:
            raise EditError(f"The model changed slide {n} twice")
        touched.add(n)
        frame = frames[n - 1]
        new = [] if delete else _frames_text(chunk, f"Slide {n}")
        edits.append((frame.start_line - 1, frame.end_line, new))
    missing = [i for i in required if i not in touched]
    if missing:
        names = ", ".join("title page" if i == TITLE_INDEX else str(i) for i in missing)
        raise EditError(f"Model did not answer the request for slide(s) {names}")
    lines = tex.split("\n")
    for start, end, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        lines[start:end] = new
    out = "\n".join(lines)
    if title_text is not None:
        block = title_block(out)
        if block is None:
            raise EditError("This deck has no title commands to change")
        out = replace_title_block(out, block, title_text)
    return out


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


def _fold(text: str) -> str:
    """Comparable form of slide text, one character per input character.

    NFKC maps the math alphabet in PDF text (𝑚, 𝐶, ℝ) to plain letters as
    written in the source; case is folded for small caps. Whitespace is dropped.
    """
    return "".join(_DASHES.get(c) or unicodedata.normalize("NFKC", c).casefold()[:1]
                   for c in text if not c.isspace())


# PDF text has the typeset dash where the source has -- or ---; both sides
# compare as one hyphen (repeated hyphens are dropped from source text).
_DASHES = {"\u2013": "-", "\u2014": "-", "\u2212": "-"}


_LEAST_RUN = 3  # shorter shared runs are coincidence ("01" in "2012", a full stop)


def _shared_runs(target: str, flat: str) -> list[difflib.Match]:
    """Runs `target` shares with `flat`, in order, as the clicked text would
    appear in the source: together. Runs under three characters are
    coincidence ("01" in "2012") unless the clicked text is that short, and
    only the best group of runs within twice the clicked length counts, so
    scattered pieces of a long paragraph do not add up to a match."""
    least = min(_LEAST_RUN, len(target))
    blocks = [b for b in difflib.SequenceMatcher(None, target, flat, autojunk=False).get_matching_blocks()
              if b.size >= least]
    best: list[difflib.Match] = []
    best_size = 0
    for i, first in enumerate(blocks):
        size = 0
        for j in range(i, len(blocks)):
            if blocks[j].b + blocks[j].size - first.b > 2 * len(target):
                break
            size += blocks[j].size
            if size > best_size:
                best, best_size = blocks[i:j + 1], size
    return best


def locate_line(
    tex: str,
    text: str,
    frame: Frame | None = None,
    frames: list[Frame] | None = None,
    before: str = "",
    after: str = "",
) -> tuple[int, str] | None:
    """Source that typesets `text`, clicked on a slide: (first line number,
    the line or consecutive lines joined by newlines).

    Only lines that can typeset the page are searched: the page's frame (if
    the page has one; section pages do not) and everything outside frames
    (title fields in the preamble, \\section). Without `frames`, the whole
    file. TeX reads consecutive non-blank lines as one paragraph, so a
    candidate is a line or a run of such lines (a display formula split over
    lines) in which every line matches at least a run's worth of the clicked
    text that the others do not.

    Candidates are compared as plain text by the characters they share with
    the clicked text (see _shared_runs). Ties go to the candidate that
    carries the clicked text on into the lines `before` and `after` it on
    the slide (a wrapped fragment, one of several identical labels), then to
    the one whose clicked sentence the text covers most (a heading "讨论"
    over a sentence containing it), then to the page's frame, then to fewer
    lines.
    """
    target = _fold(text)
    if not target:
        return None
    head = _fold(before)
    around = head + target + _fold(after)
    lines = tex.splitlines()
    need = min(len(target), max(2, int(len(target) * 0.6)))
    least = min(_LEAST_RUN, len(target))  # what counts as a line's own contribution
    others = {n for f in frames or [] if f is not frame for n in range(f.start_line, f.end_line + 1)}
    shown = {}  # line number -> folded visible text
    for n in range(1, len(lines) + 1):
        line = lines[n - 1]
        if n not in others and line.strip() and not _is_comment_line(line):
            shown[n] = _fold(_visible_text(line))

    found: tuple[tuple[int, int, float, bool, int], int, int] | None = None
    for first in shown:
        flat, last, size = "", first, 0
        while last in shown:
            # A further line joins only if the clicked text itself matches more.
            grown = sum(b.size for b in _shared_runs(target, flat + shown[last]))
            if last > first and grown - size < least:
                break
            flat, size = flat + shown[last], grown
            last += 1
            if not flat or size < need:
                continue
            # ... and the first line must be needed as well.
            if last - 1 > first and size - sum(b.size for b in _shared_runs(target, flat[len(shown[first]):])) < least:
                continue
            source = "\n".join(lines[first - 1:last - 1])
            span = text_span(source, text)
            sentence = _fold(_visible_text(source[span.start:span.end])) or flat
            score = (
                size,
                _continuation(around, len(head), len(head) + len(target), flat),
                size / max(len(sentence), size),
                frame is not None and frame.start_line <= first <= frame.end_line,
                first - last,
            )
            if found is None or score > found[0]:
                found = (score, first, last - 1)
    if found is None:
        return None
    return found[1], "\n".join(lines[found[1] - 1:found[2]])


def _continuation(around: str, start: int, end: int, flat: str) -> int:
    """How far `flat` continues the clicked text around[start:end] into its
    neighbours: the parts of shared runs that cross the click's edges.
    Words of the neighbours found elsewhere in `flat` are no evidence."""
    total = 0
    for b in _shared_runs(around, flat):
        if b.a < end and b.a + b.size > start:
            total += max(0, start - b.a) + max(0, b.a + b.size - end)
    return total


def replace_line(tex: str, line_no: int, old: str, new: str) -> str:
    """Swap the source line(s) `old` starting at line_no, refusing if they are
    no longer what the user saw."""
    lines = tex.split("\n")
    count = old.count("\n") + 1
    if not 1 <= line_no <= len(lines) - count + 1 or "\n".join(lines[line_no - 1:line_no - 1 + count]) != old:
        raise EditError("The source changed since you clicked; click the text again")
    lines[line_no - 1:line_no - 1 + count] = new.split("\n")
    return "\n".join(lines)


# --- Plain-text editing of one source line ---------------------------------
#
# A clicked line is shown as the words on the slide, not as LaTeX: the line is
# split into literal characters and markup, the run of characters the user
# clicked is found, and edits to that run are written back character by
# character so the markup around and inside it (\item, \title{..}, \textbf{..})
# stays put.

# Font/emphasis wrappers whose one argument is ordinary slide text.
_WRAPPERS = frozenset(
    "textbf textit emph alert structure underline texttt textsf textrm textsc "
    "textup textsl textmd textnormal mbox".split()
)
_ESCAPED = "%&#_${} "  # \% \& ... and "\ " typeset as the character itself
_SPACES = frozenset("quad qquad enspace enskip thinspace space".split())


@dataclass
class _Tok:
    kind: str  # "char", "open" (wrapper start), "close" (wrapper end), "markup"
    start: int
    end: int
    char: str = ""


def _tokenize_line(line: str) -> list[_Tok]:
    toks: list[_Tok] = []
    stack: list[str] = []  # "wrap" / "brace" / "bracket" for open groups
    i, n = 0, len(line)
    after_cmd = False  # a "[" right after a command name opens its optional argument
    while i < n:
        c = line[i]
        if c == "%":  # a comment runs to the end of its source line
            stop = line.find("\n", i)
            stop = n if stop < 0 else stop
            toks.append(_Tok("markup", i, stop))
            i = stop
            continue
        if c == "\\" and i + 1 < n:
            env = re.match(r"\\(begin|end)\{[^{}]*\}", line[i:])
            if env:  # the environment name is not slide text
                toks.append(_Tok("markup", i, i + env.end()))
                i, after_cmd = i + env.end(), True
                continue
            m = re.match(r"\\([a-zA-Z]+)\*?", line[i:])
            if m:
                name, j = m.group(1), i + m.end()
                if name in _SPACES:
                    toks.append(_Tok("char", i, j, " "))
                    i, after_cmd = j, False
                    continue
                if name in _WRAPPERS and j < n and line[j] == "{":
                    toks.append(_Tok("open", i, j + 1))
                    stack.append("wrap")
                    i, after_cmd = j + 1, False
                    continue
                toks.append(_Tok("markup", i, j))
                i, after_cmd = j, True
                continue
            if line[i + 1] == "(":
                close = line.find("\\)", i + 2)
                end = n if close < 0 else close + 2
                toks.append(_Tok("markup", i, end))
                i, after_cmd = end, False
                continue
            nxt = line[i + 1]
            if nxt == "\\":  # line break, maybe with [skip] after it
                toks.append(_Tok("char", i, i + 2, "\n"))
                i, after_cmd = i + 2, True
                continue
            if nxt in _ESCAPED or nxt in ",;:!":
                toks.append(_Tok("char", i, i + 2, nxt if nxt in _ESCAPED else " "))
            else:
                toks.append(_Tok("markup", i, i + 2))
            i, after_cmd = i + 2, False
            continue
        if c == "$":
            j = i + 1
            while j < n and not (line[j] == "$" and line[j - 1] != "\\"):
                j += 1
            end = min(n, j + 1)
            if line[i:i + 2] == "$$":
                close = line.find("$$", i + 2)
                end = n if close < 0 else close + 2
            toks.append(_Tok("markup", i, end))
            i, after_cmd = end, False
            continue
        if c == "{":
            toks.append(_Tok("markup", i, i + 1))
            stack.append("brace")
        elif c == "}":
            kind = stack.pop() if stack and stack[-1] != "bracket" else "brace"
            toks.append(_Tok("close" if kind == "wrap" else "markup", i, i + 1))
            i, after_cmd = i + 1, True  # \cmd{..}[..] is rare, but {..}{..} chains are common
            continue
        elif c == "[" and after_cmd:
            toks.append(_Tok("markup", i, i + 1))
            stack.append("bracket")
        elif c == "]" and stack and stack[-1] == "bracket":
            stack.pop()
            toks.append(_Tok("markup", i, i + 1))
            i, after_cmd = i + 1, True
            continue
        elif c == "&":  # table column separator
            toks.append(_Tok("markup", i, i + 1))
        elif c == "~":
            toks.append(_Tok("char", i, i + 1, " "))
        else:  # a line break inside a paragraph is a space to TeX
            toks.append(_Tok("char", i, i + 1, " " if c == "\n" else c))
        i, after_cmd = i + 1, False
    return toks


def _visible_text(line: str) -> str:
    """Roughly what `line` puts on the slide: its characters, inline math and
    symbol commands as text; command names, environment names and arguments
    of \\begin, & and comments left out."""
    out: list[str] = []
    for t in _tokenize_line(line):
        src = line[t.start:t.end]
        if t.kind == "char":
            out.append(t.char)
        elif t.kind == "markup" and (src.startswith("$") or src.startswith("\\(")):
            # Sub/superscripts and thin spaces leave no character of their own.
            out.append(re.sub(r"[_^]", "", latex_to_text(re.sub(r"\\[,;:!]", "", src))))
        elif t.kind == "markup" and src[1:] in _SYMBOLS:
            out.append(_SYMBOLS[src[1:]])
    return re.sub(r"-+", "-", "".join(out))


@dataclass
class TextSpan:
    start: int  # source offsets of the editable part of the line
    end: int
    text: str  # what the editor shows
    raw: bool  # True: `text` is LaTeX (the run has math or other commands)


def text_span(line: str, clicked: str) -> TextSpan:
    """The part of `line` that typesets `clicked`, as plain text when possible.

    The run starts at the first and ends at the last character matching the
    clicked text, then grows over neighbouring characters and emphasis
    wrappers until it meets other markup, so a wrapped PDF line still edits
    the whole sentence and \\title[short]{long} edits only the long title.
    If the run contains math or another command, that run's LaTeX source is
    returned (raw); if it is not a balanced piece of the line, the whole line.
    """
    whole = TextSpan(0, len(line), line, True)
    toks = _tokenize_line(line)
    chars = [k for k, t in enumerate(toks) if t.kind == "char" and not t.char.isspace()
             and not (t.char == "-" and k and toks[k - 1].char == "-")]
    target = _fold(clicked)
    flat = "".join(_fold(toks[k].char) for k in chars)
    if not target or not flat:
        return whole
    hit = _shared_runs(target, flat)
    if not hit:
        return whole
    a, b = chars[hit[0].b], chars[hit[-1].b + hit[-1].size - 1]
    def inside(t: _Tok) -> bool:  # what a sentence may run through: text, emphasis, math
        return t.kind != "markup" or line[t.start] == "$" or line.startswith("\\(", t.start)

    while a > 0 and inside(toks[a - 1]):
        a -= 1
    while b + 1 < len(toks) and inside(toks[b + 1]):
        b += 1
    while a <= b and toks[a].kind == "char" and toks[a].char.isspace():
        a += 1
    while b >= a and toks[b].kind == "char" and toks[b].char.isspace():
        b -= 1
    start, end = toks[a].start, toks[b].end
    if all(t.kind != "markup" for t in toks[a:b + 1]):
        return TextSpan(start, end, "".join(t.char for t in toks[a:b + 1]), False)
    # Math or other commands inside: show just this sentence's source, if it
    # is a self-contained piece of the line.
    depth = 0
    for t in toks[a:b + 1]:
        src = line[t.start:t.end]
        if t.kind in ("open", "close") or src in "{}[]" and t.kind == "markup":
            depth += 1 if src.endswith(("{", "[")) else -1
            if depth < 0:
                return whole
    return TextSpan(start, end, line[start:end], True) if depth == 0 else whole


_ESCAPES = {
    "\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "%": r"\%", "&": r"\&",
    "#": r"\#", "_": r"\_", "$": r"\$", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
}


def escape_text(text: str) -> str:
    """Plain text as LaTeX; a new line becomes a line break."""
    lines = re.split(r"[ \t]*\n\s*", text)
    return "\\\\ ".join("".join(_ESCAPES.get(c, c) for c in part) for part in lines)


def replace_text_span(line: str, start: int, end: int, new_text: str) -> str:
    """Write an edited plain-text run back into its source line.

    Characters are diffed against the run as shown; only changed characters
    are touched, so emphasis around unchanged words survives and new text
    joins the group of the character before it. New text is escaped.
    """
    if not 0 <= start <= end <= len(line):
        raise EditError("The source changed since you clicked; click the text again")
    toks = [t for t in _tokenize_line(line) if t.kind == "char" and start <= t.start and t.end <= end]
    old_text = "".join(t.char for t in toks)
    edits: list[tuple[int, int, str]] = []
    ops = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False).get_opcodes()
    for op, i1, i2, j1, j2 in ops:
        if op == "equal":
            continue
        insert = escape_text(new_text[j1:j2])
        if i1 == i2:  # pure insertion: after the previous character, else before the next
            at = toks[i1 - 1].end if i1 > 0 else (toks[0].start if toks else start)
            edits.append((at, at, insert))
            continue
        edits.append((toks[i1].start, toks[i1].end, insert))
        edits.extend((t.start, t.end, "") for t in toks[i1 + 1:i2])
    for s, e, rep in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        line = line[:s] + rep + line[e:]
    return line


@dataclass(slots=True)
class TextChange:
    """One fix typed on the preview: source[start:end] of the block at `line` becomes `text`.

    raw: `text` is LaTeX for that run (the sentence had math or commands);
    otherwise it is plain slide text, escaped and diffed in by replace_text_span.
    """

    line: int
    source: str
    start: int
    end: int
    text: str
    raw: bool = False


def apply_text_changes(tex: str, changes: list[TextChange]) -> str:
    """Apply many preview fixes at once; refuses if a source block changed meanwhile.

    Changes to one block (same line and source) are applied right to left so
    the earlier offsets stay valid; blocks are replaced bottom-up for the same
    reason, as a fix may add a line.
    """
    blocks: dict[int, list[TextChange]] = {}
    for change in changes:
        blocks.setdefault(change.line, []).append(change)
    spans: list[tuple[int, int]] = []
    for line, group in blocks.items():
        if len({c.source for c in group}) > 1:
            raise EditError("Two changes disagree about one source line; remove one and try again")
        spans.append((line, line + group[0].source.count("\n")))
    spans.sort()
    for (_, a_end), (b_start, _) in zip(spans, spans[1:]):
        if b_start <= a_end:
            raise EditError("Two changes touch the same source lines; remove one and try again")
    for line in sorted(blocks, reverse=True):
        group = sorted(blocks[line], key=lambda c: c.start, reverse=True)
        block = group[0].source
        for prev, change in zip([None, *group], group):
            if prev is not None and change.end > prev.start:
                raise EditError("Two changes overlap in one sentence; remove one and try again")
            if not 0 <= change.start <= change.end <= len(block):
                raise EditError("The source changed since you clicked; click the text again")
            if change.raw:
                block = block[: change.start] + change.text + block[change.end :]
            else:
                block = replace_text_span(block, change.start, change.end, change.text)
        tex = replace_line(tex, line, group[0].source, block)
    return tex


# --- The title page --------------------------------------------------------
#
# Most templates typeset the title page with \maketitle outside any frame, so
# no frame stands behind it; SimplePlus puts \titlepage in a frame of its own.
# Either way what the page shows comes from the title commands in the
# preamble, so a request for that page edits those commands. It travels in
# multi-frame edits as index 0, marked `%% TITLE`.

TITLE_INDEX = 0
_TITLE_COMMANDS = ("title", "subtitle", "author", "institute", "date")
_TITLE_START_RE = re.compile(r"^\s*\\(?:title|subtitle|author|institute|date)(?![A-Za-z@])")
_TITLE_PAGE_RE = re.compile(r"\\(?:titlepage|maketitle)(?![A-Za-z@])")


@dataclass(slots=True)
class TitleBlock:
    spans: list[tuple[int, int]]  # 1-based inclusive line ranges, top to bottom
    text: str  # the commands, one after another


def _brace_balance(line: str) -> int:
    code = re.sub(r"(?<!\\)%.*$", "", line)
    code = re.sub(r"\\[{}]", "", code)
    return code.count("{") - code.count("}")


def title_block(tex: str) -> TitleBlock | None:
    """The \\title ... \\date commands before \\begin{document}; None if there are none."""
    lines = tex.split("\n")
    end = next((i for i, line in enumerate(lines) if r"\begin{document}" in line), len(lines))
    spans: list[tuple[int, int]] = []
    i = 0
    while i < end:
        if _TITLE_START_RE.match(lines[i]):
            depth, j = _brace_balance(lines[i]), i
            while depth > 0 and j + 1 < end:
                j += 1
                depth += _brace_balance(lines[j])
            spans.append((i + 1, j + 1))
            i = j + 1
        else:
            i += 1
    if not spans:
        return None
    return TitleBlock(spans, "\n".join("\n".join(lines[a - 1 : b]) for a, b in spans))


def is_title_frame(frame: Frame) -> bool:
    return bool(_TITLE_PAGE_RE.search(frame.tex))


def replace_title_block(tex: str, block: TitleBlock, new_text: str) -> str:
    """Swap the title commands for new ones; refuses anything else in their place."""
    new_lines = new_text.strip("\n").split("\n")
    found = title_block("\n".join(new_lines) + "\n\\begin{document}")
    covered = {n for a, b in (found.spans if found else []) for n in range(a, b + 1)}
    for n, line in enumerate(new_lines, start=1):
        if n not in covered and re.sub(r"(?<!\\)%.*$", "", line).strip():
            raise EditError("The title page change may only contain \\title, \\subtitle, \\author, \\institute and \\date")
    if not found:
        raise EditError("The title page change has no title commands")
    lines = tex.split("\n")
    first = block.spans[0][0]
    for a, b in reversed(block.spans):
        del lines[a - 1 : b]
    lines[first - 1 : first - 1] = new_lines
    return "\n".join(lines)
