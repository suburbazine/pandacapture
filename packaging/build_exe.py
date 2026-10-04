#!/usr/bin/env python3
"""Builds the one-file PandaCapture program for this OS with PyInstaller, firmware included.

Run firmware/build.py first (or put a firmware build in pandacapture/firmware_bin), and on Windows
adapter/build.py (the RP1210/J2534 bridge).
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

ADAPTER = ROOT / "pandacapture" / "adapter_bin"
extra = []
if sys.platform == "win32":
    if not all((ADAPTER / f"pandacapture-adapter-{a}.exe").exists() for a in ("x86", "x64")):
        sys.exit("No RP1210/J2534 bridge in pandacapture/adapter_bin: run adapter/build.py first.")
    extra = ["--add-data", f"{ADAPTER};pandacapture/adapter_bin"]

sep = ";" if sys.platform == "win32" else ":"
PyInstaller.__main__.run([
    str(ROOT / "pandacapture" / "__main__.py"),
    "--name", "pandacapture",
    "--onefile",
    "--console",
    "--noconfirm",
    "--icon", str(ROOT / "packaging" / "pandacapture.ico"),
    "--clean",
    "--paths", str(ROOT),
    "--add-data", f"{FW}{sep}pandacapture/firmware_bin",
    "--add-data", f"{ROOT / 'pandacapture' / 'maps'}{sep}pandacapture/maps",
    "--add-data", f"{ROOT / 'pandacapture' / 'web'}{sep}pandacapture/web",
    *extra,
    "--collect-binaries", "libusb_package",
    # pandacapture analyze: the Anthropic SDK, and keyring's credential-store backends (found by entry point)
    "--collect-submodules", "anthropic",
    "--collect-submodules", "keyring",
    "--copy-metadata", "keyring",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build" / "pyinstaller"),
])
