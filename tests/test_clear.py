"""Clearing codes: only with the engine off, the car stopped and CLEAR typed; checked right before each request;
the confirmation enforced by the sender itself; refusals reported."""

import importlib.util
from pathlib import Path

import pytest

from pandacapture import codes as c
from pandacapture import diag
from pandacapture import policy as pol
from pandacapture import protocol as p
from pandacapture.diag import Client
from pandacapture.policy import EngineNotOff, PolicyRefused, VehicleState, build, is_flow_control, recognize


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = load("test_codes")


class Car:
    """Two OBD modules with codes, live RPM and speed over OBD (nothing broadcast, as through an ELM327), and
    modules that really clear when asked."""

    def __init__(self, rpm=0, speed=0, refuse=None):
        self.rpm, self.speed = rpm, speed
        self.refuse = refuse or {}            # module -> NRC for clearing
        self.codes = {0x7E8: [b"\x01\x33", b"\x03\x01"], 0x7E9: [b"\x07\x00"]}
        self.uds = {0x7E8: [b"\x03\x01\x00\x2f"]}
        self.sent, self.out, self.held = [], [], {}
        self.client = None

    def answer(self, module, payload):
        if payload == b"\x01\x0c":
            raw = int(self.rpm * 4)
            return b"\x41\x0c" + bytes([raw >> 8, raw & 0xFF])
        if payload == b"\x01\x0d":
            return b"\x41\x0d" + bytes([self.speed])
        if payload == b"\x01\x01":
            n = len(self.codes[module])
            return b"\x41\x01" + bytes([(0x80 if n else 0) | n, 0, 0, 0])
        if payload in (b"\x03", b"\x07", b"\x0a"):
            codes = self.codes[module] if payload == b"\x03" else []
            return bytes([payload[0] + 0x40, len(codes)]) + b"".join(codes)
        if payload == b"\x04":
            if module in self.refuse:
                return bytes([0x7F, 0x04, self.refuse[module]])
            self.codes[module] = []
            return b"\x44"
        if payload == b"\x19\x02\xff" and module in self.uds:
            return b"\x59\x02\xff" + b"".join(self.uds[module])
        if payload == b"\x14\xff\xff\xff" and module in self.uds:
            self.uds[module] = []
            return b"\x54"
        return None

    def send(self, frames):
        for f in frames:
            self.sent.append(f)
            if is_flow_control(f):
                self.out += [(f.addr + 8, d) for d in self.held.pop(f.addr + 8, [])]
                continue
            req = recognize(f)
            for module in (0x7E8, 0x7E9):
                if f.addr not in (0x7DF, module - 8):
                    continue
                a = self.answer(module, req.payload)
                if a is None:
                    continue
                frames_ = tc.isotp(a)
                self.out.append((module, frames_[0]))
                if len(frames_) > 1:
                    self.held[module] = frames_[1:]

    def wait(self, seconds):
        out, self.out = self.out, []
        for module, data in out:
            self.client.frame(p.Frame(0, module, data))

    def payloads(self):
        return [recognize(f).payload for f in self.sent if not is_flow_control(f)]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(diag, "ANSWER_WAIT", 0.02)
    monkeypatch.setattr(diag, "LONG_WAIT", 0.02)


def setup(car):
    client = Client(car, VehicleState())
    car.client = client
    modules = c.read_codes(client, [0], log=lambda s: None)
    return client, modules


def run_clear(car, typed="CLEAR", uds=(), on_ask=None):
    client, modules = setup(car)
    notes, asked = [], []

    def ask(prompt):
        asked.append(prompt)
        if on_ask:
            on_ask()
        return typed

    result = c.clear_codes(client, [0], modules, uds, ask=ask, log=lambda s: None, note=notes.append)
    return client, result, notes, asked


def test_clears_with_the_engine_off():
    car = Car()
    client, result, notes, asked = run_clear(car, uds=[(0, 0x7E0)])
    assert result == {(0, 0x7E8, "OBD"): "cleared", (0, 0x7E9, "OBD"): "cleared", (0, 0x7E8, "UDS"): "cleared"}
    assert car.codes == {0x7E8: [], 0x7E9: []} and car.uds[0x7E8] == []
    assert "0 rpm, 0 km/h" in notes[0] and len(notes) == 2 and len(asked) == 1
    after = c.read_codes(client, [0], log=lambda s: None)
    assert all(m.codes["stored"] == [] for m in after)
    # Engine speed and vehicle speed were read just before each clear
    sent = car.payloads()
    for i, payload in enumerate(sent):
        if payload in (b"\x04", b"\x14\xff\xff\xff"):
            assert {b"\x01\x0c", b"\x01\x0d"} <= set(sent[max(0, i - 4):i])


@pytest.mark.parametrize("rpm,speed,why", [(780, 0, "running"), (0, 12, "moving")])
def test_not_with_the_engine_running_or_the_car_moving(rpm, speed, why):
    car = Car(rpm=rpm, speed=speed)
    client, result, notes, asked = run_clear(car)
    assert result is None and not asked and b"\x04" not in car.payloads()
    assert car.codes[0x7E8]


@pytest.mark.parametrize("typed", ["clear", "no", "", " CLEARS"])
def test_only_clear_typed_exactly(typed):
    car = Car()
    _, result, _, asked = run_clear(car, typed=typed)
    assert result is None and asked and b"\x04" not in car.payloads() and car.codes[0x7E8]


def test_engine_started_after_confirming():
    car = Car()

    def start():
        car.rpm = 650
    with pytest.raises(EngineNotOff, match="650 rpm"):
        run_clear(car, on_ask=start)
    assert b"\x04" not in car.payloads() and car.codes[0x7E8]


def test_a_module_refusing():
    car = Car(refuse={0x7E9: 0x22})
    _, result, _, _ = run_clear(car)
    assert result[(0, 0x7E8, "OBD")] == "cleared"
    assert result[(0, 0x7E9, "OBD")] == "refused: conditions not correct (22)"


def test_the_sender_needs_the_word(monkeypatch):
    car = Car()
    client, _ = setup(car)
    c.ensure_engine_off(client, [0])
    with pytest.raises(PolicyRefused, match="CLEAR typed first"):
        client.request(0x04)
    with pytest.raises(PolicyRefused, match="CLEAR typed first"):
        client.request(0x14, b"\xff\xff\xff", target=0x7E0)
    assert b"\x04" not in car.payloads()
    client.sender.confirm("CLEAR")
    car.rpm = 900
    with pytest.raises(EngineNotOff, match="900 rpm"):
        c.ensure_engine_off(client, [0])          # the word alone isn't enough: new readings, engine off
    monkeypatch.setattr(pol, "FRESH", 0.0)
    with pytest.raises(EngineNotOff, match="isn't known"):
        client.request(0x04)                      # and the sender refuses on stale readings by itself
    assert b"" not in car.payloads()
    assert build(0x04).service.confirm == "CLEAR" and build(0x14, b"\xff\xff\xff").service.confirm == "CLEAR"
