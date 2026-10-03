import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from pandacapture import protocol as p
from pandacapture.firmware import MANIFEST_FILE, Firmware, FirmwareError

ROOT = Path(__file__).resolve().parent.parent
TREES = {"h7": ROOT / "firmware" / "panda", "f4": ROOT / "firmware" / "panda-f4"}


def write_fw(folder, app, bootstub, mcu="H7", version="PANDACAPTURE-a-b-DEBUG", tamper=False):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "app.bin").write_bytes(app)
    (folder / "bootstub.bin").write_bytes(bootstub)
    files = {"app": {"file": "app.bin", "sha256": hashlib.sha256(app).hexdigest()},
             "bootstub": {"file": "bootstub.bin", "sha256": hashlib.sha256(bootstub).hexdigest()}}
    if tamper:
        files["app"]["sha256"] = "0" * 64
    (folder / MANIFEST_FILE).write_text(json.dumps({"version": version, "mcu": mcu, "files": files,
                                                    "health_packet_version": 1, "can_packet_version": 2}))


def test_load_h7(tmp_path):
    write_fw(tmp_path / "h7", b"A" * 200000, b"B" * 1000)
    fw = Firmware.load(p.MCU_H7, tmp_path)
    assert fw.app_sectors() == range(1, 3)
    assert fw.signature == b"A" * 128


def test_load_f4_sectors(tmp_path):
    # F4 sectors 1-3 are 16 KiB and sector 4 is 64 KiB: 56 KiB needs sectors 1-4
    write_fw(tmp_path / "f4", b"A" * 56 * 1024, b"B" * 13000, mcu="F4")
    assert Firmware.load(p.MCU_F4, tmp_path).app_sectors() == range(1, 5)


def test_wrong_chip(tmp_path):
    write_fw(tmp_path, b"A" * 1000, b"B" * 10, mcu="F4")
    with pytest.raises(FirmwareError, match="F4"):
        Firmware.load(p.MCU_H7, tmp_path)


def test_f4_bootstub_must_fit_sector_0(tmp_path):
    write_fw(tmp_path, b"A" * 1000, b"B" * 0x5000, mcu="F4")
    with pytest.raises(FirmwareError, match="sector 0"):
        Firmware.load(p.MCU_F4, tmp_path)


def test_checksum_mismatch(tmp_path):
    write_fw(tmp_path, b"A" * 1000, b"B" * 10, tamper=True)
    with pytest.raises(FirmwareError, match="checksum"):
        Firmware.load(p.MCU_H7, tmp_path)


def test_rejects_other_builds(tmp_path):
    write_fw(tmp_path, b"A" * 1000, b"B" * 10, version="DEV-1234-DEBUG")
    with pytest.raises(FirmwareError, match="PandaCapture"):
        Firmware.load(p.MCU_H7, tmp_path)


def test_missing(tmp_path):
    with pytest.raises(FirmwareError, match="missing"):
        Firmware.load(p.MCU_H7, tmp_path)


@pytest.mark.parametrize("target", TREES)
def test_patch_constants_match_host(target):
    gate = (ROOT / "firmware" / "patches" / target / "0001-transmit-gate.patch").read_text()
    assert int(re.search(r"#define PANDACAPTURE_TX_ARM (0x[0-9A-F]+)U", gate)[1], 16) == p.PANDACAPTURE_TX_ARM
    # bit 0 of the ALLOUTPUT param is comma's bus-forwarding switch: the arm code must leave it off
    assert p.PANDACAPTURE_TX_ARM & 1 == 0
    build = (ROOT / "firmware" / "patches" / target / "0002-build.patch").read_text()
    assert f'+BUILDER = "{p.PANDACAPTURE_BUILDER}"' in build


@pytest.mark.parametrize("target", TREES)
def test_patches_apply_to_pinned_panda(tmp_path, target):
    panda = TREES[target]
    if not (panda / "board").exists() or not shutil.which("git"):
        pytest.skip("needs git and the firmware submodules")
    tree = tmp_path / "panda"
    archive = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(panda), "archive", "--format=tar", "HEAD"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tree, filter="data")
    for patch in sorted((ROOT / "firmware" / "patches" / target).glob("*.patch")):
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=tree, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=tree, check=True)
    # No C string literal broken across lines by a patch
    for line in (tree / "board" / "main.c").read_text().splitlines():
        assert line.count('"') % 2 == 0, line
