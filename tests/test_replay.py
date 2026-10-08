"""The replay timeline: index, play at the recorded pace, pause (the clock stops), seek (with the lead-up), stop,
looping, zips; and on the dashboard: the replay's clock holds values while paused, live frames are ignored,
stop goes back to the live source. The same cases as PandaCapture Android's ReplayPlayerTest."""

import json
import threading
import time
import urllib.request
import zipfile

import pytest

from pandacapture import replay as R
from pandacapture.logs import replay_file


def log(path, seconds):
    """`seconds` of a log, one 0x316 frame every 10 ms whose first byte counts tenths of a second."""
    lines = ["# PandaCapture candump log"]
    n = int(round(seconds * 100))
    for i in range(n + 1):
        t = i / 100
        lines.append(f"({1791000000.0 + t:.6f}) can0 316#{int(t * 10 + 1e-9) & 0xFF:02X}00000000000000")
        if i % 50 == 0:
            lines.append("# marker x")
    path.write_text("\n".join(lines) + "\n")
    return path


class Run:
    def __init__(self, path, **kw):
        self.frames, self.states, self.rewinds = [], [], 0
        self.lock = threading.Lock()

        def on_frames(fs):
            with self.lock:
                self.frames.extend(fs)

        def on_rewind():
            self.rewinds += 1
        self.player = R.ReplayPlayer(path, on_frames, on_rewind, self.states.append, **kw)

    def wait(self, what, timeout=5.0):
        end = time.monotonic() + timeout
        while not what():
            assert time.monotonic() < end, "timed out"
            time.sleep(0.005)


def test_plays_at_the_recorded_pace_to_the_end(tmp_path):
    r = Run(log(tmp_path / "a.log", 0.6))
    t0 = time.monotonic()
    r.player.start()
    r.wait(lambda: r.player.phase == R.ENDED)
    took = time.monotonic() - t0
    assert r.player.duration == pytest.approx(0.6, abs=0.011)
    assert 0.5 <= took <= 2.5, took
    assert len(r.frames) == 61
    r.player.stop()
    r.wait(lambda: r.player.phase == R.STOPPED)


def test_pause_stops_the_clock_and_play_goes_on(tmp_path):
    r = Run(log(tmp_path / "a.log", 3.0))
    r.player.start()
    r.wait(lambda: r.player.position > 0.3)
    r.player.pause()
    r.wait(lambda: r.player.phase == R.PAUSED)
    at, n = r.player.clock(), len(r.frames)
    time.sleep(0.3)
    assert r.player.clock() == at            # the gauges' clock holds, so values don't go stale
    assert len(r.frames) == n                # nothing sent while paused
    r.player.play()
    r.wait(lambda: r.player.position > at - R.CLOCK_BASE + 0.2)
    assert len(r.frames) > n
    r.player.stop()


def test_a_seek_lands_with_the_lead_up_and_a_rewind_forgets(tmp_path):
    r = Run(log(tmp_path / "a.log", 10.0))
    r.player.start()
    r.wait(lambda: r.player.phase == R.PLAYING)
    r.player.pause()
    r.wait(lambda: r.player.phase == R.PAUSED)
    with r.lock:
        r.frames.clear()
    r.player.seek(8.0)
    # Paused, yet the 2 s before 8.0 arrive at once, ending at the moment it landed on
    r.wait(lambda: len(r.frames) >= 190)
    r.wait(lambda: r.player.phase == R.PAUSED and r.player.position == pytest.approx(8.0, abs=0.05))
    time.sleep(0.1)
    assert r.frames[-1].data[0] in (79, 80)
    assert r.rewinds == 0
    r.player.seek(1.0)
    r.wait(lambda: r.rewinds == 1)
    r.wait(lambda: abs(r.player.position - 1.0) < 0.05)
    r.player.stop()
    r.wait(lambda: r.player.phase == R.STOPPED)


def test_play_at_the_end_starts_over(tmp_path):
    r = Run(log(tmp_path / "a.log", 0.3))
    r.player.start()
    r.wait(lambda: r.player.phase == R.ENDED)
    with r.lock:
        r.frames.clear()
    r.player.play()
    r.wait(lambda: r.player.phase == R.ENDED and len(r.frames) >= 31)
    assert r.rewinds == 1
    r.player.stop()


def test_looping_and_speed(tmp_path):
    r = Run(log(tmp_path / "a.log", 0.4), loop=True, speed=4.0)
    r.player.start()
    r.wait(lambda: r.rewinds >= 2)            # 0.4 s at 4x: around the log several times a second
    assert r.player.phase == R.PLAYING
    r.player.stop()
    r.wait(lambda: r.player.phase == R.STOPPED)


def test_a_file_with_no_frames_says_so(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_text("# nothing here\n")
    r = Run(empty)
    r.player.start()
    r.wait(lambda: r.player.phase == R.FAILED)
    assert r.states[-1]["message"].startswith("Nothing to replay")


def test_zips_are_played_from_their_log(tmp_path):
    z = tmp_path / "bundle.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("pandacapture-bundle-x/capture.log", log(tmp_path / "c.log", 0.2).read_text())
        f.writestr("pandacapture-bundle-x/tools.py", "")
    path = replay_file(z)
    assert path != z and path.read_text().count("316#") == 21
    assert replay_file(tmp_path / "c.log") == tmp_path / "c.log"


# ---------------------------------------------------------------- on the dashboard

def test_on_the_dashboard(tmp_path):
    from pandacapture.dashboard import Dashboard
    from pandacapture.signals import parse_map
    from pandacapture.sources import SimulatedSource
    amap = parse_map({"name": "t", "signals": [{"key": "tenths", "id": "0x316", "byte": 0}]})
    log(tmp_path / "capture-20261007-120000.log", 30.0)
    dash = Dashboard(SimulatedSource, amap, port=0, record_dir=tmp_path)
    dash.start()
    get = lambda path: json.loads(urllib.request.urlopen(dash.url + path, timeout=10).read())

    def post(body):
        req = urllib.request.Request(dash.url + "replay", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps(body).encode())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def wait(what, timeout=5.0):
        end = time.monotonic() + timeout
        while not what():
            assert time.monotonic() < end, "timed out"
            time.sleep(0.02)
    try:
        # Live first: the simulator's RPM frames on 0x316 decode as something else
        wait(lambda: "tenths" in dash.state.snapshot()["values"])
        st = post({"capture": "capture-20261007-120000.log"})
        assert st["replay"]["name"] == "capture-20261007-120000.log"
        wait(lambda: (dash.state.info()["replay"] or {}).get("phase") == "playing")
        post({"action": "pause"})
        post({"action": "seek", "s": 12.0})
        wait(lambda: dash.state.snapshot()["values"].get("tenths", {}).get("v") in (119, 120))
        time.sleep(2.0)                                   # paused past the stale limit: still fresh, live ignored
        snap = dash.state.snapshot()
        assert snap["values"]["tenths"]["v"] in (119, 120) and not snap["values"]["tenths"]["stale"]
        assert snap["replay"]["phase"] == "paused" and snap["replay"]["position"] == pytest.approx(12.0, abs=0.05)
        epoch = snap["replay"]["epoch"]
        post({"action": "seek", "s": 3.0})                # back: what was shown belongs to later
        wait(lambda: dash.state.info()["replay"]["epoch"] > epoch)
        post({"action": "stop"})
        wait(lambda: dash.state.info()["replay"] is None)
        wait(lambda: dash.state.snapshot()["values"].get("tenths", {}).get("v") not in (None, 29, 30, 119, 120))
        assert get("replay")["replay"] is None
        with pytest.raises(urllib.error.HTTPError):
            post({"capture": "nope.log"})
    finally:
        dash.stop()
