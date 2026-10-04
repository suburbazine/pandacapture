"""Engine-off diagnostic sessions against a simulated module: refused while running, ENGINE OFF needed, tester
present only while the state holds, everything undone newest first when the engine starts, resets and explicit
stops tracked, and the module's own timeout as the last resort."""

import importlib.util
import threading
import time
from pathlib import Path

import pytest

from pandacapture import diag, diagsession
from pandacapture import protocol as p
from pandacapture.diag import Client
from pandacapture.diagsession import EngineOffSession, SessionEnded
from pandacapture.policy import PolicyRefused, VehicleState, is_flow_control, recognise


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = load("test_codes")


class Module:
    """An engine module (7E0/7E8) with UDS session behaviour; the car's RPM and speed over OBD."""

    S3 = 0.6           # s without tester present before it drops back to its default session (5 s, scaled down)

    def __init__(self):
        self.rpm, self.speed = 0, 0
        self.session = 1
        self.last_tp = time.monotonic()
        self.dtc_setting = True
        self.comm = True
        self.io = {}                   # did -> adjusted value
        self.routines = set()
        self.resets = 0
        self.log = []                  # payloads received, in order
        self.lock = threading.Lock()

    def tick(self):
        if self.session == 3 and time.monotonic() - self.last_tp > self.S3:
            self.default()

    def default(self):
        self.session, self.dtc_setting, self.comm, self.io, self.routines = 1, True, True, {}, set()

    def answer(self, payload):
        sid, prm = payload[0], payload[1:]
        if payload == b"\x01\x0c":
            raw = int(self.rpm * 4)
            return b"\x41\x0c" + bytes([raw >> 8, raw & 0xFF])
        if payload == b"\x01\x0d":
            return b"\x41\x0d" + bytes([self.speed])
        self.log.append(payload)
        if sid == 0x10:
            self.session, self.last_tp = prm[0], time.monotonic()
            if prm[0] == 1:
                self.default()
            return bytes([0x50, prm[0], 0, 0x32, 0x01, 0xF4])
        if sid == 0x3E:
            self.last_tp = time.monotonic()
            return None if prm[0] & 0x80 else b"\x7e\x00"
        needs_extended = sid in (0x85, 0x28, 0x2F, 0x31)
        if needs_extended and self.session != 3:
            return bytes([0x7F, sid, 0x7F])              # not supported in this session
        if self.rpm:
            return bytes([0x7F, sid, 0x22])              # conditions not correct: the module's own check
        if sid == 0x85:
            self.dtc_setting = prm[0] == 1
            return bytes([0xC5, prm[0]])
        if sid == 0x28:
            self.comm = prm[0] == 0
            return bytes([0x68, prm[0]])
        if sid == 0x2F:
            did = prm[:2]
            if prm[2] == 0:
                self.io.pop(did, None)
            else:
                self.io[did] = prm[3:]
            return bytes([0x6F]) + prm[:3]
        if sid == 0x31:
            rid = prm[1:3]
            if prm[0] == 1:
                self.routines.add(rid)
            elif prm[0] == 2:
                self.routines.discard(rid)
            return bytes([0x71]) + prm[:3]
        if sid == 0x11:
            self.resets += 1
            self.default()
            return bytes([0x51, prm[0]])
        if sid == 0x22:
            return b"\x62" + prm[:2] + b"\x12\x34"
        return bytes([0x7F, sid, 0x11])


class Link:
    """A panda-style link to the module: long answers wait for flow control."""

    def __init__(self, module):
        self.m = module
        self.out, self.held, self.sent = [], [], []
        self.client = None

    def send(self, frames):
        for f in frames:
            self.sent.append(f)
            if is_flow_control(f):
                self.out += self.held
                self.held = []
                continue
            req = recognise(f)
            if f.addr not in (0x7DF, 0x7E0):
                continue
            with self.m.lock:
                a = self.m.answer(req.payload)
            if a is None:
                continue
            frames_ = tc.isotp(a)
            self.out.append(frames_[0])
            self.held = frames_[1:]

    def wait(self, seconds):
        self.m.tick()
        out, self.out = self.out, []
        for d in out:
            self.client.frame(p.Frame(0, 0x7E8, d))
        if not out:
            time.sleep(min(seconds, 0.005))


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(diag, "ANSWER_WAIT", 0.03)
    monkeypatch.setattr(diag, "LONG_WAIT", 0.03)
    monkeypatch.setattr(diagsession, "TESTER_PRESENT_EVERY", 0.2)
    monkeypatch.setattr(diagsession, "CHECK_EVERY", 0.05)


@pytest.fixture
def car():
    module = Module()
    link = Link(module)
    client = Client(link, VehicleState())
    link.client = client
    notes = []
    s = EngineOffSession(client, log=lambda text: None, note=notes.append).start()
    yield module, s, notes
    s.close()


def started(module, s):
    s.confirm()
    assert s.request(0x7E0, 0x10, b"\x03").ok
    assert s.request(0x7E0, 0x85, b"\x02").ok
    assert s.request(0x7E0, 0x2F, b"\xf1\xa0\x03\x40").ok
    assert s.request(0x7E0, 0x31, b"\x01\x02\x03").ok
    assert module.session == 3 and not module.dtc_setting and module.io and module.routines


def test_needs_engine_off_word_and_state(car):
    module, s, _ = car
    with pytest.raises(PolicyRefused, match="ENGINE OFF typed first"):
        s.request(0x7E0, 0x10, b"\x03")
    s.confirm()
    module.rpm = 750
    with pytest.raises(diagsession.EngineNotOff, match="750 rpm"):
        s.request(0x7E0, 0x10, b"\x03")
    assert module.session == 1 and 0x10 not in [x[0] for x in module.log]
    module.rpm = 0
    assert s.request(0x7E0, 0x22, b"\xf1\x87").data == b"\x62\xf1\x87\x12\x34"   # reads work anyway
    with pytest.raises(PolicyRefused, match="programming"):
        s.request(0x7E0, 0x10, b"\x02")


def test_engine_starting_undoes_everything_newest_first(car):
    module, s, notes = car
    started(module, s)
    module.log.clear()
    module.rpm = 700
    deadline = time.monotonic() + 3
    while not s.ended and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "700 rpm" in s.ended
    # The module's own check refuses changes with the engine running, but the undo requests go out regardless
    undo = [x for x in module.log if x[0] in (0x31, 0x2F, 0x85, 0x10)]
    assert undo == [b"\x31\x02\x02\x03", b"\x2f\xf1\xa0\x00", b"\x85\x01", b"\x10\x01"]
    assert module.session == 1
    with pytest.raises(SessionEnded):
        s.request(0x7E0, 0x10, b"\x03")
    assert "session ended" in notes[-1]


def test_tester_present_only_while_the_state_holds(car):
    module, s, _ = car
    started(module, s)
    time.sleep(1.0)                          # longer than the module's S3: tester present keeps it extended
    assert module.session == 3 and s.tester_present_sent >= 3
    module.speed = 8                         # a hybrid creeping on its motor: rpm 0, but moving
    time.sleep(0.5)
    assert "moving" in s.ended and module.session == 1
    sent = s.tester_present_sent
    time.sleep(0.5)
    assert s.tester_present_sent == sent


def test_explicit_stops_and_resets_are_tracked(car):
    module, s, _ = car
    started(module, s)
    assert s.request(0x7E0, 0x2F, b"\xf1\xa0\x00").ok       # handed back: no longer to undo
    assert s.request(0x7E0, 0x31, b"\x02\x02\x03").ok       # stopped
    st = s.status()
    assert [e[1] for e in st["effects"]] == ["85 02"] and st["extended"] == [0x7E0]
    assert s.request(0x7E0, 0x11, b"\x01").ok               # a reset: everything on that module is gone
    st = s.status()
    assert st["effects"] == [] and st["extended"] == [] and module.resets == 1


def test_close_undoes_and_the_module_times_out_by_itself(car):
    module, s, _ = car
    started(module, s)
    s.close("done")
    assert module.session == 1 and module.dtc_setting and not module.io and not module.routines
    # And if PandaCapture had simply vanished: no tester present, and the module drops back by itself
    m2 = Module()
    m2.session, m2.last_tp = 3, time.monotonic()
    time.sleep(Module.S3 + 0.1)
    m2.tick()
    assert m2.session == 1


def test_command_parsing():
    from pandacapture.diag_cli import parse
    assert parse("session extended", 0x7E0) == (0x7E0, 0x10, b"\x03")
    assert parse("io F1A0 adjust 40 @7D1", 0x7E0) == (0x7D1, 0x2F, b"\xf1\xa0\x03\x40")
    assert parse("routine start 0203", 0x7E0) == (0x7E0, 0x31, b"\x01\x02\x03")
    assert parse("comm disable", 0x7E0) == (0x7E0, 0x28, b"\x03\x03")
    assert parse("reset soft", 0x7E0) == (0x7E0, 0x11, b"\x03")
    for bad in ("flash it", "io F1 adjust 40", "session programming", "read F1"):
        with pytest.raises(ValueError):
            parse(bad, 0x7E0)
