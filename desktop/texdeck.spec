# PyInstaller spec for the TeXDeck desktop app. Run through desktop/build.py,
# which stages templates and the Tectonic binary under desktop/build/ first.
# ruff: noqa: F821 (Analysis, PYZ, EXE, COLLECT, BUNDLE, SPECPATH are injected by PyInstaller)

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

DESKTOP = Path(SPECPATH)
ROOT = DESKTOP.parent
STAGE = DESKTOP / "build" / "stage"
VERSION = os.environ.get("TEXDECK_VERSION", "0.0.0")
TECTONIC = "tectonic.exe" if sys.platform == "win32" else "tectonic"

a = Analysis(
    [str(DESKTOP / "launcher.py")],
    pathex=[str(ROOT / "server")],
    binaries=[(str(STAGE / "bin" / TECTONIC), "bin")],
    datas=[
        (str(STAGE / "templates"), "templates"),
        (str(ROOT / "web" / "dist"), "web/dist"),
    ],
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("app"),
    # uvicorn falls back to asyncio without uvloop; zstandard uses its C backend.
    excludes=["tkinter", "pytest", "uvloop", "zstandard.backend_cffi", "zstandard._cffi"],
)
pyz = PYZ(a.pure)
icon = str(DESKTOP / "assets" / ("icon.icns" if sys.platform == "darwin" else "icon.ico"))
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TeXDeck",
    console=False,
    icon=icon,
)
coll = COLLECT(exe, a.binaries, a.datas, name="TeXDeck")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TeXDeck.app",
        icon=icon,
        bundle_identifier="io.github.huanpp.texdeck",
        version=VERSION,
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
