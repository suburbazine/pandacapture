"""The dashboard's Captures & Claude page: its routes, reference uploads, bundles, and the Claude job from
prepare to proposals, with a stand-in for the API. The gauge page keeps only Record and Marker."""

import importlib.util
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from pandacapture.dashboard import LOCAL, Dashboard
from pandacapture.signals import load_map
from pandacapture.sources import SimulatedSource


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ta = load("test_ai")


def get(url, raw=False):
    with urllib.request.urlopen(url, timeout=10) as r:
        body = r.read()
        return body if raw else json.loads(body)


def post(url, body=None, raw=False):
    data = body if raw else json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={} if raw else {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"status": e.code, **json.loads(e.read() or b"{}")}


def wait(url, until, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        state = get(url)
        if until(state):
            return state
        time.sleep(0.1)
    raise AssertionError(f"timed out: {state}")


@pytest.fixture
def dash(tmp_path, monkeypatch):
    ta.write_capture(tmp_path / "capture-20261004-101500.log")
    monkeypatch.setenv("ANTHROPIC_API_KEY", ta.KEY)
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: ta.FakeClient(ta.scripted()))
    d = Dashboard(SimulatedSource, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    d.start()
    yield d
    d.stop()


def test_pages(dash):
    gauges = get(dash.url, raw=True).decode()
    assert "findDlg" not in gauges and "Find signals" not in gauges and 'href="/captures.html"' in gauges
    assert "recBtn" in gauges and "markBtn" in gauges
    page = get(dash.url + "captures.html", raw=True).decode()
    for part in ("Captures &amp; Claude", "runFind", "Download bundle", "Claude Opus 5.5", "Claude Fable 5.1", "pandacapture apikey set"):
        assert part in page


def test_references_find_and_bundle(dash, tmp_path):
    csv = "time,RPM\n" + "\n".join(f"{1000 + i * 0.1:.1f},{800 + i}" for i in range(30))
    up = post(dash.url + "reference?filename=jb4.csv", csv.encode(), raw=True)
    assert up["name"] == "jb4.csv" and get(dash.url + "references")["references"] == [{"id": up["id"], "name": "jb4.csv"}]
    assert post(dash.url + "match?capture=capture-20261004-101500.log&ref=obd") == {"started": True}
    m = wait(dash.url + "match", lambda s: s["state"] in ("done", "error"))
    assert m["state"] == "done" and [r["column"] for r in m["results"]] == ["OBD coolant C"]
    assert post(dash.url + "match?capture=nope.log&ref=obd")["status"] == 400
    data = get(dash.url + "bundle?capture=capture-20261004-101500.log&ref=" + up["id"], raw=True)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert any(n.endswith("/tools.py") for n in names) and any(n.endswith("/jb4.csv") for n in names)
    assert ta.VIN.encode() not in data


def test_claude_flow(dash, tmp_path):
    c = get(dash.url + "claude")
    assert c["state"] == "idle" and c["key"] == {"env": True, "store": False, "ready": True} and c["local"]
    assert c["models"] == {"opus": "claude-opus-5-5", "fable": "claude-fable-5-1"}
    assert post(dash.url + "claude/start")["status"] == 400                 # nothing prepared
    assert post(dash.url + "claude/prepare", {"capture": "capture-20261004-101500.log", "model": "sonnet"})["status"] == 400
    post(dash.url + "claude/prepare", {"capture": "capture-20261004-101500.log", "model": "opus", "effort": "high",
                                       "max_cost": 1.5})
    c = wait(dash.url + "claude", lambda s: s["state"] in ("confirm", "error"))
    assert c["state"] == "confirm" and c["first_tokens"] == 4321 and c["withheld"] == ["0x5B0"]
    assert c["model"] == "claude-opus-5-5" and c["max_cost"] == 1.5 and "OBD RPM" in c["references"]
    post(dash.url + "claude/start")
    c = wait(dash.url + "claude", lambda s: s["state"] in ("done", "error"))
    assert c["state"] == "done" and [p["entry"]["key"] for p in c["proposals"]] == ["coolant_c2"]
    assert Path(c["saved"]).parent == tmp_path and json.loads(Path(c["saved"]).read_text())["map_entries"]
    everything = json.dumps(c) + json.dumps(get(dash.url + "claude"))
    assert ta.KEY not in everything and "sk-ant" not in everything


def test_cancel_before_sending(dash):
    post(dash.url + "claude/prepare", {"capture": "capture-20261004-101500.log"})
    wait(dash.url + "claude", lambda s: s["state"] == "confirm")
    assert post(dash.url + "claude/cancel")["state"] == "idle"


def test_local_only_addresses():
    assert "127.0.0.1" in LOCAL and "::1" in LOCAL and "0.0.0.0" not in LOCAL
