"""Font designs: which installed name a PDF font becomes in the PPTX."""

from __future__ import annotations

from app import fonts
from app.fonts import DESIGNS, FontRef, Resolver, all_names, chain, cjk_ref, latin_ref

WINDOWS = ["Times New Roman", "Arial", "Palatino Linotype", "Book Antiqua", "Garamond", "Calibri",
           "Cambria", "Georgia", "Consolas", "Courier New", "SimSun", "Microsoft YaHei", "KaiTi", "FangSong"]
MAC = ["Times New Roman", "Times", "Arial", "Helvetica", "Helvetica Neue", "Palatino", "Georgia", "Menlo",
       "Courier New", "Hoefler Text", "Songti SC", "PingFang SC", "STKaiti", "Kaiti SC", "STFangsong"]
LINUX = ["Liberation Serif", "Liberation Sans", "DejaVu Sans", "DejaVu Serif", "Noto Serif CJK SC",
         "Noto Sans CJK SC", "AR PL UKai CN"]


def _pick(pdf_font: str, installed: list[str] | None, text: str = "Text") -> str:
    ref = cjk_ref(pdf_font, text) or latin_ref(pdf_font)
    return Resolver(installed).name(ref)


def test_the_same_design_under_each_platforms_name() -> None:
    assert _pick("TeXGyrePagella-Regular", WINDOWS) == "Palatino Linotype"
    assert _pick("TeXGyrePagella-Regular", MAC) == "Palatino"
    assert _pick("TeXGyrePagella-Regular", ["TeX Gyre Pagella", *MAC]) == "TeX Gyre Pagella"  # the very font
    assert _pick("NimbusRomNo9L-Regu", MAC) == "Times New Roman"
    assert _pick("STKaiti", WINDOWS, "楷书") == "KaiTi"
    assert _pick("KaiTi", MAC, "楷书") == "STKaiti"
    assert _pick("FandolKai-Regular", LINUX, "楷书") == "AR PL UKai CN"
    assert _pick("NotoSerifCJKjp-Regular", MAC, "宋体") == "Songti SC"
    assert _pick("NotoSerifCJKjp-Regular", LINUX, "宋体") == "Noto Serif CJK SC"
    assert _pick("SourceCodePro-Regular", MAC) == "Menlo"


def test_fallbacks_keep_the_style() -> None:
    # Serif never falls to a sans face, Kai and Fangsong fall to Song.
    assert _pick("TeXGyrePagella-Regular", ["Arial", "Liberation Serif"]) == "Liberation Serif"
    assert _pick("EBGaramond12-Regular", ["Arial", "Palatino"]) == "Palatino"
    assert _pick("LMRoman10-Regular", ["Arial", "Georgia"]) == "Georgia"
    assert _pick("FandolKai-Regular", ["Microsoft YaHei", "SimSun"], "楷书") == "SimSun"
    assert _pick("FandolFang-Regular", ["PingFang SC", "Songti SC"], "仿宋") == "Songti SC"
    assert _pick("SourceSansPro-Regular", ["Times New Roman", "Helvetica"]) == "Helvetica"
    assert _pick("SomeSlabSerif-Regular", ["Arial", "Times"]) == "Times"
    for key in DESIGNS:
        ends = {k for k in chain(key) if not DESIGNS[k].then}
        assert len(ends) == 1, key  # every design ends in one generic face
    assert chain("palatino")[-1] == "times" and chain("kai")[-1] == "song"


def test_nothing_known_or_nothing_installed_gives_office_names() -> None:
    assert _pick("TeXGyrePagella-Regular", None) == "Palatino Linotype"
    assert _pick("TeXGyrePagella-Regular", []) == "Palatino Linotype"
    assert _pick("LXGWWenKai-Regular", None, "文楷") == "KaiTi"


def test_style_words_alone_do_not_make_a_latin_font_cjk() -> None:
    assert cjk_ref("Flamingo-Regular", "Text") is None
    assert cjk_ref("CenturyGothic", "Text") is None
    assert latin_ref("URWGothic-Book") == FontRef("geometric")
    assert cjk_ref("#E6#A5#B7#E4#BD#93", "楷体") == FontRef("kai")  # 楷体, PDF-escaped
    assert cjk_ref("PingFangSC-Regular", "苹方").design == fonts.CJK_SANS
    assert cjk_ref("STXingkai", "行楷").design == "xingkai"


def test_every_name_is_offered_to_the_browser_once() -> None:
    names = all_names()
    assert len(names) == len(set(names))
    assert {"Palatino", "Palatino Linotype", "TeX Gyre Pagella", "STKaiti", "华文楷体", "Kaiti SC"} <= set(names)
