"""pandacapture analyze, with a scripted stand-in for the API: tools, proposals and their checks, building a
map from them, the turn, cost and refusal stops, text ids withheld, the model lock, and the API key kept out of
everything."""

import json
import types

import pytest

from pandacapture import ai
from pandacapture.analysis import Analysis
from pandacapture.signals import load_map

VIN = "KNAE55LC1J6000000"
KEY = "sk-ant-api03-TESTKEY-not-real-0123456789"


def write_capture(path):
    """A short P-CAN-like capture: RPM on 0x316 bytes 2-3, coolant on 0x329 byte 1, a counter on 0x2A0,
    a text-carrying id 0x5B0 (VIN pieces), and the engine computer's OBD answers for RPM and coolant."""
    lines = ["# PandaCapture candump log (test)"]
    t = 1000.0
    vin = VIN.encode()
    for i in range(600):
        t += 0.01
        rpm = 800 + 1500 * (1 if (i // 100) % 2 else 0) * min(1.0, (i % 100) / 30)
        raw = int(rpm * 4)
        coolant = 80 + i // 60
        lines.append(f"({t:.6f}) can0 316#0010{raw & 0xFF:02X}{raw >> 8:02X}00000000")
        lines.append(f"({t + 0.001:.6f}) can0 329#00{coolant + 40:02X}000000000000")
        lines.append(f"({t + 0.002:.6f}) can0 2A0#{i & 0xFF:02X}55010000000000")
        if i % 5 == 0:
            chunk = vin[(i // 5 % 3) * 7:(i // 5 % 3) * 7 + 7].ljust(7, b" ")
            lines.append(f"({t + 0.003:.6f}) can0 5B0#{i // 5 % 3:02X}{chunk.hex().upper()}")
        if i % 10 == 0:
            lines.append(f"({t + 0.004:.6f}) can0 7DF#02010C0000000000")
            lines.append(f"({t + 0.005:.6f}) can0 7E8#04410C{raw >> 8:02X}{raw & 0xFF:02X}000000")
            lines.append(f"({t + 0.006:.6f}) can0 7DF#0201050000000000")
            lines.append(f"({t + 0.007:.6f}) can0 7E8#034105{coolant + 40:02X}00000000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def capture(tmp_path):
    return write_capture(tmp_path / "capture.log")


@pytest.fixture
def analysis(capture):
    return Analysis(capture, load_map("kia-stinger-33t-pcan"), log=lambda s: None)


def block(kind, **kw):
    return types.SimpleNamespace(type=kind, **kw)


def response(*content, stop="tool_use", usage=(1000, 200, 0, 0)):
    i, o, w, r = usage
    return types.SimpleNamespace(content=list(content), stop_reason=stop, stop_details=None,
                                 usage=types.SimpleNamespace(input_tokens=i, output_tokens=o,
                                                             cache_creation_input_tokens=w,
                                                             cache_read_input_tokens=r))


class FakeClient:
    """Plays back scripted responses and records the requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = self

    def create(self, **params):
        self.requests.append(json.loads(json.dumps(params, default=lambda o: vars(o))))
        return self.script.pop(0)

    def count_tokens(self, **params):
        return types.SimpleNamespace(input_tokens=4321)


def call(id_, tool, **inp):
    return block("tool_use", id=id_, name=tool, input=inp)


GOOD = dict(key="coolant_c2", label="Coolant (second copy)", group="Temperatures", display="gauge", min=-40, max=130,
            can_id="0x329", byte=1, bit=0, bits=8, order="little", signed=False, scale=1, offset=-40, unit="°C",
            reference="OBD coolant C", confidence="high", reasoning="Tracks the OBD coolant answer exactly.")


def scripted():
    return [
        response(block("text", text="Looking at 0x329."),
                 call("t1", "id_detail", can_id="0x329"),
                 call("t2", "test_field", can_id="0x329", byte=1, bit=0, bits=8, order="little", signed=False,
                      reference="OBD coolant C")),
        response(call("t3", "propose_signal", **{**GOOD, "can_id": "0x5B0"}),          # withheld id
                 call("t4", "propose_signal", **{**GOOD, "key": "rpm"}),                # the map has it
                 call("t5", "id_detail", can_id="0x7E8")),                              # diagnostic: not offered
        response(call("t6", "propose_signal", **{**GOOD, "min": None}),               # a gauge needs a range
                 call("t7", "propose_signal", **GOOD)),
        response(call("t8", "build_map", name="Kia Stinger 3.3T (P-CAN)", keys=["coolant_c2"], notes="x"),
                 call("t9", "build_map", name="Stinger + analysis", keys=["coolant_c2", "boost_x"], notes="x"),
                 call("t10", "build_map", name="Stinger + analysis", keys=["coolant_c2"],
                      notes="A second coolant reading, matched to OBD.")),
        response(block("text", text="Proposed coolant_c2 (high confidence)."), stop="end_turn"),
    ]


def test_overview_withholds_text_and_diagnostics(analysis):
    text = analysis.overview()
    assert analysis.withheld == [0x5B0]
    assert "0x5B0" not in text.split("Broadcast ids")[1].split("Signals")[0]
    assert "0x7E8" not in text and "0x7DF" not in text
    assert VIN[:5] not in text and VIN.encode()[:5].hex().upper() not in text
    assert "OBD RPM" in text and "OBD coolant C" in text


def test_tools(analysis):
    d = analysis.id_detail({"can_id": "0x2A0"})
    assert d["bytes"][0]["kind"] == "counter" and d["bytes"][1] == {"min": 0x55, "max": 0x55, "distinct": 1,
                                                                     "changes_per_s": 0.0, "kind": "constant"}
    r = analysis.test_field({"can_id": "0x316", "byte": 2, "bit": 0, "bits": 16, "order": "little", "signed": False,
                             "reference": "OBD RPM"})
    assert r["r"] > 0.999 and abs(r["scale"] - 0.25) < 1e-3
    s = analysis.field_series({"can_id": "0x329", "byte": 1, "bit": 0, "bits": 8, "order": "little", "signed": False,
                               "points": 500})
    assert len(s["raw"]) == 120                      # capped
    for bad in ({"can_id": "0x5B0"}, {"can_id": "0x7E8"}, {"can_id": "0x999"}):
        with pytest.raises(ValueError):
            analysis.id_detail(bad)


def test_session(analysis):
    client = FakeClient(scripted())
    s = ai.Session(client, "claude-opus-5-5", analysis, log=lambda m: None).run()
    assert [p["entry"]["key"] for p in s.proposals] == ["coolant_c2"]
    p = s.proposals[0]
    assert p["check"]["r"] > 0.999 and p["check"]["rms_error"] < 0.01
    assert p["entry"]["source"] == "observed" and p["entry"]["id"] == "0x329"
    assert p["entry"]["group"] == "Temperatures" and p["entry"]["display"] == "gauge"
    assert (p["entry"]["min"], p["entry"]["max"]) == (-40, 130)
    assert len(s.rejected) == 2 and "0x5B0" in s.rejected[0]["why"] and "min and max" in s.rejected[1]["why"]
    # build_map: a new name, accepted keys only, and the result passes the map rules
    b = s.built_map
    assert b["name"] == "Stinger + analysis" and b["signals"][-1] == p["entry"]
    assert len(b["signals"]) == len(analysis.map.signals) + 1 and "A second coolant reading" in b["notes"]
    assert b["notes"].startswith("Powertrain/chassis bus")                  # the current map's own notes kept
    from pandacapture.signals import parse_map
    assert parse_map(b).signals[-1].display == "gauge"
    final = {r["tool_use_id"]: r for r in client.requests[4]["messages"][-1]["content"]}
    assert "current map's name" in final["t8"]["content"] and "boost_x" in final["t9"]["content"]
    assert final["t8"].get("is_error") and json.loads(final["t10"]["content"])["added"] == ["coolant_c2"]
    assert s.summary.startswith("Proposed coolant_c2") and not s.stopped
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["output_config"] == {"effort": "high"}
    assert "thinking" not in req and req["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] for t in req["tools"]) and "tool_choice" not in req
    # Tool errors go back as errors, and the conversation is append-only
    last = client.requests[2]["messages"]
    results = {r["tool_use_id"]: r for r in last[-1]["content"]}
    assert results["t5"].get("is_error") and results["t4"]["content"].count("already has")
    assert last[:len(client.requests[1]["messages"])] == client.requests[1]["messages"]
    assert s.usage == {"input": 5000, "output": 1000, "cache_write": 0, "cache_read": 0}
    assert s.cost() == pytest.approx((5000 * 4 + 1000 * 20) / 1e6)


def test_stops(analysis):
    loop = [response(call(f"x{i}", "id_detail", can_id="0x316"), usage=(200000, 1000, 0, 0)) for i in range(50)]
    s = ai.Session(FakeClient(loop), "claude-opus-5-5", analysis, log=lambda m: None, max_cost=2.0).run()
    assert "cost limit" in s.stopped and s.cost() >= 2.0
    s = ai.Session(FakeClient(loop), "claude-opus-5-5", analysis, log=lambda m: None, max_turns=3, max_cost=1e9).run()
    assert "after 3 turns" in s.stopped
    refused = response(block("text", text=""), stop="refusal")
    refused.stop_details = types.SimpleNamespace(category="other", explanation="")
    s = ai.Session(FakeClient([refused]), "claude-opus-5-5", analysis, log=lambda m: None).run()
    assert "declined" in s.stopped and not s.proposals


def test_only_opus_and_fable(analysis):
    assert set(ai.MODELS.values()) == {"claude-opus-5-5", "claude-fable-5-1"}
    for other in ("claude-sonnet-5-5", "claude-haiku-4-5", "gpt-5"):
        with pytest.raises(ai.AnalyzeError):
            ai.Session(FakeClient([]), other, analysis)


def test_api_key_sources(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(ai, "stored_key", lambda: None)
    with pytest.raises(ai.AnalyzeError, match="apikey set"):
        ai.api_key()
    monkeypatch.setattr(ai, "stored_key", lambda: "sk-ant-api03-from-store")
    assert ai.api_key() == ("sk-ant-api03-from-store", "the system credential store")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    assert ai.api_key() == (KEY, "ANTHROPIC_API_KEY")


@pytest.mark.parametrize("key,msg", [("sk-ant-oat01-x", "subscription login token"), ("sk-ant-admin01-x", "Admin"),
                                     ("sk-proj-x", "doesn't look like")])
def test_only_api_keys(monkeypatch, key, msg):
    # The user's rule: API keys only. No subscription OAuth tokens, no admin keys, from either source
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    with pytest.raises(ai.AnalyzeError, match=msg) as e:
        ai.api_key()
    assert key not in str(e.value)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(ai, "stored_key", lambda: key)
    with pytest.raises(ai.AnalyzeError, match=msg):
        ai.api_key()


def test_command_keeps_the_key_out_of_everything(capture, tmp_path, monkeypatch, capsys):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    made = {}

    def fake_anthropic(**kw):
        made.update(kw)
        return FakeClient(scripted())
    monkeypatch.setattr(anthropic, "Anthropic", fake_anthropic)
    out = tmp_path / "out"
    assert ai.main([str(capture), "--yes", "--out", str(out)]) == 0
    assert made == {"api_key": KEY}                  # always given explicitly: no other login is used
    printed = capsys.readouterr().out
    saved = next(p for p in out.glob("analysis-*.json") if not p.stem.endswith("-map")).read_text(encoding="utf-8")
    assert KEY not in printed and KEY not in saved and "sk-ant" not in saved
    assert "first request is 4,321 tokens" in printed and "0x5B0" in printed
    data = json.loads(saved)
    assert [e["key"] for e in data["map_entries"]] == ["coolant_c2"] and data["model"] == "claude-opus-5-5"
    # The built map is saved beside the results, never over a map: the user decides whether to use it
    built = load_map(data["built_map"])
    assert built.name == "Stinger + analysis" and "--map" in printed and "current map is unchanged" in printed


def test_command_asks_before_sending(capture, monkeypatch, capsys):
    import anthropic
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    client = FakeClient(scripted())
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: client)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert ai.main([str(capture)]) == 0
    assert "Nothing sent." in capsys.readouterr().out and client.requests == []
