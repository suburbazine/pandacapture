"""Reading codes: ISO-TP answers (flow control, pending, broken), decoding, and the whole read through a
panda-style link and an ELM327, against a simulated car."""

import types

import pytest

from pandacapture import codes as c
from pandacapture import diag
from pandacapture import protocol as p
from pandacapture.codes import dtc, dtc_kind, parse_dtcs, read_codes, report
from pandacapture.diag import Client
from pandacapture.policy import PolicyRefused, VehicleState, is_flow_control, recognize


def bitmask(*pids, base=0):
    v = 0
    for pid in pids:
        v |= 1 << (31 - (pid - base - 1))
    return v.to_bytes(4, "big")


def isotp(payload):
    """The CAN frames (data only) a module sends for one answer."""
    if len(payload) <= 7:
        return [bytes([len(payload)]) + payload + bytes(7 - len(payload))]
    frames = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]]
    rest, seq = payload[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        frames.append(bytes([0x20 | seq]) + chunk + bytes(7 - len(chunk)))
        seq = (seq + 1) & 0x0F
    return frames


VIN = b"KNAE55LC1J6000000"
CAL = b"STINGER33T-ECM01"
ECU = b"ECM-EngineControl\0\0\0"


def stinger(pending_once=True):
    """module -> request payload -> answer payload (or a list: answers in turn)."""
    engine = {
        b"\x01\x01": b"\x41\x01\x83\x07\x65\x04",                          # MIL on, 3 codes
        b"\x03": b"\x43\x03\x01\x33\x03\x01\xC1\x00",                       # P0133 P0301 U0100: a long answer
        b"\x07": b"\x47\x00",
        b"\x0a": b"\x7f\x0a\x11",                                           # permanent codes not supported
        b"\x02\x02\x00": b"\x42\x02\x00\x03\x01",                           # freeze frame stored by P0301
        b"\x02\x00\x00": b"\x42\x00\x00" + bitmask(0x02, 0x05, 0x0C),
        b"\x02\x05\x00": b"\x42\x05\x00\x7b",
        b"\x02\x0c\x00": b"\x42\x0c\x00\x0c\x80",
        b"\x09\x00": b"\x49\x00" + bitmask(0x02, 0x04, 0x0A),
        b"\x09\x02": b"\x49\x02\x01" + VIN,
        b"\x09\x04": b"\x49\x04\x01" + CAL,
        b"\x09\x0a": b"\x49\x0a\x01" + ECU,
    }
    trans = {
        b"\x01\x01": b"\x41\x01\x00\x00\x00\x00",
        b"\x03": b"\x43\x01\x07\x00",                                       # P0700
        b"\x07": [b"\x7f\x07\x78", b"\x47\x00"] if pending_once else b"\x47\x00",   # needs a moment
        b"\x0a": b"\x4a\x00",
        b"\x02\x02\x00": b"\x42\x02\x00\x00\x00",
        b"\x09\x00": b"\x49\x00" + bitmask(0x0A),
        b"\x09\x0a": b"\x49\x0a\x01" + b"TCM-TransmissionCtl\0",
    }
    return {0x7E8: engine, 0x7E9: trans}


class Car:
    """A panda-style link to a car on bus 0: long answers wait for flow control before the rest comes."""

    def __init__(self, modules, flow_control_works=True, misorder=False):
        self.modules = modules
        self.flow_control_works = flow_control_works
        self.misorder = misorder
        self.out = []
        self.held = {}          # module -> frames waiting for flow control
        self.sent = []
        self.client = None

    def send(self, frames):
        for f in frames:
            self.sent.append(f)
            if is_flow_control(f):
                module = f.addr + 8
                if self.flow_control_works and module in self.held:
                    rest = self.held.pop(module)
                    if self.misorder:
                        rest = rest[1:] + rest[:1]
                    self.out += [(module, d) for d in rest]
                continue
            req = recognize(f)               # the car only ever sees policy requests
            for module, answers in self.modules.items():
                if f.addr not in (0x7DF, module - 8) or req.payload not in answers:
                    continue
                a = answers[req.payload]
                if isinstance(a, list):
                    a = a.pop(0) if len(a) > 1 else a[0]
                    self.out.append((module, isotp(a)[0]))
                    if a[2:3] == b"\x78":
                        self.out.append((module, isotp(answers[req.payload][0])[0]))
                    continue
                frames = isotp(a)
                self.out.append((module, frames[0]))
                if len(frames) > 1:
                    self.held[module] = frames[1:]

    def wait(self, seconds):
        out, self.out = self.out, []
        for module, data in out:
            self.client.frame(p.Frame(0, module, data))


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(diag, "ANSWER_WAIT", 0.03)
    monkeypatch.setattr(diag, "LONG_WAIT", 0.03)
    monkeypatch.setattr(diag, "PENDING_WAIT", 0.05)


def client_for(car):
    client = Client(car, VehicleState())
    car.client = client
    return client


# ---- decoding ----

def test_dtc_decoding():
    assert dtc(0x01, 0x33) == "P0133" and dtc(0xC1, 0x00) == "U0100" and dtc(0x5A, 0xBC) == "C1ABC"
    assert parse_dtcs(b"\x43\x03\x01\x33\x03\x01\xC1\x00") == ["P0133", "P0301", "U0100"]
    assert parse_dtcs(b"\x43\x00") == [] and parse_dtcs(b"\x43\x01\x00\x00") == []
    kinds = {code: dtc_kind(code) for code in ("P0301", "P1326", "P2187", "P3400", "P3000", "U0100", "B1342", "C3000")}
    assert kinds == {"P0301": "generic", "P1326": "manufacturer", "P2187": "generic", "P3400": "generic",
                     "P3000": "manufacturer", "U0100": "generic", "B1342": "manufacturer", "C3000": "reserved"}


# ---- ISO-TP ----

def test_long_answer_needs_flow_control_and_gets_it():
    car = Car(stinger())
    answers = client_for(car).request(0x03)
    assert answers.positive[0x7E8] == b"\x43\x03\x01\x33\x03\x01\xC1\x00"
    assert answers.positive[0x7E9] == b"\x43\x01\x07\x00"
    fcs = [f for f in car.sent if is_flow_control(f)]
    assert [f.addr for f in fcs] == [0x7E0]          # only to the module with the long answer


def test_long_answer_without_flow_control_is_reported_broken():
    car = Car(stinger(), flow_control_works=False)
    answers = client_for(car).request(0x03)
    assert 0x7E8 not in answers.positive and "part way" in answers.broken[0x7E8]
    car = Car(stinger(), misorder=True)
    answers = client_for(car).request(0x09, b"\x02", target=0x7E0)
    assert "where 1 was due" in answers.broken[0x7E8]


def test_pending_and_refusals():
    car = Car(stinger())
    answers = client_for(car).request(0x07)
    assert answers.positive[0x7E9] == b"\x47\x00"    # after "response pending"
    answers = client_for(car).request(0x0A)
    assert answers.negative == {0x7E8: 0x11} and answers.positive[0x7E9] == b"\x4a\x00"


def test_client_only_sends_policy_frames():
    with pytest.raises(PolicyRefused):
        client_for(Car(stinger())).request(0x14, b"\xff\xff\xff")     # clearing codes: not in the table yet
    with pytest.raises(PolicyRefused):
        client_for(Car(stinger())).request(0x04)


# ---- the whole read ----

def check_read(modules):
    by = {m.module: m for m in modules}
    e, t = by[0x7E8], by[0x7E9]
    assert e.mil == {"mil_on": True, "codes": 3} and t.mil == {"mil_on": False, "codes": 0}
    assert e.codes == {"stored": ["P0133", "P0301", "U0100"], "pending": []}
    assert "service not supported" in e.refused["permanent"] and t.codes["permanent"] == []
    assert t.codes["stored"] == ["P0700"]
    assert e.freeze_dtc == "P0301" and not t.freeze_dtc
    assert c.describe(0x05, e.freeze[0x05]) == "83 °C" and c.describe(0x0C, e.freeze[0x0C]) == "800 rpm"
    assert e.info["Calibration ID"] == ["STINGER33T-ECM01"] and e.info["ECU name"] == ["ECM-EngineControl"]
    assert t.info["ECU name"] == ["TCM-TransmissionCtl"]
    text = report(modules)
    assert "Check-engine light: ON, 3 codes counted" in text and "P0301 (generic)" in text
    assert "Permanent  not read: service not supported (11)" in text
    return e


def test_read_codes_through_a_panda_style_link():
    car = Car(stinger())
    e = check_read(read_codes(client_for(car), [0], log=lambda s: None))
    assert "VIN" not in e.info
    assert not any(recognize(f).payload == b"\x09\x02" for f in car.sent if not is_flow_control(f))
    for f in car.sent:                               # every frame: a policy request or flow control
        assert is_flow_control(f) or recognize(f)


def test_vin_only_when_asked():
    car = Car(stinger())
    e = check_read(read_codes(client_for(car), [0], with_vin=True, log=lambda s: None))
    assert e.info["VIN"] == [VIN.decode()]


def test_read_codes_through_an_elm():
    import importlib.util
    from pathlib import Path

    from pandacapture.elm import Elm, ElmLink
    spec = importlib.util.spec_from_file_location("elm_fakes", Path(__file__).with_name("test_elm.py"))
    elm_fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(elm_fakes)
    FakeElm = elm_fakes.FakeElm

    car_modules = stinger(pending_once=False)

    class CodesElm(FakeElm):
        def _command(self, cmd):
            if cmd.startswith("AT") or not cmd or cmd == "0100":
                return super()._command(cmd)
            self.requests.append(cmd)
            payload = bytes.fromhex(cmd)
            target = getattr(self, "header", 0x7DF)
            lines = []
            for module, answers in car_modules.items():
                if target in (0x7DF, module - 8) and payload in answers:
                    a = answers[payload]
                    for data in isotp(a if isinstance(a, bytes) else a[-1]):   # the ELM does flow control
                        lines.append(f"{module:03X} " + " ".join(f"{b:02X}" for b in data))
            return self._reply(cmd, lines or ["NO DATA"])

        def write(self, data):
            text = data.decode().strip().upper()
            if text.startswith("ATSH"):
                self.header = int(text[4:], 16)
            super().write(data)

    fake = CodesElm()
    elm = Elm("COM9", serial_factory=lambda port: fake)
    holder = types.SimpleNamespace(client=None)
    link = ElmLink(elm, lambda f: holder.client.frame(f))
    holder.client = Client(link, VehicleState())
    check_read(read_codes(holder.client, [0], log=lambda s: None))
    assert "0902" not in fake.requests                # no VIN unless asked
