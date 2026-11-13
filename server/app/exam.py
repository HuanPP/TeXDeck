"""Learn: practice exams made from the reader's material.

An exam is a generation of kind "exam". The reader gives the material, a table
of question types (how many, how many points) and, optionally, sample papers.
The model first proposes a blueprint (sections, types, counts, points, style)
that the reader can edit; then it writes the questions as JSON, which the
server checks, scores from the blueprint and typesets as a paper and an answer
key. The same JSON drives the interactive practice in the browser. Answers to
open questions are graded by the model on request.

Files, in <generation>/exam/: spec.json, samples/, blueprint.json, exam.json,
paper.tex/pdf, answers.tex/pdf, grades.json, attempts.json.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import fitz

from . import figures
from .llm import LlmError, create_client, image_block, text_block

EXAM_DIR = "exam"
SAMPLE_DIR = "samples"
TYPES = ("single", "multiple", "truefalse", "fill", "short", "calc", "other")
TYPE_NAMES = {
    "single": ("单项选择题", "Single choice"),
    "multiple": ("多项选择题", "Multiple choice"),
    "truefalse": ("判断题", "True or false"),
    "fill": ("填空题", "Fill in the blanks"),
    "short": ("简答题", "Short answer"),
    "calc": ("计算题", "Calculation"),
    "other": ("其他题型", "Other"),
}
OPEN_TYPES = ("short", "calc", "other")
MAX_QUESTIONS = 80
MAX_SAMPLES = 6
MAX_SAMPLE_PAGES = 4  # per sample PDF
MAX_TEXT = 4000
BLANK = re.compile(r"\{\{(\d+)\}\}")
CN_NUMBERS = "一二三四五六七八九十"

_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="texdeck-grade")


class ExamError(ValueError):
    """User-facing English."""


def exam_dir(gdir: Path) -> Path:
    return Path(gdir) / EXAM_DIR


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _num(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:  # NaN
        return default
    n = min(max(n, lo), hi)
    return int(n) if n == int(n) else round(n, 2)


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit]


def parse_parts(parts: str) -> list[float]:
    """"4+6" or "4, 6" -> [4, 6]; anything else -> []."""
    found = re.findall(r"\d+(?:\.\d+)?", parts or "")
    return [_num(x, 0, 1000, 0) for x in found] if found else []


# ---- the reader's request ------------------------------------------------------


def clean_spec(raw: dict[str, Any], samples: int) -> dict[str, Any]:
    """The question-type table, checked. Rows with no questions are dropped."""
    rows = []
    for row in raw.get("types") or []:
        if not isinstance(row, dict) or row.get("type") not in TYPES:
            continue
        items = []
        for item in row.get("items") or []:
            if isinstance(item, dict):
                items.append({"points": _num(item.get("points"), 0, 1000, 0), "parts": _text(item.get("parts"), 100)})
        count = int(_num(row.get("count"), 0, MAX_QUESTIONS, 0))
        if row["type"] in OPEN_TYPES and items:
            count = len(items)
        if count <= 0:
            continue
        description = _text(row.get("description"), 1000)
        if row["type"] == "other" and not description:
            raise ExamError("Describe the \"Other\" question type")
        rows.append({
            "type": row["type"], "count": count, "points": _num(row.get("points"), 0, 1000, 0),
            "items": items[:count] if row["type"] in OPEN_TYPES else [], "description": description,
        })
    if sum(r["count"] for r in rows) > MAX_QUESTIONS:
        raise ExamError(f"At most {MAX_QUESTIONS} questions per exam")
    if not rows and not samples:
        raise ExamError("Add at least one question type, or a sample paper")
    return {
        "types": rows,
        "duration": int(_num(raw.get("duration"), 0, 600, 0)),
        "extra": _text(raw.get("extra")),
    }


def store_samples(gdir: Path, uploads: list[tuple[str, bytes]]) -> list[str]:
    """Sample papers: images (re-encoded) or PDFs. Returns the stored names."""
    if len(uploads) > MAX_SAMPLES:
        raise ExamError(f"At most {MAX_SAMPLES} sample papers")
    folder = exam_dir(gdir) / SAMPLE_DIR
    names = []
    for i, (name, data) in enumerate(uploads, start=1):
        folder.mkdir(parents=True, exist_ok=True)
        if data[:5] == b"%PDF-":
            try:
                with fitz.open(stream=data, filetype="pdf") as doc:
                    if doc.page_count == 0:
                        raise ExamError(f"Sample PDF has no pages: {name}")
            except (RuntimeError, ValueError) as exc:
                raise ExamError(f"Cannot read sample PDF: {name}") from exc
            out = f"sample-{i:02d}.pdf"
            (folder / out).write_bytes(data)
        else:
            try:
                clean, ext, _w, _h = figures._normalize(name, data)
            except figures.FigureError as exc:
                raise ExamError(str(exc)) from exc
            out = f"sample-{i:02d}.{ext}"
            (folder / out).write_bytes(clean)
        names.append(out)
    return names


def sample_blocks(gdir: Path, limit: int = 12) -> list[dict[str, Any]]:
    """The sample papers as images for the model (PDF pages rendered)."""
    folder = exam_dir(gdir) / SAMPLE_DIR
    if not folder.is_dir():
        return []
    blocks: list[dict[str, Any]] = []
    cache = exam_dir(gdir) / "sample_pages"
    for path in sorted(folder.iterdir()):
        if len(blocks) >= limit:
            break
        if path.suffix == ".pdf":
            cache.mkdir(parents=True, exist_ok=True)
            with fitz.open(path) as doc:
                for n in range(min(doc.page_count, MAX_SAMPLE_PAGES)):
                    png = cache / f"{path.stem}-p{n + 1}.png"
                    if not png.is_file():
                        doc.load_page(n).get_pixmap(dpi=110).save(png)
                    blocks.append(image_block(png))
        else:
            blocks.append(image_block(path))
    return blocks[:limit]


# ---- blueprint -------------------------------------------------------------------


def _zh(text: str) -> bool:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    return cjk > 0 and cjk >= len(re.findall(r"[A-Za-z]", text)) / 4


def paper_language(language: str, sample: str) -> str:
    if language in ("zh", "en"):
        return language
    return "zh" if _zh(sample) else "en"


def local_blueprint(spec: dict[str, Any], title: str, lang: str) -> dict[str, Any]:
    """The blueprint the table alone gives (no sample papers to read)."""
    sections = []
    for i, row in enumerate(spec["types"]):
        zh, en = TYPE_NAMES[row["type"]]
        name = (row["description"][:30] if row["type"] == "other" else zh if lang == "zh" else en)
        heading = f"{CN_NUMBERS[i] if i < 10 else i + 1}、{name}" if lang == "zh" else f"Part {i + 1}. {name}"
        if row["items"]:
            points = [it["points"] for it in row["items"]]
            parts = [it["parts"] for it in row["items"]]
        else:
            points = [row["points"]] * row["count"]
            parts = [""] * row["count"]
        sections.append({
            "title": heading, "type": row["type"], "count": row["count"], "points": points, "parts": parts,
            "format": row["description"], "passage": False,
        })
    return {"title": title, "instructions": "", "duration": spec["duration"], "style": "", "sections": sections}


# The count and points after a heading, e.g. "（共 4 题，每题 2 分）" or "(5 questions, 10 points)":
# the paper writes these itself from the blueprint, so a heading must not carry them.
_HEAD_TALLY = re.compile(r"\s*[(（][^()（）]*\d[^()（）]*(分|题|points?|marks?|questions?)[^()（）]*[)）]\s*$", re.I)


def clean_blueprint(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ExamError("The blueprint is not an object")
    sections = []
    for s in raw.get("sections") or []:
        if not isinstance(s, dict):
            continue
        kind = s.get("type") if s.get("type") in TYPES else "other"
        count = int(_num(s.get("count"), 0, MAX_QUESTIONS, 0))
        if count <= 0:
            continue
        points = s.get("points")
        if not isinstance(points, list):
            points = [points] * count
        points = [_num(p, 0, 1000, 0) for p in points][:count]
        points += [points[-1] if points else 0] * (count - len(points))
        parts = s.get("parts") if isinstance(s.get("parts"), list) else []
        parts = [_text(p, 100) for p in parts][:count]
        parts += [""] * (count - len(parts))
        sections.append({
            "title": _HEAD_TALLY.sub("", _text(s.get("title"), 200)) or TYPE_NAMES[kind][1], "type": kind, "count": count,
            "points": points, "parts": parts, "format": _text(s.get("format"), 1000), "passage": bool(s.get("passage")),
        })
    if not sections:
        raise ExamError("The blueprint has no sections")
    if sum(s["count"] for s in sections) > MAX_QUESTIONS:
        raise ExamError(f"At most {MAX_QUESTIONS} questions per exam")
    return {
        "title": _text(raw.get("title"), 200), "instructions": _text(raw.get("instructions"), 2000),
        "duration": int(_num(raw.get("duration"), 0, 600, 0)), "style": _text(raw.get("style"), 2000),
        "sections": sections,
    }


LANGUAGE_RULES = {
    "source": "Write in the language of the material (the dominant one if it mixes languages).",
    "en": "Write in English.",
    "zh": "Write in Simplified Chinese; keep technical terms, names and formulas as they are.",
}

BLUEPRINT_PROMPT = """You design the structure of a practice exam. You get the student's request (a table of
question types, possibly incomplete), sample papers they uploaded as images, and the start of the
study material. Work out the structure the exam should have and output it as JSON, nothing else:

{"title": "named after the study material (or the student's title), never the sample's title",
 "instructions": "notes to candidates printed under the title, or empty; do not copy the sample's",
 "duration": minutes or 0, "style": "a few sentences on how the sample papers ask questions:
 wording, difficulty, how options and answers look; what the question writer must imitate",
 "sections": [{"title": "the heading's name and numbering as the samples write it, without the question
   count or points (the paper adds those)", "type": "single|multiple|truefalse|fill|short|calc|other",
   "count": N, "points": [points of each question], "parts": ["sub-question split such as 4+6, or empty", ...],
   "format": "how this section's questions look, especially for type other", "passage": true if the
   section's questions share a reading passage}]}

Rules: the student's table wins over the samples where it says something (types, counts, points);
the samples fill in what it leaves open and give the order, headings and style. Types that fit none
of single, multiple, truefalse, fill, short, calc are "other", with the format described (for example
matching headings to paragraphs). Section headings follow the samples' numbering style. Questions
must be answerable from the study material; do not plan listening or speaking sections."""


def blueprint_messages(spec: dict[str, Any], samples: list[dict[str, Any]], material: str,
                       title: str, language: str) -> list[dict[str, Any]]:
    request = json.dumps({"title": title, "duration": spec["duration"], "types": spec["types"]}, ensure_ascii=False)
    content: list[dict[str, Any]] = [
        text_block(f"Student's request: {request}"),
        text_block(f"Extra requirements: {spec['extra'] or '(none)'}"),
        text_block(f"Language: {LANGUAGE_RULES.get(language, LANGUAGE_RULES['source'])}"),
        text_block(f"Start of the study material:\n{material[:6000]}"),
        text_block("Sample papers:"),
        *samples,
    ]
    return [{"role": "system", "content": BLUEPRINT_PROMPT}, {"role": "user", "content": content}]


def parse_json(reply: str) -> Any:
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ExamError("The model did not answer with JSON")
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise ExamError(f"The model's JSON does not parse: {exc}") from exc


# ---- the paper -----------------------------------------------------------------

PAPER_PROMPT = """You write a practice exam from study material, following an agreed blueprint. Output
JSON only, nothing else:

{"title": "...", "instructions": "...", "sections": [ one object per blueprint section, same order:
  {"title": "...", "instructions": "one line on how to answer, or empty",
   "passage": "the shared reading passage when the blueprint says passage, else empty",
   "questions": [ exactly the blueprint's count ] } ]}

Questions, by the section's type:
  single:    {"stem": "...", "options": ["...", "...", "...", "..."], "answer": "B", "explanation": "..."}
  multiple:  {"stem": "...", "options": [...], "answer": ["A", "C"], "explanation": "..."}
  truefalse: {"stem": "a statement", "answer": true, "explanation": "..."}
  fill:      {"stem": "text with blanks written {{1}}, {{2}} ...", "answer": [["accepted", "variants"], ["..."]],
              "explanation": "..."}  (one list of accepted answers per blank, short answers only)
  short, calc: {"stem": "...", "answer": "model answer", "rubric": ["scoring point (N points)", ...],
              "parts": [{"stem": "(1) ...", "answer": "...", "rubric": [...]}] when the blueprint gives a
              split, else omit parts, "explanation": "..."}
  other:     follow the section's format; if each question has a fixed set of options, give "options" and
             the answer letter(s) as for single/multiple; otherwise as for short.

Rules: every question is answerable from the material, tests understanding rather than copying, and has
one defensible answer. Options do not give the answer away by length or wording; spread correct
letters. Do not number questions or options (no "1." or "A." prefixes). Text may use **bold** and line
breaks; write maths as LaTeX between $...$ (display: $$...$$), math-mode commands only. Explanations
are short and cite the material's pages as (p. N) when it has pages. Points come from the blueprint;
do not write them in stems."""


def paper_messages(blueprint: dict[str, Any], spec: dict[str, Any], material: str | list[dict[str, Any]],
                   samples: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        text_block("Blueprint: " + json.dumps(blueprint, ensure_ascii=False)),
        text_block(f"Extra requirements: {spec.get('extra') or '(none)'}"),
        text_block(f"Language: {LANGUAGE_RULES.get(language, LANGUAGE_RULES['source'])}"),
        text_block("Study material:"),
    ]
    content += [text_block(material)] if isinstance(material, str) else material
    if samples:
        content += [text_block("Sample papers, for style only (do not copy their questions):"), *samples[:4]]
    return [{"role": "system", "content": PAPER_PROMPT}, {"role": "user", "content": content}]


_OPTION_PREFIX = re.compile(r"^\s*[\(（]?[A-Ha-h][\)）\.．、:：]\s*")


def _letter(value: Any, options: int) -> str:
    if isinstance(value, int) and 0 <= value < options:
        return "ABCDEFGH"[value]
    m = re.match(r"\s*[\(（]?([A-Ha-h])\b", str(value or ""))
    if not m or "ABCDEFGH".index(m.group(1).upper()) >= options:
        raise ExamError(f"answer {value!r} is not one of the options")
    return m.group(1).upper()


def _truth(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "t", "yes", "对", "正确", "√", "1"):
        return True
    if s in ("false", "f", "no", "错", "错误", "×", "0"):
        return False
    raise ExamError(f"true/false answer {value!r} is neither")


def _strings(value: Any) -> list[str]:
    return [str(v).strip() for v in (value if isinstance(value, list) else [value]) if str(v).strip()]


def clean_question(raw: Any, kind: str, points: float, split: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ExamError("a question is not an object")
    stem = _text(raw.get("stem"), 6000)
    if not stem:
        raise ExamError("a question has no stem")
    q: dict[str, Any] = {"type": kind, "stem": stem, "points": points,
                         "explanation": _text(raw.get("explanation"), 3000)}
    options = [_OPTION_PREFIX.sub("", str(o)).strip() for o in raw.get("options") or [] if str(o).strip()]
    choice = kind in ("single", "multiple") or (kind == "other" and len(options) >= 2)
    if choice:
        if len(options) < 2 or len(options) > 8:
            raise ExamError(f"a choice question needs 2 to 8 options: {stem[:60]}")
        q["options"] = options
        answer = raw.get("answer")
        many = kind == "multiple" or (kind == "other" and isinstance(answer, list) and len(answer) > 1)
        if many:
            letters = sorted({_letter(a, len(options)) for a in (answer if isinstance(answer, list) else re.findall(r"[A-Ha-h]", str(answer)))})
            if not letters:
                raise ExamError(f"a multiple-choice question has no answer: {stem[:60]}")
            q["answer"], q["mode"] = letters, "multiple"
        else:
            q["answer"], q["mode"] = _letter(answer[0] if isinstance(answer, list) and answer else answer, len(options)), "single"
    elif kind == "truefalse":
        q["answer"], q["mode"] = _truth(raw.get("answer")), "truefalse"
    elif kind == "fill":
        blanks = sorted({int(n) for n in BLANK.findall(stem)})
        answer = raw.get("answer")
        lists = [_strings(a) for a in answer] if isinstance(answer, list) else [_strings(answer)]
        lists = [a for a in lists if a]
        if not blanks and len(lists) == 1:
            q["stem"] = stem = stem.rstrip() + " {{1}}"
            blanks = [1]
        if blanks != list(range(1, len(blanks) + 1)) or len(lists) != len(blanks):
            raise ExamError(f"a fill-in question needs one answer list per blank {{{{n}}}}: {stem[:60]}")
        q["answer"], q["mode"] = lists, "fill"
    else:
        q["mode"] = "open"
        q["answer"] = _text(raw.get("answer"), 6000)
        q["rubric"] = [_text(r, 500) for r in raw.get("rubric") or [] if _text(r, 500)][:12]
        parts = [p for p in raw.get("parts") or [] if isinstance(p, dict) and _text(p.get("stem"), 3000)]
        if parts:
            weights = parse_parts(split)
            if len(weights) != len(parts) or abs(sum(weights) - points) > 0.01:
                weights = [_num(points / len(parts), 0, 1000, 0)] * len(parts)
            q["parts"] = [{
                "stem": _text(p.get("stem"), 3000), "answer": _text(p.get("answer"), 6000),
                "rubric": [_text(r, 500) for r in p.get("rubric") or [] if _text(r, 500)][:8], "points": w,
            } for p, w in zip(parts, weights)]
        if not q["answer"] and not any(p["answer"] for p in q.get("parts", [])):
            raise ExamError(f"an open question has no model answer: {stem[:60]}")
    return q


def clean_exam(raw: Any, blueprint: dict[str, Any]) -> dict[str, Any]:
    """The model's paper, checked against the blueprint; points come from it."""
    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        raise ExamError("the paper has no sections list")
    plan = blueprint["sections"]
    if len(raw["sections"]) != len(plan):
        raise ExamError(f"the paper has {len(raw['sections'])} sections; the blueprint has {len(plan)}")
    sections, n = [], 0
    for got, want in zip(raw["sections"], plan):
        questions = got.get("questions") if isinstance(got, dict) else None
        if not isinstance(questions, list) or len(questions) < want["count"]:
            have = len(questions) if isinstance(questions, list) else 0
            raise ExamError(f"section \"{want['title']}\" has {have} questions; the blueprint asks for {want['count']}")
        cleaned = []
        for i, q in enumerate(questions[:want["count"]]):
            n += 1
            item = clean_question(q, want["type"], want["points"][i], want["parts"][i])
            item["id"] = f"q{n}"
            item["number"] = n
            cleaned.append(item)
        sections.append({
            "title": want["title"], "type": want["type"],
            "instructions": _text(got.get("instructions"), 1000), "passage": _text(got.get("passage"), 20000),
            "questions": cleaned,
        })
    total = sum(q["points"] for s in sections for q in s["questions"])
    return {
        "title": _text(raw.get("title"), 200) or blueprint["title"],
        "instructions": _text(raw.get("instructions"), 2000) or blueprint["instructions"],
        "duration": blueprint["duration"], "total": _num(total, 0, 100000, 0), "sections": sections,
    }


# ---- typesetting -----------------------------------------------------------------

_SPECIAL = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#",
            "^": r"\^{}", "_": r"\_", "%": r"\%", "~": r"\~{}"}
_MATH = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.S)
# Math is passed to TeX as written; anything that is not mathematics is refused.
_UNSAFE = re.compile(r"\\(input|include|write|openout|openin|read|immediate|catcode|def|edef|gdef|let|"
                     r"newcommand|renewcommand|providecommand|special|directlua|usepackage|csname|"
                     r"begin\s*\{\s*document|end\s*\{\s*document|loop|makeatletter)", re.I)


def _escape(text: str) -> str:
    out = "".join(_SPECIAL.get(c, c) for c in text)
    out = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", out)
    lines = [re.sub(r"^\s*[-*]\s+", r"\\textbullet\\ ", line) for line in out.split("\n")]
    return "\\par ".join(line for line in lines if line.strip()) if len(lines) > 1 else lines[0]


def _math_ok(math: str) -> bool:
    depth = 0
    for c in re.sub(r"\\[{}]", "", math):
        depth += c == "{"
        depth -= c == "}"
        if depth < 0:
            return False
    return depth == 0 and not _UNSAFE.search(math)


def to_tex(text: str, blank: str = r"\underline{\makebox[5em]{}}") -> str:
    """Markdown-ish question text (with $maths$ and {{n}} blanks) as LaTeX."""
    text = BLANK.sub("\x00", text.replace("\x00", ""))  # blanks survive the escaping
    text = re.sub(r"\\\((.+?)\\\)", r"$\1$", text, flags=re.S)
    text = re.sub(r"\\\[(.+?)\\\]", r"$$\1$$", text, flags=re.S)
    out, at = [], 0
    for m in _MATH.finditer(text):
        out.append(_escape(text[at:m.start()]))
        display, inline = m.group(1), m.group(2)
        math = display if display is not None else inline
        if _math_ok(math):
            out.append(f"\\[{math}\\]" if display is not None else f"${math}$")
        else:
            out.append(_escape(m.group(0)))
        at = m.end()
    out.append(_escape(text[at:]))
    return "".join(out).replace("\x00", blank)


def _pts(points: float, lang: str) -> str:
    p = int(points) if points == int(points) else points
    return f"{p} 分" if lang == "zh" else f"{p} {'point' if p == 1 else 'points'}"


def _section_head(section: dict[str, Any], lang: str) -> str:
    qs = section["questions"]
    total = sum(q["points"] for q in qs)
    same = len({q["points"] for q in qs}) == 1
    if lang == "zh":
        each = f"每题 {_pts(qs[0]['points'], lang)}，" if same and len(qs) > 1 else ""
        return f"{section['title']}（共 {len(qs)} 题，{each}共 {_pts(total, lang)}）"
    each = f", {_pts(qs[0]['points'], lang)} each" if same and len(qs) > 1 else ""
    return f"{section['title']} ({len(qs)} questions{each}, {_pts(total, lang)})"


def _options(options: list[str]) -> str:
    items = [f"{'ABCDEFGH'[i]}.~{to_tex(o)}" for i, o in enumerate(options)]
    # Display width: a CJK character is as wide as two Latin ones.
    width = max(len(o) + len(re.findall(r"[\u2e80-\uffef]", o)) for o in options)
    if width <= 12 and len(options) <= 4:
        return "\\par " + "\\hspace{2.2em}".join(items)
    if width <= 28:
        rows = [items[i:i + 2] for i in range(0, len(items), 2)]
        return "\\par\\begin{tabular}{@{}p{0.45\\linewidth}p{0.45\\linewidth}@{}}" + \
            "\\\\".join(" & ".join(r + [""] * (2 - len(r))) for r in rows) + "\\end{tabular}"
    return "".join(f"\\par {it}" for it in items)


PREAMBLE = r"""\documentclass[11pt]{ctexart}
\usepackage[a4paper,margin=2.2cm]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{enumitem}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.35em}
\pagestyle{plain}
\begin{document}
"""


def _answer_text(q: dict[str, Any], lang: str) -> str:
    if q["mode"] == "single":
        return q["answer"]
    if q["mode"] == "multiple":
        return "".join(q["answer"])
    if q["mode"] == "truefalse":
        return ("正确" if q["answer"] else "错误") if lang == "zh" else ("True" if q["answer"] else "False")
    if q["mode"] == "fill":
        return "; ".join(" / ".join(to_tex(a) for a in alts) for alts in q["answer"])
    return ""


def render(exam: dict[str, Any], lang: str, answers: bool) -> str:
    zh = lang == "zh"
    lines = [PREAMBLE, "\\begin{center}{\\Large\\bfseries " + to_tex(exam["title"] or ("练习卷" if zh else "Practice exam")) + "}"]
    if answers:
        lines.append("\\par\\smallskip{\\large " + ("参考答案与解析" if zh else "Answer key") + "}")
    info = [("满分 " if zh else "Total ") + _pts(exam["total"], lang)]
    if exam["duration"]:
        info.append(f"考试时间 {exam['duration']} 分钟" if zh else f"Time: {exam['duration']} minutes")
    lines.append("\\par\\smallskip{\\small " + ("\\quad ".join(info)) + "}\\end{center}")
    if not answers:
        if exam["instructions"]:
            lines.append("{\\small " + to_tex(exam["instructions"]) + "}\\par")
        lines.append("\\medskip " + ("姓名\\underline{\\makebox[8em]{}}\\quad 学号\\underline{\\makebox[8em]{}}\\quad 得分\\underline{\\makebox[5em]{}}"
                                       if zh else "Name\\underline{\\makebox[10em]{}}\\quad Score\\underline{\\makebox[5em]{}}") + "\\par\\medskip")
    for s in exam["sections"]:
        lines.append("\\section*{" + to_tex(_section_head(s, lang)) + "}")
        if s["instructions"] and not answers:
            lines.append("{\\small " + to_tex(s["instructions"]) + "}\\par")
        if s["passage"] and not answers:
            lines.append("\\begin{quote}\\small " + to_tex(s["passage"]) + "\\end{quote}")
        first = s["questions"][0]["number"]
        lines.append(f"\\begin{{enumerate}}[label=\\arabic*., start={first}, leftmargin=2em, itemsep=0.8em]")
        for q in s["questions"]:
            pts = f"\\hfill{{\\small ({_pts(q['points'], lang)})}}" if q["mode"] == "open" else ""
            blank = "\\underline{\\makebox[5em]{}}"
            stem = to_tex(q["stem"], blank)
            if q["mode"] == "truefalse" and not answers:
                stem += "\\quad (\\quad\\ )"
            lines.append(f"\\item {stem}{pts}")
            if q.get("options"):
                lines.append(_options(q["options"]))
            for p in q.get("parts", []):
                lines.append(f"\\par {to_tex(p['stem'])}\\hfill{{\\small ({_pts(p['points'], lang)})}}")
                if answers:
                    lines.append("\\par{\\small " + to_tex(p["answer"]) + "}")
                    for r in p["rubric"]:
                        lines.append("\\par{\\small\\textbullet\\ " + to_tex(r) + "}")
                elif q["type"] in ("calc", "short", "other"):
                    lines.append(f"\\par\\vspace{{{min(6.0, 1.0 + p['points'] * 0.35):.1f}cm}}")
            if answers:
                label = "答案：" if zh else "Answer: "
                if q["mode"] != "open":
                    lines.append("\\par{\\small\\textbf{" + label + "}" + _answer_text(q, lang) + "}")
                elif q["answer"]:
                    lines.append("\\par{\\small\\textbf{" + label + "}" + to_tex(q["answer"]) + "}")
                for r in q.get("rubric", []):
                    lines.append("\\par{\\small\\textbullet\\ " + to_tex(r) + "}")
                if q["explanation"]:
                    lines.append("\\par{\\small\\textbf{" + ("解析：" if zh else "Why: ") + "}" + to_tex(q["explanation"]) + "}")
            elif q["mode"] == "open" and not q.get("parts"):
                lines.append(f"\\par\\vspace{{{min(8.0, 1.5 + q['points'] * 0.4):.1f}cm}}")
        lines.append("\\end{enumerate}")
    lines.append("\\end{document}\n")
    return "\n".join(lines)




# ---- grading open answers --------------------------------------------------------

GRADE_PROMPT = """You grade a student's answer to one exam question against the model answer and its
scoring points. Give credit for correct reasoning and equivalent wording; take points off for what
is missing or wrong, following the scoring points. Reply with JSON only:
{"score": points earned (a number from 0 to the maximum, halves allowed), "feedback": "two to five
sentences in the language of the question: what was right, what was missing or wrong, and the key
step or fact to remember. Maths as LaTeX between $...$."}"""


def grade_messages(q: dict[str, Any], part: int | None, answer: str, image: Path | None) -> list[dict[str, Any]]:
    target = q["parts"][part] if part is not None else q
    stem = q["stem"] + (f"\n{target['stem']}" if part is not None else "")
    if part is None and q.get("parts"):
        stem += "\n" + "\n".join(p["stem"] for p in q["parts"])
    reference = target["answer"] or "\n".join(p["answer"] for p in q.get("parts", []))
    rubric = target.get("rubric") or [r for p in q.get("parts", []) for r in p["rubric"]]
    content: list[dict[str, Any]] = [text_block(
        f"Question ({target['points']} points):\n{stem}\n\nModel answer:\n{reference}\n\n"
        f"Scoring points:\n" + ("\n".join(f"- {r}" for r in rubric) or "(none given)")
        + f"\n\nStudent's answer:\n{answer or '(see the photo)'}"
    )]
    if image is not None:
        content += [text_block("The student's handwritten answer:"), image_block(image)]
    return [{"role": "system", "content": GRADE_PROMPT}, {"role": "user", "content": content}]


def find_question(exam: dict[str, Any], qid: str) -> dict[str, Any] | None:
    return next((q for s in exam["sections"] for q in s["questions"] if q["id"] == qid), None)


def start_grade(gdir: Path, qid: str, part: int | None, answer: str, image: bytes | None) -> dict[str, Any]:
    exam = _read(exam_dir(gdir) / "exam.json", None)
    q = find_question(exam, qid) if exam else None
    if q is None or q["mode"] != "open":
        raise ExamError("No open question with that id")
    if part is not None and not 0 <= part < len(q.get("parts", [])):
        raise ExamError("No such part")
    if not answer.strip() and image is None:
        raise ExamError("Write an answer or add a photo of it")
    grade_id = uuid.uuid4().hex[:10]
    photo = None
    if image is not None:
        try:
            clean, ext, _w, _h = figures._normalize("answer", image)
        except figures.FigureError as exc:
            raise ExamError(str(exc)) from exc
        photo = exam_dir(gdir) / "answers" / f"{grade_id}.{ext}"
        photo.parent.mkdir(parents=True, exist_ok=True)
        photo.write_bytes(clean)
    target = q["parts"][part] if part is not None else q
    record = {"id": grade_id, "qid": qid, "part": part, "answer": answer[:MAX_TEXT], "status": "pending",
              "score": 0, "max": target["points"], "feedback": "", "at": time.time()}
    with _lock:
        grades = _read(exam_dir(gdir) / "grades.json", {})
        grades[grade_id] = record
        _write(exam_dir(gdir) / "grades.json", grades)
    _pool.submit(contextvars.copy_context().run, _grade, gdir, grade_id, q, part, answer, photo)
    return record


def _grade(gdir: Path, grade_id: str, q: dict[str, Any], part: int | None, answer: str, photo: Path | None) -> None:
    target = q["parts"][part] if part is not None else q
    try:
        reply = create_client().complete(grade_messages(q, part, answer, photo))
        data = parse_json(reply)
        update = {"status": "done", "score": _num(data.get("score"), 0, target["points"], 0),
                  "feedback": _text(data.get("feedback"), 3000)}
    except (LlmError, ExamError) as exc:
        update = {"status": "error", "feedback": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a grade must always end
        update = {"status": "error", "feedback": f"Could not grade: {exc}"}
    with _lock:
        grades = _read(exam_dir(gdir) / "grades.json", {})
        if grade_id in grades:
            grades[grade_id].update(update)
            _write(exam_dir(gdir) / "grades.json", grades)


def get_grade(gdir: Path, grade_id: str) -> dict[str, Any] | None:
    record = _read(exam_dir(gdir) / "grades.json", {}).get(grade_id)
    if record and record["status"] == "pending" and time.time() - record["at"] > 1800:
        record.update(status="error", feedback="Grading took too long; try again")
    return record


# ---- attempts --------------------------------------------------------------------


def add_attempt(gdir: Path, score: float, total: float, answered: int, correct: int) -> list[dict[str, Any]]:
    with _lock:
        attempts = _read(exam_dir(gdir) / "attempts.json", [])
        attempts.append({"score": _num(score, 0, 100000, 0), "total": _num(total, 0, 100000, 0),
                         "answered": int(answered), "correct": int(correct), "at": time.time()})
        attempts = attempts[-50:]
        _write(exam_dir(gdir) / "attempts.json", attempts)
    return attempts


def state(gdir: Path) -> dict[str, Any]:
    d = exam_dir(gdir)
    exam = _read(d / "exam.json", None)
    blueprint = _read(d / "blueprint.json", None)
    return {
        "spec": _read(d / "spec.json", None),
        "blueprint": blueprint,
        "exam": exam,
        "stage": "ready" if exam else "blueprint" if blueprint else "drafting",
        "paper": (d / "paper.pdf").is_file(),
        "answers": (d / "answers.pdf").is_file(),
        "attempts": _read(d / "attempts.json", []),
        "samples": sorted(p.name for p in (d / SAMPLE_DIR).iterdir()) if (d / SAMPLE_DIR).is_dir() else [],
    }


def save(gdir: Path, name: str, data: Any) -> None:
    _write(exam_dir(gdir) / name, data)


def load(gdir: Path, name: str) -> Any:
    return _read(exam_dir(gdir) / name, None)
