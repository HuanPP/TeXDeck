"""The PDF viewer loads pdf.js's worker from the top-level pdfjs-dist; react-pdf
runs its own. Two different versions break every PDF in the browser."""

from __future__ import annotations

import json

from app.config import REPO_ROOT


def test_pdfjs_worker_matches_react_pdf() -> None:
    packages = json.loads((REPO_ROOT / "web" / "package-lock.json").read_text())["packages"]
    top = packages["node_modules/pdfjs-dist"]["version"]
    wanted = packages["node_modules/react-pdf"]["dependencies"]["pdfjs-dist"]
    assert top == wanted, f"pin pdfjs-dist to {wanted} in web/package.json (now {top})"
    assert "node_modules/react-pdf/node_modules/pdfjs-dist" not in packages
