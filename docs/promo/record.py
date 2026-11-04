"""Step the promo timeline frame by frame and screenshot each frame.

usage: record.py FPS [t1 t2 ...]   (with times: write stills only)
Set CHROMIUM to use a system browser instead of the one Playwright downloads.
"""
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
fps = float(sys.argv[1])
stills = [float(x) for x in sys.argv[2:]]
out = os.path.join(HERE, "build", "stills" if stills else "frames")
os.makedirs(out, exist_ok=True)

with sync_playwright() as pw:
    browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None,
                                 args=["--allow-file-access-from-files"])
    try:
        page = browser.new_page(viewport={"width": 1280, "height": 544}, device_scale_factor=1)
        page.goto("file://" + os.path.join(HERE, "promo.html"))
        page.evaluate("document.fonts.ready")
        page.wait_for_function("Array.from(document.images).every(i => i.complete)")
        stage = page.locator("#stage")
        times = stills or [i / fps for i in range(int(page.evaluate("DURATION") * fps))]
        for i, t in enumerate(times):
            page.evaluate(f"renderAt({t})")
            name = f"t{t:05.2f}.png" if stills else f"f{i:04d}.png"
            stage.screenshot(path=os.path.join(out, name))
    finally:
        browser.close()
