"""The Firmware page: what it says about each panda, the typed words, the dashboard letting go of the panda around a
job, and a first install, an update, a backup and a restore through the page's job against the simulated panda."""

import json
import time
import urllib.error
import urllib.request

import pytest

from pandacapture import flashjob
from pandacapture import protocol as p
from pandacapture.flashjob import FlashJob, backups, describe
from tests.test_flasher import OURS, SERIAL, firmware, world  # noqa: F401 - world is a fixture

CARRIED = {"STM32H7": OURS, "STM32F4": OURS}


def test_what_it_says_about_a_panda():
    assert describe(SERIAL, "panda", p.HW_RED_PANDA, "v1.2.3-RELEASE", CARRIED)["state"] == "install"
    assert describe(SERIAL, "panda", p.HW_RED_PANDA, OURS, CARRIED)["state"] == "current"
    up = describe(SERIAL, "panda", p.HW_RED_PANDA, "PANDACAPTURE-old-DEBUG", CARRIED)
    assert up["state"] == "update" and OURS in up["why"]
    assert describe(SERIAL, "bootstub", p.HW_RED_PANDA, "bootstub (no firmware running)", CARRIED)["state"] == "install"
    assert describe(SERIAL, "panda", p.HW_GREY_PANDA, OURS, CARRIED)["mcu"] == "STM32F4"
    assert describe(SERIAL, "panda", p.HW_TRES, "x", CARRIED)["state"] == "unsupported"
    assert describe(SERIAL, "panda", p.HW_WHITE_PANDA, "x", CARRIED)["state"] == "unsupported"
    assert describe(SERIAL, "panda", p.HW_RED_PANDA, "x", {})["state"] == "unsupported"   # nothing carried


@pytest.fixture
def job(world, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(flashjob.Firmware, "load", classmethod(lambda cls, mcu, folder=None: firmware(mcu)))
    held = []
    j = FlashJob(lambda why: held.append(("hold", why)), lambda: held.append(("release",)), backup_dir=tmp_path,
                 recording=lambda: False)
    j.held = held
    return j


def wait(j):
    end = time.monotonic() + 5
    while j.snapshot()["state"] == "running":
        assert time.monotonic() < end
        time.sleep(0.01)
    return j.snapshot()


def test_the_typed_words(job):
    with pytest.raises(ValueError, match="Type FLASH"):
        job.start("flash", confirm="flash it")
    with pytest.raises(ValueError, match="Type RESTORE"):
        job.start("restore", backup="x.bin", confirm="")
    with pytest.raises(ValueError, match="Pick a backup"):
        job.start("restore", backup="nope.bin", confirm="RESTORE")
    job.recording = lambda: True
    with pytest.raises(ValueError, match="recording is running"):
        job.start("backup")
    assert job.snapshot()["state"] == "idle" and not job.held


def test_a_first_install_then_an_update_through_the_page(job, world):  # noqa: F811
    job.start("flash", confirm=" flash ")
    s = wait(job)
    assert s["state"] == "done" and s["result"] == f"Done: the panda runs {OURS}."
    assert job.held == [("hold", "the Firmware page is using the panda"), ("release",)]   # let go, then picked up
    assert any("Backing up the whole flash" in line for line in s["log"])
    assert backups(job.backup_dir) and backups(job.backup_dir)[0]["firmware"] == "v1.2.3-RELEASE"
    # Now PandaCapture's: an update goes through its bootstub, no DFU
    world.version, world.events = "PANDACAPTURE-old-DEBUG", []
    job.start("flash", confirm="FLASH")
    assert wait(job)["state"] == "done" and not any(e.startswith("dfu") for e in world.events)


def test_a_backup_and_a_restore_through_the_page(job, world):  # noqa: F811
    job.start("backup")
    assert wait(job)["state"] == "done"
    name = backups(job.backup_dir)[0]["name"]
    world.events = []
    job.start("restore", backup=name, confirm="RESTORE")
    s = wait(job)
    assert s["state"] == "done" and "dfu-program" in world.events


def test_a_failure_is_said_and_the_panda_released(job, world):  # noqa: F811
    world.hw = p.HW_TRES
    job.start("flash", confirm="FLASH")
    s = wait(job)
    assert s["state"] == "error" and "comma device" in s["error"] and job.held[-1] == ("release",)


def test_the_dashboard_lets_go_of_the_panda(tmp_path):
    from pandacapture.dashboard import Dashboard
    from pandacapture.signals import load_map
    from pandacapture.sources import SimulatedSource
    opened = []

    def open_source():
        opened.append(1)
        return SimulatedSource()
    dash = Dashboard(open_source, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    dash.start()
    try:
        end = time.monotonic() + 5
        while dash.state.info()["status"] != "live":
            assert time.monotonic() < end
            time.sleep(0.02)
        dash.reader.hold("testing")
        assert dash.state.info()["status"] == "paused" and dash.state.info()["error"] == "testing"
        frames = dash.state.frames
        time.sleep(0.3)
        assert dash.state.frames == frames                           # not reading meanwhile
        dash.reader.release()
        while dash.state.info()["status"] != "live":
            assert time.monotonic() < end + 5
            time.sleep(0.02)
        assert len(opened) == 2                                      # opened again after

        get = lambda path: urllib.request.urlopen(dash.url + path, timeout=10).read()
        assert b"Type <b>FLASH</b>" in get("firmware.html") and b'id="firmwareLink"' in get("")
        info = json.loads(get("firmware"))
        assert info["job"] == {"state": "idle"} and info["local"] and "backups" in info and "bundled" in info
        req = urllib.request.Request(dash.url + "firmware", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"action": "flash", "confirm": "nope"}).encode())
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert "Type FLASH" in e.value.read().decode()
    finally:
        dash.stop()
