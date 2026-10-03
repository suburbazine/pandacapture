#!/usr/bin/env python3
"""Builds the FrostCapture firmware for the Red Panda.

comma's panda firmware (firmware/panda, pinned) with firmware/patches applied, built with comma's
own SCons build against the pinned opendbc (firmware/opendbc). The app is signed with the panda
project's public development key, like any panda firmware built from source.

Output (default frostcapture/firmware_bin/): panda_h7.bin.signed, bootstub.panda_h7.bin and
manifest.json, which `frostcapture flash` uses and release builds carry inside the program.

Needs: Python 3.10+ with scons (pip install scons), git, and the arm-none-eabi GCC toolchain:
  Linux / macOS: pip install comma-deps-gcc-arm-none-eabi  (comma's build of it), or your package manager
  Windows:       the Arm GNU Toolchain (arm-none-eabi), with --toolchain pointing at its bin folder
  Anywhere with Docker: python firmware/build.py --docker
"""

import argparse
import datetime as dt
import glob
import hashlib
import io
import json
import os
import shutil
import site
import subprocess
import sys
import sysconfig
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PANDA = HERE / "panda"
OPENDBC = HERE / "opendbc"
PATCHES = sorted((HERE / "patches").glob("*.patch"))
BUILD = HERE / "build"
TREE = BUILD / "panda"
TARGETS = ("board/obj/panda_h7.bin.signed", "board/obj/bootstub.panda_h7.bin")
DOCKER_IMAGE = "frostcapture-firmware"


def fail(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def git(*args, cwd=ROOT, binary=False):
    out = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True)
    return out.stdout if binary else out.stdout.decode().strip()


def find_gcc(toolchain):
    name = "arm-none-eabi-gcc" + (".exe" if os.name == "nt" else "")
    places = []
    if toolchain:
        places += [Path(toolchain), Path(toolchain) / "bin"]
    if os.getenv("ARM_TOOLCHAIN"):
        places += [Path(os.environ["ARM_TOOLCHAIN"]), Path(os.environ["ARM_TOOLCHAIN"]) / "bin"]
    places += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    places.append(Path(sysconfig.get_path("scripts")))
    # comma's pip package of the toolchain
    for sp in site.getsitepackages() + [site.getusersitepackages()]:
        places += [Path(p) for p in glob.glob(os.path.join(sp, "comma_deps_gcc_arm_none_eabi*", "**", "bin"), recursive=True)]
    for place in places:
        if (place / name).is_file():
            return place.resolve()
    return None


def export_tree():
    """A clean copy of the pinned panda source with the FrostCapture patches applied."""
    if not (PANDA / "board").is_dir() or not (OPENDBC / "opendbc").is_dir():
        fail("firmware/panda or firmware/opendbc is empty. Run: git submodule update --init")
    if TREE.exists():
        shutil.rmtree(TREE)
    TREE.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD", cwd=PANDA, binary=True))) as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(TREE, filter="data")
        else:
            tar.extractall(TREE)
    # The tree sits inside this repository: stop git from finding it, or `git apply` would take the
    # patch paths from the repository root and silently skip them
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(TREE.parent))
    for patch in PATCHES:
        print(f"Applying {patch.name}")
        subprocess.run(["git", "apply", "--verbose", str(patch)], cwd=TREE, check=True, env=env)
    if 'BUILDER = "FROSTCAPTURE"' not in (TREE / "SConscript").read_text():
        fail("the FrostCapture patches didn't apply")


def version_hash(path):
    return int.from_bytes(hashlib.sha256(Path(path).read_bytes().replace(b"\r", b"")).digest()[:4], "little")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build(args):
    try:
        import SCons  # noqa: F401
    except ImportError:
        fail("SCons isn't installed for this Python: pip install scons")
    gcc_dir = find_gcc(args.toolchain)
    if gcc_dir is None:
        fail("arm-none-eabi-gcc not found. Install the Arm GNU Toolchain (see the top of firmware/build.py) "
             "and pass --toolchain, or build with --docker.")
    print(f"Toolchain: {gcc_dir}")

    export_tree()
    try:
        ours = git("rev-parse", "--short=7", "HEAD")
        if git("status", "--porcelain", "--untracked-files=no"):
            ours += "+"
    except subprocess.CalledProcessError:
        ours = "nogit"
    panda_commit = git("rev-parse", "HEAD", cwd=PANDA)
    opendbc_commit = git("rev-parse", "HEAD", cwd=OPENDBC)

    env = dict(os.environ)
    env["PATH"] = str(gcc_dir) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(OPENDBC) + os.pathsep + env.get("PYTHONPATH", "")
    env["FROSTCAPTURE_GIT"] = f"{ours}-{panda_commit[:8]}"
    jobs = str(max(1, (os.cpu_count() or 2) - 1))
    subprocess.run([sys.executable, "-m", "SCons", "-C", str(TREE), "-j", jobs, *TARGETS], env=env, check=True)

    out = Path(args.out) if args.out else ROOT / "frostcapture" / "firmware_bin"
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for target in TARGETS:
        src = TREE / target
        shutil.copyfile(src, out / src.name)
        files[src.name] = sha256(src)
    manifest = {
        "version": (TREE / "board" / "obj" / "version").read_text().strip(),
        "built_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "frostcapture_commit": ours,
        "panda_commit": panda_commit,
        "opendbc_commit": opendbc_commit,
        "patches": {p.name: sha256(p) for p in PATCHES},
        "health_packet_version": version_hash(TREE / "board" / "health.h"),
        "can_packet_version": version_hash(OPENDBC / "opendbc" / "safety" / "can.h"),
        "sha256": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"\nBuilt {manifest['version']}")
    for name, digest in files.items():
        print(f"  {out / name}  sha256 {digest}")


def docker_build(args):
    if not shutil.which("docker"):
        fail("docker not found")
    subprocess.run(["docker", "build", "-t", DOCKER_IMAGE, str(HERE)], check=True)
    out = Path(args.out).resolve() if args.out else ROOT / "frostcapture" / "firmware_bin"
    try:
        rel_out = out.relative_to(ROOT).as_posix()
    except ValueError:
        fail("with --docker, --out must be inside the repository")
    subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/src", "-w", "/src", DOCKER_IMAGE,
                    "python3", "firmware/build.py", "--out", rel_out], check=True)


def main():
    ap = argparse.ArgumentParser(description="Build the FrostCapture Red Panda firmware.")
    ap.add_argument("--toolchain", help="folder holding arm-none-eabi-gcc (or its parent)")
    ap.add_argument("--out", help="output folder (default frostcapture/firmware_bin)")
    ap.add_argument("--docker", action="store_true", help="build inside Docker (firmware/Dockerfile)")
    args = ap.parse_args()
    try:
        docker_build(args) if args.docker else build(args)
    except subprocess.CalledProcessError as e:
        fail(f"{' '.join(map(str, e.cmd))} failed ({e.returncode})")


if __name__ == "__main__":
    main()
