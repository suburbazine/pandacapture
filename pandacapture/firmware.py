"""The firmware PandaCapture flashes: built by firmware/build.py, one folder per panda family
(firmware_bin/h7 for the Red Panda, firmware_bin/f4 for the Black Panda), bundled into releases."""

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import protocol as p

MANIFEST_FILE = "manifest.json"


def bundled_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "pandacapture" / "firmware_bin"


class FirmwareError(Exception):
    pass


@dataclass
class Firmware:
    app: bytes
    bootstub: bytes
    manifest: dict
    folder: Path
    mcu: p.Mcu

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def signature(self) -> bytes:
        return self.app[-p.SIGNATURE_LEN:]

    @property
    def packet_versions(self):
        return self.manifest["health_packet_version"], self.manifest["can_packet_version"]

    def app_sectors(self) -> range:
        """App sectors to erase: enough sectors from sector 1 to hold the app."""
        try:
            return self.mcu.sectors_for(len(self.app))
        except ValueError as e:
            raise FirmwareError(str(e)) from None

    @classmethod
    def load(cls, mcu: p.Mcu = p.MCU_H7, folder=None) -> "Firmware":
        """The build for [mcu]: from [folder] (which may hold the target's subfolder), or bundled."""
        folder = Path(folder) if folder else bundled_dir()
        if (folder / mcu.target / MANIFEST_FILE).exists():
            folder = folder / mcu.target
        try:
            manifest = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
            files = manifest["files"]
            app = (folder / files["app"]["file"]).read_bytes()
            bootstub = (folder / files["bootstub"]["file"]).read_bytes()
        except FileNotFoundError as e:
            where = "bundled with this PandaCapture" if folder.parent == bundled_dir() or folder == bundled_dir() \
                else f"in {folder}"
            raise FirmwareError(f"No {mcu.name} firmware {where} ({Path(e.filename).name} missing). Build it with "
                                "firmware/build.py, or use a release of PandaCapture.") from None
        except (OSError, ValueError, KeyError) as e:
            raise FirmwareError(f"Can't read the firmware in {folder}: {e}") from None
        if manifest.get("mcu") != mcu.name.removeprefix("STM32"):
            raise FirmwareError(f"{folder} holds firmware for the {manifest.get('mcu')}, not the {mcu.name}.")
        for kind, blob in (("app", app), ("bootstub", bootstub)):
            if files[kind].get("sha256") != hashlib.sha256(blob).hexdigest():
                raise FirmwareError(f"{files[kind]['file']} doesn't match its manifest checksum: rebuild the firmware.")
        if not p.is_pandacapture_version(manifest.get("version", "")):
            raise FirmwareError(f"{folder} doesn't hold a PandaCapture firmware build.")
        fw = cls(app, bootstub, manifest, folder, mcu)
        fw.app_sectors()
        if len(bootstub) > mcu.sector_sizes[0]:
            raise FirmwareError("bootstub is bigger than sector 0")
        return fw


def bundled_versions() -> dict:
    """Target name -> bundled firmware version, for the builds that are there."""
    out = {}
    for mcu in (p.MCU_H7, p.MCU_F4):
        try:
            out[mcu.target] = Firmware.load(mcu).version
        except FirmwareError:
            pass
    return out


def expected_packet_versions(mcu: p.Mcu = p.MCU_H7):
    """(health, CAN) packet versions of the bundled firmware for [mcu], or None without one."""
    try:
        return Firmware.load(mcu).packet_versions
    except FirmwareError:
        return None
