#!/usr/bin/env python3
"""Builds the PandaCapture firmware: one build per panda family.

  h7  Red Panda (STM32H7): comma's panda firmware at a recent commit (firmware/panda, firmware/opendbc)
  f4  Black Panda (STM32F4): comma's last panda commit that still built for the F4
      (firmware/panda-f4, firmware/opendbc-f4)

Each is comma's source with firmware/patches/<target> applied, built with comma's own SCons build,
and signed with the panda project's public development key like any panda firmware built from source.

Output (default pandacapture/firmware_bin/<target>/): the signed app, the bootstub and
manifest.json, which `pandacapture flash` uses and release builds carry inside the program.

Needs: Python 3.10+ with scons and pycryptodome (pip install scons pycryptodome), git, and the
arm-none-eabi GCC toolchain:
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
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tarfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BUILD = HERE / "build"
DOCKER_IMAGE = "pandacapture-firmware"


@dataclass
class Target:
    name: str
    mcu: str
    boards: tuple     # hardware types it's for
    panda: Path
    opendbc: Path
    app: str          # build outputs, relative to the panda tree
    bootstub: str
    scons_args: tuple = ()

    @property
    def patches(self):
        return sorted((HERE / "patches" / self.name).glob("*.patch"))

    @property
    def tree(self):
        return BUILD / self.name

    def packet_versions(self):
        """(health, CAN) packet versions the firmware reports, as pandacapture checks them."""
        if self.mcu == "H7":  # hashes of the struct headers
            return (version_hash(self.tree / "board" / "health.h"),
                    version_hash(self.opendbc / "opendbc" / "safety" / "can.h"))
        health = re.search(r"#define HEALTH_PACKET_VERSION (\d+)", (self.tree / "board" / "health.h").read_text())
        can = re.search(r"#define CAN_PACKET_VERSION (\d+)", (self.tree / "board" / "can_declarations.h").read_text())
        return int(health[1]), int(can[1])


TARGETS = {
    "h7": Target("h7", "H7", (7,), HERE / "panda", HERE / "opendbc",
                 "board/obj/panda_h7.bin.signed", "board/obj/bootstub.panda_h7.bin"),
    "f4": Target("f4", "F4", (3, 1), HERE / "panda-f4", HERE / "opendbc-f4",
                 "board/obj/panda.bin.signed", "board/obj/bootstub.panda.bin", ("--minimal",)),
}


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


def export_tree(t: Target):
    """A clean copy of the pinned panda source with the target's patches applied."""
    if not (t.panda / "board").is_dir() or not (t.opendbc / "opendbc").is_dir():
        fail(f"{t.panda.name} or {t.opendbc.name} is empty. Run: git submodule update --init")
    if t.tree.exists():
        shutil.rmtree(t.tree)
    t.tree.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", "HEAD", cwd=t.panda, binary=True))) as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(t.tree, filter="data")
        else:
            tar.extractall(t.tree)
    # The tree sits inside this repository: stop git from finding it, or `git apply` would take the
    # patch paths from the repository root and silently skip them
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(t.tree.parent))
    for patch in t.patches:
        print(f"Applying {t.name}/{patch.name}")
        subprocess.run(["git", "apply", "--verbose", str(patch)], cwd=t.tree, check=True, env=env)
    if 'BUILDER = "PANDACAPTURE"' not in (t.tree / "SConscript").read_text():
        fail(f"the {t.name} patches didn't apply")


def version_hash(path):
    return int.from_bytes(hashlib.sha256(Path(path).read_bytes().replace(b"\r", b"")).digest()[:4], "little")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_target(t: Target, gcc_dir: Path, out_root: Path, ours: str):
    print(f"\n== {t.name} ({t.mcu}) ==")
    export_tree(t)
    panda_commit = git("rev-parse", "HEAD", cwd=t.panda)
    env = dict(os.environ)
    env["PATH"] = str(gcc_dir) + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = str(t.opendbc) + os.pathsep + env.get("PYTHONPATH", "")
    env["PANDACAPTURE_GIT"] = f"{ours}-{panda_commit[:8]}"
    jobs = str(max(1, (os.cpu_count() or 2) - 1))
    subprocess.run([sys.executable, "-m", "SCons", "-C", str(t.tree), "-j", jobs, *t.scons_args, t.app, t.bootstub],
                   env=env, check=True)

    out = out_root / t.name
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for kind, rel in (("app", t.app), ("bootstub", t.bootstub)):
        src = t.tree / rel
        shutil.copyfile(src, out / src.name)
        files[kind] = {"file": src.name, "sha256": sha256(src)}
    health, can = t.packet_versions()
    manifest = {
        "version": (t.tree / "board" / "obj" / "version").read_text().strip(),
        "target": t.name,
        "mcu": t.mcu,
        "hw_types": list(t.boards),
        "built_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "pandacapture_commit": ours,
        "panda_commit": panda_commit,
        "opendbc_commit": git("rev-parse", "HEAD", cwd=t.opendbc),
        "patches": {p.name: sha256(p) for p in t.patches},
        "health_packet_version": health,
        "can_packet_version": can,
        "files": files,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Built {manifest['version']} for {t.mcu}")
    for f in files.values():
        print(f"  {out / f['file']}  sha256 {f['sha256']}")


def build(args):
    for module, pip_name in (("SCons", "scons"), ("Crypto", "pycryptodome")):
        try:
            __import__(module)
        except ImportError:
            fail(f"{pip_name} isn't installed for this Python: pip install {pip_name}")
    gcc_dir = find_gcc(args.toolchain)
    if gcc_dir is None:
        fail("arm-none-eabi-gcc not found. Install the Arm GNU Toolchain (see the top of firmware/build.py) "
             "and pass --toolchain, or build with --docker.")
    print(f"Toolchain: {gcc_dir}")
    try:
        ours = git("rev-parse", "--short=7", "HEAD")
        if git("status", "--porcelain", "--untracked-files=no"):
            ours += "+"
    except subprocess.CalledProcessError:
        ours = "nogit"
    out_root = Path(args.out) if args.out else ROOT / "pandacapture" / "firmware_bin"
    for name in (TARGETS if args.target == "all" else [args.target]):
        build_target(TARGETS[name], gcc_dir, out_root, ours)


def docker_build(args):
    if not shutil.which("docker"):
        fail("docker not found")
    subprocess.run(["docker", "build", "-t", DOCKER_IMAGE, str(HERE)], check=True)
    out = Path(args.out).resolve() if args.out else ROOT / "pandacapture" / "firmware_bin"
    try:
        rel_out = out.relative_to(ROOT).as_posix()
    except ValueError:
        fail("with --docker, --out must be inside the repository")
    subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/src", "-w", "/src", DOCKER_IMAGE,
                    "python3", "firmware/build.py", "--out", rel_out, "--target", args.target], check=True)


def main():
    ap = argparse.ArgumentParser(description="Build the PandaCapture firmware.")
    ap.add_argument("--target", choices=["all", *TARGETS], default="all", help="h7 (Red Panda), f4 (Black Panda), or all")
    ap.add_argument("--toolchain", help="folder holding arm-none-eabi-gcc (or its parent)")
    ap.add_argument("--out", help="output folder (default pandacapture/firmware_bin); each target gets a subfolder")
    ap.add_argument("--docker", action="store_true", help="build inside Docker (firmware/Dockerfile)")
    args = ap.parse_args()
    try:
        docker_build(args) if args.docker else build(args)
    except subprocess.CalledProcessError as e:
        fail(f"{' '.join(map(str, e.cmd))} failed ({e.returncode})")


if __name__ == "__main__":
    main()
