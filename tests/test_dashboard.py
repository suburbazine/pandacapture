import json
import time
import urllib.request

import pytest

from pandacapture import protocol as p
from pandacapture.dashboard import Dashboard, LiveState
from pandacapture.signals import MapError, builtin_maps, load_map, parse_map
from pandacapture.sources import ReplaySource, SimulatedSource


def sig(**kw):
    base = {"key": "x", "id": "0x100", "byte": 0}
    base.update(kw)
    return parse_map({"signals": [base]}).signals[0]


def test_little_endian_16bit():
    assert sig(byte=2, bits=16, scale=0.25).decode(bytes([0, 0x10, 0x80, 0x0C, 0, 0, 0, 0])) == 800


def test_big_endian_12bit_with_bit_offset():
    # Motorola: bytes 3-4 big-endian = 0x0ABC; skip 4 low bits -> 0xAB
    assert sig(byte=3, bits=8, bit=4, order="big").decode(bytes([0, 0, 0, 0x0A, 0xBC])) == 0xAB


def test_single_bit_and_signed():
    assert sig(byte=0, bit=1, bits=1).decode(bytes([0b10])) == 1
    assert sig(byte=0, bit=1, bits=1).decode(bytes([0b01])) == 0
    assert sig(byte=1, bits=8, signed=True).decode(bytes([0, 0xFE])) == -2


def test_short_frame_gives_none():
    assert sig(byte=6, bits=16).decode(bytes(7)) is None


def test_scale_offset_and_levels():
    s = sig(scale=0.75, offset=-48, display="gauge", min=-20, max=100, warn_above=50, alert_above=60)
    assert s.decode(bytes([160])) == 72
    assert (s.level(40), s.level(55), s.level(72)) == ("ok", "warn", "alert")


def test_lights():
    assert sig(display="light").light_on(1) and not sig(display="light").light_on(0)
    low = sig(display="light", on_below=11.8)
    assert low.light_on(11.5) and not low.light_on(12.5)


@pytest.mark.parametrize("bad, msg", [
    ({"key": "a", "byte": 0}, "needs 'id'"),
    ({"key": "a", "id": "0x100", "byte": 0, "display": "gauge"}, "min and max"),
    ({"key": "a", "id": "0x100", "byte": 0, "colour": "red"}, "unknown field"),
    ({"key": "a", "id": "zz", "byte": 0}, "isn't a number"),
    ({"key": "a", "id": "0x100", "byte": 0, "bits": 99}, "out of range"),
])
def test_map_errors(bad, msg):
    with pytest.raises(MapError, match=msg):
        parse_map({"signals": [bad]})


def test_duplicate_keys():
    with pytest.raises(MapError, match="duplicate"):
        parse_map({"signals": [{"key": "a", "id": 1, "byte": 0}, {"key": "a", "id": 2, "byte": 0}]})


def test_builtin_maps_load():
    assert builtin_maps()
    for name in builtin_maps():
        m = load_map(name)
        assert m.signals and all(s.to_json()["id"].startswith("0x") for s in m.signals)


def test_stinger_rpm_and_mil():
    m = load_map("kia-stinger-33t-pcan")
    state = LiveState(m)
    state.feed([p.Frame(0, 0x316, bytes([0, 0x10, 0x80, 0x0C, 0, 0, 0, 0])),
                p.Frame(0, 0x545, bytes([0b10, 0, 0, 140, 0, 0, 0, 0]))], time.monotonic())
    snap = state.snapshot()["values"]
    assert snap["rpm"]["v"] == 800 and snap["mil"]["on"] and snap["battery"]["v"] == pytest.approx(14.22, abs=0.01)


def test_high_resolution_keeps_every_sample_and_reports_loss():
    m = parse_map({"signals": [{"key": "a", "id": "0x100", "byte": 0}]})
    state = LiveState(m)
    for i in range(10):
        state.feed([p.Frame(0, 0x100, bytes([i]))], time.monotonic())
    samples, seq, lost = state.samples_after(0, 0)
    assert [v for _, v, _ in samples] == list(range(10)) and lost == 0
    assert state.samples_after(seq, 0)[0] == []
    state.samples.clear()
    state.feed([p.Frame(0, 0x100, bytes([99]))], time.monotonic())
    samples, _, lost = state.samples_after(seq - 5, 0)
    assert lost == 5 and [v for _, v, _ in samples] == [99]


def get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def test_server_end_to_end():
    dash = Dashboard(SimulatedSource, load_map("kia-stinger-33t-pcan"), port=0)
    dash.start()
    try:
        assert b"PandaCapture" in get(dash.url)
        assert json.loads(get(dash.url + "map"))["signals"][0]["key"] == "rpm"
        for _ in range(30):
            state = json.loads(get(dash.url + "state"))
            if "rpm" in state["values"]:
                break
            time.sleep(0.1)
        assert state["status"] == "live" and "rpm" in state["values"]
        # high resolution: a snapshot first, then sample batches
        with urllib.request.urlopen(dash.url + "events?mode=high", timeout=3) as r:
            seen = []
            while len(seen) < 3:
                line = r.readline().decode()
                if line.startswith("event: "):
                    seen.append(line.split()[1])
        assert seen[0] == "snapshot" and "samples" in seen
    finally:
        dash.stop()


def test_replay_source(tmp_path):
    log = tmp_path / "c.log"
    log.write_text("(10.000000) can0 316#0010800C00000000\n(10.050000) can0 316#0010000D00000000\n")
    src = ReplaySource(log, speed=10, loop=False)
    time.sleep(0.02)
    frames = src.read()
    assert [f.addr for f in frames] == [0x316, 0x316]
