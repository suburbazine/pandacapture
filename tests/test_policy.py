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

# What the table allows today: the standard OBD reads, as (service, parameter length)
READS = {(0x01, 1), (0x02, 2), (0x03, 0), (0x07, 0), (0x09, 1), (0x0A, 0), (0x22, 2)}   # (0x19: below)
CLEARS = {(0x04, 0), (0x14, 3)}     # engine off, and CLEAR typed
ENGINE_OFF_SAMPLE = {(0x31, 3)}     # of the step-4 services, the only one the sample parameters below happen to fit


def test_what_the_table_allows():
    built = set()
    for sid in range(256):
        for params in (b"", b"\x0c", b"\x0c\x00", b"\x01\x02\x03"):
            try:
                build(sid, params)
                built.add((sid, len(params)))
            except PolicyRefused:
                pass
    assert built == READS | CLEARS | ENGINE_OFF_SAMPLE
    assert all(pol.SERVICES[sid].tier is Tier.READ and not pol.SERVICES[sid].confirm for sid, _ in READS)
    assert all(pol.SERVICES[sid].tier is Tier.ENGINE_OFF and pol.SERVICES[sid].confirm == "CLEAR" for sid, _ in CLEARS)
    with pytest.raises(PolicyRefused, match="freeze frame 00"):
        build(0x02, b"\x0c\x01")


def test_never_services_say_why():
    for sid in (0x27, 0x34, 0x35, 0x36, 0x37):
        with pytest.raises(PolicyRefused, match="never sent"):
            build(sid, b"\x01")
    for sid in (0x2E, 0x3D, 0x86, 0x87):   # writing data, writing memory, events, link control: not in the table
        with pytest.raises(PolicyRefused, match="isn't in"):
            build(sid, b"\x01")
    with pytest.raises(PolicyRefused, match="programming session is never"):
        build(0x10, b"\x02")
    with pytest.raises(PolicyRefused, match="rapid power shutdown"):
        build(0x11, b"\x04")
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
        assert r.payload[0] in pol.SERVICES and f.addr in (0x7DF, 0x7E0)
    for refused in (p.Frame(0, 0x7DF, bytes([0x10, 0x14, 0x2E, 0xF1, 0x90, 1, 2, 3])),    # a multi-frame write
                    p.Frame(0, 0x7E0, bytes([4, 0x2E, 0xF1, 0x90, 0x01, 0, 0, 0])),        # a write
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


def test_flow_control_is_the_only_other_frame():
    f = pol.flow_control(0x7E9, bus=2)
    assert (f.bus, f.addr, f.data) == (2, 0x7E1, bytes([0x30, 0, 0, 0, 0, 0, 0, 0])) and pol.is_flow_control(f)
    with pytest.raises(PolicyRefused):
        pol.flow_control(0x7DF)                      # its request id would be 7D7, which answers on 7DF
    for not_fc in (p.Frame(0, 0x7E0, bytes([0x30, 0x08, 0x14, 0, 0, 0, 0, 0])),     # a block limit
                   p.Frame(0, 0x316, bytes([0x30, 0, 0, 0, 0, 0, 0, 0])),
                   p.Frame(0, 0x7DF, bytes([0x30, 0, 0, 0, 0, 0, 0, 0]))):
        assert not pol.is_flow_control(not_fc)
        with pytest.raises(PolicyRefused):
            recognise(not_fc)


def test_uds_reads():
    assert build(0x19, b"\x02\xff", target=0x7D1).payload == b"\x19\x02\xff"
    assert build(0x19, b"\x04\x01\x23\x45\x01", target=0x7E0).payload[:2] == b"\x19\x04"
    assert build(0x19, b"\x0a").payload == b"\x19\x0a"
    for bad, why in ((b"", "report type"), (b"\x14", "isn't one"), (b"\x82\xff", "isn't one"),   # 0x82: suppress answer
                     (b"\x02", "takes 1"), (b"\x04\x01\x23", "takes 4")):
        with pytest.raises(PolicyRefused, match=why):
            build(0x19, bad)
    assert build(0x22, b"\xf1\x90\xe0\x01\xe0\x02", target=0x7A0).payload == b"\x22\xf1\x90\xe0\x01\xe0\x02"
    for bad in (b"", b"\xf1", b"\xf1\x90\xe0", b"\x01\x02\x03\x04\x05\x06\x07\x08"):
        with pytest.raises(PolicyRefused):
            build(0x22, bad)


def test_module_addresses():
    for ok in (0x700, 0x7A0, 0x7D1, 0x7E0, 0x7E7, 0x7F7):
        assert pol.is_physical(ok) and build(0x22, b"\xf1\x87", target=ok).target == ok
    assert not pol.is_physical(0x7DF)       # everyone's address, not one module's (build allows it as that)
    for bad in (0x6FF, 0x7D7, 0x7E8, 0x7EF, 0x7F8, 0x7FF):
        assert not pol.is_physical(bad)
        with pytest.raises(PolicyRefused):
            build(0x22, b"\xf1\x87", target=bad)
    assert pol.flow_control(0x7D9).addr == 0x7D1


def test_ids_carrying_ordinary_traffic_are_never_sent_to():
    s, _ = state()
    link = Link()
    sender = Sender(link, s)
    s.frame(p.Frame(0, 0x7A0, bytes([0x88, 0x13, 0, 0x5A, 0, 0, 0, 0])))   # a broadcast that happens to use 7A0
    s.frame(p.Frame(0, 0x7E0, bytes([0x02, 0x01, 0x0C, 0, 0, 0, 0, 0])))   # another tester's request: fine
    with pytest.raises(PolicyRefused, match="carries other traffic"):
        sender.send(build(0x22, b"\xf1\x87", target=0x7A0))
    with pytest.raises(PolicyRefused, match="carries other traffic"):
        sender.flow_control(0x7A8)
    sender.send(build(0x22, b"\xf1\x87", target=0x7A0), bus=1)              # other buses are separate
    sender.send(build(0x22, b"\xf1\x87", target=0x7E0))
    assert [f.addr for f in link.frames] == [0x7A0, 0x7E0]
