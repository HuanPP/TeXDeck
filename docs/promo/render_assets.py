"""Rasterize the demo PDF and the compiled template decks into build/assets.

usage: render_assets.py DECKS_DIR
DECKS_DIR holds compiled templates as <anything>/<template>/main.pdf, which is
what `pytest -m slow --basetemp=DECKS_DIR` leaves behind.
"""
import glob
import os
import shutil
import sys

import pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ASSETS = os.path.join(HERE, "build", "assets")
FONTS = os.path.join(HERE, "build", "fonts")
TEMPLATES = ["academic", "emerald", "graphite", "navy", "scholar", "simpleplus", "execushares"]

os.makedirs(ASSETS, exist_ok=True)
os.makedirs(FONTS, exist_ok=True)

demo = pymupdf.open(os.path.join(REPO, "docs/demo/gold-price-2024.pdf"))
demo[0].get_pixmap(dpi=160).save(os.path.join(ASSETS, "demo-1.png"))
shutil.copy(os.path.join(REPO, "docs/hero.png"), ASSETS)

for name in TEMPLATES:
    found = sorted(glob.glob(os.path.join(sys.argv[1], "*", name, "main.pdf")))
    if not found:
        sys.exit(f"no compiled {name}/main.pdf under {sys.argv[1]}")
    for i, page in enumerate(pymupdf.open(found[0])):
        zoom = 1280 / page.rect.width
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(os.path.join(ASSETS, f"{name}-{i + 1:02d}.png"))

for src in ["emerald/font/EBGaramond12-Regular.otf", "emerald/font/EBGaramond-Italic.otf",
            "graphite/font/texgyreheros-regular.otf", "graphite/font/texgyreheros-bold.otf"]:
    shutil.copy(os.path.join(REPO, "templates", src), FONTS)
