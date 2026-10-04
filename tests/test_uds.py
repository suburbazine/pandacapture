"""UDS reads: finding modules, identifying them, their own codes and data identifiers, against the simulated
car from test_codes.py (plus an ABS module and a broadcast on a diagnostic-looking id)."""

import importlib.util
import types
from pathlib import Path

import pytest

from pandacapture import diag, uds
from pandacapture import protocol as p
from pandacapture.diag import Client
from pandacapture.policy import VehicleState, is_flow_control, recognise


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = load("test_codes")


def car_modules():
    m = tc.stinger()
    m[0x7E8].update({
        b"\x22\xf1\x87": b"\x62\xf1\x87" + b"39101-3L400",
        b"\x22\xf1\x97": b"\x62\xf1\x97" + b"ENGINE ECU",
        b"\x22\xf1\x95": b"\x62\xf1\x95" + b"SW 1.02\0\0\0",
        b"\x22\xf1\x90": b"\x62\xf1\x90" + tc.VIN,
        b"\x22\xe0\x01": b"\x62\xe0\x01\x12\x34",
        b"\x22\xe0\x02": b"\x62\xe0\x09\x00",                          # answers the wrong identifier
        b"\x19\x02\xff": b"\x59\x02\xff" + b"\x03\x01\x00\x2f" + b"\xc1\x00\x00\x09",
    })
    m[0x7E9].update({b"\x22\xf1\x87": b"\x7f\x22\x31", b"\x19\x02\xff": b"\x7f\x19\x11"})
    m[0x7D9] = {                                                        # ABS, request id 7D1
        b"\x22\xf1\x87": b"\x7f\x22\x31",                               # refuses, but it's there
        b"\x22\xf1\x97": b"\x62\xf1\x97" + b"ABS/ESC",
        b"\x19\x02\xff": b"\x59\x02\xff",                               # no codes
    }
    return m


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(diag, "ANSWER_WAIT", 0.02)
    monkeypatch.setattr(diag, "LONG_WAIT", 0.02)
    monkeypatch.setattr(uds, "PROBE_WAIT", 0.001)


def connect():
    car = tc.Car(car_modules())
    car.out.append((0x7A0, bytes([0x88, 0x13, 0, 0x5A, 0, 0, 0, 0])))   # a broadcast on 7A0
    client = Client(car, VehicleState())
    car.client = client
    car.wait(0)                                                          # heard before anything is sent
    return car, client


def test_decoding():
    assert uds.uds_dtc(b"\x03\x01\x00") == "P0301-00" and uds.uds_dtc(b"\xc1\x00\x09") == "U0100-09"
    assert uds.parse_dtc_report(b"\x59\x02\xff\x03\x01\x00\x2f\xc1\x00\x00\x09") == [("P0301-00", 0x2F), ("U0100-00", 0x09)]
    assert uds.status_text(0x2F) == "confirmed, pending, failing now, failed this drive, failed since cleared"
    assert uds.status_text(0x00) == "stored"
    assert uds.did_text(b"ENGINE ECU\0\0") == "ENGINE ECU" and uds.did_text(b"\x12\x34") == "12 34"


def test_find_modules_skips_ids_with_other_traffic():
    car, client = connect()
    found = uds.find_modules(client, 0, log=lambda s: None)
    assert found == [0x7D1, 0x7E0, 0x7E1]
    asked = {f.addr for f in car.sent if not is_flow_control(f)}
    assert 0x7A0 not in asked and 0x7D7 not in asked and not asked & set(range(0x7E8, 0x7F0))
    assert len(asked) == 248 - 1 - 2 - 8      # 700-7F7, less 7A0, 7D7/7DF, 7E8-7EF
    for f in car.sent:
        assert is_flow_control(f) or recognise(f).payload == b"\x22\xf1\x87"


def test_identify_and_did():
    car, client = connect()
    info = uds.identify(client, 0, 0x7E0)
    assert info == {"System name": "ENGINE ECU", "Part number": "39101-3L400", "Software version": "SW 1.02"}
    assert "VIN" not in info and uds.identify(client, 0, 0x7E0, with_vin=True)["VIN"] == tc.VIN.decode()
    assert uds.identify(client, 0, 0x7D1) == {"System name": "ABS/ESC"}
    assert uds.read_did(client, 0, 0x7E0, 0xE001) == (b"\x12\x34", None)
    assert uds.read_did(client, 0, 0x7E0, 0xE002) == (None, "answered a different identifier")
    assert uds.read_did(client, 0, 0x7D1, 0xF187)[1] == "out of range (31)"
    assert uds.read_did(client, 0, 0x7D2, 0xF187)[1] == "no answer"


def test_module_codes():
    car, client = connect()
    results = {(0, t): uds.read_module_codes(client, 0, t) for t in (0x7E0, 0x7E1, 0x7D1)}
    assert results[(0, 0x7E0)]["codes"] == [("P0301-00", 0x2F), ("U0100-00", 0x09)]   # a long answer
    assert any(is_flow_control(f) and f.addr == 0x7E0 for f in car.sent)
    assert results[(0, 0x7E1)] == {"refused": "service not supported (11)"}
    assert results[(0, 0x7D1)] == {"codes": []}
    text = uds.codes_report(results)
    assert "P0301-00 (generic): confirmed, pending, failing now" in text and "module 7D1" in text


def test_module_ids():
    assert uds.module_ids(["7d1", "7E0,7A0"]) == [0x7D1, 0x7E0, 0x7A0]
    for bad in ("7E8", "7DF", "ABS", "123"):
        with pytest.raises(Exception, match="--module"):
            uds.module_ids([bad])


def test_elm_listens_for_other_modules():
    te = load("test_elm")
    fake = te.FakeElm()
    commands = []
    fake._command = (lambda orig: lambda cmd: (commands.append(cmd), orig(cmd))[1])(fake._command)
    from pandacapture.elm import Elm, ElmLink
    from pandacapture.policy import build
    link = ElmLink(Elm("COM9", serial_factory=lambda port: fake), lambda f: None)
    commands.clear()
    link.send([build(0x22, b"\xf1\x87", target=0x7D1).frame()])
    assert commands == ["ATSH7D1", "ATCRA7D9", "22F187", "ATAR", "ATSH7DF"]
