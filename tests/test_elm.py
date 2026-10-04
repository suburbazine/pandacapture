"""ELM327: answer parsing, setup, and the scan and poll through a simulated adapter."""

import json
import types

import pytest

from pandacapture import obd
from pandacapture.elm import Elm, ElmError, ElmLink, parse_line
from pandacapture.obd import RpmGuard, ScanBlocked, Scanner
from pandacapture.obd_run import planned_poll


def bitmask(*pids, base=0):
    v = 0
    for pid in pids:
        v |= 1 << (31 - (pid - base - 1))
    return v.to_bytes(4, "big")


class FakeElm:
    """A serial port with an ELM327 behind it, on a CAN car (or not)."""

    def __init__(self, protocol="6", spaces=True, rpm=800, modules=None, error=None):
        self.protocol = protocol
        self.spaces = spaces
        self.rpm = rpm
        self.error = error
        self.modules = modules or {0x7E8: {0x00: bitmask(0x05, 0x0C, 0x0D, 0x1E), 0x05: bytes([130]),
                                           0x0D: bytes([0]), 0x1E: bytes([0xFF])},
                                   0x7E9: {0x00: bitmask(0x05), 0x05: bytes([88])}}
        self.out = bytearray()
        self.typed = bytearray()
        self.echo = True
        self.requests = []

    # pyserial's interface, as far as Elm uses it
    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n):
        chunk, self.out = bytes(self.out[:n]), self.out[n:]
        return chunk

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        pass

    def write(self, data):
        self.typed += data
        while b"\r" in self.typed:
            line, _, rest = bytes(self.typed).partition(b"\r")
            self.typed = bytearray(rest)
            self._command(line.decode().strip().upper())

    def _reply(self, cmd, lines):
        text = (cmd + "\r" if self.echo and cmd else "") + "".join(ln + "\r" for ln in lines) + "\r>"
        self.out += text.encode()

    def _command(self, cmd):
        if not cmd:
            return self._reply("", [])
        if cmd == "ATZ":
            self.echo = True
            return self._reply(cmd, ["", "ELM327 v1.5"])
        if cmd == "ATI":
            return self._reply(cmd, ["ELM327 v1.5"])
        if cmd == "ATE0":
            self._reply(cmd, ["OK"])
            self.echo = False
            return None
        if cmd.startswith("AT") and cmd != "ATDPN":
            return self._reply(cmd, ["OK"])
        if cmd == "ATDPN":
            return self._reply(cmd, ["A" + self.protocol])
        self.requests.append(cmd)
        if self.error:
            return self._reply(cmd, [self.error])
        pid = int(cmd[2:4], 16)
        lines = []
        for module, pids in self.modules.items():
            if pid == 0x0C and module == 0x7E8:
                raw = int(self.rpm * 4)
                data = bytes([raw >> 8, raw & 0xFF])
            elif pid in pids:
                data = pids[pid]
            else:
                continue
            payload = bytes([2 + len(data), 0x41, pid]) + data
            head = f"18 DA F1 {module - 0x7E8 + 0x10:02X}" if self.protocol in "79" else f"{module:03X}"
            body = " ".join(f"{b:02X}" for b in payload)
            line = f"{head} {body}"
            lines.append(line if self.spaces else line.replace(" ", ""))
        return self._reply(cmd, lines or ["NO DATA"])


def open_elm(fake):
    return Elm("COM9", serial_factory=lambda port: fake)


def test_parse_line():
    assert parse_line("7E8 03 41 0D 00", False) == (0x7E8, bytes([3, 0x41, 0x0D, 0]))
    assert parse_line("7E803410D00", False) == (0x7E8, bytes([3, 0x41, 0x0D, 0]))
    assert parse_line("18 DA F1 11 03 41 05 80", True) == (0x7E9, bytes([3, 0x41, 5, 0x80]))
    assert parse_line("18DAF11003410D00", True) == (0x7E8, bytes([3, 0x41, 0x0D, 0]))
    assert parse_line("18 DB 33 F1 02 01 0D", True) is None      # not an answer
    for junk in ("SEARCHING...", "NO DATA", "", "7E8 0", "OK"):
        assert parse_line(junk, False) is None


def test_setup_and_description():
    elm = open_elm(FakeElm())
    assert elm.version == "ELM327 v1.5" and elm.protocol == "6" and not elm.extended
    assert "11-bit ids, 500 kbit/s" in elm.description


def test_non_can_car_is_refused():
    with pytest.raises(ElmError, match="not CAN"):
        open_elm(FakeElm(protocol="3"))


def test_adapter_errors():
    elm = open_elm(FakeElm())
    elm.ser.error = "CAN ERROR"
    with pytest.raises(ElmError, match="CAN ERROR"):
        elm.request(bytes([1, 0x0D]))
    elm.ser.error = "NO DATA"
    assert elm.request(bytes([1, 0x0D])) == []


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(obd, "ANSWER_WAIT", 0.05)


def scan_through(fake):
    elm = open_elm(fake)
    holder = types.SimpleNamespace(scanner=None)
    link = ElmLink(elm, lambda f: holder.scanner.frame(f))
    holder.scanner = Scanner(link, RpmGuard(None), [0], log=lambda s: None)
    return holder.scanner


@pytest.mark.parametrize("protocol,spaces", [("6", True), ("6", False), ("7", True)])
def test_scan_and_poll_through_an_elm(protocol, spaces):
    fake = FakeElm(protocol=protocol, spaces=spaces)
    scanner = scan_through(fake)
    r = scanner.scan()
    assert fake.requests[0] == "0100"        # setup's own first request
    assert "010C" in fake.requests[1:3]      # engine speed read over OBD before the scan's requests
    assert r.values[(0, 0x7E9)][0x05] == bytes([88])
    keep, discarded = r.assess([0])
    assert {(f.pid, f.modules) for f in keep} >= {(0x05, (0x7E8, 0x7E9)), (0x0D, (0x7E8,))}
    assert 0x1E in discarded

    got = []
    fake.rpm = 4000                          # polling runs at any engine speed
    n = {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > 6
    scanner.poll(keep, stop, lambda bus, module, pid, data: got.append((module, pid)))
    assert (0x7E9, 0x05) in got


def test_scan_through_an_elm_is_blocked_above_900():
    fake = FakeElm(rpm=1200)
    scanner = scan_through(fake)
    with pytest.raises(ScanBlocked, match="1200 rpm"):
        scanner.scan()
    assert fake.requests == ["0100", "010C"]   # setup's request, then only the RPM read


def test_planned_poll(tmp_path):
    args = types.SimpleNamespace(pids="0c, 0D,05", poll=None)
    assert [f.pid for f in planned_poll(args)] == [0x0C, 0x0D, 0x05]
    fake = FakeElm()
    r = scan_through(fake).scan()
    path = tmp_path / "obd-scan.json"
    path.write_text(json.dumps(r.to_json([0])), encoding="utf-8")
    keep = planned_poll(types.SimpleNamespace(pids=None, poll=str(path)))
    assert {(f.bus, f.pid, f.modules) for f in keep} == {(f.bus, f.pid, f.modules) for f in r.assess([0])[0]}
    with pytest.raises(Exception, match="hex PIDs"):
        planned_poll(types.SimpleNamespace(pids="RPM", poll=None))


def test_elm_link_only_sends_what_the_policy_allows():
    from pandacapture import protocol as p
    from pandacapture.policy import build
    fake = FakeElm()
    link = ElmLink(open_elm(fake), lambda f: None)
    with pytest.raises(ElmError, match="Refused"):
        link.send([p.Frame(0, 0x7E0, bytes([2, 0x11, 0x01, 0, 0, 0, 0, 0]))])   # ECU reset
    commands = []
    fake._command = (lambda orig: lambda cmd: (commands.append(cmd), orig(cmd))[1])(fake._command)
    link.send([build(0x01, b"\x0d", target=0x7E0).frame()])                    # one module: header switched
    assert commands == ["ATSH7E0", "010D", "ATSH7DF"]
