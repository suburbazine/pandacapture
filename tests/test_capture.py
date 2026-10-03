import re

from pandacapture import protocol as p
from pandacapture.capture import CaptureOptions, Console, capture
from pandacapture.sources import SimulatedSource, SourceError

# The FrostBYTE Android signal finder's patterns (canlog/LogParsers.kt)
ANDROID_CANDUMP = re.compile(r"\(\s*([0-9.]+)\)\s+\S+\s+([0-9A-Fa-f]{3,8})#(R?[0-9A-Fa-f]*)")
ANDROID_MARKER = re.compile(r"^#\s*marker\s+(\S+)\s+\(\s*([0-9.]+)\)")


class Quiet:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass

    def isatty(self):
        return False


class ScriptedKeys:
    """Presses keys on the given polls."""

    def __init__(self, script):
        self.script = dict(script)
        self.polls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def poll(self):
        self.polls += 1
        return list(self.script.pop(self.polls, ""))


def run(tmp_path, source_factory, keys=None, **opts):
    out = Quiet()
    rc = capture(source_factory, CaptureOptions(out_dir=tmp_path, stamp="t", **opts), Console(out),
                 keys or ScriptedKeys({}))
    return rc, (tmp_path / "capture-t.log").read_text(encoding="utf-8"), out.text


def test_log_readable_by_android_finder(tmp_path):
    rc, log, _ = run(tmp_path, SimulatedSource, keys=ScriptedKeys({50: "m", 300: "3", 2000: "q"}), seconds=5)
    assert rc == 0
    lines = log.splitlines()
    frames = [line for line in lines if line.startswith("(")]
    assert frames and all(ANDROID_CANDUMP.search(line) for line in frames)
    markers = [ANDROID_MARKER.match(line) for line in lines if line.startswith("# marker")]
    assert [m[1] for m in markers] == ["1", "3"]
    assert lines[0] == "# PandaCapture candump log"
    assert any(line.startswith("# 0x316 (engine RPM) present") for line in lines)


def test_bus_filter(tmp_path):
    _, log, _ = run(tmp_path, SimulatedSource, seconds=0.5, buses=(1,))
    frames = [line for line in log.splitlines() if line.startswith("(")]
    assert frames and all(" can1 " in line for line in frames)


def test_returned_frames_not_recorded(tmp_path):
    class Echoes(SimulatedSource):
        def read(self):
            return [p.Frame(0, 0x123, b"\x01", returned=True)] + super().read()

    _, log, _ = run(tmp_path, Echoes, seconds=0.3)
    assert " 123#" not in log


def test_open_failure_reported(tmp_path):
    def fail():
        raise SourceError("No panda found.")

    out = Quiet()
    assert capture(fail, CaptureOptions(out_dir=tmp_path), Console(out), ScriptedKeys({})) == 1
    assert "No panda found" in out.text


def test_error_without_reconnect_is_logged(tmp_path):
    _, log, _ = run(tmp_path, lambda: SimulatedSource(dropout_at=0.3), seconds=10, reconnect=False)
    assert "# stall: no frames since" in log
    assert "# adapter error: simulated dropout" in log
    assert "# stopped with error" in log


def test_rolls_over_to_new_files(tmp_path):
    rc, _, out = run(tmp_path, SimulatedSource, seconds=2, split_mb=0.01)
    assert rc == 0
    parts = sorted(tmp_path.glob("capture-*.log"))
    assert len(parts) >= 3
    texts = [p.read_text(encoding="utf-8") for p in parts]
    first = (tmp_path / "capture-t.log").read_text(encoding="utf-8")
    # every part repeats the header; the chain names the next and previous parts
    assert all(t.startswith("# PandaCapture candump log") for t in texts)
    nxt = re.search(r"# continued in: (\S+)", first)[1]
    assert f"# continues from: capture-t.log" in (tmp_path / nxt).read_text(encoding="utf-8")
    frames = sum(len([line for line in t.splitlines() if line.startswith("(")]) for t in texts)
    assert f"\n# {frames} frames" in "".join(texts)  # the summary counts the whole recording
    assert "Recorded in" in "".join(texts)
