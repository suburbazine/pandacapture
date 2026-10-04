"""Capture bundles: what's scrubbed (the header, text-carrying ids, the VIN in diagnostic answers), what's
kept (frames, markers, OBD mode 01 exchanges, long ones included), and that the bundled tools run on their own."""

import json
import subprocess
import sys
import zipfile

from pandacapture.bundle import scrub, write_bundle
from pandacapture.signals import load_map

SERIAL = "3a0018000a51393432343034"
VIN = "KNAE55LC1J6000000"


def isotp(can_id, payload):
    if len(payload) <= 7:
        return [(can_id, bytes([len(payload)]) + payload + bytes(7 - len(payload)))]
    out = [(can_id, bytes([0x10 | len(payload) >> 8, len(payload) & 0xFF]) + payload[:6])]
    rest, seq = payload[6:], 1
    while rest:
        out.append((can_id, bytes([0x20 | seq]) + rest[:7] + bytes(7 - len(rest[:7]))))
        rest, seq = rest[7:], (seq + 1) & 0x0F
    return out


def capture(path):
    t = 1791000000.0
    lines = ["# PandaCapture candump log", f"# source: Grey Panda {SERIAL}, firmware X", "# started: 2026-10-04"]

    def add(can_id, data):
        nonlocal t
        t += 0.001
        lines.append(f"({t:.6f}) can0 {can_id:03X}#{data.hex().upper()}")

    for i in range(300):
        raw = (800 + 4 * i) * 4
        add(0x316, bytes([0, 0x10, raw & 0xFF, raw >> 8, 0, 0, 0, 0]))
        if i % 3 == 0:
            add(0x5B0, bytes([i // 3 % 3]) + VIN.encode()[(i // 3 % 3) * 7:(i // 3 % 3) * 7 + 7].ljust(7))
        if i == 100:
            lines.append(f"# marker 1 ({t:.6f})")
        if i % 10 == 0:
            add(0x7E0, bytes([3, 0x01, 0x0C, 0x05, 0, 0, 0, 0]))                       # two PIDs in one request
            for can_id, data in isotp(0x7E8, bytes([0x41, 0x0C, raw >> 8, raw & 0xFF, 0x05, 120])):
                add(can_id, data)
            for can_id, data in isotp(0x7E8, bytes([0x41, 0x0C, raw >> 8, raw & 0xFF, 0x05, 120, 0x0D, 0])):
                add(can_id, data)                                                        # a long one
                if data[0] >> 4 == 1:
                    add(0x7E0, bytes([0x30, 0, 0, 0, 0, 0, 0, 0]))
        if i == 150:
            add(0x7DF, bytes([2, 0x09, 0x02, 0, 0, 0, 0, 0]))
            for can_id, data in isotp(0x7E8, b"\x49\x02\x01" + VIN.encode()):           # the VIN, mode 09
                add(can_id, data)
            add(0x7E0, bytes([3, 0x22, 0xF1, 0x90, 0, 0, 0, 0]))
            for can_id, data in isotp(0x7E8, b"\x62\xf1\x90" + VIN.encode()):           # the VIN, UDS
                add(can_id, data)
    lines.append(f"# summary: 0x5B0 last data {VIN.encode()[:7].hex()} from {SERIAL}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_scrub(tmp_path):
    text, summary = scrub(capture(tmp_path / "c.log"))
    assert SERIAL not in text and VIN not in text
    assert VIN.encode()[:5].hex().upper() not in text and VIN.encode()[7:12].hex().upper() not in text
    assert summary["withheld_ids"] == ["0x5B0"] and summary["markers_and_events"] == 1
    assert "# marker 1 (" in text and text.count(" 316#") == 300
    assert "7E8#10" in text and "7E0#30" in text                     # the long mode 01 answer and its flow control
    assert "09020000" not in text and "22F190" not in text


def test_bundle_runs_on_its_own(tmp_path):
    out = tmp_path / "b.zip"
    info = write_bundle(capture(tmp_path / "c.log"), load_map("kia-stinger-33t-pcan"), out)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        everything = b"".join(z.read(n) for n in names)
        z.extractall(tmp_path / "x")
    assert SERIAL.encode() not in everything and VIN.encode() not in everything
    root = next((tmp_path / "x").iterdir())
    assert {"README.md", "CLAUDE.md", "tools.py", "capture.log", "map.json", "bundle.json"} <= {p.name for p in root.iterdir()}
    assert info["withheld_ids"] == ["0x5B0"]

    def tools(*args):
        r = subprocess.run([sys.executable, str(root / "tools.py"), *args], capture_output=True, text=True, cwd=root)
        assert r.returncode == 0, r.stderr
        return r.stdout

    overview = tools()
    assert "OBD RPM" in overview and "marker 1" in overview
    fit = json.loads(tools("test_field", "can_id=0x316", "byte=2", "bit=0", "bits=16", "order=little", "signed=false",
                           "reference=OBD RPM"))
    assert fit["r"] > 0.999 and abs(fit["scale"] - 0.25) < 1e-3
    check = json.loads(tools("check_signal", json.dumps({"key": "rpm2", "id": "0x316", "byte": 2, "bits": 16,
                                                          "scale": 0.25}), "OBD RPM"))
    assert check["accepted"] and check["check"]["r"] > 0.999
    # The bundled modules alone: no USB library, nothing from outside the bundle
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); import pandacapture.analysis, pandacapture.signals; "
             "assert 'usb1' not in sys.modules; "
             "assert pandacapture.__file__.startswith(sys.argv[1]), pandacapture.__file__")
    r = subprocess.run([sys.executable, "-c", probe, str(root)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "tools.py" in (root / "README.md").read_text(encoding="utf-8")
