from __future__ import annotations

import re
import shutil
import subprocess
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
    if shutil.which("synctex") is None:
        raise EditError("synctex not found; please install TeX Live")

    tex_path = Path(tex_path)
    work_dir = tex_path.parent
    tex_name = tex_path.name
    pdf_name = tex_path.with_suffix(".pdf").name

    for frame in frames:
        frame.page = _synctex_page(work_dir, tex_name, pdf_name, frame.end_line)
    return frames


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
                "No explanation, no markdown fences, and do not change anything outside this frame."
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


def _synctex_page(
    work_dir: Path,
    tex_name: str,
    pdf_name: str,
    line_no: int,
) -> int:
    """synctex view -i '<line>:1:<tex>' -o '<pdf>'; take the first Page:."""
    cmd = [
        "synctex",
        "view",
        "-i",
        f"{line_no}:1:{tex_name}",
        "-o",
        pdf_name,
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise EditError("synctex not found; please install TeX Live") from exc

    for line in (proc.stdout or "").splitlines():
        if line.startswith("Page:"):
            raw = line[len("Page:") :].strip()
            try:
                return int(raw)
            except ValueError:
                return 0
    return 0
