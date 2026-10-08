"""The dashboard's Firmware page: each panda connected and what it runs, against the PandaCapture firmware this
program carries, and flashing, backing up and restoring it, one job at a time in the background. The same steps
as pandacapture flash, backup and restore (flasher.py): the first install replaces comma's bootstub through the
STM32 bootloader (DFU) after saving the whole flash; updates go through PandaCapture's bootstub.

Flashing and restoring are typed out (FLASH, RESTORE) and only from the computer running PandaCapture
(dashboard.py checks that). While a job runs, the dashboard stops reading the panda, and picks it up again after.
"""

import json
import threading
from pathlib import Path

from . import protocol as p
from .backup import default_dir as default_backup_dir
from .dfu import list_dfu
from .firmware import Firmware, FirmwareError
from .flasher import FlashError, flash, make_backup, open_panda, restore
from .panda import list_pandas
from .usbdev import UsbError

CONFIRM = {"flash": "FLASH", "restore": "RESTORE"}


def bundled() -> dict:
    """MCU name -> the firmware version this program carries for it."""
    out = {}
    for mcu in (p.MCU_H7, p.MCU_F4):
        try:
            out[mcu.name] = Firmware.load(mcu).version
        except FirmwareError:
            pass
    return out


def describe(serial, kind, hw, version, carried) -> dict:
    """What the page says about one panda: its hardware, firmware, and what flashing would do."""
    mcu = p.MCU_BY_HW.get(hw)
    e = {"serial": serial, "kind": kind, "hardware": p.HW_NAMES.get(hw, f"unknown hardware 0x{hw:02X}"),
         "mcu": mcu.name if mcu else None, "firmware": version, "pandacapture": p.is_pandacapture_version(version),
         "bundled": carried.get(mcu.name) if mcu else None}
    if hw in (p.HW_TRES, p.HW_CUATRO, p.HW_BODY, p.HW_UNO, p.HW_DOS):
        e["state"], e["why"] = "unsupported", "the panda inside a comma device: PandaCapture firmware is for the Red and Black Panda"
    elif hw == p.HW_WHITE_PANDA:
        e["state"], e["why"] = "unsupported", "a White Panda: untried with PandaCapture (pandacapture flash --force flashes it anyway)"
    elif mcu is None or e["bundled"] is None:
        e["state"], e["why"] = "unsupported", "no PandaCapture firmware for this hardware in this program"
    elif kind == "bootstub":
        e["state"], e["why"] = "install", "in its bootstub, no firmware running"
    elif not e["pandacapture"]:
        e["state"], e["why"] = "install", "not PandaCapture firmware: the first install replaces comma's bootstub (through DFU)"
    elif version == e["bundled"]:
        e["state"], e["why"] = "current", "PandaCapture firmware, the version this program carries"
    else:
        e["state"], e["why"] = "update", f"PandaCapture firmware {version}; this program carries {e['bundled']}"
    return e


def devices(held=None) -> list:
    """Each panda (running firmware or in its bootstub) and STM32 bootloader connected. held: the one the
    dashboard is reading, as {serial, hw, version} from when it opened it (it can't be opened twice)."""
    carried = bundled()
    out = []
    for d in list_pandas():
        if d.note:
            out.append({"serial": d.serial, "kind": d.kind, "state": "unknown", "why": d.note})
            continue
        if held and d.serial == held["serial"]:
            out.append(dict(describe(d.serial, d.kind, held["hw"], held["version"], carried), reading=True))
            continue
        try:
            with open_panda(d.serial, timeout=1.0) as pd:
                hw = pd.hw_type()
                version = "bootstub (no firmware running)" if pd.bootstub else pd.version()
            out.append(describe(d.serial, d.kind, hw, version, carried))
        except UsbError as e:
            out.append({"serial": d.serial, "kind": d.kind, "state": "unknown", "why": f"can't open it: {e}"})
    for d in list_dfu():
        out.append({"serial": d.serial, "kind": "dfu", "state": "install" if not d.note else "unknown",
                    "why": d.note or "waiting in the STM32 bootloader (DFU): a flash can carry on from here"})
    return out


def backups(folder=None) -> list:
    """The whole-flash backups saved so far, newest first."""
    folder = Path(folder) if folder else default_backup_dir()
    out = []
    for b in sorted(folder.glob("panda-*.bin"), reverse=True) if folder.is_dir() else []:
        try:
            meta = json.loads(b.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"name": b.name, "serial": meta.get("serial"), "mcu": meta.get("mcu"),
                    "firmware": meta.get("firmware"), "created": meta.get("created"), "size": b.stat().st_size})
    return out


class FlashJob:
    """One flash, backup or restore at a time, in the background, with its log for the page. hold() and
    release() stop and restart the dashboard's reading of the panda around it."""

    def __init__(self, hold=lambda why: None, release=lambda: None, backup_dir=None, recording=lambda: False):
        self.hold, self.release, self.recording = hold, release, recording
        self.backup_dir = Path(backup_dir) if backup_dir else default_backup_dir()
        self.lock = threading.Lock()
        self.state = {"state": "idle"}

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def start(self, action, serial=None, backup=None, recover=False, skip_backup=False, confirm=""):
        if action not in ("flash", "backup", "restore"):
            raise ValueError(f"unknown firmware action {action!r}")
        word = CONFIRM.get(action)
        if word and (confirm or "").strip().upper() != word:
            raise ValueError(f"Type {word} to {action} the panda.")
        if self.recording():
            raise ValueError("A recording is running: stop it first (the panda is out of reach while it's flashed).")
        path = None
        if action == "restore":
            path = self.backup_dir / Path(backup or "").name
            if not backup or not path.is_file():
                raise ValueError("Pick a backup from the list.")
        with self.lock:
            if self.state.get("state") == "running":
                raise ValueError("A firmware job is already running.")
            self.state = {"state": "running", "action": action, "serial": serial, "log": []}
        threading.Thread(target=self._work, args=(action, serial, path, recover, skip_backup), daemon=True,
                         name="pandacapture-firmware").start()

    def _log(self, text):
        with self.lock:
            self.state["log"].append(text)

    def _work(self, action, serial, path, recover, skip_backup):
        def load_firmware(mcu):
            try:
                return Firmware.load(mcu)
            except FirmwareError as e:
                raise FlashError(str(e)) from None
        try:
            self._log("The dashboard stops reading the panda while this runs.")
            self.hold("the Firmware page is using the panda")
            if action == "flash":
                version = flash(load_firmware, serial=serial, recover=True if recover else None, log=self._log,
                                backup_dir=None if skip_backup else self.backup_dir)
                result = f"Done: the panda runs {version}."
            elif action == "backup":
                saved = make_backup(self.backup_dir, serial=serial, log=self._log)
                result = f"Done: saved {saved.name}."
            else:
                runs = restore(path, serial=serial, log=self._log)
                result = f"Done: the panda runs {runs}."
            with self.lock:
                self.state.update(state="done", result=result)
        except (FlashError, UsbError, FirmwareError, OSError) as e:
            with self.lock:
                self.state.update(state="error", error=str(e))
        except Exception as e:  # noqa: BLE001 - the page says what went wrong rather than spinning forever
            with self.lock:
                self.state.update(state="error", error=f"{type(e).__name__}: {e}")
        finally:
            self.release()
            self._log("The dashboard reads the panda again.")
