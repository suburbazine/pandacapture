import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from frostcapture import protocol as p
from frostcapture.firmware import APP_FILE, BOOTSTUB_FILE, MANIFEST_FILE, Firmware, FirmwareError

ROOT = Path(__file__).resolve().parent.parent
PATCHES = sorted((ROOT / "firmware" / "patches").glob("*.patch"))
PANDA = ROOT / "firmware" / "panda"


def write_fw(folder, app, bootstub, version="FROSTCAPTURE-a-b-DEBUG", tamper=False):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / APP_FILE).write_bytes(app)
    (folder / BOOTSTUB_FILE).write_bytes(bootstub)
    sha = {APP_FILE: hashlib.sha256(app).hexdigest(), BOOTSTUB_FILE: hashlib.sha256(bootstub).hexdigest()}
    if tamper:
        sha[APP_FILE] = "0" * 64
    (folder / MANIFEST_FILE).write_text(json.dumps({"version": version, "sha256": sha,
                                                    "health_packet_version": 1, "can_packet_version": 2}))


def test_load(tmp_path):
    write_fw(tmp_path, b"A" * 200000, b"B" * 1000)
    fw = Firmware.load(tmp_path)
    assert fw.app_sectors() == range(1, 3)
    assert fw.signature == b"A" * 128


def test_checksum_mismatch(tmp_path):
    write_fw(tmp_path, b"A" * 1000, b"B" * 10, tamper=True)
    with pytest.raises(FirmwareError, match="checksum"):
        Firmware.load(tmp_path)


def test_rejects_other_builds(tmp_path):
    write_fw(tmp_path, b"A" * 1000, b"B" * 10, version="DEV-1234-DEBUG")
    with pytest.raises(FirmwareError, match="FrostCapture"):
        Firmware.load(tmp_path)


def test_missing(tmp_path):
    with pytest.raises(FirmwareError, match="missing"):
        Firmware.load(tmp_path)


def test_patch_constants_match_host():
    gate = (ROOT / "firmware" / "patches" / "0001-transmit-gate.patch").read_text()
    assert int(re.search(r"#define FROSTCAPTURE_TX_ARM (0x[0-9A-F]+)U", gate)[1], 16) == p.FROSTCAPTURE_TX_ARM
    # bit 0 of the ALLOUTPUT param is comma's bus-forwarding switch: the arm code must leave it off
    assert p.FROSTCAPTURE_TX_ARM & 1 == 0
    build = (ROOT / "firmware" / "patches" / "0002-build.patch").read_text()
    assert f'+BUILDER = "{p.FROSTCAPTURE_BUILDER}"' in build


@pytest.mark.skipif(not (PANDA / "board").exists() or not shutil.which("git"), reason="needs git and firmware/panda")
def test_patches_apply_to_pinned_panda(tmp_path):
    tree = tmp_path / "panda"
    archive = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", str(PANDA), "archive", "--format=tar", "HEAD"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(tree, filter="data")
    for patch in PATCHES:
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=tree, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=tree, check=True)
