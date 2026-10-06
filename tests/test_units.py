"""US or metric: conversions and the {kind:value:decimals} tokens (the same as PandaCapture Android's), and the
runs following the setting: results, headline, the launch event, the coaching's text, a capture read again,
and the shift points."""

import json
import urllib.request
from pathlib import Path

import pytest

from pandacapture import runs as R
from pandacapture import units as U
from tests.test_runs import MAP, T0, drive

ANDROID_BOOST = Path(__file__).resolve().parents[2] / "PandaCapture Android" / "captures" / "panda-20261005-234311_boost.log"


def test_conversions():
    assert U.show(96.56064, "km/h", 0) == "60 mph" and U.show(96.56064, "km/h", 0, "metric") == "97 km/h"
    assert U.show(46.0, "°C", 0) == "115 °F" and U.show(46.0, "°C", 0, "metric") == "46 °C"
    assert U.show(20.4, "psi", 1, "metric") == "1.41 bar" and U.show(20.4, "psi", 1) == "20.4 psi"
    assert U.show(140.6, "kPa", 0) == "20.4 psi" and U.show(140.6, "kPa", 0, "metric") == "1.41 bar"
    assert U.show(1940, "kg", 0) == "4,277 lb" and U.show(18.288, "m", 0) == "60 ft"
    assert U.show(18.288, "m", 1, "metric") == "18.3 m" and U.show(18.288, "ft", 0, "metric") == "5.6 m" and U.show(300, "hp", 0, "metric") == "224 kW"
    assert U.show(5400, "rpm", 0, "metric") == "5,400 rpm" and U.show(12.5, "%", 1) == "12.5 %"
    assert U.system_of("Metric") == "metric" and U.system_of(None) == "us"


def test_tokens():
    assert U.psi(20.4) == "{psi:20.4000:1}" and U.kmh(96.56064) == "{kmh:96.5606:0}" and U.celsius(46) == "{c:46.0000:0}"
    text = f"Boost {U.psi(20.4)} at {U.kmh(96.56064)}, intake {U.celsius(46)}, {U.token('m', 3.2, 1)} off"
    assert U.render(text) == "Boost 20.4 psi at 60 mph, intake 115 °F, 10.5 ft off"
    assert U.render(text, "metric") == "Boost 1.41 bar at 97 km/h, intake 46 °C, 3.2 m off"
    assert U.render("Older text, 20 psi, no tokens") == "Older text, 20 psi, no tokens"


def test_results_and_headline_follow_the_units():
    run = R.analyze_capture(drive(Path(__import__("tempfile").mkdtemp()) / "a.log"), MAP)["runs"][0]
    names = [m["name"] for m in run["metrics"]]
    assert "0-60 mph" in names and "0-100 km/h" in names            # each run measures both
    assert R.headline(run)["name"] == "0-60 mph" and R.headline(run, "metric")["name"] == "0-100 km/h"
    us = [m["name"] for m in R.shown(run["metrics"], "us")]
    metric = [m["name"] for m in R.shown(run["metrics"], "metric")]
    assert "0-100 km/h" not in us and "60 ft" in us and "0-60 mph" not in metric and "60 ft" in metric
    only_kmh = [{"name": "100-200 km/h", "value": 9.0, "unit": "s", "standard": True}]
    assert R.shown(only_kmh, "us") == only_kmh                        # none in the units: all of them


def test_metric_launch_and_coaching(tmp_path):
    res = R.analyze_capture(drive(tmp_path / "a.log", tcs_cut=40.0), MAP, units="metric")
    launch = next(e for e in res["events"] if e["kind"] == "launch")
    assert launch["summary"].startswith("0-100 km/h ")
    us = R.analyze_capture(drive(tmp_path / "b.log", tcs_cut=40.0), MAP)
    assert next(e for e in us["events"] if e["kind"] == "launch")["summary"].startswith("0-60 mph ")
    tcs = next(t["text"] for t in us["runs"][0]["insights"] if "Traction control" in t["text"])
    assert "{kmh:" in tcs and " mph)" in U.render(tcs) and " km/h)" in U.render(tcs, "metric")
    stand = next(e for e in us["events"] if e["kind"] == "brake stand")["summary"]
    assert "{psi:" in stand and "bar boost at release" in U.render(stand, "metric")
    check = next(c for c in us["runs"][0]["checks"] if c["id"] == "speeds_agree")
    assert U.render(check["detail"]).endswith("more than 2 mph apart")


def test_report_in_metric(tmp_path, capsys):
    assert R.main([str(drive(tmp_path / "a.log")), "--out", str(tmp_path / "runs"), "--units", "metric"]) == 0
    out = capsys.readouterr().out
    assert "0-100 km/h: 3.5" in out and "0-60 mph" not in out and "up to 130 km/h" in out and "60 ft: " in out


def test_a_capture_read_again_isnt_compared_with_itself(tmp_path):
    folder = tmp_path / "runs"
    log = drive(tmp_path / "a.log")
    first = R.analyze_capture(log, MAP, saved=R.saved_runs(folder))
    R.save_runs(first, folder)
    assert R.found_text(first) == "a.log: 1 run found."
    again = R.analyze_capture(log, MAP, saved=R.saved_runs(folder))
    run = again["runs"][0]
    assert run["best_id"] is None and not any(t["topic"] == "compare" for t in run["insights"])
    assert R.found_text(again) == "a.log: 1 run read again (updated)."
    R.save_runs(again, folder)
    other = R.analyze_capture(drive(tmp_path / "b.log", g=0.7, start=T0 + 3600), MAP, saved=R.saved_runs(folder))
    assert R.found_text(other) == "b.log: 1 run found." and other["runs"][0]["best_id"] == run["id"]


def shift(before, after, rpm, cross):
    return {"from": 2, "to": 3, "t": 3.0, "rpm": rpm, "kmh": 88.0, "duration_s": 0.2, "g_before": before,
            "g_after": after, "cross_rpm": cross}


def test_shift_advice():
    coach = R.Coach.__new__(R.Coach)
    advice = coach._shift_advice
    assert advice(shift(0.69, 0.71, 5722, 5481)) is None                       # within 5 %: right
    late = advice(shift(0.474, 0.533, 5596, 5418))
    assert late.startswith("2-3 at 5,596 rpm came late: 2nd had fallen to 0.47 g, under the 0.53 g 3rd gives")
    assert "from about 5,418 rpm" in late
    assert advice(shift(0.474, 0.533, 5596, 5500)) is None                     # under 150 rpm off
    early = advice(shift(0.80, 0.60, 5500, 6000))
    assert "They'd meet at about 6,000 rpm" in early
    assert "toward the limiter" in advice(shift(0.80, 0.60, 5500, None))
    assert advice(shift(0.80, 0.60, 6400, None)) is None                       # at the top already
    assert advice(shift(0.80, 0.60, 5500, 5600)) is None


@pytest.mark.skipif(not ANDROID_BOOST.is_file(), reason="capture not here")
def test_shift_points_on_a_real_pull_match_android():
    run = R.analyze_capture(ANDROID_BOOST, MAP)["runs"][0]
    one_two, two_three = run["shifts"]
    assert (one_two["rpm"], one_two["g_before"], one_two["g_after"]) == (5722, pytest.approx(0.69, abs=0.005), pytest.approx(0.71, abs=0.01))
    assert two_three["rpm"] == 5596 and two_three["cross_rpm"] == 5418
    shifts = [t["text"] for t in run["insights"] if t["topic"] == "shifts"]
    assert len(shifts) == 1 and "2-3 at 5,596 rpm came late" in shifts[0]


def test_the_pages_have_the_setting(tmp_path):
    from pandacapture.dashboard import Dashboard
    from pandacapture.sources import SimulatedSource
    drive(tmp_path / "capture-20261006-120000.log")
    dash = Dashboard(SimulatedSource, MAP, port=0, record_dir=tmp_path)
    dash.start()
    try:
        get = lambda path: urllib.request.urlopen(dash.url + path, timeout=10).read().decode()
        js = get("units.js")
        assert "window.PCUnits" in js and "{(kmh|psi|c|m|kg|hp|lbft)" in js.replace("\\", "")
        for page in ("", "runs.html", "captures.html"):
            assert '<script src="/units.js"></script>' in get(page)
        assert 'id="units"' in get("") and 'id="units"' in get("runs.html")
        req = urllib.request.Request(dash.url + "runs/analyze", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"capture": "capture-20261006-120000.log", "units": "metric"}).encode())
        res = json.loads(urllib.request.urlopen(req, timeout=30).read())
        assert next(e for e in res["events"] if e["kind"] == "launch")["summary"].startswith("0-100 km/h")
        saved = json.loads(get("runs/saved?units=metric"))["runs"][0]
        assert saved["headline"]["name"] == "0-100 km/h" and any(m["name"] == "0-60 mph" for m in saved["metrics"])
    finally:
        dash.stop()
