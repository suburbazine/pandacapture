"""The diagnostic policy: what can be built, what links accept, and the engine-off state checks."""

import random
import types

import pytest

from pandacapture import policy as pol
from pandacapture import protocol as p
from pandacapture.policy import (EngineNotOff, PolicyRefused, Request, Sender, Service, Tier, VehicleState,
                                 build, recognise)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def obd_answer(pid, *data, module=0x7E8):
    return p.Frame(0, module, bytes([2 + len(data), 0x41, pid, *data]) + bytes(5 - len(data)))


def rpm(value):
    raw = int(value * 4)
    return obd_answer(0x0C, raw >> 8, raw & 0xFF)


def speed(kmh):
    return obd_answer(0x0D, kmh)


GEAR = types.SimpleNamespace(can_id=0x112, bus=None, labels={"0": "P", "5": "D"},
                             decode=lambda data: data[0], text=lambda v: {0: "P", 5: "D"}.get(v, str(v)))


def gear(value):
    return p.Frame(0, 0x112, bytes([value, 0, 0, 0, 0, 0, 0, 0]))


# ---- the table ----

def test_only_mode_01_is_in_the_table_today():
    built = set()
    for sid in range(256):
        for params in (b"", b"\x0c", b"\x01\x02", b"\x01\x02\x03"):
            try:
                build(sid, params)
                built.add((sid, len(params)))
            except PolicyRefused:
                pass
    assert built == {(0x01, 1)}


def test_never_services_say_why():
    for sid in (0x27, 0x34, 0x35, 0x36, 0x37):
        with pytest.raises(PolicyRefused, match="never sent"):
            build(sid, b"\x01")
    with pytest.raises(PolicyRefused, match="isn't in"):
        build(0x04)            # clearing codes: planned, not in the table yet
    with pytest.raises(PolicyRefused, match="one PID"):
        build(0x01, b"\x0c\x0d")


def test_targets():
    assert build(0x01, b"\x0c").target == 0x7DF
    assert build(0x01, b"\x0c", target=0x7E0).frame(2).addr == 0x7E0
    for bad in (0x7E8, 0x123, 0x18DB33F1):
        with pytest.raises(PolicyRefused, match="request address"):
            build(0x01, b"\x0c", target=bad)


def test_frame_layout_and_recognise():
    f = build(0x01, b"\x0d").frame(1)
    assert (f.bus, f.addr, f.data) == (1, 0x7DF, bytes([2, 1, 0x0D, 0, 0, 0, 0, 0]))
    assert recognise(f).payload == b"\x01\x0d"


def test_links_refuse_everything_else():
    rnd = random.Random(7)
    for _ in range(5000):
        data = bytes(rnd.randrange(256) for _ in range(8))
        f = p.Frame(0, rnd.choice([0x7DF, 0x7E0, 0x316, 0x7E8]), data)
        try:
            r = recognise(f)
        except PolicyRefused:
            continue
        assert r.payload[0] == 0x01 and len(r.payload) == 2 and f.addr in (0x7DF, 0x7E0)
    for refused in (p.Frame(0, 0x7DF, bytes([0x10, 0x14, 0x2E, 0xF1, 0x90, 1, 2, 3])),    # a multi-frame write
                    p.Frame(0, 0x7E0, bytes([2, 0x11, 0x01, 0, 0, 0, 0, 0])),              # ECU reset
                    p.Frame(0, 0x7E0, bytes([2, 0x10, 0x02, 0, 0, 0, 0, 0])),              # programming session
                    p.Frame(0, 0x18DB33F1, bytes([2, 1, 0x0C, 0, 0, 0, 0, 0]), extended=True)):
        with pytest.raises(PolicyRefused):
            recognise(refused)


# ---- vehicle state ----

def state(**kw):
    clock = Clock()
    return VehicleState(gear_signal=kw.get("gear_signal"), clock=clock), clock


def test_engine_off_needs_all_of_it():
    s, clock = state(gear_signal=GEAR)
    with pytest.raises(EngineNotOff, match="Engine speed isn't known"):
        s.check_engine_off()
    s.frame(rpm(0))
    with pytest.raises(EngineNotOff, match="Vehicle speed isn't known"):
        s.check_engine_off()
    s.frame(speed(3))
    with pytest.raises(EngineNotOff, match="moving"):
        s.check_engine_off()
    s.frame(speed(0))
    s.frame(gear(5))
    with pytest.raises(EngineNotOff, match="Park"):
        s.check_engine_off()
    s.frame(gear(0))
    s.check_engine_off()
    assert s.describe() == "0 rpm, 0 km/h, P"


def test_engine_off_refuses_running_spiked_or_stale():
    s, clock = state()
    s.frame(speed(0))
    s.frame(rpm(750))
    with pytest.raises(EngineNotOff, match="750 rpm"):
        s.check_engine_off()
    s.frame(rpm(0))
    s.frame(rpm(40))         # cranking, then back to 0 before the check
    s.frame(rpm(0))
    with pytest.raises(EngineNotOff, match="running"):
        s.check_engine_off()
    s.frame(rpm(0))
    s.check_engine_off()     # the spike was counted once; a clean 0 since then passes
    clock.t += pol.FRESH + 0.1
    with pytest.raises(EngineNotOff, match="isn't known"):
        s.check_engine_off()


def test_unknown_gear_is_allowed_only_when_the_map_has_no_gear():
    s, _ = state(gear_signal=GEAR)
    s.frame(rpm(0))
    s.frame(speed(0))
    s.check_engine_off()     # the map has a gear signal but it hasn't been heard: speed 0 still covers it


# ---- the sender ----

class Link:
    def __init__(self):
        self.frames = []

    def send(self, frames):
        self.frames += frames


CLEAR = Service(0x04, "Clear codes (test entry)", Tier.ENGINE_OFF, lambda params: "" if not params else "no params")
TABLE = {**pol.SERVICES, 0x04: CLEAR}


def test_sender_checks_the_tier_before_anything_goes_out():
    s, _ = state()
    link = Link()
    sender = Sender(link, s, table=TABLE)
    clear = build(0x04, b"", table=TABLE)
    s.frame(rpm(800))
    s.frame(speed(0))
    with pytest.raises(EngineNotOff):
        sender.send(clear)
    sender.send(build(0x01, b"\x0c", table=TABLE))     # reads go out whatever the engine does
    assert [f.data[:3] for f in link.frames] == [bytes([2, 1, 0x0C])]
    s.frame(rpm(0))
    with pytest.raises(EngineNotOff, match="800 rpm"):   # still running at the last check
        sender.send(clear)
    s.frame(rpm(0))                                        # a whole interval at 0 since then
    sender.send(clear)
    assert link.frames[-1].data[:2] == bytes([1, 0x04]) and sender.sent == 2


def test_sender_refuses_requests_not_from_its_table():
    s, _ = state()
    link = Link()
    forged = Request(Service(0x01, "lookalike", Tier.READ, lambda params: ""), b"\x01\x0c")
    with pytest.raises(PolicyRefused):
        Sender(link, s).send(forged)
    with pytest.raises(PolicyRefused):
        Sender(link, s).send(build(0x04, b"", table=TABLE))   # an entry the real table doesn't have
    assert link.frames == []
