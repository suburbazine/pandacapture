#!/usr/bin/env python3
"""Builds the one-file FrostCapture program for this OS with PyInstaller, firmware included.

Run firmware/build.py first (or put a firmware build in frostcapture/firmware_bin).
Output: dist/frostcapture(.exe)
"""

import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "frostcapture" / "firmware_bin"

if not (FW / "manifest.json").exists():
    sys.exit("No firmware in frostcapture/firmware_bin: run firmware/build.py first.")

sep = ";" if sys.platform == "win32" else ":"
PyInstaller.__main__.run([
    str(ROOT / "frostcapture" / "__main__.py"),
    "--name", "frostcapture",
    "--onefile",
    "--console",
    "--noconfirm",
    "--clean",
    "--paths", str(ROOT),
    "--add-data", f"{FW}{sep}frostcapture/firmware_bin",
    "--collect-binaries", "libusb_package",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build" / "pyinstaller"),
])
