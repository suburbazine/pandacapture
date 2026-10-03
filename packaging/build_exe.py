#!/usr/bin/env python3
"""Builds the one-file PandaCapture program for this OS with PyInstaller, firmware included.

Run firmware/build.py first (or put a firmware build in pandacapture/firmware_bin).
Output: dist/pandacapture(.exe)
"""

import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "pandacapture" / "firmware_bin"

missing = [t for t in ("h7", "f4") if not (FW / t / "manifest.json").exists()]
if missing:
    sys.exit(f"No {' or '.join(missing)} firmware in pandacapture/firmware_bin: run firmware/build.py first.")

sep = ";" if sys.platform == "win32" else ":"
PyInstaller.__main__.run([
    str(ROOT / "pandacapture" / "__main__.py"),
    "--name", "pandacapture",
    "--onefile",
    "--console",
    "--noconfirm",
    "--clean",
    "--paths", str(ROOT),
    "--add-data", f"{FW}{sep}pandacapture/firmware_bin",
    "--add-data", f"{ROOT / 'pandacapture' / 'maps'}{sep}pandacapture/maps",
    "--add-data", f"{ROOT / 'pandacapture' / 'web'}{sep}pandacapture/web",
    "--collect-binaries", "libusb_package",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build" / "pyinstaller"),
])
