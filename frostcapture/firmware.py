"""The Red Panda firmware FrostCapture flashes: built by firmware/build.py, bundled into releases."""

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import protocol as p

APP_FILE = "panda_h7.bin.signed"
BOOTSTUB_FILE = "bootstub.panda_h7.bin"
MANIFEST_FILE = "manifest.json"


def bundled_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "frostcapture" / "firmware_bin"


class FirmwareError(Exception):
    pass


@dataclass
class Firmware:
    app: bytes
    bootstub: bytes
    manifest: dict
    folder: Path

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
        """App sectors to erase: enough 128 KiB sectors from sector 1 to hold the app."""
        last = 1 + (len(self.app) - 1) // p.SECTOR_SIZE
        if last not in p.APP_SECTORS:
            raise FirmwareError(f"app is {len(self.app)} bytes: too big for the app sectors")
        return range(1, last + 1)

    @classmethod
    def load(cls, folder=None) -> "Firmware":
        folder = Path(folder) if folder else bundled_dir()
        try:
            manifest = json.loads((folder / MANIFEST_FILE).read_text(encoding="utf-8"))
            app = (folder / APP_FILE).read_bytes()
            bootstub = (folder / BOOTSTUB_FILE).read_bytes()
        except FileNotFoundError as e:
            where = "bundled with this FrostCapture" if folder == bundled_dir() else f"in {folder}"
            raise FirmwareError(f"No firmware {where} ({Path(e.filename).name} missing). Build it with "
                                "firmware/build.py, or use a release of FrostCapture.") from None
        except (OSError, ValueError) as e:
            raise FirmwareError(f"Can't read the firmware in {folder}: {e}") from None
        for name, blob in ((APP_FILE, app), (BOOTSTUB_FILE, bootstub)):
            want = manifest.get("sha256", {}).get(name)
            if want != hashlib.sha256(blob).hexdigest():
                raise FirmwareError(f"{name} doesn't match its manifest checksum: rebuild the firmware.")
        if not p.is_frostcapture_version(manifest.get("version", "")):
            raise FirmwareError(f"{folder} doesn't hold a FrostCapture firmware build.")
        fw = cls(app, bootstub, manifest, folder)
        fw.app_sectors()
        if len(bootstub) > p.SECTOR_SIZE:
            raise FirmwareError("bootstub is bigger than sector 0")
        return fw


def expected_packet_versions():
    """(health, CAN) packet versions of the bundled firmware, or None without one."""
    try:
        return Firmware.load().packet_versions
    except FirmwareError:
        return None
