"""The dashboard's Modules page: the scan (OBD modules, the sweep and its 900 rpm gate, identification, each
module's own codes), identifier reads and their validation, the link over the dashboard's own panda, and the whole
job through the dashboard against a simulated car, with its files. The same steps as PandaCapture Android's
Modules card."""

import json
import time
import urllib.error
import urllib.request

import pytest

from pandacapture import diag, udsjob
from pandacapture import protocol as p
from pandacapture.diag import Client
from pandacapture.policy import VehicleState, is_flow_control, recognize
from tests.test_codes import bitmask
from tests.test_uds import car_modules, tc


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(diag, "ANSWER_WAIT", 0.02)
    monkeypatch.setattr(diag, "LONG_WAIT", 0.02)
    monkeypatch.setattr(udsjob, "LISTEN_S", 0.2)


def modules(rpm=800):
    m = car_modules()
    raw = int(rpm * 4)
    m[0x7E8][b"\x01\x00"] = b"\x41\x00" + bitmask(0x0C)
    m[0x7E8][b"\x01\x0c"] = b"\x41\x0c" + bytes([raw >> 8, raw & 0xFF])
    m[0x7E9][b"\x01\x00"] = b"\x41\x00" + bitmask(0x05)
    return m


def connect(rpm=800):
    car = tc.Car(modules(rpm))
    car.out.append((0x7A0, bytes([0x88, 0x13, 0, 0x5A, 0, 0, 0, 0])))   # a broadcast on 7A0
    client = Client(car, VehicleState())
    car.client = client
    car.wait(0)
    return car, client


def test_the_scan():
    car, client = connect()
    said = []
    r = udsjob.scan_modules(client, [0], said.append, probe_wait=0.001)
    found = {m["request_id"]: m for m in r["modules"]}
    assert list(found) == ["7D1", "7E0", "7E1"] and r["swept"] and "stopped" not in r
    e = found["7E0"]
    assert e["name"] == "engine" and e["answer_id"] == "7E8" and e["bus"] == 0
    assert e["info"] == {"System name": "ENGINE ECU", "Part number": "39101-3L400", "Software version": "SW 1.02"}
    assert e["codes"][0]["code"] == "P0301-00" and "confirmed" in e["codes"][0]["status"]
    assert found["7E1"]["codes_refused"] == "service not supported (11)" and found["7E1"]["info"] == {}
    assert found["7D1"]["codes"] == [] and found["7D1"]["info"] == {"System name": "ABS/ESC"}
    assert r["requests"] == client.sender.sent and r["max_rpm"] == 800
    assert any(s.startswith("Bus 0: asking 700 for its part number (1 of ") for s in said)
    assert "Bus 0: reading what 7E0 is…" in said and "Bus 0: reading 7E0's codes…" in said
    # Only reads ever went out, never to 7A0 (other traffic) or 798 (it would answer on 7A0), and not the VIN
    asked = [f for f in car.sent if not is_flow_control(f)]
    assert {recognize(f).payload[:1] for f in asked} <= {b"\x01", b"\x22", b"\x19"}
    assert not {0x7A0, 0x798} & {f.addr for f in asked}
    assert not any(recognize(f).payload == b"\x22\xf1\x90" for f in asked)


def test_the_sweep_stops_above_900():
    car, client = connect(rpm=3500)
    r = udsjob.scan_modules(client, [0], probe_wait=0.001)
    assert r["stopped"] == "Engine at 3500 rpm: the scan only runs key-on or at idle (up to 900 rpm)."
    assert not any(f.data[1:4] == b"\x22\xf1\x87" for f in car.sent)    # not one probe went out


def test_identifier_reads_and_what_they_take():
    with pytest.raises(ValueError, match="Give the module's request id in hex, e.g. 7E0"):
        udsjob.parse_did_request("engine", "F187")
    with pytest.raises(ValueError, match=r"Request ids are 700-7F7 \(not 7D7, 7DF or 7E8-7EF\)"):
        udsjob.parse_did_request("7E8", "F187")
    with pytest.raises(ValueError, match="Identifiers are 4 hex digits each, e.g. E019 F187"):
        udsjob.parse_did_request("7E0", "F18")
    with pytest.raises(ValueError, match="Up to 32 identifiers at a time"):
        udsjob.parse_did_request("7E0", " ".join(["F187"] * 33))
    assert udsjob.parse_did_request(" 7e0 ", "e001, F187  e002") == (0x7E0, [0xE001, 0xF187, 0xE002])
    car, client = connect()
    r = udsjob.read_dids(client, 0, 0x7E0, [0xE001, 0xE002, 0xF187, 0x1234])
    rows = {x["did"]: x for x in r["reads"]}
    assert rows["E001"]["hex"] == "1234" and rows["F187"]["text"] == "39101-3L400"
    assert rows["E002"]["refused"] == "answered a different identifier" and rows["1234"]["refused"] == "no answer"
    assert r["module"] == "7E0" and r["bus"] == 0


# ---------------------------------------------------------------- through the dashboard

class CarSource:
    """A panda with PandaCapture firmware on a car: the simulated modules answer what's sent, the panda echoes
    what it sends, and the engine's rpm comes on 0x316 as the Stinger map has it."""

    description = "simulated panda, firmware PANDACAPTURE-test"
    header = []
    serial = "sim"

    def __init__(self, rpm=800):
        self.car = tc.Car(modules(rpm))
        self.car.client = self
        self.can_transmit, self.armed, self.arms = True, False, 0
        self.pending, self.rpm = [], rpm
        self.last_rpm = 0.0

    def frame(self, f):                       # the car hands its answers here (as if to a client)
        self.pending.append(f)

    def arm(self):
        self.armed, self.arms = True, self.arms + 1

    def disarm(self):
        self.armed = False

    def beat(self):
        pass

    def send(self, frames):
        assert self.armed, "nothing goes out unarmed"
        self.car.send(frames)
        self.pending += [p.Frame(f.bus, f.addr, f.data, returned=True) for f in frames]
        self.car.wait(0)

    def read(self):
        now = time.monotonic()
        if now - self.last_rpm > 0.05:
            self.last_rpm = now
            raw = int(self.rpm * 4)
            self.pending.append(p.Frame(0, 0x316, bytes([0, 0x10, raw & 0xFF, raw >> 8, 0, 0, 0, 0])))
        out, self.pending = self.pending, []
        if not out:
            time.sleep(0.002)
        return out

    def health(self):
        return None

    def close(self):
        pass


def test_the_modules_page(tmp_path, monkeypatch):
    from pandacapture.dashboard import Dashboard
    from pandacapture.signals import load_map
    from pandacapture import uds
    monkeypatch.setattr(uds, "PROBE_WAIT", 0.001)
    source = CarSource()
    dash = Dashboard(lambda: source, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    dash.start()

    def post(body):
        req = urllib.request.Request(dash.url + "uds", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps(body).encode())
        return json.loads(urllib.request.urlopen(req, timeout=10).read())

    def get():
        return json.loads(urllib.request.urlopen(dash.url + "uds", timeout=10).read())

    def done():
        end = time.monotonic() + 30
        while get()["job"]["state"] == "running":
            assert time.monotonic() < end
            time.sleep(0.05)
        return get()
    try:
        end = time.monotonic() + 5
        while dash.reader.source is not source:
            assert time.monotonic() < end
            time.sleep(0.02)
        page = urllib.request.urlopen(dash.url + "modules.html", timeout=10).read().decode()
        assert "Scan modules" in page and "Type <b>TRANSMIT</b>" in page
        assert get()["can_send"] and not get()["confirmed"] and get()["last"] is None
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"action": "scan"})
        assert "Type TRANSMIT first" in e.value.read().decode()
        post({"action": "scan", "confirm": "transmit"})
        s = done()
        job = s["job"]
        assert job["state"] == "done", job
        assert job["progress"].startswith("Found 3 modules (") and not source.armed and source.arms == 1
        assert [m["request_id"] for m in job["result"]["modules"]] == ["7D1", "7E0", "7E1"]
        saved = sorted(tmp_path.glob("obd-modules-*.json"))
        assert len(saved) == 1 and s["last"]["file"] == saved[0].name                    # shown again on load
        log = saved[0].with_suffix(".log").read_text().splitlines()
        assert log[0] == udsjob.LOG_HEADER
        assert any(" 7E0#0322F197" in x for x in log) and any(" 7E8#" in x for x in log)
        assert all(" can" in x and int(x.split()[2].split("#")[0], 16) >= 0x700 for x in log[1:])
        # An identifier read, on the bus the scan found the module on
        post({"action": "did", "module": "7E0", "dids": "E001 F187"})
        job = done()["job"]
        assert job["state"] == "done" and job["result"]["reads"][0]["hex"] == "1234"
        assert job["progress"] == "Read 2 of 2 identifiers from 7E0" and sorted(tmp_path.glob("obd-did-*.json"))
        with pytest.raises(urllib.error.HTTPError) as e:
            post({"action": "did", "module": "7E8", "dids": "F187"})
        assert "Request ids are 700-7F7" in e.value.read().decode()
    finally:
        dash.stop()


def test_without_a_panda_that_can_send(tmp_path):
    from pandacapture.dashboard import Dashboard
    from pandacapture.signals import load_map
    from pandacapture.sources import SimulatedSource
    dash = Dashboard(SimulatedSource, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    dash.start()
    try:
        time.sleep(0.3)
        info = json.loads(urllib.request.urlopen(dash.url + "uds", timeout=10).read())
        assert not info["can_send"]
        req = urllib.request.Request(dash.url + "uds", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"action": "scan", "confirm": "TRANSMIT"}).encode())
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert "no panda to send through" in e.value.read().decode()
    finally:
        dash.stop()
