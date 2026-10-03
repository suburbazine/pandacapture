"""The flash workflow against a simulated panda: app, bootstub and STM32 bootloader states."""

import json
from pathlib import Path

import pytest

from pandacapture import backup, flasher
from pandacapture import protocol as p
from pandacapture.firmware import Firmware
from pandacapture.panda import DeviceEntry
from pandacapture.usbdev import UsbError

SERIAL = "1d0032000f51333231373438"
OURS = "PANDACAPTURE-abc-def-DEBUG"


def firmware(mcu=p.MCU_H7):
    app = bytes(range(256)) * 600 + b"S" * p.SIGNATURE_LEN  # 150 KiB: two H7 sectors, five F4 ones
    return Firmware(app=app, bootstub=b"B" * 4096, manifest={"version": OURS}, folder=Path("."), mcu=mcu)


def load(mcu):
    return firmware(mcu)


class World:
    """One panda. bootstub_kind 'comma' only starts comma-signed apps; 'ours' starts PandaCapture's."""

    def __init__(self, state="app", version="v1.2.3-RELEASE", bootstub_kind="comma", hw=p.HW_RED_PANDA):
        self.state = state  # "app", "bootstub" or "dfu"
        self.version = version
        self.signature = b"old"
        self.bootstub_kind = bootstub_kind
        self.hw = hw
        self.events = []
        self.flashed = None
        self.chunk = None
        self.read_protected = False
        self.programmed = None
        self.original_flash = b"stock bootstub and firmware"

    @property
    def mcu(self):
        return p.MCU_F4 if self.hw in p.HW_F4 else p.MCU_H7

    def boot(self):
        """What a reset into the firmware does: the bootstub checks the app it holds."""
        if self.flashed is None:
            self.state = "app"
        elif self.bootstub_kind == "ours":
            self.state, self.version, self.signature = "app", OURS, self.flashed[-p.SIGNATURE_LEN:]
        else:
            self.state = "bootstub"


class FakePanda:
    world = None

    def __init__(self):
        self.bootstub = self.world.state == "bootstub"
        self.serial = SERIAL

    @classmethod
    def open(cls, serial=None, bootstub_ok=True):
        assert cls.world.state in ("app", "bootstub"), f"opened a panda in state {cls.world.state}"
        return cls()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def close(self):
        pass

    def hw_type(self):
        return self.world.hw

    def version(self):
        assert not self.bootstub
        return self.world.version

    def signature(self):
        return self.world.signature

    def reset(self, into="firmware"):
        self.world.events.append(f"reset:{into}")
        if into == "bootstub":
            self.world.state = "bootstub"
        elif into == "bootloader":
            assert self.world.state == "bootstub", "only the bootstub can enter DFU on release firmware"
            self.world.state = "dfu"
        else:
            self.world.boot()

    def flasher_present(self):
        return self.bootstub

    def flash_unlock(self):
        self.world.events.append("unlock")

    def flash_erase(self, sector):
        self.world.events.append(f"erase:{sector}")

    def flash_write(self, data, chunk):
        self.world.chunk = chunk
        self.world.events.append("write")
        self.world.flashed = data


class FakeDfu:
    world = None

    @classmethod
    def open(cls, serial=None):
        assert cls.world.state == "dfu"
        assert serial == p.dfu_serial(SERIAL, cls.world.mcu)
        return cls()

    @property
    def mcu(self):
        return self.world.mcu

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def clear_status(self):
        pass

    def erase_sector(self, s):
        self.world.events.append(f"dfu-erase:{s}")
        if s == 1:
            self.world.flashed = None

    def program(self, address, data, progress=None):
        assert address == p.FLASH_BASE
        self.world.events.append("dfu-program")
        self.world.bootstub_kind = "ours"
        self.world.programmed = data

    def read(self, address, length, progress=None):
        assert address == p.FLASH_BASE
        if self.world.read_protected:
            raise UsbError("read-protected")
        self.world.events.append("dfu-read")
        return self.world.original_flash[:length].ljust(length, b"\xFF")

    serial = property(lambda self: p.dfu_serial(SERIAL, self.world.mcu))

    def jump(self, address):
        self.world.events.append("dfu-jump")
        erased = any(e.startswith("dfu-erase") for e in self.world.events)
        # app sector erased: the bootstub stays in its flasher; otherwise the panda starts as before
        self.world.state = "bootstub" if erased else "app"


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def world(monkeypatch):
    w = World()
    FakePanda.world = FakeDfu.world = w
    monkeypatch.setattr(flasher, "Panda", FakePanda)
    monkeypatch.setattr(flasher, "StDfu", FakeDfu)
    monkeypatch.setattr(flasher, "time", Clock())
    monkeypatch.setattr(flasher, "list_pandas", lambda: [] if w.state == "dfu" else
                        [DeviceEntry("bootstub" if w.state == "bootstub" else "panda", SERIAL)])
    monkeypatch.setattr(flasher, "list_dfu", lambda: [DeviceEntry("dfu", p.dfu_serial(SERIAL, w.mcu))] if w.state == "dfu" else [])
    return w


def test_first_install_goes_through_dfu(world):
    version = flasher.flash(load, log=lambda s: None)
    assert version == OURS
    assert world.events == ["reset:bootstub", "reset:bootloader", "dfu-erase:0", "dfu-erase:1", "dfu-program",
                            "dfu-jump", "unlock", "erase:1", "erase:2", "write", "reset:firmware"]


def test_update_uses_bootstub_only(world):
    world.version, world.bootstub_kind = "PANDACAPTURE-old-DEBUG", "ours"
    assert flasher.flash(load, log=lambda s: None) == OURS
    assert not any(e.startswith("dfu") for e in world.events)


def test_comma_bootstub_refuses_without_recover(world):
    with pytest.raises(flasher.FlashError, match="--recover"):
        flasher.flash(load, recover=False, log=lambda s: None)


def test_panda_left_in_bootstub_is_recovered(world):
    world.state = "bootstub"
    assert flasher.flash(load, log=lambda s: None) == OURS
    assert "dfu-program" in world.events


def test_refuses_comma_device_panda(world):
    world.hw = p.HW_TRES
    with pytest.raises(flasher.FlashError, match="inside a comma device"):
        flasher.flash(load, log=lambda s: None)
    assert world.events == []


def test_cancel_changes_nothing(world):
    with pytest.raises(flasher.FlashError, match="Cancelled"):
        flasher.flash(load, confirm=lambda: False, log=lambda s: None)
    assert world.events == []


def test_continues_from_dfu(world):
    world.state = "dfu"  # e.g. stopped there for a missing Windows driver
    assert flasher.flash(load, log=lambda s: None) == OURS
    assert world.events[:4] == ["dfu-erase:0", "dfu-erase:1", "dfu-program", "dfu-jump"]


def test_black_panda_first_install(world):
    world.hw = p.HW_BLACK_PANDA
    assert flasher.flash(load, log=lambda s: None) == OURS
    erases = [e for e in world.events if e.startswith("dfu-erase")]
    assert erases == [f"dfu-erase:{i}" for i in range(16)]  # comma's F4 recovery erases every sector
    assert [e for e in world.events if e.startswith("erase:")] == [f"erase:{i}" for i in range(1, 6)]
    assert world.chunk == 0x10


def test_white_panda_needs_force(world):
    world.hw = p.HW_WHITE_PANDA
    with pytest.raises(flasher.FlashError, match="--force"):
        flasher.flash(load, log=lambda s: None)
    assert flasher.flash(load, force=True, log=lambda s: None) == OURS


@pytest.mark.parametrize("hw", [p.HW_UNO, p.HW_DOS, 0x42])
def test_refuses_unsupported(world, hw):
    world.hw = hw
    with pytest.raises(flasher.FlashError):
        flasher.flash(load, force=True, log=lambda s: None)
    assert world.events == []


def test_grey_panda_takes_the_f4_build(world):
    world.hw = p.HW_GREY_PANDA  # e.g. oneclone's mini blackpanda detects as grey
    assert flasher.flash(load, log=lambda s: None) == OURS
    assert world.chunk == 0x10


def test_backup_before_anything_is_erased(world, tmp_path):
    world.hw = p.HW_GREY_PANDA
    flasher.flash(load, log=lambda s: None, backup_dir=tmp_path)
    assert world.events.index("dfu-read") < world.events.index("dfu-erase:0")
    (bin_file,) = tmp_path.glob("*.bin")
    data, meta, mcu = backup.load(bin_file)
    assert data.startswith(b"stock bootstub and firmware") and len(data) == backup.flash_size(p.MCU_F4)
    assert meta["serial"] == SERIAL and meta["firmware"] == "v1.2.3-RELEASE" and mcu is p.MCU_F4


def test_failed_backup_erases_nothing(world, tmp_path):
    world.read_protected = True
    with pytest.raises(flasher.FlashError, match="nothing was erased"):
        flasher.flash(load, log=lambda s: None, backup_dir=tmp_path)
    assert not any(e.startswith("dfu-erase") for e in world.events)


def test_restore_round_trip(world, tmp_path):
    world.hw = p.HW_GREY_PANDA
    flasher.flash(load, log=lambda s: None, backup_dir=tmp_path)
    (bin_file,) = tmp_path.glob("*.bin")
    world.events.clear()
    flasher.restore(bin_file, log=lambda s: None)
    assert world.events[:2] == ["reset:bootstub", "reset:bootloader"]
    assert [e for e in world.events if e.startswith("dfu-erase")] == [f"dfu-erase:{i}" for i in range(16)]
    assert world.programmed == b"stock bootstub and firmware"


def test_restore_refuses_another_pandas_backup(world, tmp_path):
    flasher.flash(load, log=lambda s: None, backup_dir=tmp_path)
    (bin_file,) = tmp_path.glob("*.bin")
    meta = json.loads(bin_file.with_suffix(".json").read_text())
    meta["serial"] = "someotherpanda"
    bin_file.with_suffix(".json").write_text(json.dumps(meta))
    with pytest.raises(flasher.FlashError, match="someotherpanda"):
        flasher.restore(bin_file, log=lambda s: None)


def test_backup_command_writes_nothing(world, tmp_path):
    world.hw = p.HW_GREY_PANDA
    path = flasher.make_backup(tmp_path, log=lambda s: None)
    assert path.exists() and world.state == "app" and world.version == "v1.2.3-RELEASE"
    assert not any(e.startswith(("dfu-erase", "dfu-program", "erase", "write")) for e in world.events)


def test_backup_resumes_from_dfu(world, tmp_path):
    world.hw, world.state = p.HW_GREY_PANDA, "dfu"
    path = flasher.make_backup(tmp_path, log=lambda s: None)
    assert path.exists() and world.state == "app"
    assert not any(e.startswith(("dfu-erase", "dfu-program")) for e in world.events)
