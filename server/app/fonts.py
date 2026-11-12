"""Which installed font a PPTX run should name, for a font found in the PDF.

A PPTX run names one typeface, and PowerPoint, WPS and Keynote look it up by
its exact name. The same design goes by many names: Palatino is "Palatino
Linotype" on Windows, "Palatino" on macOS, "URW Palladio L" or "P052" on
Linux, "TeX Gyre Pagella" as the TeX clone. So fonts are grouped into
designs, each listing every name it goes by, and each design names the
designs to fall back to, nearest first and always in the same style: a
serif design ends in the Times family, a sans design in the Arial family,
a monospaced one in the Courier family, Kai and Fangsong in Song.

The browser reports which of these names are installed (it can tell by
measuring text); resolve() then picks, for each PDF font, its own name if
installed, else the first installed name along its design chain. Without
that report the first (Windows / Office) name of the design is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Design:
    names: tuple[str, ...]  # every name of this design; the first is the Office name
    then: tuple[str, ...] = ()  # designs to try next, nearest first


DESIGNS: dict[str, Design] = {
    # ---- serif ----
    "times": Design((
        "Times New Roman", "Times", "Tinos", "Liberation Serif", "Nimbus Roman", "Nimbus Roman No9 L",
        "TeX Gyre Termes", "STIX Two Text", "STIXGeneral", "FreeSerif", "Thorndale AMT",
    )),
    "palatino": Design((
        "Palatino Linotype", "Palatino", "Book Antiqua", "URW Palladio L", "P052", "TeX Gyre Pagella",
        "Palladio", "Domitian",
    ), ("garamond", "times")),
    "garamond": Design((
        "Garamond", "EB Garamond", "EB Garamond 12", "Adobe Garamond Pro", "Garamond Premier Pro",
        "ITC Garamond", "Cormorant Garamond", "Sabon", "Hoefler Text",
    ), ("palatino", "times")),
    "oldstyle": Design((
        "Minion Pro", "Crimson Pro", "Crimson Text", "Cochineal", "Linux Libertine O", "Linux Libertine",
        "Libertinus Serif", "Alegreya",
    ), ("garamond", "palatino", "times")),
    "schoolbook": Design((
        "Century Schoolbook", "New Century Schoolbook", "Century Schoolbook L", "C059", "TeX Gyre Schola",
        "Century",
    ), ("georgia", "times")),
    "bookman": Design((
        "Bookman Old Style", "Bookman", "ITC Bookman", "URW Bookman", "URW Bookman L", "TeX Gyre Bonum",
    ), ("schoolbook", "georgia", "times")),
    "baskerville": Design(("Baskerville Old Face", "Baskerville", "Libre Baskerville"), ("times",)),
    "didone": Design(("Bodoni MT", "Didot", "Bodoni 72", "Bodoni Moda", "GFS Didot"), ("times",)),
    "georgia": Design(("Georgia", "Gelasio"), ("schoolbook", "times")),
    "cambria": Design(("Cambria", "Caladea"), ("georgia", "times")),
    "computermodern": Design((
        "Latin Modern Roman", "LM Roman 10", "CMU Serif", "New Computer Modern",
    ), ("cambria",)),
    "charter": Design((
        "Charter", "Bitstream Charter", "XCharter", "Charis SIL", "Source Serif 4", "Source Serif Pro",
        "Utopia", "Erewhon",
    ), ("georgia", "times")),
    "textserif": Design((
        "Noto Serif", "Droid Serif", "DejaVu Serif", "Bitstream Vera Serif", "PT Serif", "IBM Plex Serif",
        "Merriweather", "Lora",
    ), ("georgia", "times")),
    "chancery": Design((
        "Monotype Corsiva", "Apple Chancery", "URW Chancery L", "Z003", "TeX Gyre Chorus", "Zapf Chancery",
    ), ("times",)),
    # ---- sans serif ----
    "arial": Design((
        "Arial", "Helvetica", "Helvetica Neue", "Liberation Sans", "Arimo", "Nimbus Sans", "Nimbus Sans L",
        "TeX Gyre Heros", "FreeSans",
    )),
    "humanist": Design((
        "Calibri", "Carlito", "Segoe UI", "Source Sans 3", "Source Sans Pro", "Open Sans", "Noto Sans",
        "Fira Sans", "Lato", "PT Sans", "Myriad Pro", "Frutiger", "Ubuntu", "Candara", "Avenir Next",
        "Trebuchet MS", "Gill Sans",
    ), ("arial",)),
    "computermodernsans": Design((
        "Latin Modern Sans", "LM Sans 10", "CMU Sans Serif",
    ), ("humanist", "arial")),
    "verdana": Design(("Verdana", "DejaVu Sans", "Bitstream Vera Sans"), ("tahoma", "humanist", "arial")),
    "tahoma": Design(("Tahoma",), ("verdana", "arial")),
    "gill": Design(("Gill Sans MT", "Gill Sans", "Gillius ADF"), ("humanist", "arial")),
    "geometric": Design((
        "Century Gothic", "ITC Avant Garde Gothic", "Avant Garde", "URW Gothic", "URW Gothic L",
        "TeX Gyre Adventor", "Futura", "Avenir Next", "Avenir",
    ), ("arial",)),
    "grotesque": Design(("Roboto", "Inter", "Helvetica Neue", "SF Pro Text"), ("arial",)),
    # ---- monospaced ----
    "courier": Design((
        "Courier New", "Courier", "Nimbus Mono PS", "Nimbus Mono L", "TeX Gyre Cursor", "Liberation Mono",
        "Cousine", "FreeMono",
    )),
    "typewriter": Design(("Latin Modern Mono", "LM Mono 10", "CMU Typewriter Text"), ("courier",)),
    "mono": Design((
        "Consolas", "Menlo", "SF Mono", "Monaco", "DejaVu Sans Mono", "Source Code Pro", "Fira Mono",
        "Fira Code", "JetBrains Mono", "Cascadia Mono", "Cascadia Code", "Inconsolata", "Roboto Mono",
        "Ubuntu Mono", "Noto Sans Mono", "Lucida Console",
    ), ("courier",)),
    # ---- CJK ----
    "song": Design((
        "SimSun", "NSimSun", "宋体", "新宋体", "Songti SC", "STSong", "华文宋体", "STZhongsong", "华文中宋",
        "Source Han Serif SC", "Source Han Serif CN", "思源宋体", "Noto Serif CJK SC", "Noto Serif SC",
        "FandolSong", "Adobe Song Std", "AR PL UMing CN", "AR PL SungtiL GB", "Songti TC", "PMingLiU",
        "MingLiU",
    )),
    "fangsong": Design((
        "FangSong", "仿宋", "FangSong_GB2312", "仿宋_GB2312", "STFangsong", "华文仿宋", "FandolFang",
        "Adobe Fangsong Std",
    ), ("song",)),
    "kai": Design((
        "KaiTi", "楷体", "KaiTi_GB2312", "楷体_GB2312", "STKaiti", "华文楷体", "Kaiti SC", "Kaiti TC",
        "BiauKai", "DFKai-SB", "标楷体", "AR PL UKai CN", "AR PL KaitiM GB", "FandolKai", "Adobe Kaiti Std",
        "LXGW WenKai", "霞鹜文楷",
    ), ("fangsong", "song")),
    "xingkai": Design(("STXingkai", "华文行楷", "Xingkai SC"), ("kai", "song")),
    "xinwei": Design(("STXinwei", "华文新魏", "Weibei SC"), ("kai", "song")),
    "lishu": Design(("LiSu", "隶书", "STLiti", "华文隶书", "Libian SC"), ("song",)),
    "hei": Design((
        "Microsoft YaHei", "微软雅黑", "PingFang SC", "苹方-简", "Hiragino Sans GB", "SimHei", "黑体",
        "Heiti SC", "STHeiti", "华文黑体", "STXihei", "华文细黑", "Source Han Sans SC", "Source Han Sans CN",
        "思源黑体", "Noto Sans CJK SC", "Noto Sans SC", "DengXian", "等线", "FandolHei", "WenQuanYi Micro Hei",
        "文泉驿微米黑", "WenQuanYi Zen Hei", "Lantinghei SC", "Droid Sans Fallback",
    )),
    "yuan": Design(("YouYuan", "幼圆", "Yuanti SC", "圆体-简"), ("hei",)),
    "display": Design(("STCaiyun", "华文彩云", "STHupo", "华文琥珀"), ("hei",)),
}

SERIF, SANS, MONO, CJK_SERIF, CJK_SANS = "times", "arial", "mono", "song", "hei"


@dataclass(frozen=True)
class FontRef:
    """A PDF font as a design, plus the names of the very font (tried first)."""

    design: str | None  # None: unknown design, only `own` can be used
    own: tuple[str, ...] = ()


# PDF font name (normalised: lower case, no spaces, dashes or underscores)
# contains a key -> design, and the names of that exact font. First match
# wins, so specific keys come first ("dejavusansmono" before "dejavusans",
# "xingkai" before "kai").
_LATIN_RULES: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = [
    (("courier", "nimbusmon", "freemono", "liberationmono", "cousine"), "courier", ()),
    (("cursor",), "courier", ("TeX Gyre Cursor",)),
    (("lmmono", "lmtt", "cmtt", "cmuntt", "cmutypewriter"), "typewriter", ()),
    (("sourcecode",), "mono", ("Source Code Pro",)),
    (("firamono", "firacode"), "mono", ("Fira Mono", "Fira Code")),
    (("dejavusansmono",), "mono", ("DejaVu Sans Mono",)),
    (("consol", "menlo", "monaco", "inconsolata", "jetbrains", "cascadia", "mono", "code", "typewriter"),
     "mono", ()),
    (("ebgaramond",), "garamond", ("EB Garamond", "EB Garamond 12")),
    (("garamond", "garamon", "sabon"), "garamond", ()),
    (("pagella",), "palatino", ("TeX Gyre Pagella",)),
    (("palatino", "palladio", "pplr", "pplb", "p052", "bookantiqua", "domitian", "asanatext"), "palatino", ()),
    (("schola",), "schoolbook", ("TeX Gyre Schola",)),
    (("schoolbook", "centuryschl", "ncnr", "c059"), "schoolbook", ()),
    (("bonum",), "bookman", ("TeX Gyre Bonum",)),
    (("bookman",), "bookman", ()),
    (("baskerv",), "baskerville", ()),
    (("didot", "bodoni"), "didone", ()),
    (("georgia", "gelasio"), "georgia", ()),
    (("cambria", "caladea"), "cambria", ()),
    (("termes",), "times", ("TeX Gyre Termes",)),
    (("times", "nimbusrom", "tinos", "liberationserif", "stixtwo", "stixgeneral", "freeserif", "ntxtlf",
      "ntxr", "txr", "ptmr", "thorndale"), "times", ()),
    (("lmroman", "lmr", "cmuserif", "cmunrm", "cmroman", "newcm", "latinmodernroman"), "computermodern", ()),
    (("sourceserif",), "charter", ("Source Serif 4", "Source Serif Pro")),
    (("charter", "charis", "utopia", "erewhon"), "charter", ()),
    (("crimson", "minion", "cochineal", "libertin", "alegreya"), "oldstyle", ()),
    (("notoserif", "droidserif", "dejavuserif", "veraserif", "ptserif", "plexserif", "merriweather", "lora"),
     "textserif", ()),
    (("chancery", "zcmi", "chorus", "corsiva", "z003"), "chancery", ()),
    (("adventor",), "geometric", ("TeX Gyre Adventor",)),
    (("avant", "urwgothic", "centurygothic", "futura", "avenir"), "geometric", ()),
    (("verdana", "dejavusans", "bitstreamvera", "verasans"), "verdana", ()),
    (("tahoma",), "tahoma", ()),
    (("gillsans", "gillius"), "gill", ()),
    (("heros",), "arial", ("TeX Gyre Heros",)),
    (("helvet", "nimbussan", "arial", "liberationsans", "arimo", "freesans", "phvr"), "arial", ()),
    (("roboto", "sfpro"), "grotesque", ()),
    (("lmsans", "cmuss", "cmunss", "latinmodernsans"), "computermodernsans", ()),
    (("sourcesans",), "humanist", ("Source Sans 3", "Source Sans Pro")),
    (("firasans",), "humanist", ("Fira Sans",)),
    (("opensans",), "humanist", ("Open Sans",)),
    (("notosans",), "humanist", ("Noto Sans",)),
    (("segoe", "selawik"), "humanist", ("Segoe UI",)),
    (("calibri", "carlito", "fira", "lato", "ptsans", "ubuntu", "cabin", "myriad", "frutiger", "candara",
      "plexsans", "trebuchet"), "humanist", ()),
]
_CJK_RULES: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = [
    (("xingkai", "行楷"), "xingkai", ()),
    (("xinwei", "weibei", "新魏"), "xinwei", ()),
    (("lishu", "lisu", "liti", "libian", "隶书", "隸書"), "lishu", ()),
    (("youyuan", "yuanti", "幼圆", "圆体", "圓體"), "yuan", ()),
    (("caiyun", "hupo", "彩云", "琥珀"), "display", ()),
    (("fandolfang",), "fangsong", ("FandolFang",)),
    (("fangsong", "仿宋", "倣宋"), "fangsong", ()),
    (("fandolkai",), "kai", ("FandolKai",)),
    (("lxgw", "wenkai", "文楷"), "kai", ("LXGW WenKai", "霞鹜文楷")),
    (("kaiti", "kai", "楷"), "kai", ()),
    (("notoserifcjk", "serifcjk"), "song", ("Noto Serif CJK SC", "Source Han Serif SC", "Noto Serif SC")),
    (("sourcehanserif", "hanserif"), "song", ("Source Han Serif SC", "Source Han Serif CN", "思源宋体",
                                             "Noto Serif CJK SC")),
    (("fandolsong",), "song", ("FandolSong",)),
    (("songti", "simsun", "nsimsun", "stsong", "zhongsong", "song", "mincho", "ming", "batang", "myeongjo",
      "宋", "明朝", "明體"), "song", ()),
    (("notosanscjk", "sanscjk"), "hei", ("Noto Sans CJK SC", "Source Han Sans SC", "Noto Sans SC")),
    (("sourcehansans", "hansans"), "hei", ("Source Han Sans SC", "Source Han Sans CN", "思源黑体",
                                          "Noto Sans CJK SC")),
    (("fandolhei",), "hei", ("FandolHei",)),
    (("pingfang", "苹方"), "hei", ("PingFang SC",)),
    (("yahei", "雅黑"), "hei", ("Microsoft YaHei",)),
    (("heiti", "simhei", "stheiti", "xihei", "hei", "dengxian", "wqy", "wenquanyi", "droidsansfallback",
      "lantinghei", "hiragino", "gothic", "dotum", "gulim", "黑", "等线"), "hei", ()),
]
# No rule matched: the words in the name still tell the style.
_LATIN_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("mono", "code", "type", "typewriter"), MONO),
    (("script", "hand", "brush", "callig", "chancery"), "chancery"),
    (("sans", "grotesk", "grotesque", "gothic", "grot", "ui"), SANS),
    (("serif", "roman", "book", "antiqua", "text", "old", "slab", "rm"), SERIF),
]

# Names that mark a CJK font even when a span holds only Latin characters.
# Style words alone (ming, gothic, kai) are not enough: Latin fonts use them.
_CJK_NAME = re.compile(
    r"cjk|sourcehan|notoserif(sc|tc|jp|kr|hk)|notosans(sc|tc|jp|kr|hk)|fandol|simsun|simhei|yahei|songti|heiti|"
    r"kaiti|fangsong|^st(song|kaiti|fangsong|heiti|xihei|xingkai|xinwei|caiyun|hupo|liti|zhongsong)|pingfang|"
    r"wqy|wenquanyi|lxgw|dengxian|mincho|batang|dotum|gulim|droidsansfallback|lantinghei|hiragino|[^\x00-\x7f]"
)
# Han, kana, hangul, CJK punctuation and full-width forms.
CJK_CHAR = re.compile(r"[⺀-鿿가-힯豈-﫿︰-﹏＀-￯\U00020000-\U0003134f]")


def _family(pdf_font: str) -> str:
    name = pdf_font.split("+", 1)[-1]
    if "#" in name:  # PDF name escapes, e.g. #E6#A5#B7 for a Chinese font name
        try:
            name = re.sub(r"(?:#[0-9A-Fa-f]{2})+", lambda m: bytes.fromhex(m[0].replace("#", "")).decode(), name)
        except UnicodeDecodeError:
            pass
    return re.sub(r"[-_ ,]", "", name).lower()


def _words(pdf_font: str) -> set[str]:
    """Lower-case words of the name, split at separators and case changes."""
    name = pdf_font.split("+", 1)[-1]
    return {w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+", name)}


def _match(fam: str, rules: list[tuple[tuple[str, ...], str, tuple[str, ...]]]) -> FontRef | None:
    for keys, design, own in rules:
        if any(k in fam for k in keys):
            return FontRef(design, own)
    return None


def cjk_ref(pdf_font: str, text: str = "") -> FontRef | None:
    """The CJK design of a PDF font; None when it is not a CJK font.

    A font is CJK when the text set in it has CJK characters (xeCJK sets only
    those in the CJK font) or when its name says so."""
    fam = _family(pdf_font)
    if not (CJK_CHAR.search(text) or _CJK_NAME.search(fam)):
        return None
    return _match(fam, _CJK_RULES) or FontRef(
        CJK_SERIF if _words(pdf_font) & {"serif", "roman", "book", "ming"} else CJK_SANS
    )


def latin_ref(pdf_font: str) -> FontRef:
    fam = _family(pdf_font)
    ref = _match(fam, _LATIN_RULES)
    if ref:
        return ref
    words = _words(pdf_font)
    for keys, design in _LATIN_HINTS:
        if words & set(keys):
            return FontRef(design)
    # Unknown design: only its own name, which works where it is installed.
    return FontRef(None, (pdf_font.split("+", 1)[-1].split("-", 1)[0],))


def chain(design: str) -> list[str]:
    """The design, then its fallbacks depth-first, each once."""
    out: list[str] = []

    def walk(key: str) -> None:
        if key in out:
            return
        out.append(key)
        for nxt in DESIGNS[key].then:
            walk(nxt)

    walk(design)
    return out


def all_names() -> list[str]:
    """Every name the browser should check for, each once."""
    seen: dict[str, None] = {}
    for design in DESIGNS.values():
        for name in design.names:
            seen.setdefault(name, None)
    for rules in (_LATIN_RULES, _CJK_RULES):
        for _, _, own in rules:
            for name in own:
                seen.setdefault(name, None)
    return list(seen)


def _fold(name: str) -> str:
    return name.casefold().replace(" ", "")


class Resolver:
    """Picks installed names. installed=None: nothing is known about the machine,
    so every design gets its Office name."""

    def __init__(self, installed: list[str] | set[str] | None = None) -> None:
        self.installed = None if installed is None else {_fold(n) for n in installed}

    def has(self, name: str) -> bool:
        return self.installed is not None and _fold(name) in self.installed

    def name(self, ref: FontRef) -> str:
        if self.installed is not None:
            for own in ref.own:
                if self.has(own):
                    return own
            if ref.design is not None:
                for key in chain(ref.design):
                    for name in DESIGNS[key].names:
                        if self.has(name):
                            return name
        if ref.design is None:
            return ref.own[0] if ref.own and ref.own[0] else DESIGNS[SANS].names[0]
        return DESIGNS[ref.design].names[0]
