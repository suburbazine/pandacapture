"""OBD scan and poll against a simulated car: supported-PID discovery, discarding, and the 900 rpm interlock."""

import pytest

from pandacapture import obd
from pandacapture import protocol as p
from pandacapture.match import read_reference
from pandacapture.obd import RpmGuard, ScanBlocked, Scanner
from pandacapture.obd_run import Columns
from pandacapture.signals import load_map


def bitmask(*pids, base=0):
    v = 0
    for pid in pids:
        v |= 1 << (31 - (pid - base - 1))
    return v.to_bytes(4, "big")


class Car:
    """Answers mode 01 requests per bus and module, and broadcasts RPM on 0x316 (bus 0) if asked to."""

    def __init__(self, modules, rpm=800, broadcast=True):
        self.modules = modules      # {bus: {module id: {pid: bytes}}}
        self.rpm = rpm
        self.broadcast = broadcast
        self.pending = []
        self.sent = []
        self.on_request = None      # hook: called with the request count, may change rpm
        self.scanner = None

    def send(self, frames):
        for f in frames:
            assert f.addr == 0x7DF and f.data[:2] == b"\x02\x01", "only mode 01 requests may be sent"
            self.sent.append(f)
            if self.on_request:
                self.on_request(len(self.sent))
            pid = f.data[2]
            for module, pids in self.modules.get(f.bus, {}).items():
                if pid == 0x0C and module == 0x7E8:
                    raw = int(self.rpm * 4)
                    data = bytes([raw >> 8, raw & 0xFF])
                elif pid in pids:
                    data = pids[pid]
                else:
                    continue
                if data is None:
                    continue
                if data == b"LONG":
                    self.pending.append(p.Frame(f.bus, module, bytes([0x10, 0x0A, 0x41, pid, 1, 2, 3, 4])))
                else:
                    self.pending.append(p.Frame(f.bus, module, bytes([2 + len(data), 0x41, pid]) + data
                                                + bytes(5 - len(data))))

    def wait(self, seconds):
        if self.broadcast:
            raw = int(self.rpm * 4)
            self.scanner.frame(p.Frame(0, 0x316, bytes([0, 0x10, raw & 0xFF, raw >> 8, 0, 0, 0, 0])))
        frames, self.pending = self.pending, []
        for f in frames:
            self.scanner.frame(f)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(obd, "ANSWER_WAIT", 0.01)


@pytest.fixture
def rpm_signal():
    return next(s for s in load_map("kia-stinger-33t-pcan").signals if s.key == "rpm")


def stinger_like():
    engine = {0x00: bitmask(0x05, 0x0C, 0x0D, 0x1E, 0x20), 0x20: bitmask(0x33, 0x40, base=0x20),
              0x40: bitmask(0x42, 0x49, base=0x40),
              0x05: bytes([130]), 0x0D: bytes([0]), 0x1E: bytes([0xFF]),     # 1E: FF = not available
              0x33: bytes([101]), 0x42: bytes([0x34, 0x6E]), 0x49: None}     # 49: never answers
    trans = {0x00: bitmask(0x05, 0x0D), 0x05: bytes([90]), 0x0D: bytes([0])}
    camera_bus = {0x7E8: {0x00: bitmask(0x20), 0x20: bitmask(0x40, base=0x20), 0x40: bitmask(0x49, 0x4F, base=0x40),
                          0x49: bytes([30]), 0x4F: b"LONG"}}
    return {0: {0x7E8: engine, 0x7E9: trans}, 2: camera_bus}


def run_scan(car, signal, buses=(0, 2)):
    guard = RpmGuard(signal)
    scanner = Scanner(car, guard, buses, log=lambda s: None)
    car.scanner = scanner
    return scanner, scanner.scan()


def test_scan_finds_reads_and_discards(rpm_signal):
    car = Car(stinger_like())
    scanner, r = run_scan(car, rpm_signal)
    assert r.supported[(0, 0x7E8)] >= {0x05, 0x0C, 0x0D, 0x1E, 0x33, 0x42, 0x49}
    assert 0x60 not in {f.data[2] for f in car.sent}            # 0x40's answer didn't list 0x60
    assert r.values[(0, 0x7E9)][0x05] == bytes([90])
    keep, discarded = r.assess((0, 2))
    kept = {(f.bus, f.pid): f.modules for f in keep}
    assert kept[(0, 0x05)] == (0x7E8, 0x7E9)
    assert (2, 0x49) in kept and (0, 0x49) not in kept           # only answered on bus 2: polled there
    assert set(discarded) == {0x1E, 0x4F}
    assert "FF" in discarded[0x1E][0] and "multi-frame" in discarded[0x4F][2]
    text = r.report((0, 2))
    assert "Discarded 2 PIDs" in text and "Coolant temperature" in text and "130" not in text.split("Discarded")[1]
    j = r.to_json((0, 2))
    assert j["discarded"]["1E"]["why"]["bus0"].startswith("7E8") and j["max_rpm"] == 800
    assert obd.describe(0x42, bytes([0x34, 0x6E])) == "13.42 V"


def test_blocked_above_900_before_anything_is_sent(rpm_signal):
    car = Car(stinger_like(), rpm=1500)
    with pytest.raises(ScanBlocked, match="1500 rpm"):
        run_scan(car, rpm_signal)
    assert car.sent == []    # the broadcast was enough: not even one request


def test_without_broadcast_rpm_is_read_over_obd_first():
    car = Car(stinger_like(), rpm=1200, broadcast=False)
    with pytest.raises(ScanBlocked, match="1200 rpm"):
        run_scan(car, None)
    assert [f.data[2] for f in car.sent] == [0x0C]    # one RPM request, then nothing


def test_unknown_engine_speed_blocks():
    car = Car({0: {}}, broadcast=False)               # nobody answers anything
    with pytest.raises(ScanBlocked, match="isn't known"):
        run_scan(car, None, buses=(0,))
    assert {f.data[2] for f in car.sent} == {0x0C}


def test_stops_when_the_engine_speeds_up(rpm_signal):
    car = Car(stinger_like())

    def rev(n):
        if n == 6:
            car.rpm = 2200
    car.on_request = rev
    with pytest.raises(ScanBlocked, match="2200 rpm"):
        run_scan(car, rpm_signal)
    assert len(car.sent) == 6     # stopped right after the request during which it rose


def test_a_brief_spike_still_stops_it(rpm_signal):
    guard = RpmGuard(rpm_signal)
    for rpm in (800, 950, 820):    # all between two checks
        raw = rpm * 4
        guard.frame(p.Frame(0, 0x316, bytes([0, 0x10, raw & 0xFF, raw >> 8, 0, 0, 0, 0])))
    with pytest.raises(ScanBlocked, match="950 rpm"):
        guard.check_scan()


def test_limit_is_900():
    assert obd.MAX_SCAN_RPM == 900


def test_poll_answers_and_interlock(rpm_signal, tmp_path):
    car = Car(stinger_like())
    scanner, r = run_scan(car, rpm_signal)
    keep, _ = r.assess((0, 2))
    columns = Columns(tmp_path / "obd.csv")
    got = []
    t = [1000.0]

    def on_answer(bus, module, pid, data):
        got.append((bus, module, pid))
        t[0] += 0.05
        columns.add(t[0], bus, module, pid, data)

    rounds = {"n": 0}

    def stop():
        rounds["n"] += 1
        return rounds["n"] > 40
    scanner.poll(keep, stop, on_answer)
    assert (0, 0x7E9, 0x05) in got and (2, 0x7E8, 0x49) in got
    assert all(pid not in (0x1E, 0x4F) for _, _, pid in got)    # discarded PIDs aren't polled
    assert columns.save()
    ref = read_reference(columns.path)
    assert "Coolant temperature (°C)" in ref.columns and "Coolant temperature (°C) [7E9]" in ref.columns
    assert "Accelerator pedal position D (%) [bus 2]" in ref.columns

    # The poll runs at any engine speed: these PIDs are known to answer
    car.rpm = 3500
    got.clear()
    rounds["n"] = 0
    scanner.poll(keep, stop, on_answer)
    assert (0, 0x7E8, 0x05) in got


def test_only_read_requests_are_ever_built():
    for pid in range(256):
        f = obd.request_frame(pid)
        assert f.addr == 0x7DF and f.data[:3] == bytes([2, 1, pid])


def test_scan_with_obd_rpm_only_through_a_panda(monkeypatch):
    """No broadcast RPM: engine speed comes from 01 0C answers, and every request waits its full answer time (a
    panda can't tell when all modules have answered). A reading that's fresh before a request must still be fresh
    at the check after it, or the scan stops part way. Real timings scaled down: FRESH 0.5 s / ANSWER_WAIT 0.2 s."""
    from pandacapture import policy
    monkeypatch.setattr(policy, "FRESH", 0.05)
    monkeypatch.setattr(obd, "ANSWER_WAIT", 0.02)
    car = Car(stinger_like(), rpm=800, broadcast=False)
    scanner, r = run_scan(car, None)
    assert r.values[(0, 0x7E8)][0x05] == bytes([130]) and r.max_rpm == 800
    rpm_asks = sum(1 for f in car.sent if f.data[2] == 0x0C)
    assert 2 <= rpm_asks < len(car.sent) // 2     # asked again when needed, not before every request
