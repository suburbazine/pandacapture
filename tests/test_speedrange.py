"""Captures' speed range tags: the labels, the line a recording ends with (the same one PandaCapture Android
writes), read back from a log's end, read from a whole older log (the slower axle, so wheelspin isn't speed),
and shown in the capture lists, read in the background and cached."""

import json
import time
import urllib.request
from pathlib import Path

import pytest

from pandacapture import protocol as p
from pandacapture.capture import RollingLog
from pandacapture.signals import AddressMap, load_map
from pandacapture.speedrange import CACHE_NAME, SpeedTag, SpeedTags, SpeedTracker

from tests.test_runs import SIG, drive, put

MAP = load_map("kia-stinger-33t-pcan")
ANDROID_BOOST = Path(__file__).resolve().parents[2] / "PandaCapture Android" / "captures" / "panda-20261005-234311_boost.log"


def frames_at(fl, fr, rl, rr, speed=None):
    f = {}
    for key, v in (("wheel_fl", fl), ("wheel_fr", fr), ("wheel_rl", rl), ("wheel_rr", rr)):
        put(f, key, v)
    if speed is not None:
        put(f, "speed_kmh", speed)
    return [p.Frame(0, can_id, bytes(d)) for can_id, d in f.items()]


def test_labels_and_the_note():
    assert SpeedTag(0.0, 154.5).label() == "0–96 mph"
    assert SpeedTag(64.4, 154.5).label() == "40–96 mph"
    assert SpeedTag(0.0, 1.2).label() == "Stationary"
    assert SpeedTag(0.0, 0.0, heard=False).label() == "No speed"
    assert SpeedTag(0.0, 102.25).note() == "speed range: 0.0-102.2 km/h"
    for tag in (SpeedTag(0.0, 154.53), SpeedTag(0.0, 0.0, heard=False)):
        back = SpeedTag.parse("# " + tag.note())
        assert back.heard == tag.heard and back.max_kmh == pytest.approx(tag.max_kmh, abs=0.05)
    assert SpeedTag.parse("# marker 1 (1791000000.000000)") is None


def test_the_slower_axle_and_misreads():
    t = SpeedTracker(MAP)
    assert t.available and t.use_wheels
    for f in frames_at(50, 50, 80, 80, speed=80):      # rear wheelspin: the fronts are the car's speed
        t.feed(f)
    for f in frames_at(60, 60, 61, 61):
        t.feed(f)
    assert (t.min, t.max) == (pytest.approx(50, abs=0.1), pytest.approx(60, abs=0.1))
    t._see(450.0)                                      # a misread
    assert t.max == pytest.approx(60, abs=0.1)
    # Only the ECU's speed: used when the map lacks the four wheels
    ecu = AddressMap(name="t", signals=[SIG["speed_kmh"]])
    t = SpeedTracker(ecu)
    assert t.available and not t.use_wheels
    t.feed(next(f for f in frames_at(0, 0, 0, 0, speed=33) if f.addr == SIG["speed_kmh"].can_id))
    assert t.tag().max_kmh == pytest.approx(33, abs=0.1)
    assert not SpeedTracker(AddressMap(name="none", signals=[SIG["rpm"]])).available
    assert SpeedTracker(MAP).tag().label() == "No speed"


def test_a_recording_ends_with_its_tag_before_the_summary(tmp_path):
    w = RollingLog(tmp_path, ["# test"], split_mb=0, stamp="20261006-120000", speed_map=MAP)
    for v in (0, 30, 64.4, 20):
        for f in frames_at(v, v, v, v):
            w.write(f"(1791000000.000000) can0 {f.addr:03X}#{f.data.hex().upper()}\n")
            w.track(f)
    for i in range(3000):                              # a long summary table after the note
        w.write(f"# can0 0x{i:03X} filler line for the per-id table, as long as a real one\n")
    w.speed_note()
    w.write("# 4 frames\n" * 3000)
    w.close()
    tag = SpeedTag.from_tail(w.path)
    assert tag.heard and tag.max_kmh == pytest.approx(64.4, abs=0.05) and tag.label() == "0–40 mph"
    assert "# speed range: 0.0-64.4 km/h" in w.path.read_text()
    old = tmp_path / "old.log"
    old.write_text("(1791000000.000000) can0 316#00\n")
    assert SpeedTag.from_tail(old) is None
    # A map without speed signals: no line at all
    w = RollingLog(tmp_path, ["# test"], split_mb=0, speed_map=AddressMap(name="none", signals=[SIG["rpm"]]))
    w.speed_note()
    w.close()
    assert "speed range" not in w.path.read_text()


def test_each_part_of_a_long_recording_has_its_own(tmp_path):
    w = RollingLog(tmp_path, ["# test"], split_mb=0.001, stamp="20261006-120000", speed_map=MAP)
    for v in (10, 20):
        for f in frames_at(v, v, v, v):
            w.write(f"(1791000000.000000) can0 {f.addr:03X}#{f.data.hex().upper()}\n")
            w.track(f)
    w.write("#" + "x" * 2000 + "\n")
    assert w.maybe_rotate()
    for f in frames_at(90, 90, 90, 90):
        w.write(f"(1791000001.000000) can0 {f.addr:03X}#{f.data.hex().upper()}\n")
        w.track(f)
    w.speed_note()
    w.close()
    first, second = (SpeedTag.from_tail(x) for x in w.paths)
    assert (first.min_kmh, first.max_kmh) == (pytest.approx(10, abs=0.1), pytest.approx(20, abs=0.1))
    assert (second.min_kmh, second.max_kmh) == (pytest.approx(90, abs=0.1),) * 2
    text = w.paths[0].read_text()
    assert text.index("# speed range") < text.index("# continued in")


def test_an_older_log_is_read_whole(tmp_path):
    tag = SpeedTag.scan(drive(tmp_path / "a.log"), MAP)
    assert tag.heard and tag.min_kmh == 0 and tag.max_kmh == pytest.approx(130, abs=0.1)
    assert tag.label() == "0–81 mph"


@pytest.mark.skipif(not ANDROID_BOOST.is_file(), reason="capture not here")
def test_the_same_as_android_on_a_real_capture():
    tag = SpeedTag.scan(ANDROID_BOOST, MAP)
    assert tag.note() == "speed range: 0.0-102.2 km/h" and tag.label() == "0–64 mph"


def test_tags_for_a_folder_are_read_in_the_background_and_cached(tmp_path):
    log = drive(tmp_path / "capture-20261006-120000.log")
    tags = SpeedTags(tmp_path)
    tag, pending = tags.get(log, MAP)
    assert tag is None and pending
    for _ in range(200):
        tag, pending = tags.get(log, MAP)
        if tag:
            break
        time.sleep(0.05)
    assert tag.label() == "0–81 mph" and not pending
    cached = json.loads((tmp_path / CACHE_NAME).read_text())
    assert cached[log.name] == {"bytes": log.stat().st_size, "min": 0.0, "max": tag.max_kmh, "heard": True}
    tag, pending = SpeedTags(tmp_path).get(log, MAP)        # read from the cache, not the log
    assert tag.label() == "0–81 mph" and not pending
    # No speed signals in the map: no tag, nothing to wait for
    assert SpeedTags(tmp_path / "x").get(log, AddressMap(name="none", signals=[SIG["rpm"]])) == (None, False)


def test_the_capture_lists_and_a_dashboard_recording(tmp_path):
    from pandacapture.dashboard import Dashboard
    from pandacapture.sources import SimulatedSource
    drive(tmp_path / "capture-20261006-120000.log")
    dash = Dashboard(SimulatedSource, MAP, port=0, record_dir=tmp_path)
    dash.start()
    try:
        get = lambda path: urllib.request.urlopen(dash.url + path, timeout=10).read()
        for _ in range(200):
            c = json.loads(get("captures"))["captures"][0]
            if c["speed"]:
                break
            time.sleep(0.05)
        assert c["speed"]["label"] == "0–81 mph" and not c["speed_pending"]
        assert "function speedTag" in get("captures.html").decode() and "function speedTag" in get("runs.html").decode()
        # A recording made on the dashboard ends with its tag (the simulated traffic has no wheel speeds)
        path = dash.reader.start_recording()
        time.sleep(0.3)
        dash.reader.stop_recording()
        assert SpeedTag.from_tail(path).label() == "No speed"
    finally:
        dash.stop()
