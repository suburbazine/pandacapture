"""Whole-flash backups through the STM32 bootloader (DFU), and the way back.

Before PandaCapture first writes a panda's bootstub, it reads the panda's entire flash back:
bootstub, firmware and settings, byte for byte. Firmware PandaCapture can't rebuild, such as a
clone maker's or a fork's, can then be put back exactly with `pandacapture restore`.
"""

import datetime as dt
import hashlib
import json
from pathlib import Path

from . import protocol as p
from .capture import default_out_dir


class BackupError(Exception):
    pass


def default_dir() -> Path:
    return default_out_dir().parent / "backups"


def flash_size(mcu: p.Mcu) -> int:
    return sum(mcu.sector_sizes)


def _progress(log, what):
    shown = [-1]

    def report(done, total):
        tenth = done * 10 // total
        if tenth != shown[0]:
            shown[0] = tenth
            log(f"{what}: {done * 100 // total}%")
    return report


def save(dfu, folder, serial, firmware, log) -> Path:
    """Reads the whole flash from an open StDfu and saves it with a JSON description."""
    mcu = dfu.mcu
    size = flash_size(mcu)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = folder / f"panda-{serial or dfu.serial}-{mcu.target}-{stamp}.bin"
    log(f"Backing up the whole flash ({size // 1024} KiB) to {path}")
    data = dfu.read(p.FLASH_BASE, size, _progress(log, "Backup"))
    path.write_bytes(data)
    meta = {
        "serial": serial,
        "dfu_serial": dfu.serial,
        "mcu": mcu.name,
        "firmware": firmware,
        "address": p.FLASH_BASE,
        "size": size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "created": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return path


def load(path):
    """(image, description, mcu) of a backup, checked against its description."""
    path = Path(path)
    try:
        data = path.read_bytes()
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BackupError(f"Can't read the backup {path} and its .json: {e}") from None
    if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
        raise BackupError(f"{path.name} doesn't match the checksum in its .json: it's damaged or not a backup.")
    mcu = {m.name: m for m in (p.MCU_H7, p.MCU_F4)}.get(meta.get("mcu"))
    if mcu is None or len(data) != flash_size(mcu) or meta.get("address") != p.FLASH_BASE:
        raise BackupError(f"{path.name} isn't a whole-flash backup PandaCapture made.")
    return data, meta, mcu


def restore_sectors(mcu: p.Mcu) -> range:
    """Sectors a restore rewrites. The H7's last sector holds the panda's provisioning and is never
    erased, the same rule comma's library follows."""
    return range(0, 7) if mcu is p.MCU_H7 else range(len(mcu.sector_sizes))


def write(dfu, data: bytes, log):
    """Erases the restorable sectors and writes the backup's image back."""
    mcu = dfu.mcu
    sectors = restore_sectors(mcu)
    end = mcu.sector_address(sectors.stop) - p.FLASH_BASE
    image = data[:end].rstrip(b"\xFF")
    dfu.clear_status()
    log(f"Erasing flash sectors {sectors.start}-{sectors.stop - 1}")
    for s in sectors:
        dfu.erase_sector(s)
    log(f"Writing the backup ({len(image) // 1024} KiB)")
    dfu.program(p.FLASH_BASE, image, _progress(log, "Restore"))
