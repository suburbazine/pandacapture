import math

import pytest

from pandacapture import match
from pandacapture import protocol as p
from pandacapture.signals import parse_map

MAP = parse_map({"signals": [{"key": "rpm", "id": "0x316", "byte": 2, "bits": 16, "scale": 0.25}]})


def write_capture(path, seconds=40, rate=50, with_obd=False):
    """A fake bus: RPM on 0x316, a 'boost' on 0x123 byte 1 (= kPa x 2), a drifting temp on 0x200
    byte 0, and optionally OBD RPM answers on 0x7E8."""
    lines = ["# PandaCapture candump log"]
    t0 = 1000.0
    for i in range(seconds * rate):
        s = i / rate
        rpm = 800 + 2500 * max(0.0, math.sin(s / 3)) + 300 * math.sin(s * 1.7)
        boost = 100 + 60 * max(0.0, math.sin(s / 3 + 0.4)) + 10 * math.sin(s * 2.3)
        temp = 80 + s / 4
        raw = int(rpm * 4)
        t = t0 + s
        lines.append(f"({t:.6f}) can0 316#0000{raw & 0xFF:02X}{raw >> 8:02X}00000000")
        lines.append(f"({t:.6f}) can0 123#00{int(boost * 2) & 0xFF:02X}000000000000")
        lines.append(f"({t:.6f}) can0 200#{int(temp) & 0xFF:02X}00")
        if with_obd and i % 5 == 0:
            r = int(rpm * 4)
            lines.append(f"({t + 0.001:.6f}) can0 7E8#04410C{r >> 8:02X}{r & 0xFF:02X}AAAA")
    path.write_text("\n".join(lines) + "\n")
    return t0


def write_jb4(path, offset=3.0, unit=0.1, seconds=30):
    """A JB4-style CSV: settings rows, then a header with timestamp in tenths of a second."""
    rows = ["Firmware,Interface,VIN", "21/36,Android,", "", "timestamp,RPM,Boost kPa,Temp,Flat"]
    for k in range(int(seconds * 8)):
        tr = k / 8  # reference seconds
        s = offset + tr
        rpm = 800 + 2500 * max(0.0, math.sin(s / 3)) + 300 * math.sin(s * 1.7)
        boost = 100 + 60 * max(0.0, math.sin(s / 3 + 0.4)) + 10 * math.sin(s * 2.3)
        rows.append(f"{tr / unit:.2f},{rpm:.0f},{boost:.1f},{80 + s / 4:.1f},5")
    path.write_text("\n".join(rows) + "\n")


def test_aligns_and_finds_field(tmp_path):
    write_capture(tmp_path / "c.log")
    write_jb4(tmp_path / "jb4.csv", offset=3.0)
    out = []
    results, (unit, off) = match.run(tmp_path / "c.log", tmp_path / "jb4.csv", MAP, log=out.append)
    assert unit == 0.1 and off == pytest.approx(3.0, abs=0.05)
    best = results["Boost kPa"][0]
    assert (best.candidate.can_id, best.candidate.byte, best.candidate.bits) == (0x123, 1, 8)
    assert best.scale == pytest.approx(0.5, rel=0.03)
    assert results["RPM"][0].known == "rpm"
    assert "Flat" not in results  # constant columns are skipped


def test_drift_is_flagged(tmp_path):
    write_capture(tmp_path / "c.log")
    write_jb4(tmp_path / "jb4.csv")
    results, _ = match.run(tmp_path / "c.log", tmp_path / "jb4.csv", MAP, log=lambda s: None, min_r=0.5)
    temp = results["Temp"][0]
    assert temp.candidate.can_id == 0x200  # the real one is found
    # a field that only shares a slow drift with the column shows weak change correlation
    for m in results["Temp"][1:]:
        if m.candidate.can_id != 0x200:
            assert match.verdict(m) in ("drift only?", "follows RPM?", "")


def test_unaligned_logs_are_refused(tmp_path):
    write_capture(tmp_path / "c.log")
    (tmp_path / "jb4.csv").write_text("timestamp,RPM\n" + "\n".join(f"{i},{(i * 7919) % 5000}" for i in range(200)))
    with pytest.raises(match.MatchError, match="line the logs up"):
        match.run(tmp_path / "c.log", tmp_path / "jb4.csv", MAP, log=lambda s: None)


def test_obd_answers_single_and_multi_frame():
    frames = [
        (0.0, p.Frame(0, 0x7E8, bytes.fromhex("04410C1AF8AAAAAA"))),             # RPM 1726
        (0.1, p.Frame(0, 0x7E8, bytes.fromhex("100E410C08A40E9F"))),             # first frame, 14 bytes
        (0.11, p.Frame(0, 0x7E8, bytes.fromhex("2111220F5B492506"))),
        (0.12, p.Frame(0, 0x7E8, bytes.fromhex("2280AAAAAAAAAAAA"))),
    ]
    a = match.obd_answers(frames)
    assert [v for _, v in a["OBD RPM"]] == [1726, 0x08A4 / 4]
    assert a["OBD timing advance deg"][0][1] == 0x9F / 2 - 64
    assert a["OBD throttle %"][0][1] == pytest.approx(0x22 * 100 / 255)
    assert a["OBD IAT C"][0][1] == 0x5B - 40


def test_obd_reference_mode(tmp_path):
    write_capture(tmp_path / "c.log", with_obd=True)
    results, (unit, off) = match.run(tmp_path / "c.log", None, MAP, log=lambda s: None)
    assert (unit, off) == (1.0, 0.0)
    assert results["OBD RPM"][0].known == "rpm"


def test_dedupe_keeps_one_per_byte():
    c = lambda b, bits: match.Candidate(0x123, b, 0, bits, "little", False)
    ms = [match.Match("x", c(1, 8), 0.99, 0.9, 0.9, 1, 0, ""), match.Match("x", c(0, 16), 0.99, 0.9, 0.9, 1, 0, ""),
          match.Match("x", c(3, 8), 0.95, 0.9, 0.9, 1, 0, "")]
    assert [m.candidate.byte for m in match.dedupe(ms, 5)] == [1, 3]
