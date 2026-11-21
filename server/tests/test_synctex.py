"""The SyncTeX reader, on a small hand-written file (no TeX needed)."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from app.synctex import SyncTeX

SP = 65781.76  # scaled points per PDF point
INCH = 4736287

# One page: a vbox holding two typeset lines (hboxes) of input 1 and one of
# input 2. Line 5's words are kerns (XeTeX's interword space); a stray kern from
# line 7 ends the first typeset line, as TeX files line-end kerns.
SAMPLE = f"""SyncTeX Version:1
Input:1:/proj/./main.tex
Input:2:/proj/./part.tex
Output:pdf
Magnification:1000
Unit:1
X Offset:{INCH}
Y Offset:{INCH}
Content:
!100
{{1
[1,9:0,40000000:30000000,40000000,0
(1,6:0,1000000:30000000,600000,200000
k1,5:2000000,1000000:200000
k1,5:5000000,1000000:200000
k1,7:29000000,1000000:9000000
)
(1,8:0,3000000:30000000,600000,200000
k1,7:3000000,3000000:200000
k1,7:9000000,3000000:200000
)
(2,3:0,5000000:30000000,600000,200000
g2,3:4000000,5000000
)
]
}}1
Input:3:/proj/./late.tex
"""


def pt(sp: float) -> float:
    return (sp + INCH) / SP


@pytest.fixture
def sync(tmp_path: Path) -> SyncTeX:
    path = tmp_path / "x.synctex.gz"
    path.write_bytes(gzip.compress(SAMPLE.encode()))
    return SyncTeX.load(path)


def test_inputs_and_pages(sync: SyncTeX) -> None:
    assert sync.inputs == {1: "/proj/./main.tex", 2: "/proj/./part.tex", 3: "/proj/./late.tex"}
    assert list(sync.pages) == [1]


def test_forward_takes_the_first_record_in_the_box_with_most_of_the_line(sync: SyncTeX) -> None:
    # Kerns record where they end: line 5 starts at the first kern's start.
    assert sync.forward({1}, 5) == pytest.approx((1, pt(2000000 - 200000), pt(1000000)))
    # Line 7 has a stray kern on the first typeset line and two on the second.
    assert sync.forward({1}, 7) == pytest.approx((1, pt(3000000 - 200000), pt(3000000)))
    # Line 3 of the part is a typeset line of its own: its box's left end.
    assert sync.forward({2}, 3) == pytest.approx((1, pt(0), pt(5000000)))
    # No records for line 1 (the preamble): the next line that has some.
    assert sync.forward({1}, 1)[2] == pytest.approx(pt(1000000))
    assert sync.forward({3}, 1) is None


def test_inverse_finds_the_line_under_a_point(sync: SyncTeX) -> None:
    assert sync.inverse(1, pt(5000000), pt(1000000) - 2) == ("/proj/./main.tex", 5)
    assert sync.inverse(1, pt(9000000), pt(3000000) - 2) == ("/proj/./main.tex", 7)
    assert sync.inverse(1, pt(4500000), pt(5000000)) == ("/proj/./part.tex", 3)
    assert sync.inverse(2, 10, 10) is None


def test_plain_file_and_junk_lines(tmp_path: Path) -> None:
    path = tmp_path / "x.synctex"
    path.write_text(SAMPLE.replace("k1,5:5000000", "k1,5:oops") + "Postamble:\nCount:9\n")
    sync = SyncTeX.load(path)
    assert sync.inverse(1, pt(2000000), pt(1000000)) == ("/proj/./main.tex", 5)
