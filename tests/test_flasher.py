"""The flash workflow against a simulated panda: app, bootstub and STM32 bootloader states."""

from pathlib import Path

import pytest

from frostcapture import flasher
from frostcapture import protocol as p
from frostcapture.firmware import Firmware
from frostcapture.panda import DeviceEntry

SERIAL = "1d0032000f51333231373438"
OURS = "FROSTCAPTURE-abc-def-DEBUG"


def firmware():
    app = bytes(range(256)) * 600 + b"S" * p.SIGNATURE_LEN  # 150 KiB: two app sectors
    return Firmware(app=app, bootstub=b"B" * 4096, manifest={"version": OURS}, folder=Path("."))


class World:
    """One panda. bootstub_kind 'comma' only starts comma-signed apps; 'ours' starts FrostCapture's."""

    def __init__(self, state="app", version="v1.2.3-RELEASE", bootstub_kind="comma", hw=p.HW_RED_PANDA):
        self.state = state  # "app", "bootstub" or "dfu"
        self.version = version
        self.signature = b"old"
        self.bootstub_kind = bootstub_kind
        self.hw = hw
        self.events = []
        self.flashed = None

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

    def flash_write(self, data):
        self.world.events.append("write")
        self.world.flashed = data


class FakeDfu:
    world = None

    @classmethod
    def open(cls, serial=None):
        assert cls.world.state == "dfu"
        assert serial == p.dfu_serial(SERIAL)
        return cls()

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

    def program(self, address, data):
        assert address == p.FLASH_BASE
        self.world.events.append("dfu-program")
        self.world.bootstub_kind = "ours"

    def jump(self, address):
        self.world.events.append("dfu-jump")
        self.world.state = "bootstub"  # app sector erased: the bootstub stays in its flasher


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
    monkeypatch.setattr(flasher, "list_dfu", lambda: [DeviceEntry("dfu", p.dfu_serial(SERIAL))] if w.state == "dfu" else [])
    return w


def test_first_install_goes_through_dfu(world):
    version = flasher.flash(firmware(), log=lambda s: None)
    assert version == OURS
    assert world.events == ["reset:bootstub", "reset:bootloader", "dfu-erase:0", "dfu-erase:1", "dfu-program",
                            "dfu-jump", "unlock", "erase:1", "erase:2", "write", "reset:firmware"]


def test_update_uses_bootstub_only(world):
    world.version, world.bootstub_kind = "FROSTCAPTURE-old-DEBUG", "ours"
    assert flasher.flash(firmware(), log=lambda s: None) == OURS
    assert not any(e.startswith("dfu") for e in world.events)


def test_comma_bootstub_refuses_without_recover(world):
    with pytest.raises(flasher.FlashError, match="--recover"):
        flasher.flash(firmware(), recover=False, log=lambda s: None)


def test_panda_left_in_bootstub_is_recovered(world):
    world.state = "bootstub"
    assert flasher.flash(firmware(), log=lambda s: None) == OURS
    assert "dfu-program" in world.events


def test_refuses_comma_device_panda(world):
    world.hw = p.HW_TRES
    with pytest.raises(flasher.FlashError, match="not a Red Panda"):
        flasher.flash(firmware(), log=lambda s: None)
    assert world.events == []


def test_cancel_changes_nothing(world):
    with pytest.raises(flasher.FlashError, match="Cancelled"):
        flasher.flash(firmware(), confirm=lambda: False, log=lambda s: None)
    assert world.events == []


def test_continues_from_dfu(world):
    world.state = "dfu"  # e.g. stopped there for a missing Windows driver
    assert flasher.flash(firmware(), log=lambda s: None) == OURS
    assert world.events[:4] == ["dfu-erase:0", "dfu-erase:1", "dfu-program", "dfu-jump"]


@pytest.mark.parametrize("force", [False, True])
def test_refuses_f4_pandas_even_forced(world, force):
    world.hw = p.HW_BLACK_PANDA
    with pytest.raises(flasher.FlashError, match="STM32F4"):
        flasher.flash(firmware(), force=force, log=lambda s: None)
    assert world.events == []
