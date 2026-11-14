"""The PDF viewer loads pdf.js's worker from the top-level pdfjs-dist; react-pdf
runs its own. Two different versions break every PDF in the browser. KaTeX the
same way: the stylesheet comes from the top-level katex, the HTML from the copy
rehype-katex renders with; two versions stack fractions and lose superscripts."""

from __future__ import annotations

import json

from app.config import REPO_ROOT


def test_pdfjs_worker_matches_react_pdf() -> None:
    packages = json.loads((REPO_ROOT / "web" / "package-lock.json").read_text())["packages"]
    top = packages["node_modules/pdfjs-dist"]["version"]
    wanted = packages["node_modules/react-pdf"]["dependencies"]["pdfjs-dist"]
    assert top == wanted, f"pin pdfjs-dist to {wanted} in web/package.json (now {top})"
    assert "node_modules/react-pdf/node_modules/pdfjs-dist" not in packages


def test_katex_stylesheet_matches_rehype_katex() -> None:
    packages = json.loads((REPO_ROOT / "web" / "package-lock.json").read_text())["packages"]
    nested = [k for k in packages if k.endswith("/node_modules/katex")]
    assert not nested, f"one katex only: pin web/package.json katex to {packages[nested[0]]['version']} ({nested})"
