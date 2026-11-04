"""Build the TeXDeck desktop release for the current platform.

    python desktop/build.py            # stage, freeze, package, smoke-test
    python desktop/build.py --no-smoke

Needs web/dist (cd web && npm run build) and, in the active environment, the
server package plus pyinstaller and pywebview. Output lands in
desktop/dist/release/: a .dmg on macOS, a setup .exe on Windows (Inno Setup;
a .zip if ISCC is not installed), a .tar.gz on Linux.
"""

from __future__ import annotations

import argparse
import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
ROOT = DESKTOP.parent
BUILD = DESKTOP / "build"
STAGE = BUILD / "stage"
DIST = DESKTOP / "dist"
RELEASE = DIST / "release"

TECTONIC_VERSION = "0.17.0"
TECTONIC_TARGETS = {
    ("darwin", "arm64"): "aarch64-apple-darwin.tar.gz",
    ("darwin", "x86_64"): "x86_64-apple-darwin.tar.gz",
    ("win32", "amd64"): "x86_64-pc-windows-msvc.zip",
    ("linux", "x86_64"): "x86_64-unknown-linux-musl.tar.gz",
}


def run(cmd: list[str], **kwargs) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def target() -> tuple[str, str]:
    machine = platform.machine().lower()
    machine = {"aarch64": "arm64", "x64": "x86_64"}.get(machine, machine)
    return sys.platform, machine


def version() -> str:
    tag = os.environ.get("TEXDECK_VERSION", "").lstrip("v")
    if tag:
        return tag
    with open(ROOT / "server" / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def fetch_tectonic() -> None:
    suffix = TECTONIC_TARGETS.get(target())
    if suffix is None:
        sys.exit(f"No Tectonic build for {target()}")
    name = "tectonic.exe" if sys.platform == "win32" else "tectonic"
    dest = STAGE / "bin" / name
    if dest.is_file():
        return
    url = (
        "https://github.com/tectonic-typesetting/tectonic/releases/download/"
        f"tectonic%40{TECTONIC_VERSION}/tectonic-{TECTONIC_VERSION}-{suffix}"
    )
    print(f"Downloading {url}", flush=True)
    with urllib.request.urlopen(url, timeout=300) as res:
        payload = res.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            member = next(m for m in zf.namelist() if Path(m).name == name)
            dest.write_bytes(zf.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            member = next(m for m in tf.getmembers() if Path(m.name).name == name and m.isfile())
            extracted = tf.extractfile(member)
            assert extracted is not None
            dest.write_bytes(extracted.read())
    dest.chmod(0o755)


def stage_templates() -> None:
    """Copy only git-tracked template files, so local private packs never ship."""
    out = STAGE / "templates"
    shutil.rmtree(out, ignore_errors=True)
    files = subprocess.run(
        ["git", "ls-files", "-z", "templates"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode().split("\0")
    for rel in filter(None, files):
        src = ROOT / rel
        dst = STAGE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def freeze(ver: str) -> None:
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        sys.exit("web/dist is missing; run `npm run build` in web/ first")
    env = {**os.environ, "TEXDECK_VERSION": ver}
    run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"),
            str(DESKTOP / "texdeck.spec"),
        ],
        env=env,
    )


def executable() -> Path:
    if sys.platform == "darwin":
        return DIST / "TeXDeck.app" / "Contents" / "MacOS" / "TeXDeck"
    return DIST / "TeXDeck" / ("TeXDeck.exe" if sys.platform == "win32" else "TeXDeck")


def smoke(template: str) -> None:
    env = {**os.environ, "TEXDECK_DESKTOP_SMOKE": template}
    for key in ("TEXDECK_TEX_ENGINE", "TEXDECK_TECTONIC_COMMAND", "TEXDECK_TEMPLATES_DIR"):
        env.pop(key, None)
    run([str(executable())], env=env, timeout=1200)


def find_iscc() -> str | None:
    found = shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (Path(base) / "Inno Setup 6" / "ISCC.exe").is_file():
            return str(Path(base) / "Inno Setup 6" / "ISCC.exe")
    return None


def package(ver: str) -> Path:
    RELEASE.mkdir(parents=True, exist_ok=True)
    plat, arch = target()
    if plat == "darwin":
        out = RELEASE / f"TeXDeck-{ver}-macos-{arch}.dmg"
        staging = BUILD / "dmg"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        run(["ditto", str(DIST / "TeXDeck.app"), str(staging / "TeXDeck.app")])
        (staging / "Applications").symlink_to("/Applications")
        out.unlink(missing_ok=True)
        run(["hdiutil", "create", "-volname", "TeXDeck", "-srcfolder", str(staging),
             "-ov", "-format", "UDZO", str(out)])
        return out
    if plat == "win32":
        iscc = find_iscc()
        if iscc:
            run([iscc, f"/DAppVersion={ver}", f"/DSourceDir={DIST / 'TeXDeck'}",
                 f"/DOutputDir={RELEASE}", str(DESKTOP / "installer.iss")])
            return RELEASE / f"TeXDeck-{ver}-windows-x64-setup.exe"
        out = RELEASE / f"TeXDeck-{ver}-windows-x64"
        return Path(shutil.make_archive(str(out), "zip", DIST, "TeXDeck"))
    out = RELEASE / f"TeXDeck-{ver}-linux-{arch}"
    return Path(shutil.make_archive(str(out), "gztar", DIST, "TeXDeck"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-smoke", action="store_true", help="skip the frozen-app smoke test")
    parser.add_argument("--smoke-template", default="navy")
    args = parser.parse_args()

    ver = version()
    fetch_tectonic()
    stage_templates()
    freeze(ver)
    if not args.no_smoke:
        smoke(args.smoke_template)
    print(f"Built {package(ver)}")


if __name__ == "__main__":
    main()
