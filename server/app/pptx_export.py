"""Turn a compiled deck PDF into an editable PPTX that looks like the PDF.

Each PDF page becomes one slide built from three layers:

1. a background picture: the page rendered with the editable text (and the
   large figures) removed, so theme decoration, rules, TikZ drawings and math
   stay pixel-identical;
2. the large raster figures as separate pictures (movable / replaceable);
3. one text box per PDF text block, placed at the block's exact position with
   the PDF's size, colour and weight, PDF line breaks kept as soft breaks and
   leading bullets turned into real PowerPoint bullets.

Blocks containing math glyphs or rotated text are left in the background:
PowerPoint has no faithful equivalent and they would not survive font
substitution.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import fitz
from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt

from . import fonts

# Bump when the output changes so cached exports are rebuilt.
EXPORT_VERSION = 4  # 2: speaker notes; 3: font families; 4: installed-font names

# Slides are scaled to PowerPoint's standard 13.333 in width so font sizes
# read like normal slide sizes (a 9 pt beamer body becomes ~19 pt).
SLIDE_WIDTH_PT = 960.0
EMU_PER_PT = 12700
BACKGROUND_PX_WIDTH = 2400
# Raster images at least this fraction of the page become their own picture.
FIGURE_MIN_AREA = 0.02
# Text this many times the body size is decoration (ghost numerals, swash
# glyphs): it keeps its exact shape in the background picture.
DECORATIVE_FACTOR = 3.5
# Leading glyphs that are beamer itemize/enumerate markers.
BULLET_CHARS = "•‣▸▶►▪■□◦○●–—✓✔➤"

# Math / symbol fonts: blocks using them stay in the background picture.
_MATH_FONT = re.compile(
    r"^(CM(?!U)[A-Z]*\d|cm[a-z]+\d|NewPX|newpx|px|tx|zplm|zpl|MSAM|MSBM|eufm|eusm|rsfs|"
    r"stmary|wasy|esint|LMMath|LatinModernMath|lmmath|STIXMath|Asana|TeXGyre\w*Math|"
    r"FiraMath|Cambria-?Math|Symbol|Dingbats|ZapfDingbats|msam|msbm|NewTXMI|ntx|rtx|mt2|MT2|mtp)",
)

class PptxExportError(RuntimeError):
    pass


def _is_math_font(pdf_font: str) -> bool:
    return bool(_MATH_FONT.match(pdf_font.split("+", 1)[-1]))


@dataclass
class _Span:
    text: str
    font: str
    size: float
    color: int
    bold: bool
    italic: bool
    x0: float  # left edge of the first non-space glyph
    x1: float
    small_caps: bool = False
    char_x: list[float] = field(default_factory=list)
    tracking: float = 0.0  # extra space after each glyph, PDF points
    boxes: list[fitz.Rect] = field(default_factory=list)  # visible glyph boxes


@dataclass
class _Line:
    spans: list[_Span]
    baseline: float
    rects: list[fitz.Rect]
    math: bool

    @property
    def x0(self) -> float:
        return self.spans[0].x0

    @property
    def x1(self) -> float:
        return self.spans[-1].x1

    @property
    def size(self) -> float:
        return max(s.size for s in self.spans)


@dataclass
class _Block:
    lines: list[_Line]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(ln.x0 for ln in self.lines),
            0.0,
            max(ln.x1 for ln in self.lines),
            0.0,
        )

    @property
    def span_rects(self) -> list[fitz.Rect]:
        return [r for ln in self.lines for r in ln.rects]

    @property
    def glyph_rects(self) -> list[fitz.Rect]:
        return [b for ln in self.lines for sp in ln.spans for b in sp.boxes]


class _InkProbe:
    """Tells real capitals/ascenders from small caps by where the glyph has ink.

    Small caps come out of the PDF either as capitals (TeX Gyre) or as
    lowercase letters (EB Garamond); either way the drawn glyph is only about
    0.45 em tall, while a real capital or an ascender reaches ~0.65 em.
    """

    ZOOM = 4

    def __init__(self, page: fitz.Page) -> None:
        self.page = page
        self._pix: fitz.Pixmap | None = None

    def tall(self, char: dict, size: float, color: int, italic: bool = False) -> bool | None:
        if self._pix is None:
            z = self.ZOOM
            self._pix = self.page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        pix, z = self._pix, self.ZOOM
        x0, _, x1, _ = char["bbox"]
        base = char["origin"][1]
        if italic:
            # Slanted glyphs lean right: their tops sit past the advance box.
            left, right = x0 + 0.05 * size, x1 + 0.15 * size
        else:
            inset = (x1 - x0) * 0.2
            left, right = x0 + inset, x1 - inset
        cols = range(max(int(left * z), 0), min(int(right * z) + 1, pix.width))
        rows = range(max(int((base - 0.60 * size) * z), 0), min(int((base - 0.52 * size) * z) + 1, pix.height))
        if not cols or not rows:
            return None
        r, g, b = (color >> 16) & 255, (color >> 8) & 255, color & 255
        for y in rows:
            for x in cols:
                pr, pg, pb = pix.pixel(x, y)
                if abs(pr - r) + abs(pg - g) + abs(pb - b) < 150:
                    return True
        return False


_ASCENDERS = set("bdfhkl")


def _small_caps_flags(chars: list[dict], size: float, color: int, probe: _InkProbe, italic: bool) -> list[bool]:
    """Per character: is it inside a small-caps word?"""
    flags = [False] * len(chars)
    state = False
    i = 0
    while i < len(chars):
        if chars[i]["c"].isspace():
            flags[i] = state
            i += 1
            continue
        j = i
        while j < len(chars) and not chars[j]["c"].isspace():
            j += 1
        word = chars[i:j]
        decided: bool | None = None
        caps = [c for c in word if c["c"].isascii() and c["c"].isupper()]
        if any(probe.tall(c, size, color, italic) is False for c in caps):
            decided = True
        else:
            # Lowercase-coded small caps: every ascender letter must be short.
            heights = [probe.tall(c, size, color, italic) for c in word if c["c"] in _ASCENDERS]
            heights = [h for h in heights if h is not None]
            if heights:
                decided = not any(heights)
        if decided is not None:
            state = decided
        for k in range(i, j):
            flags[k] = state
        i = j
    return flags


def _respace(seg: list[dict], letters: list[str], size: float) -> tuple[str, list[float], float]:
    """Rebuild word spaces from glyph positions and measure letter-spacing.

    TeX spaces words with glue, not space glyphs. MuPDF guesses spaces from
    gaps, which misses some (titles) and, in letter-spaced text, turns every
    letter gap into a space. Returns text, per-character x and the uniform
    tracking (0 for normally set text).
    """
    solid = [k for k, c in enumerate(seg) if not c["c"].isspace()]
    gaps = [seg[b]["bbox"][0] - seg[a]["bbox"][2] for a, b in zip(solid, solid[1:])]
    tracking = 0.0
    if len(gaps) >= 3:
        median = sorted(gaps)[len(gaps) // 2]
        if median > 0.04 * size:
            tracking = median
    text: list[str] = []
    xs: list[float] = []
    if tracking:
        # Letter-spaced: drop the guessed spaces, keep only real word gaps.
        for n, k in enumerate(solid):
            if n and gaps[n - 1] - tracking > 0.15 * size:
                text.append(" ")
                xs.append(seg[solid[n - 1]]["bbox"][2])
            text.append(letters[k])
            xs.append(seg[k]["bbox"][0])
        return "".join(text), xs, tracking
    for k, c in enumerate(seg):
        text.append(letters[k])
        xs.append(c["bbox"][0])
        if k + 1 < len(seg):
            nxt = seg[k + 1]
            # Inside a word glyphs touch (kerning is a few hundredths of an em);
            # even narrow word spaces (Source Sans bold: 0.13 em) clear 0.09.
            if not c["c"].isspace() and not nxt["c"].isspace() and nxt["bbox"][0] - c["bbox"][2] > 0.09 * size:
                text.append(" ")
                xs.append(c["bbox"][2])
    return "".join(text), xs, 0.0


def _spans_from_raw(s: dict, probe: _InkProbe) -> list[_Span]:
    chars = s["chars"]
    if not chars:
        return []
    font = s["font"].split("+", 1)[-1]
    low = font.lower()
    latin = any(c["c"].isascii() and c["c"].isalpha() for c in chars)
    italic = bool(s["flags"] & 2) or "italic" in low or low.endswith("-it")
    if latin and fonts.cjk_ref(font, "".join(c["c"] for c in chars)) is None:
        sc_flags = _small_caps_flags(chars, s["size"], s["color"], probe, italic)
    else:
        sc_flags = [False] * len(chars)
    out: list[_Span] = []
    i = 0
    while i < len(chars):
        j = i
        while j < len(chars) and sc_flags[j] == sc_flags[i]:
            j += 1
        seg = chars[i:j]
        small = sc_flags[i]
        size = s["size"]
        letters = [
            c["c"].lower() if small and c["c"].isupper() and probe.tall(c, size, s["color"], italic) is False else c["c"]
            for c in seg
        ]
        text, char_x, tracking = _respace(seg, letters, size)
        first = next((c for c in seg if not c["c"].isspace()), seg[0])
        last = next((c for c in reversed(seg) if not c["c"].isspace()), seg[-1])
        out.append(
            _Span(
                text=text,
                font=font,
                size=s["size"],
                color=s["color"],
                bold=bool(s["flags"] & 16) or "bold" in low,
                italic=italic,
                x0=first["bbox"][0],
                x1=last["bbox"][2],
                small_caps=small or low.endswith(("-sc", "sc-regular", "caps")) or "smallcaps" in low,
                char_x=char_x,
                tracking=tracking,
                boxes=[fitz.Rect(c["bbox"]) for c in seg if not c["c"].isspace()],
            )
        )
        i = j
    return out


def _pieces(raw_line: dict, probe: _InkProbe, max_size: float) -> list[_Line]:
    """Cut one PDF line where glyphs are far apart (table cells, side-by-side boxes)."""
    dx, dy = raw_line["dir"]
    upright = abs(dy) < 1e-3 and dx > 0
    out: list[_Line] = []
    cur: _Line | None = None
    for s in raw_line["spans"]:
        # Math, rotated text and huge decorative glyphs (ghost section numbers,
        # swash ampersands) stay in the background picture.
        fixed = _is_math_font(s["font"]) or not upright or s["size"] > max_size
        rect = fitz.Rect(s["bbox"])
        for span in _spans_from_raw(s, probe) if not fixed else [_fixed_span(s)]:
            if span.text.strip() and cur is not None and span.x0 - cur.x1 > 1.2 * max(span.size, cur.size):
                out.append(cur)
                cur = None
            if cur is None:
                cur = _Line(spans=[span], baseline=s["origin"][1], rects=[], math=fixed)
            else:
                cur.spans.append(span)
                cur.math = cur.math or fixed
        if cur is not None:
            cur.rects.append(rect)
    if cur is not None:
        out.append(cur)
    return [p for p in out if "".join(s.text for s in p.spans).strip()]


def _fixed_span(s: dict) -> _Span:
    chars = s["chars"]
    return _Span(
        text="".join(c["c"] for c in chars),
        font=s["font"],
        size=s["size"],
        color=s["color"],
        bold=False,
        italic=False,
        x0=s["bbox"][0],
        x1=s["bbox"][2],
    )


def _bullet_of(ln: _Line) -> str | None:
    head = ln.spans[0].text.lstrip()
    if head and head[0] in BULLET_CHARS and (len(head) == 1 or head[1].isspace()):
        return head[0]
    return None


def _continues(box: _Block, ln: _Line) -> bool:
    """Is `ln` the next wrapped line of the paragraph in `box`?"""
    last = box.lines[-1]
    step = ln.baseline - last.baseline
    # Wrapped lines sit one baselineskip (~1.2 em) apart; list items and table
    # rows are further apart and stay separate boxes.
    if not (0.6 * last.size < step < 1.45 * last.size) or _bullet_of(ln) is not None:
        return False
    if abs(ln.size - last.size) > 0.5:
        return False
    first = box.lines[0]
    # Same left edge, or the hanging indent under a bullet / first line.
    return abs(ln.x0 - first.x0) < 1.5 or (first.x0 < ln.x0 < first.x0 + 3 * first.size and ln.x0 < last.x1)


def _blocks(page: fitz.Page, max_size: float = float("inf")) -> tuple[list[_Block], list[fitz.Rect]]:
    """Split page text into editable boxes and rects that must stay in the background."""
    flags = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_MEDIABOX_CLIP
    probe = _InkProbe(page)
    editable: list[_Block] = []
    keep: list[fitz.Rect] = []
    for b in page.get_text("rawdict", flags=flags)["blocks"]:
        if b.get("type") != 0:
            continue
        boxes: list[_Block] = []
        for raw_line in b["lines"]:
            for piece in _pieces(raw_line, probe, max_size):
                target = next((box for box in reversed(boxes) if _continues(box, piece)), None)
                if target is None:
                    boxes.append(_Block(lines=[piece]))
                else:
                    target.lines.append(piece)
        for box in boxes:
            if any(ln.math for ln in box.lines):
                keep.extend(box.span_rects)
            else:
                editable.append(box)
    return editable, keep


@dataclass
class _Figure:
    rect: fitz.Rect
    png: bytes


def _image_uses(doc: fitz.Document) -> dict[int, int]:
    uses: dict[int, int] = {}
    for page in doc:
        for info in page.get_image_info(xrefs=True):
            uses[info["xref"]] = uses.get(info["xref"], 0) + 1
    return uses


def _figures(doc: fitz.Document, page: fitz.Page, uses: dict[int, int]) -> list[_Figure]:
    """Large, upright raster images drawn exactly once in the whole document.

    Single use matters: the background step blanks the image object itself.
    """
    infos = page.get_image_info(xrefs=True)
    page_area = abs(page.rect)
    out = []
    for info in infos:
        xref = info["xref"]
        rect = fitz.Rect(info["bbox"])
        a, b, c, d, _, _ = info["transform"]
        if (
            xref <= 0
            or uses[xref] != 1
            or abs(b) > 1e-6
            or abs(c) > 1e-6
            or a <= 0
            or d <= 0
            or not (FIGURE_MIN_AREA * page_area <= abs(rect & page.rect) <= 0.9 * page_area)
        ):
            continue
        try:
            pix = fitz.Pixmap(doc, xref)
            smask = doc.xref_get_key(xref, "SMask")
            if smask[0] == "xref":
                pix = fitz.Pixmap(pix, fitz.Pixmap(doc, int(smask[1].split()[0])))
            if pix.colorspace and pix.colorspace.n not in (1, 3):
                pix = fitz.Pixmap(fitz.csRGB, pix)
            out.append(_Figure(rect=rect, png=pix.tobytes("png")))
        except Exception:  # unusual encodings: leave it in the background
            continue
    return out


def _background_png(
    page: fitz.Page,
    text_rects: list[fitz.Rect],
    keep_rects: list[fitz.Rect],
    figures: list[_Figure],
) -> bytes:
    """Render the page without the editable text and the extracted figures.

    Modifies the page, so call it only after everything has been read from
    the document. (A second in-memory copy of the PDF would avoid that, but
    redacting one copy while the other is open corrupts MuPDF's shared font
    store and crashes the process.)

    Redacting single spans is unsafe: MuPDF drops every glyph whose box
    touches the redaction, and a huge ghost numeral's box covers half the
    slide. So render the page once as is and once with all text removed, then
    paste the text that must stay (math, decoration) back from the first.
    """
    if figures:
        fig_rects = [f.rect for f in figures]
        for info in page.get_image_info(xrefs=True):
            if info["xref"] > 0 and fitz.Rect(info["bbox"]) in fig_rects:
                page.delete_image(info["xref"])
    matrix = fitz.Matrix(BACKGROUND_PX_WIDTH / page.rect.width, BACKGROUND_PX_WIDTH / page.rect.width)
    full = page.get_pixmap(matrix=matrix, alpha=False) if keep_rects else None
    if text_rects:
        page.add_redact_annot(page.rect)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
    bare = page.get_pixmap(matrix=matrix, alpha=False)
    if full is not None and text_rects:
        clean = fitz.Pixmap(fitz.csRGB, bare)  # a copy; `bare` stays the text-free source
        bounds = bare.irect
        for k in keep_rects:
            area = (k * matrix).irect & bounds
            if not area.is_empty:
                clean.copy(full, area)
        # Where kept and editable text overlap, the text box covers it: keep it clean.
        for r in text_rects:
            area = (r * matrix).irect & bounds
            if not area.is_empty and any(r.intersects(k) for k in keep_rects):
                clean.copy(bare, area)
        bare = clean
    return bare.tobytes("png")


_CJK = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
QUARTER_EM_SPACE = "\u2005"


def _narrow_cjk_spaces(texts: list[str]) -> list[str]:
    """xeCJK separates CJK and Latin with ~1/4 em glue, extracted as an ordinary
    space; Office would draw a full word space, so use U+2005 instead."""
    joined = "".join(texts)
    chars = list(joined)
    for i, c in enumerate(chars):
        if c != " ":
            continue
        prev = joined[i - 1] if i > 0 else ""
        nxt = joined[i + 1] if i + 1 < len(joined) else ""
        if (prev and _CJK.match(prev)) or (nxt and _CJK.match(nxt)):
            chars[i] = QUARTER_EM_SPACE
    out, pos = [], 0
    for t in texts:
        out.append("".join(chars[pos : pos + len(t)]))
        pos += len(t)
    return out


def _set_fonts(rPr: etree._Element, latin: str, east_asian: str) -> None:
    for tag in ("a:latin", "a:ea"):
        for el in rPr.findall(qn(tag)):
            rPr.remove(el)
    latin_el = etree.SubElement(rPr, qn("a:latin"))
    latin_el.set("typeface", latin)
    ea_el = etree.SubElement(rPr, qn("a:ea"))
    ea_el.set("typeface", east_asian)
    # Schema order: fill elements before latin/ea; hyperlinks after.
    for child in list(rPr):
        if child.tag in (qn("a:hlinkClick"), qn("a:hlinkMouseOver"), qn("a:rtl"), qn("a:extLst")):
            rPr.remove(child)
            rPr.append(child)


def _rgb(color: int) -> RGBColor:
    return RGBColor((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)


def _add_text_block(
    slide, block: _Block, scale: float, default_latin: str, default_cjk: str, pick: fonts.Resolver
) -> None:
    def emu(pt: float) -> Emu:
        return Emu(int(round(pt * scale * EMU_PER_PT)))

    first = block.lines[0]
    sizes = [s.size for ln in block.lines for s in ln.spans]
    size = max(set(sizes), key=sizes.count)
    if len(block.lines) > 1:
        pitch = (block.lines[-1].baseline - first.baseline) / (len(block.lines) - 1)
    else:
        pitch = size * 1.2
    pitch = max(pitch, size * 0.9)

    # Leading bullet glyph -> PowerPoint bullet with a hanging indent.
    bullet: _Span | None = None
    lines = [list(ln.spans) for ln in block.lines]
    text_left = first.x0
    if _bullet_of(first) is not None:
        lead = lines[0][0]
        k = len(lead.text) - len(lead.text.lstrip())  # index of the bullet glyph
        bullet = replace(lead, text=lead.text[k], x0=lead.char_x[k])
        rest = lead.text[k + 1 :]
        r = len(rest) - len(rest.lstrip())
        if rest.strip():
            pos = k + 1 + r
            lines[0][0] = replace(lead, text=rest.lstrip(), x0=lead.char_x[pos], char_x=lead.char_x[pos:])
        else:
            lines[0] = lines[0][1:]
        if lines[0]:
            nxt = lines[0][0]
            lines[0][0] = replace(nxt, text=nxt.text.lstrip())
            text_left = nxt.x0
        if len(block.lines) > 1:
            text_left = min(text_left, min(ln.x0 for ln in block.lines[1:]))
    left = bullet.x0 if bullet else min(ln.x0 for ln in block.lines)

    width = block.bbox[2] - left + 2
    top = first.baseline - pitch * 0.8
    height = pitch * len(block.lines)
    box = slide.shapes.add_textbox(emu(left), emu(top), emu(width), emu(height))
    tf = box.text_frame
    tf.word_wrap = False
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    para = tf.paragraphs[0]
    pPr = para._p.get_or_add_pPr()
    ln_spc = etree.SubElement(pPr, qn("a:lnSpc"))
    etree.SubElement(ln_spc, qn("a:spcPts")).set("val", str(int(round(pitch * scale * 100))))
    if bullet:
        indent = (text_left - left) * scale * EMU_PER_PT
        pPr.set("marL", str(int(round(indent))))
        pPr.set("indent", str(-int(round(indent))))
        clr = etree.SubElement(pPr, qn("a:buClr"))
        etree.SubElement(clr, qn("a:srgbClr")).set("val", str(_rgb(bullet.color)))
        # TeX's bullet glyphs are larger than Office fonts' U+2022; Arial's
        # bullet at ~130% matches the dot size beamer draws.
        round_dot = bullet.text == "\u2022"
        pct = bullet.size / size * (1.3 if round_dot else 1.0)
        etree.SubElement(pPr, qn("a:buSzPct")).set("val", str(int(round(pct * 100000))))
        bu_font = etree.SubElement(pPr, qn("a:buFont"))
        bu_font.set("typeface", pick.name(fonts.FontRef(fonts.SANS)) if round_dot else pick.name(fonts.latin_ref(bullet.font)))
        etree.SubElement(pPr, qn("a:buChar")).set("char", bullet.text.strip()[0])
    else:
        etree.SubElement(pPr, qn("a:buNone"))

    for i, spans in enumerate(lines):
        if i > 0:
            para.add_line_break()
        for s, text in zip(spans, _narrow_cjk_spaces([s.text for s in spans])):
            run = para.add_run()
            run.text = text
            f = run.font
            f.size = Pt(round(s.size * scale * 2) / 2)
            f.bold = s.bold
            f.italic = s.italic
            f.color.rgb = _rgb(s.color)
            if s.small_caps:
                run._r.get_or_add_rPr().set("cap", "small")
            if s.tracking:
                run._r.get_or_add_rPr().set("spc", str(int(round(s.tracking * scale * 100))))
            cjk = fonts.cjk_ref(s.font, s.text)
            if cjk:
                _set_fonts(run._r.get_or_add_rPr(), default_latin, pick.name(cjk))
            else:
                _set_fonts(run._r.get_or_add_rPr(), pick.name(fonts.latin_ref(s.font)), default_cjk)
    # The trailing endParaRPr keeps the size when the user adds text at the end.
    end = para._p.get_or_add_endParaRPr()
    end.set("sz", str(int(round(size * scale)) * 100))


def _lock(picture) -> None:
    """Keep the background from being grabbed while clicking around the text."""
    nv = picture._element.nvPicPr.find(qn("p:cNvPicPr"))
    locks = nv.find(qn("a:picLocks"))
    if locks is None:
        locks = etree.SubElement(nv, qn("a:picLocks"))
    for attr in ("noGrp", "noSelect", "noMove", "noResize", "noRot", "noChangeAspect"):
        locks.set(attr, "1")


def _dominant_fonts(doc: fitz.Document) -> tuple[fonts.FontRef, fonts.FontRef, float]:
    """Most used Latin and CJK fonts, and the body text size."""
    latin: dict[fonts.FontRef, int] = {}
    cjk: dict[fonts.FontRef, int] = {}
    sizes: dict[float, int] = {}
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    if _is_math_font(s["font"]):
                        continue
                    office = fonts.cjk_ref(s["font"], s["text"])
                    target = cjk if office else latin
                    name = office or fonts.latin_ref(s["font"])
                    target[name] = target.get(name, 0) + len(s["text"])
                    size = round(s["size"], 1)
                    sizes[size] = sizes.get(size, 0) + len(s["text"])
    pick = lambda d, fallback: max(d, key=d.get) if d else fallback  # noqa: E731
    return (
        pick(latin, fonts.FontRef("humanist")), pick(cjk, fonts.FontRef(fonts.CJK_SANS)), pick(sizes, 10.0)
    )


def pdf_to_pptx(
    pdf_path: Path,
    out_path: Path,
    notes: dict[int, str] | None = None,
    installed: list[str] | None = None,
) -> None:
    """notes: 1-based PDF page -> speaker notes for that slide.
    installed: font names present where the file will be opened (see fonts.py);
    None writes Office names."""
    try:
        doc = fitz.open("pdf", Path(pdf_path).read_bytes())
    except Exception as e:
        raise PptxExportError(f"Cannot open PDF: {e}") from e
    if doc.page_count == 0:
        raise PptxExportError("PDF has no pages")

    first = doc[0].rect
    scale = SLIDE_WIDTH_PT / first.width
    prs = Presentation()
    prs.slide_width = Emu(int(round(first.width * scale * EMU_PER_PT)))
    prs.slide_height = Emu(int(round(first.height * scale * EMU_PER_PT)))
    blank = prs.slide_layouts[6]
    resolver = fonts.Resolver(installed)
    latin_ref, cjk_ref, body_size = _dominant_fonts(doc)
    default_latin, default_cjk = resolver.name(latin_ref), resolver.name(cjk_ref)

    # Pass 1 reads everything; pass 2 rewrites pages to render backgrounds.
    uses = _image_uses(doc)
    pages = []
    for page in doc:
        blocks, keep = _blocks(page, max_size=DECORATIVE_FACTOR * body_size)
        pages.append((page, blocks, keep, _figures(doc, page, uses)))

    for page, blocks, keep, figures in pages:
        page_scale = SLIDE_WIDTH_PT / page.rect.width
        note = (notes or {}).get(page.number + 1, "")
        rects = [r for b in blocks for r in b.glyph_rects]
        bg = _background_png(page, rects, keep, figures)
        slide = prs.slides.add_slide(blank)
        background = slide.shapes.add_picture(io.BytesIO(bg), 0, 0, prs.slide_width, prs.slide_height)
        background.name = "Background"
        _lock(background)
        for fig in figures:
            r = fig.rect
            slide.shapes.add_picture(
                io.BytesIO(fig.png),
                Emu(int(r.x0 * page_scale * EMU_PER_PT)),
                Emu(int(r.y0 * page_scale * EMU_PER_PT)),
                Emu(int(r.width * page_scale * EMU_PER_PT)),
                Emu(int(r.height * page_scale * EMU_PER_PT)),
            )
        for block in blocks:
            _add_text_block(slide, block, page_scale, default_latin, default_cjk, resolver)
        if note:
            slide.notes_slide.notes_text_frame.text = note
    pages.clear()
    doc.close()

    out_path = Path(out_path)
    fd, tmp = tempfile.mkstemp(prefix=".pptx-", suffix=".tmp", dir=out_path.parent)
    os.close(fd)
    try:
        prs.save(tmp)
        os.replace(tmp, out_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
