"""Builds the RP1210/J2534 bridge (PandaCaptureAdapter.cs), 32-bit and 64-bit, into pandacapture/adapter_bin.

Uses the C# compiler that ships with Windows (.NET Framework 4), so nothing needs installing. Both
builds are needed because a program can only load a driver DLL of its own bitness, and most adapter
vendors ship 32-bit drivers.
"""

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "pandacapture" / "adapter_bin"


def csc() -> Path:
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for framework in ("Framework64", "Framework"):
        path = windir / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
        if path.exists():
            return path
    sys.exit("The .NET Framework 4 C# compiler (csc.exe) wasn't found. It ships with Windows 10 and 11.")


def main() -> int:
    if os.name != "nt":
        sys.exit("The adapter bridge is built on Windows: RP1210 and J2534 drivers only exist there.")
    OUT.mkdir(parents=True, exist_ok=True)
    compiler = csc()
    for arch in ("x86", "x64"):
        exe = OUT / f"pandacapture-adapter-{arch}.exe"
        subprocess.run([str(compiler), "/nologo", f"/platform:{arch}", "/target:exe", "/optimize+",
                        f"/out:{exe}", str(HERE / "PandaCaptureAdapter.cs")], check=True)
        print(f"Built {exe.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
