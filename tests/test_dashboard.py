import json
import urllib.error
from pathlib import Path
import time
import urllib.request

import pytest

from pandacapture import protocol as p
from pandacapture.dashboard import Dashboard, LiveState
from pandacapture import signals
from pandacapture.signals import MapError, builtin_maps, load_map, parse_map
from pandacapture.sources import ReplaySource, SimulatedSource


# A made-up map for the tests: a few signals laid out like a typical ECU broadcast
TEST_MAP = {"name": "Test car", "bitrate": 500, "signals": [
    {"key": "rpm", "id": "0x316", "byte": 2, "bits": 16, "scale": 0.25, "unit": "rpm", "display": "gauge",
     "min": 0, "max": 7000},
    {"key": "battery", "id": "0x545", "byte": 3, "scale": 0.1015625, "unit": "V"},
    {"key": "mil", "id": "0x545", "byte": 0, "bit": 1, "bits": 1, "display": "light"},
    {"key": "gear", "id": "0x112", "byte": 1, "bits": 4, "labels": {"0": "P", "14": "R"}},
    {"key": "rpm_swing", "expr": "p2p(rpm, 5)"},
]}


def sig(**kw):
    base = {"key": "x", "id": "0x100", "byte": 0}
    base.update(kw)
    return parse_map({"signals": [base]}).signals[0]


def test_little_endian_16bit():
    assert sig(byte=2, bits=16, scale=0.25).decode(bytes([0, 0x10, 0x80, 0x0C, 0, 0, 0, 0])) == 800


def test_big_endian_12bit_with_bit_offset():
    # Motorola: bytes 3-4 big-endian = 0x0ABC; skip 4 low bits -> 0xAB
    assert sig(byte=3, bits=8, bit=4, order="big").decode(bytes([0, 0, 0, 0x0A, 0xBC])) == 0xAB


def test_single_bit_and_signed():
    assert sig(byte=0, bit=1, bits=1).decode(bytes([0b10])) == 1
    assert sig(byte=0, bit=1, bits=1).decode(bytes([0b01])) == 0
    assert sig(byte=1, bits=8, signed=True).decode(bytes([0, 0xFE])) == -2


def test_short_frame_gives_none():
    assert sig(byte=6, bits=16).decode(bytes(7)) is None


def test_scale_offset_and_levels():
    s = sig(scale=0.75, offset=-48, display="gauge", min=-20, max=100, warn_above=50, alert_above=60)
    assert s.decode(bytes([160])) == 72
    assert (s.level(40), s.level(55), s.level(72)) == ("ok", "warn", "alert")


def test_lights():
    assert sig(display="light").light_on(1) and not sig(display="light").light_on(0)
    low = sig(display="light", on_below=11.8)
    assert low.light_on(11.5) and not low.light_on(12.5)


@pytest.mark.parametrize("bad, msg", [
    ({"key": "a", "byte": 0}, "needs 'id'"),
    ({"key": "a", "id": "0x100", "byte": 0, "display": "gauge"}, "min and max"),
    ({"key": "a", "id": "0x100", "byte": 0, "colour": "red"}, "unknown field"),
    ({"key": "a", "id": "zz", "byte": 0}, "isn't a number"),
    ({"key": "a", "id": "0x100", "byte": 0, "bits": 99}, "out of range"),
])
def test_map_errors(bad, msg):
    with pytest.raises(MapError, match=msg):
        parse_map({"signals": [bad]})


def test_duplicate_keys():
    with pytest.raises(MapError, match="duplicate"):
        parse_map({"signals": [{"key": "a", "id": 1, "byte": 0}, {"key": "a", "id": 2, "byte": 0}]})


def test_maps_folder_lookup(tmp_path, monkeypatch):
    folder = tmp_path / "maps"
    folder.mkdir()
    (folder / "test-car.json").write_text(json.dumps(TEST_MAP))
    monkeypatch.setattr(signals, "user_dir", lambda: folder)
    assert "test-car" in builtin_maps()
    m = load_map("test-car")
    assert m.name == "Test car" and len(m.signals) == 5
    with pytest.raises(MapError, match="No address map"):
        load_map("no-such-car")


def test_decode_rpm_mil_battery():
    state = LiveState(parse_map(TEST_MAP))
    state.feed([p.Frame(0, 0x316, bytes([0, 0x10, 0x80, 0x0C, 0, 0, 0, 0])),
                p.Frame(0, 0x545, bytes([0b10, 0, 0, 140, 0, 0, 0, 0])),
                p.Frame(0, 0x112, bytes([0, 0, 0]))], time.monotonic())
    snap = state.snapshot()["values"]
    assert snap["rpm"]["v"] == 800 and snap["mil"]["on"] and snap["battery"]["v"] == pytest.approx(14.22, abs=0.01)
    assert snap["gear"]["text"] == "P" and snap["rpm_swing"]["v"] == 0


def test_high_resolution_keeps_every_sample_and_reports_loss():
    m = parse_map({"signals": [{"key": "a", "id": "0x100", "byte": 0}]})
    state = LiveState(m)
    for i in range(10):
        state.feed([p.Frame(0, 0x100, bytes([i]))], time.monotonic())
    samples, seq, lost = state.samples_after(0, 0)
    assert [v for _, v, _ in samples] == list(range(10)) and lost == 0
    assert state.samples_after(seq, 0)[0] == []
    state.samples.clear()
    state.feed([p.Frame(0, 0x100, bytes([99]))], time.monotonic())
    samples, _, lost = state.samples_after(seq - 5, 0)
    assert lost == 5 and [v for _, v, _ in samples] == [99]


def get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def test_server_end_to_end():
    dash = Dashboard(SimulatedSource, parse_map(TEST_MAP), port=0)
    dash.start()
    try:
        assert b"PandaCapture" in get(dash.url)
        assert b'id="pinned"' in get(dash.url) and b"function togglePin" in get(dash.url)
        assert b'href="icon.svg"' in get(dash.url) and b'href="icon.svg"' in get(dash.url + "captures.html")
        assert get(dash.url + "icon.svg").startswith(b"<svg") and get(dash.url + "icon-512.png")[:4] == b"\x89PNG"
        assert json.loads(get(dash.url + "map"))["signals"][0]["key"] == "rpm"
        for _ in range(30):
            state = json.loads(get(dash.url + "state"))
            if "rpm" in state["values"]:
                break
            time.sleep(0.1)
        assert state["status"] == "live" and "rpm" in state["values"]
        # high resolution: a snapshot first, then sample batches
        with urllib.request.urlopen(dash.url + "events?mode=high", timeout=3) as r:
            seen = []
            while len(seen) < 3:
                line = r.readline().decode()
                if line.startswith("event: "):
                    seen.append(line.split()[1])
        assert seen[0] == "snapshot" and "samples" in seen
    finally:
        dash.stop()


def test_replay_source(tmp_path):
    log = tmp_path / "c.log"
    log.write_text("(10.000000) can0 316#0010800C00000000\n(10.050000) can0 316#0010000D00000000\n")
    src = ReplaySource(log, speed=10, loop=False)
    time.sleep(0.02)
    frames = src.read()
    assert [f.addr for f in frames] == [0x316, 0x316]


def test_replay_opens_zips(tmp_path):
    import zipfile
    from pandacapture.sources import SourceError
    log = "(10.000000) can0 316#0010800C00000000\n(10.050000) can0 329#0010000D00000000\n"
    share = tmp_path / "share.zip"                 # a shared capture: its log and the OBD CSV
    with zipfile.ZipFile(share, "w") as z:
        z.writestr("capture-20261006-120000.log", log)
        z.writestr("obd-20261006-120000.csv", "t,RPM\n0,800\n")
    assert len(ReplaySource(share).frames) == 2
    bundle = tmp_path / "bundle.zip"               # an exported bundle: capture.log among the tools
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr("pandacapture-bundle-x/capture.log", log)
        z.writestr("pandacapture-bundle-x/reference.log", log)
        z.writestr("pandacapture-bundle-x/tools.py", "")
    assert [f.addr for _, f in ReplaySource(bundle).frames] == [0x316, 0x329]
    several = tmp_path / "several.zip"
    with zipfile.ZipFile(several, "w") as z:
        z.writestr("a.log", log)
        z.writestr("b.log", log)
    with pytest.raises(SourceError, match=r"several logs \(a.log, b.log\): unzip it and pick one"):
        ReplaySource(several)
    empty = tmp_path / "empty.log"
    empty.write_text("# PandaCapture candump log\n")
    with pytest.raises(SourceError, match="Nothing to replay in empty.log"):
        ReplaySource(empty)


def test_developer_tools_page(tmp_path):
    dash = Dashboard(SimulatedSource, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    dash.start()
    try:
        get = lambda p: urllib.request.urlopen(dash.url + p, timeout=10).read()
        page = get("devtools.html").decode()
        # the build area a developer session drops tools into, and the hooks library it loads
        assert 'id="toolHost"' in page and 'id="buildMarker"' in page
        assert '<script src="/devtools.js"></script>' in page and 'window.PCDev' in page
        r = urllib.request.urlopen(dash.url + "devtools.js", timeout=10)
        assert "text/javascript" in r.headers["Content-Type"]
        lib = r.read().decode()
        for hook in ("onState", "onSignal", "onSamples", "onMap", "panel", "spark", "record", "marker"):
            assert hook in lib, hook
        # Live hooks share one connection per feed: a browser allows 6 to the dashboard across all its windows, and
        # one EventSource per subscriber stalled every other request, the gauges' included
        assert lib.count("new EventSource") == 1 and "EventSource" not in page
        # the tab is on the top row of the other pages, and the page links back to them
        for p in ("", "captures.html", "runs.html"):
            assert 'href="/devtools.html"' in get(p).decode(), p
        assert 'href="/"' in page and 'href="/captures.html"' in page and 'href="/runs.html"' in page
    finally:
        dash.stop()


def test_raw_sentinels_and_labels():
    s = sig(byte=0, raw_max=127)
    assert s.decode(bytes([127])) == 127 and s.decode(bytes([128])) is None
    t = sig(byte=0, scale=-1, offset=195, raw_invalid=[128])
    assert t.decode(bytes([183])) == 12 and t.decode(bytes([128])) is None
    g = sig(byte=0, bits=4, labels={"0": "P", "14": "R"})
    assert g.text(0) == "P" and g.text(3) is None


def derived_map(*exprs):
    return parse_map({"signals": [{"key": "a", "id": "0x100", "byte": 0}, {"key": "b", "id": "0x100", "byte": 1}] +
                     [{"key": f"d{i}", "expr": e} for i, e in enumerate(exprs)]})


def test_expressions():
    m = derived_map("(a - b) * 5", "a > b", "abs(b - a)", "max(a, b, 3)", "-a / 2")
    state = LiveState(m)
    state.feed([p.Frame(0, 0x100, bytes([10, 4]))], time.monotonic())
    v = {k: e["v"] for k, e in state.snapshot()["values"].items()}
    assert (v["d0"], v["d1"], v["d2"], v["d3"], v["d4"]) == (30, 1, 6, 10, -5)


def test_windowed_expressions():
    m = derived_map("p2p(a, 5)", "lo(a, 5)", "hi(a, 1)")
    state = LiveState(m)
    for t, a in ((0.0, 50), (2.0, 80), (4.0, 60), (6.5, 70)):
        state.feed([p.Frame(0, 0x100, bytes([a, 0]))], t)
    v = {k: e["v"] for k, e in state.snapshot()["values"].items()}
    # at t=6.5 the 5 s window holds 80, 60, 70 (the 50 at t=0 has aged out); the 1 s window only 70
    assert (v["d0"], v["d1"], v["d2"]) == (20, 60, 70)


def test_avg_and_windows_over_derived_signals():
    m = derived_map("avg(a, 5)", "abs(a - b)", "avg(d1, 3)", "(d2 > 5) * (a > 10)")
    state = LiveState(m)
    for t, a, b in ((0.0, 50, 50), (2.0, 80, 60), (4.0, 60, 60), (6.5, 70, 40)):
        state.feed([p.Frame(0, 0x100, bytes([a, b]))], t)
    v = {k: e["v"] for k, e in state.snapshot()["values"].items()}
    # avg(a, 5) at 6.5 s: 80, 60, 70. d1 = |a - b| is derived; its own 3 s window at 6.5 s holds 0 (4 s) and 30
    assert v["d0"] == 70 and v["d1"] == 30 and v["d2"] == 15 and v["d3"] == 1


def test_stinger_cam_lag_lights():
    from pandacapture.signals import load_map
    m = load_map("kia-stinger-33t-pcan")
    keys = {s.key: s for s in m.signals}
    for cam in ("intake_b1", "exhaust_b1", "intake_b2", "exhaust_b2"):
        assert keys[f"{cam}_slow"].color == "amber" and keys[f"{cam}_error"].color == "red"
        assert "rpm > 400" in keys[f"{cam}_error"].expr and f"avg({cam}_err, 3)" == keys[f"{cam}_lag"].expr


def test_derived_samples_stream_at_input_rate():
    m = derived_map("a * 2")
    state = LiveState(m)
    for i in range(5):
        state.feed([p.Frame(0, 0x100, bytes([i, 0]))], time.monotonic())
    samples, _, _ = state.samples_after(0, 0)
    assert [v for k, v, _ in samples if k == "d0"] == [0, 2, 4, 6, 8]


@pytest.mark.parametrize("expr, msg", [
    ("__import__('os')", "can only use"),
    ("a.real", "can only use"),
    ("open('x')", "can only use"),
    ("c + 1", "isn't a signal defined above"),
    ("p2p(a, 600)", "window"),
    ("1 + 2", "uses no signals"),
    ("a +", "valid expression"),
    ("[a]", "can only use"),
])
def test_expression_rejects(expr, msg):
    with pytest.raises(MapError, match=msg):
        derived_map(expr)


def test_derived_cannot_also_decode():
    with pytest.raises(MapError, match="can't also have"):
        parse_map({"signals": [{"key": "a", "id": 1, "byte": 0}, {"key": "b", "expr": "a", "byte": 0}]})


def test_shipped_maps_load():
    for path in signals.builtin_dir().glob("*.json"):
        m = load_map(path)
        for s in m.signals:
            j = s.to_json()
            assert ("expr" in j) != ("id" in j) and ("id" not in j or j["id"].startswith("0x"))
            # a gauge or number tile fits about 20 characters (111 px at the narrowest desktop column): longer
            # labels need a short name. Lamps' labels wrap
            assert s.display == "light" or len(s.short or s.label) <= 20, (s.key, s.short or s.label)


def test_stinger_map_cam_conventions():
    m = load_map("kia-stinger-33t-pcan")
    by = {s.key: s for s in m.signals}
    # intake (cam B) at its 195 lock = 0 advance; exhaust (cam A) at its 71 lock = 0 retard
    assert by["intake_b1_actual"].decode(bytes([0, 0, 0, 0, 0, 195, 0, 0])) == 0
    assert by["exhaust_b1_actual"].decode(bytes([0, 0, 0, 0, 71, 0, 0, 0])) == 0
    assert by["intake_b1_target"].decode(bytes([0x80, 0x80, 0x80, 0x80, 0x82, 0x82, 0x70, 0])) is None
    assert by["exhaust_b1_target"].decode(bytes([0x80, 0x80, 0x80, 0x80, 0x82, 0x82, 0x70, 0])) is None
    assert by["gear"].text(0) == "P" and by["drive_mode"].text(1) == "Sport"


def post(url, body=b"{}", ctype="application/json"):
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_controls_record_marker_map(tmp_path):
    dash = Dashboard(SimulatedSource, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    dash.start()
    try:
        assert post(dash.url + "marker")[0] == 409            # no marker without a recording
        code, j = post(dash.url + "record", b'{"on": true}')
        assert code == 200 and j["recording"]
        time.sleep(0.5)
        assert post(dash.url + "marker") == (200, {"marker": 1})
        caps = json.loads(get(dash.url + "captures"))["captures"]
        assert caps and caps[0]["recording"]
        code, j = post(dash.url + "record", b'{"on": false}')
        saved = Path(j["saved"])
        assert "# marker 1 (" in saved.read_text(encoding="utf-8")
        maps = json.loads(get(dash.url + "maps"))
        assert "kia-stinger-33t-pcan" in maps["maps"]
        v0 = json.loads(get(dash.url + "state"))["map_version"]
        assert post(dash.url + "map", b'{"name": "kia-stinger-33t-pcan"}')[0] == 200
        assert json.loads(get(dash.url + "state"))["map_version"] == v0 + 1
        assert post(dash.url + "map", b'{"name": "nope"}')[0] == 404
    finally:
        dash.stop()


def test_find_signals_from_the_page(tmp_path):
    from tests.test_match import write_capture, write_datalog
    write_capture(tmp_path / "capture-x.log", with_obd=True)
    write_datalog(tmp_path / "datalog.csv")
    m = parse_map({"signals": [{"key": "rpm", "id": "0x316", "byte": 2, "bits": 16, "scale": 0.25}]})
    dash = Dashboard(SimulatedSource, m, port=0, record_dir=tmp_path)
    dash.start()
    try:
        assert post(dash.url + "match?capture=../secret.log&ref=obd")[0] == 400   # only listed captures
        for ref, body, ctype in (("obd", b"", "application/json"),
                                 ("csv", (tmp_path / "datalog.csv").read_bytes(), "text/csv")):
            code, _ = post(dash.url + f"match?capture=capture-x.log&ref={ref}&filename=datalog.csv", body, ctype)
            assert code == 200
            for _ in range(100):
                state = json.loads(get(dash.url + "match"))
                if state["state"] != "running":
                    break
                time.sleep(0.1)
            assert state["state"] == "done", state
            cols = {c["column"]: c for c in state["results"]}
            rpm_col = "OBD RPM" if ref == "obd" else "RPM"
            assert cols[rpm_col]["matches"][0]["known"] == "rpm"
            if ref == "csv":
                best = cols["Boost kPa"]["matches"][0]
                assert best["entry"]["id"] == "0x123" and best["entry"]["source"] == "observed"
    finally:
        dash.stop()


def test_restart_note_tells_power_from_usb():
    from types import SimpleNamespace
    from pandacapture.capture import restart_note
    restarted = restart_note(SimpleNamespace(uptime=3, voltage=4.8), seconds_away=4.0)
    assert "restarted" in restarted and "4.80 V" in restarted
    assert "only the USB link" in restart_note(SimpleNamespace(uptime=900, voltage=None), seconds_away=4.0)
    assert restart_note(SimpleNamespace(), seconds_away=4.0) == ""


def test_dropout_is_logged_and_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr("pandacapture.dashboard.RECONNECT_EVERY", 0.2)
    opened = []

    def open_source():
        src = SimulatedSource(dropout_at=0.4 if not opened else 0)
        src.uptime = 2 if opened else 500          # second open: the panda had restarted
        opened.append(src)
        return src

    lines = []
    dash = Dashboard(open_source, parse_map(TEST_MAP), port=0, record=True, record_dir=tmp_path, log=lines.append)
    dash.start()
    try:
        for _ in range(100):
            if any("Reconnected" in x for x in lines):
                break
            time.sleep(0.1)
    finally:
        dash.stop()
    assert any("Panda error" in x for x in lines)
    assert any("Reconnected after" in x and "restarted" in x for x in lines), lines
    rec = "".join(p.read_text(encoding="utf-8") for p in dash.reader.saved)
    assert "# adapter error: simulated dropout" in rec and "The panda had restarted" in rec
