"""The local analysis tools: markers, what moved, bits, multiplexing, checksums, co-changes, the reference
search, map signals and clock-aligned logs as references, and the header kept private."""

import math

import pytest

from pandacapture import analysis as an
from pandacapture.analysis import Analysis, call
from pandacapture.signals import load_map

SERIAL = "3a0018000a51393432343034"
T0 = 1791000000.0


def build(tmp_path):
    lines = ["# PandaCapture candump log", f"# source: Grey Panda {SERIAL}, firmware X", "# started: 2026-10-04"]
    csv = ["time,RPM,Coolant"]
    for i in range(600):
        t = T0 + i * 0.01
        rpm = int(800 + 1200 * math.sin(math.pi * (i - 160) / 120)) if 160 <= i < 280 else 800
        raw = rpm * 4
        coolant = 80 + i // 60
        if i == 150:
            lines.append(f"# marker 1 ({t:.6f})")
        if i == 300:
            lines.append(f"# marker 2 ({t:.6f})")
        lines.append(f"({t:.6f}) can0 316#0010{raw & 0xFF:02X}{raw >> 8:02X}00000000")
        lamp = 0x20 if i >= 300 else 0x00
        lines.append(f"({t + 0.001:.6f}) can0 4F1#0000{lamp:02X}0000000000")
        page = i % 2
        b1 = 0x10 if page == 0 else coolant + 40
        lines.append(f"({t + 0.002:.6f}) can0 2B0#{page:02X}{b1:02X}000000000000")
        body = bytes([i & 0x0F, raw & 0xFF, raw >> 8, 0x11, 0x22, 0, 0])
        x = 0
        for b in body:
            x ^= b
        lines.append(f"({t + 0.003:.6f}) can0 3A0#{(body + bytes([x])).hex().upper()}")
        pedal = 40 + (i - 300) if 300 <= i < 450 else 40
        lines.append(f"({t + 0.004:.6f}) can0 2C0#00{pedal:02X}000000000000")
        if i % 10 == 0:
            lines.append(f"({t + 0.005:.6f}) can0 7E8#04410C{raw >> 8:02X}{raw & 0xFF:02X}000000")
            csv.append(f"{t:.3f},{rpm},{coolant}")
    cap = tmp_path / "capture.log"
    cap.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ref = tmp_path / "obd-poll.csv"
    ref.write_text("\n".join(csv) + "\n", encoding="utf-8")
    return cap, ref


@pytest.fixture
def a(tmp_path):
    cap, ref = build(tmp_path)
    return Analysis(cap, load_map("kia-stinger-33t-pcan"), ref_path=ref, log=lambda s: None)


def fld(can_id, byte, bits=8, order="little", **kw):
    return {"can_id": can_id, "byte": byte, "bit": 0, "bits": bits, "order": order, "signed": False, **kw}


def test_markers_and_privacy(a):
    m = call(a, "markers", {})["markers_and_events"]
    assert [(e["what"], e["t"]) for e in m] == [("marker 1", 1.5), ("marker 2", 3.0)]
    text = a.overview()
    assert SERIAL not in text and "Grey Panda" not in text
    assert "marker 1 at 1.5 s" in text and "Coolant" in text and "map:<key>" in text


def test_clock_aligned_log_and_map_references(a):
    assert a.columns["Coolant"].source == "obd-poll.csv" and a.columns["Coolant"].rpm == "RPM"
    r = call(a, "test_field", {**fld("0x3A0", 1, 16), "reference": "map:rpm"})
    assert r["r"] > 0.999 and abs(r["scale"] - 0.25) < 1e-3
    r = call(a, "test_field", {**fld("0x3A0", 1, 16), "reference": "RPM"})
    assert r["r"] > 0.999
    with pytest.raises(ValueError, match="no reference"):
        call(a, "test_field", {**fld("0x3A0", 1), "reference": "Boost"})


def test_search_references(a):
    top = call(a, "search_references", {"reference": "OBD RPM", "top": 3})["results"]
    assert {(x["can_id"], x["byte"], x["bits"]) for x in top[:2]} == {("0x316", 2, 16), ("0x3A0", 1, 16)}
    assert abs(top[0]["scale"] - 0.25) < 1e-3


def test_what_moved_after_a_marker(a):
    moved = call(a, "what_moved", {"start_s": 3.0, "end_s": 4.5, "baseline_end_s": 1.5})["moved"]
    assert moved[0]["can_id"] == "0x2C0" and moved[0]["byte"] == 1 and moved[0]["baseline_max"] == 40
    assert not any(m["can_id"] == "0x3A0" and m["byte"] == 0 for m in moved)    # a counter is left out


def test_bits_mux_checksum(a):
    bits = call(a, "bit_stats", {"can_id": "0x4F1", "byte": 2})["bits"]
    assert bits[5]["toggles"] == 1 and abs(bits[5]["first_toggle_s"] - 3.0) < 0.02
    assert all(b["toggles"] == 0 for b in bits if b["bit"] != 5)
    mux = call(a, "mux_check", {"can_id": "0x2B0"})["selectors"][0]
    assert mux["values"] == 2 and mux["bytes_differing_between_pages"] >= 1
    best = call(a, "checksum_check", {"can_id": "0x3A0", "byte": 7})["best"][0]
    assert best["checksum"] == "xor of the other bytes" and best["matches"] == 1.0


def test_co_changes_and_frames(a):
    related = call(a, "co_changes", {**fld("0x316", 2, 16), "window_ms": 15})["related"]
    assert related[0]["can_id"] == "0x3A0" and related[0]["share_of_changes_near"] > 0.7   # its copy of RPM
    always = next(r for r in related if r["can_id"] == "0x2B0")      # changes on every frame: near anything
    assert always["by_chance"] == 1.0 and always["excess"] <= 0
    w = call(a, "frames_window", {"can_id": "0x4F1", "start_s": 2.98, "end_s": 3.02, "max_frames": 10})
    assert w["frames_in_window"] == 4 and w["frames"][-1].endswith("00 00 20 00 00 00 00 00")
    s = call(a, "field_series", {**fld("0x2C0", 1), "start_s": 0, "end_s": -1, "points": 1000})
    assert len(s["raw"]) == an.MAX_POINTS
    assert "notes" not in call(a, "map_signal", {"key": "rpm"}) and call(a, "map_signal", {"key": "rpm"})["id"]


def test_only_read_tools(a):
    for name in ("propose_signal", "send", "__init__", "check_signal"):
        with pytest.raises(ValueError, match="no tool"):
            call(a, name, {})
    assert {t["name"] for t in an.TOOLS} == an.READ_TOOLS and len(an.TOOLS) == 13


def test_one_answer_per_row_log(tmp_path):
    # pandacapture obd writes one answer per row, so each column is mostly blank. Every column keeps only its
    # own rows: otherwise the gaps leave no neighboring pairs, and r_changes and r_with_rpm_held come out None
    cap, _ = build(tmp_path)
    rows = ["time,RPM,Coolant"]
    for i in range(0, 600, 10):
        t = T0 + i * 0.01
        rpm = int(800 + 1200 * math.sin(math.pi * (i - 160) / 120)) if 160 <= i < 280 else 800
        rows += [f"{t:.3f},{rpm},", f"{t + 0.005:.3f},,{80 + i // 60}"]
    ref = tmp_path / "obd-one-per-row.csv"
    ref.write_text("\n".join(rows) + "\n", encoding="utf-8")
    a = Analysis(cap, load_map("kia-stinger-33t-pcan"), ref_path=ref, log=lambda s: None)
    assert len(a.columns["RPM"].times) == 60 and None not in a.columns["RPM"].values
    r = call(a, "test_field", {**fld("0x3A0", 1, 16), "reference": "RPM"})
    assert r["r"] > 0.999 and r["r_changes"] > 0.99
    r = call(a, "test_field", {**fld("0x2C0", 1), "reference": "Coolant"})
    assert "r_with_rpm_held" in r
    top = call(a, "search_references", {"reference": "RPM", "top": 1})["results"][0]
    assert top["r_changes"] is not None
