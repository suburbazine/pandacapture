"""Performance runs from a capture: found, timed against known physics, coached (each pointer with its chart),
compared with your best, knock told apart from a shift's or traction control's spark retard, the events, and
the Runs page and command."""

import json
import math
import urllib.request

import pytest

from pandacapture import runs as R
from pandacapture.signals import load_map

MAP = load_map("kia-stinger-33t-pcan")
SIG = {s.key: s for s in MAP.signals}
G = 9.80665
T0 = 1791000000.0


def put(frames, key, value):
    """Encode value into its signal's frame (little-endian fields only, as these are)."""
    s = SIG[key]
    data = frames.setdefault(s.can_id, bytearray(8))
    raw = int(round((value - s.offset) / s.scale)) & ((1 << s.bits) - 1)
    word = int.from_bytes(data, "little")
    shift = s.byte * 8 + s.bit
    word = (word & ~(((1 << s.bits) - 1) << shift)) | (raw << shift)
    data[:] = word.to_bytes(8, "little")


def drive(path, g=0.8, spark_dip=None, shift_retard=True, start=T0, tcs_cut=None):
    """A standing launch: 3 s still (a 1.5 s brake stand at full throttle), then full throttle at a steady `g`
    to 130 km/h with a 1-2 shift at 60 km/h, then a lift. spark_dip: (t0, t1) seconds after the launch where spark
    drops 6° at full throttle with no torque cut (knock). Every frame every 20 ms."""
    lines = ["# PandaCapture candump log (test)"]
    t, v, gear, shifted = 0.0, 0.0, 1, None
    launch = 3.0
    while t < 30:
        f = {}
        stand = 1.5 <= t < launch
        moving = t >= launch and v < 130
        if moving:
            v = min(130.0, (t - launch) * g * G * 3.6)
        if gear == 1 and v >= 60:
            gear, shifted = 2, t
        lifted = t >= launch and v >= 130
        if lifted and t > 25:
            v = max(0.0, v - 0.02 * 0.5 * G * 3.6)
        pedal = 0.0 if lifted or t < 1.5 else 99.0
        rpm = 900 if v < 1 else v * (95 if gear == 1 else 60)
        for w in ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr"):
            put(f, w, v)
        put(f, "speed_kmh", v)
        put(f, "rpm", min(rpm, 6800))
        put(f, "pedal", pedal)
        put(f, "brake_switch", 2 if stand or t < 1.5 else 1)
        put(f, "gear", gear)
        put(f, "accel_long_g", g if moving else 0.0)
        limit = 50.0 if shift_retard and shifted is not None and 0 <= t - shifted < 0.25 else 99.6
        put(f, "tcu_torque_limit", limit)
        put(f, "tqi_tcs", tcs_cut if tcs_cut and moving and t - launch < 1.0 else 99.6)
        spark = 18.0
        if limit < 95:
            spark = 2.0                                   # the shift's torque cut: spark pulled on purpose
        if spark_dip and spark_dip[0] <= t - launch < spark_dip[1]:
            spark = 12.0
        put(f, "spark", spark)
        put(f, "boost_psi", 15.0 if moving else (4.0 if stand else 0.0))
        put(f, "iat", 30)
        for can_id, data in sorted(f.items()):
            lines.append(f"({start + t:.6f}) can0 {can_id:03X}#{bytes(data).hex().upper()}")
        t = round(t + 0.02, 4)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_finds_and_times_a_standing_launch(tmp_path):
    res = R.analyze_capture(drive(tmp_path / "a.log"), MAP)
    assert len(res["runs"]) == 1
    run = res["runs"][0]
    assert run["kind"] == "standing"
    m = {x["name"]: x for x in run["metrics"]}
    a = 0.8 * G
    assert m["0-60 mph"]["value"] == pytest.approx(96.56064 / 3.6 / a, abs=0.04)
    assert m["60 ft"]["value"] == pytest.approx(math.sqrt(2 * 18.288 / a), abs=0.04)
    assert m["0-60 mph"]["rollout"] < m["0-60 mph"]["value"]
    assert R.headline(run)["name"] == "0-60 mph"
    assert [(s["from"], s["to"]) for s in run["shifts"]] == [(1, 2)]
    assert run["shifts"][0]["duration_s"] == pytest.approx(0.24, abs=0.03)     # the torque hold
    assert all(c["pass"] for c in run["checks"]), run["checks"]
    assert run["stats"]["launch_boost_psi"] == pytest.approx(15.0, abs=0.1)
    # The brake stand and the launch as events too
    kinds = [e["kind"] for e in res["events"]]
    assert "brake stand" in kinds and "launch" in kinds


def test_every_pointer_has_a_chart(tmp_path):
    run = R.analyze_capture(drive(tmp_path / "a.log", tcs_cut=40.0), MAP)["runs"][0]
    tips = {t["topic"]: t for t in run["insights"]}
    assert "traction" in tips                          # traction control cut torque
    for tip in run["insights"]:
        ch = tip["chart"]
        assert ch["title"] and ch["series"] and isinstance(ch["highlight"], list)
    tcs = next(t for t in run["insights"] if "Traction control" in t["text"])
    assert "tqi_tcs" in tcs["chart"]["series"] and tcs["chart"]["highlight"][0][0] < 1.0


def test_shift_retard_is_not_knock_but_a_real_dip_is(tmp_path):
    shift_only = R.analyze_capture(drive(tmp_path / "a.log"), MAP)["runs"][0]
    assert not any(t["topic"] == "knock" for t in shift_only["insights"])
    knock = R.analyze_capture(drive(tmp_path / "b.log", spark_dip=(3.0, 3.6)), MAP)["runs"][0]
    tip = next(t for t in knock["insights"] if t["topic"] == "knock")
    assert "6.0°" in tip["text"] and tip["chart"]["series"][0] == "spark"


def test_compared_with_your_best(tmp_path):
    folder = tmp_path / "runs"
    first = R.analyze_capture(drive(tmp_path / "fast.log", g=0.9), MAP, saved=R.saved_runs(folder))
    R.save_runs(first, folder)
    second = R.analyze_capture(drive(tmp_path / "slow.log", g=0.7, start=T0 + 3600), MAP, saved=R.saved_runs(folder))
    run = second["runs"][0]
    assert run["best_id"] == first["runs"][0]["id"] and run["best_id"] in second["bests"]
    tip = run["insights"][0]
    assert tip["topic"] == "compare" and "behind your best (0-60 mph)" in tip["text"]
    assert tip["chart"]["x"] == "mph" and tip["chart"]["highlight"]
    R.save_runs(second, folder)
    listed = R.saved_summaries(folder)
    assert [r["headline"]["name"] for r in listed] == ["0-60 mph", "0-60 mph"] and listed[0]["started"] > listed[1]["started"]
    again = R.open_saved(folder, run["id"])
    assert again["runs"][0]["id"] == run["id"] and list(again["bests"]) == [first["runs"][0]["id"]]


def test_command(tmp_path, capsys):
    path = drive(tmp_path / "a.log")
    assert R.main([str(path), "--out", str(tmp_path / "runs")]) == 0
    out = capsys.readouterr().out
    assert "0-60 mph: 3.4" in out and "Saved 1 run(s)" in out
    assert list((tmp_path / "runs").glob("run-*-standing.json"))


def test_runs_page_and_routes(tmp_path):
    from pandacapture.dashboard import Dashboard
    from pandacapture.sources import SimulatedSource
    drive(tmp_path / "capture-20261006-120000.log")
    dash = Dashboard(SimulatedSource, MAP, port=0, record_dir=tmp_path)
    dash.start()
    try:
        get = lambda p: urllib.request.urlopen(dash.url + p, timeout=10).read()
        assert b'id="runsLink"' in get("") and b"href=\"/runs.html\"" in get("")
        page = get("runs.html").decode()
        assert "Where it could be better" in page and "function tipChart" in page
        req = urllib.request.Request(dash.url + "runs/analyze", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"capture": "capture-20261006-120000.log", "mass_kg": 1900}).encode())
        res = json.loads(urllib.request.urlopen(req, timeout=30).read())
        assert res["runs"][0]["kind"] == "standing" and res["mass_kg"] == 1900
        saved = json.loads(get("runs/saved"))
        assert saved["runs"][0]["id"] == res["runs"][0]["id"]
        one = json.loads(get("runs/run?id=" + res["runs"][0]["id"]))
        assert one["runs"][0]["samples"]
    finally:
        dash.stop()
