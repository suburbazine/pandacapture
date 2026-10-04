"""OBD-II mode 01 scan: which standard PIDs each module answers, and their current values.

Read-only: the only requests sent are mode 01 "show current data" (02 01 PID) to the functional
address 0x7DF, one at a time. Nothing that clears codes, changes settings or writes is ever sent.

The scan (finding what's supported) is blocked above MAX_SCAN_RPM, so it only runs key-on or at
idle. Engine speed comes from the address map's broadcast RPM signal while listening silently, or,
when there's none, from one OBD RPM request (01 0C) before anything else is asked. It's checked
again before and after every scan request; the scan stops as soon as it's above the limit or no
longer known. Polling PIDs already known to answer runs at any engine speed, as scan tools and
loggers do.
"""

import collections
import time
from dataclasses import dataclass, field

from . import protocol as p
from .policy import FRESH as RPM_FRESH
from .policy import MAX_SCAN_RPM, ScanBlocked, Sender, VehicleState, build
ANSWER_WAIT = 0.2           # s to collect answers after each request (J1979 allows 50 ms per module)
ANSWER_IDS = range(0x7E8, 0x7F0)
SUPPORT_PIDS = (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0)   # "which of the next 32 do you support?"
MODULES = {0x7E8: "engine", 0x7E9: "transmission"}


def _u16(d):
    return (d[0] << 8) | d[1]


def _s16(d):
    v = _u16(d)
    return v - 0x10000 if v & 0x8000 else v


# SAE J1979 mode 01: pid -> (name, unit, value from the data bytes). None = shown as hex.
PIDS = {
    0x01: ("Monitor status since codes cleared", "", None),
    0x02: ("DTC that caused the freeze frame", "", None),
    0x03: ("Fuel system status", "", None),
    0x04: ("Calculated engine load", "%", lambda d: d[0] * 100 / 255),
    0x05: ("Coolant temperature", "°C", lambda d: d[0] - 40),
    0x06: ("Short-term fuel trim, bank 1", "%", lambda d: d[0] / 1.28 - 100),
    0x07: ("Long-term fuel trim, bank 1", "%", lambda d: d[0] / 1.28 - 100),
    0x08: ("Short-term fuel trim, bank 2", "%", lambda d: d[0] / 1.28 - 100),
    0x09: ("Long-term fuel trim, bank 2", "%", lambda d: d[0] / 1.28 - 100),
    0x0A: ("Fuel pressure (gauge)", "kPa", lambda d: d[0] * 3),
    0x0B: ("Intake manifold pressure", "kPa", lambda d: d[0]),
    0x0C: ("Engine speed", "rpm", lambda d: _u16(d) / 4),
    0x0D: ("Vehicle speed", "km/h", lambda d: d[0]),
    0x0E: ("Timing advance", "° before TDC", lambda d: d[0] / 2 - 64),
    0x0F: ("Intake air temperature", "°C", lambda d: d[0] - 40),
    0x10: ("Mass air flow", "g/s", lambda d: _u16(d) / 100),
    0x11: ("Throttle position", "%", lambda d: d[0] * 100 / 255),
    0x12: ("Commanded secondary air status", "", None),
    0x13: ("Oxygen sensors present (2 banks)", "", None),
    **{0x14 + i: (f"Oxygen sensor {i + 1} voltage", "V", lambda d: d[0] / 200) for i in range(8)},
    0x1C: ("OBD standard", "", lambda d: d[0]),
    0x1D: ("Oxygen sensors present (4 banks)", "", None),
    0x1E: ("Auxiliary input status", "", None),
    0x1F: ("Run time since engine start", "s", lambda d: _u16(d)),
    0x21: ("Distance with the MIL on", "km", lambda d: _u16(d)),
    0x22: ("Fuel rail pressure (relative to manifold)", "kPa", lambda d: _u16(d) * 0.079),
    0x23: ("Fuel rail pressure (gauge)", "kPa", lambda d: _u16(d) * 10),
    **{0x24 + i: (f"Oxygen sensor {i + 1} lambda", "λ", lambda d: _u16(d) / 32768) for i in range(8)},
    0x2C: ("Commanded EGR", "%", lambda d: d[0] * 100 / 255),
    0x2D: ("EGR error", "%", lambda d: d[0] / 1.28 - 100),
    0x2E: ("Commanded evaporative purge", "%", lambda d: d[0] * 100 / 255),
    0x2F: ("Fuel tank level", "%", lambda d: d[0] * 100 / 255),
    0x30: ("Warm-ups since codes cleared", "", lambda d: d[0]),
    0x31: ("Distance since codes cleared", "km", lambda d: _u16(d)),
    0x32: ("Evap system vapor pressure", "Pa", lambda d: _s16(d) / 4),
    0x33: ("Barometric pressure", "kPa", lambda d: d[0]),
    **{0x34 + i: (f"Oxygen sensor {i + 1} lambda (wide-band)", "λ", lambda d: _u16(d) / 32768) for i in range(8)},
    0x3C: ("Catalyst temperature, bank 1 sensor 1", "°C", lambda d: _u16(d) / 10 - 40),
    0x3D: ("Catalyst temperature, bank 2 sensor 1", "°C", lambda d: _u16(d) / 10 - 40),
    0x3E: ("Catalyst temperature, bank 1 sensor 2", "°C", lambda d: _u16(d) / 10 - 40),
    0x3F: ("Catalyst temperature, bank 2 sensor 2", "°C", lambda d: _u16(d) / 10 - 40),
    0x41: ("Monitor status this drive cycle", "", None),
    0x42: ("Control module voltage", "V", lambda d: _u16(d) / 1000),
    0x43: ("Absolute load", "%", lambda d: _u16(d) * 100 / 255),
    0x44: ("Commanded lambda", "λ", lambda d: _u16(d) / 32768),
    0x45: ("Relative throttle position", "%", lambda d: d[0] * 100 / 255),
    0x46: ("Ambient air temperature", "°C", lambda d: d[0] - 40),
    0x47: ("Absolute throttle position B", "%", lambda d: d[0] * 100 / 255),
    0x48: ("Absolute throttle position C", "%", lambda d: d[0] * 100 / 255),
    0x49: ("Accelerator pedal position D", "%", lambda d: d[0] * 100 / 255),
    0x4A: ("Accelerator pedal position E", "%", lambda d: d[0] * 100 / 255),
    0x4B: ("Accelerator pedal position F", "%", lambda d: d[0] * 100 / 255),
    0x4C: ("Commanded throttle actuator", "%", lambda d: d[0] * 100 / 255),
    0x4D: ("Time run with the MIL on", "min", lambda d: _u16(d)),
    0x4E: ("Time since codes cleared", "min", lambda d: _u16(d)),
    0x4F: ("Maximum values (lambda, O2 voltage, current, MAP)", "", None),
    0x50: ("Maximum mass air flow", "g/s", lambda d: d[0] * 10),
    0x51: ("Fuel type", "", lambda d: d[0]),
    0x52: ("Ethanol fuel", "%", lambda d: d[0] * 100 / 255),
    0x53: ("Absolute evap system vapor pressure", "kPa", lambda d: _u16(d) / 200),
    0x54: ("Evap system vapor pressure", "Pa", lambda d: _s16(d)),
    0x55: ("Short-term secondary O2 trim, bank 1", "%", lambda d: d[0] / 1.28 - 100),
    0x56: ("Long-term secondary O2 trim, bank 1", "%", lambda d: d[0] / 1.28 - 100),
    0x57: ("Short-term secondary O2 trim, bank 2", "%", lambda d: d[0] / 1.28 - 100),
    0x58: ("Long-term secondary O2 trim, bank 2", "%", lambda d: d[0] / 1.28 - 100),
    0x59: ("Fuel rail pressure (absolute)", "kPa", lambda d: _u16(d) * 10),
    0x5A: ("Relative accelerator pedal position", "%", lambda d: d[0] * 100 / 255),
    0x5B: ("Hybrid battery pack remaining life", "%", lambda d: d[0] * 100 / 255),
    0x5C: ("Engine oil temperature", "°C", lambda d: d[0] - 40),
    0x5D: ("Fuel injection timing", "°", lambda d: _u16(d) / 128 - 210),
    0x5E: ("Engine fuel rate", "L/h", lambda d: _u16(d) / 20),
    0x5F: ("Emission requirements", "", None),
    0x61: ("Driver's demand engine torque", "%", lambda d: d[0] - 125),
    0x62: ("Actual engine torque", "%", lambda d: d[0] - 125),
    0x63: ("Engine reference torque", "Nm", lambda d: _u16(d)),
    0x64: ("Engine percent torque data", "", None),
    0x65: ("Auxiliary input / output", "", None),
    0x66: ("Mass air flow sensor", "", None),
    0x67: ("Engine coolant temperature (sensors)", "", None),
    0x68: ("Intake air temperature (sensors)", "", None),
    0x6B: ("Exhaust gas recirculation temperature", "", None),
    0x6C: ("Commanded throttle actuator control", "", None),
    0x6F: ("Turbocharger compressor inlet pressure", "", None),
    0x70: ("Boost pressure control", "", None),
    0x71: ("Variable geometry turbo control", "", None),
    0x73: ("Exhaust pressure", "", None),
    0x74: ("Turbocharger RPM", "", None),
    0x75: ("Turbocharger temperature A", "", None),
    0x76: ("Turbocharger temperature B", "", None),
    0x77: ("Charge air cooler temperature", "", None),
    0x78: ("Exhaust gas temperature, bank 1", "", None),
    0x79: ("Exhaust gas temperature, bank 2", "", None),
    0x7C: ("Diesel particulate filter temperature", "", None),
    0x7F: ("Engine run time", "", None),
    0x8E: ("Engine friction, percent torque", "%", lambda d: d[0] - 125),
    0xA6: ("Odometer", "km", lambda d: ((d[0] << 24) | (d[1] << 16) | (d[2] << 8) | d[3]) / 10),
}


def pid_name(pid) -> str:
    return PIDS.get(pid, (f"PID {pid:02X} (not in the table)",))[0]


def describe(pid, data: bytes) -> str:
    """A value for people: the decoded number with its unit, or the raw bytes."""
    _, unit, fn = PIDS.get(pid, ("", "", None))
    if fn is not None:
        try:
            v = fn(data)
            text = f"{v:.0f}" if float(v).is_integer() or abs(v) >= 100 else f"{v:.2f}".rstrip("0").rstrip(".")
            return f"{text} {unit}".strip()
        except (IndexError, ZeroDivisionError):
            pass
    return data.hex(" ").upper() if data else "(no data)"


def supported_from(base, data: bytes) -> set:
    """The PIDs a "supported PIDs [base+1 .. base+32]" answer lists (bit 31 = base+1)."""
    if len(data) < 4:
        return set()
    bits = int.from_bytes(data[:4], "big")
    return {base + 1 + i for i in range(32) if bits & (1 << (31 - i))}


def request_frame(pid, bus=0) -> p.Frame:
    """A mode 01 request for one PID, built from the diagnostic policy (policy.py)."""
    return build(0x01, bytes([pid])).frame(bus)


RpmGuard = VehicleState   # the vehicle state the scan checks (engine speed, from broadcast or OBD)


def meaningful(pid, data: bytes) -> str:
    """Empty if an answer carries a real value, else why not."""
    if not data:
        return "answered with no data"
    if all(b == 0xFF for b in data):
        return "answered FF (not available)"
    fn = PIDS.get(pid, ("", "", None))[2]
    if fn is not None:
        try:
            fn(data)
        except (IndexError, ZeroDivisionError):
            return f"answer too short ({len(data)} bytes)"
    return ""


@dataclass
class Found:
    """A PID worth polling: where it answered, and which modules gave a real value."""
    bus: int
    pid: int
    modules: tuple


@dataclass
class ScanResult:
    supported: dict = field(default_factory=lambda: collections.defaultdict(set))    # (bus, module) -> pids
    values: dict = field(default_factory=lambda: collections.defaultdict(dict))      # (bus, module) -> pid -> bytes
    long_answers: dict = field(default_factory=lambda: collections.defaultdict(set))  # (bus, module) -> pids
    requests: int = 0
    max_rpm: float = 0.0

    def wanted(self, bus) -> list:
        sets = [pids for (b, _), pids in self.supported.items() if b == bus]
        return sorted(set().union(*sets) - set(SUPPORT_PIDS) - {0xE0}) if sets else []

    def assess(self, buses):
        """(PIDs to poll, discarded): a PID is polled on each bus where a module gave a real value;
        it's discarded, with the reason on each bus, when no bus did."""
        keep, why = [], collections.defaultdict(dict)   # why: pid -> bus -> reason
        for bus in buses:
            for pid in self.wanted(bus):
                good, reasons = [], []
                for (b, module), pids in sorted(self.supported.items()):
                    if b != bus or pid not in pids:
                        continue
                    if pid in self.values[(b, module)]:
                        r = meaningful(pid, self.values[(b, module)][pid])
                    elif pid in self.long_answers[(b, module)]:
                        r = "long (multi-frame) answer, not read"
                    else:
                        r = "no answer"
                    if r:
                        reasons.append(f"{module:03X}: {r}")
                    else:
                        good.append(module)
                if good:
                    keep.append(Found(bus, pid, tuple(good)))
                else:
                    why[pid][bus] = "; ".join(reasons) or "no answer"
        kept = {f.pid for f in keep}
        discarded = {pid: dict(per_bus) for pid, per_bus in why.items() if pid not in kept}
        return keep, discarded

    def to_json(self, buses) -> dict:
        keep, discarded = self.assess(buses)
        return {
            "modules": {f"bus{b} {m:03X}": {
                "module": MODULES.get(m, "module"),
                "supported": [f"{pid:02X}" for pid in sorted(pids)],
                "values": {f"{pid:02X}": {"name": pid_name(pid), "raw": self.values[(b, m)][pid].hex(),
                                          "value": describe(pid, self.values[(b, m)][pid])}
                           for pid in sorted(self.values[(b, m)])},
            } for (b, m), pids in sorted(self.supported.items())},
            "polled": [{"bus": f.bus, "pid": f"{f.pid:02X}", "name": pid_name(f.pid),
                        "modules": [f"{m:03X}" for m in f.modules]} for f in keep],
            "discarded": {f"{pid:02X}": {"name": pid_name(pid), "why": {f"bus{b}": r for b, r in per_bus.items()}}
                          for pid, per_bus in sorted(discarded.items())},
            "requests": self.requests,
            "max_rpm": self.max_rpm,
        }

    def report(self, buses) -> str:
        keep, discarded = self.assess(buses)
        lines = []
        for (b, m), pids in sorted(self.supported.items()):
            shown = sorted(pids - set(SUPPORT_PIDS) - {0xE0})
            lines.append(f"Bus {b}, module {m:03X} ({MODULES.get(m, 'module')}): {len(shown)} PIDs supported")
            for pid in shown:
                if pid in self.values[(b, m)]:
                    data = self.values[(b, m)][pid]
                    value = describe(pid, data) + ("   (discarded)" if meaningful(pid, data) else "")
                elif pid in self.long_answers[(b, m)]:
                    value = "(long answer, not read)"
                else:
                    value = "(no answer)"
                lines.append(f"  {pid:02X}  {pid_name(pid):<46} {value}")
        if not lines:
            return "No module answered."
        if discarded:
            lines.append(f"Discarded {len(discarded)} PIDs (no real value on any bus):")
            for pid, per_bus in sorted(discarded.items()):
                lines.append(f"  {pid:02X}  {pid_name(pid)}: " + "; ".join(f"bus {b}: {r}" for b, r in per_bus.items()))
        lines.append(f"Polling {len(keep)} PIDs." if keep else "Nothing to poll.")
        return "\n".join(lines)


class Scanner:
    """Runs the scan and the poll over a link with send(frames) and wait(seconds); the link hands every
    frame it receives to Scanner.frame (ArmedPanda's on_frame)."""

    def __init__(self, link, guard: VehicleState, buses=(0,), log=print, on_sent=None):
        self.link = link
        self.sender = Sender(link, guard, on_sent)
        self.guard = guard
        self.buses = tuple(buses)
        self.log = log
        self.on_sent = on_sent   # e.g. TxLog.sent
        self.result = ScanResult()
        self._rpm_modules = {}   # bus -> modules that answered 01 0C
        self._answers = None
        self._pid = self._bus = None

    def frame(self, f):
        self.guard.frame(f)
        if self._answers is None or f.addr not in ANSWER_IDS or f.bus != self._bus or len(f.data) < 3:
            return
        d = f.data
        if d[0] >> 4 == 1 and len(d) >= 4 and d[2:4] == bytes([0x41, self._pid]):
            self.result.long_answers[(f.bus, f.addr)].add(self._pid)   # first frame of a multi-frame answer
        elif 3 <= d[0] <= 7 and d[1] == 0x41 and d[2] == self._pid:
            self._answers[f.addr] = bytes(d[3:1 + d[0]])

    def _ask(self, bus, pid, expect=()) -> dict:
        """Sends one request and collects answers until every expected module has answered, or
        ANSWER_WAIT has passed."""
        self._answers, self._pid, self._bus = {}, pid, bus
        self.sender.send(build(0x01, bytes([pid])), bus)
        self.result.requests += 1
        end = time.monotonic() + ANSWER_WAIT
        while time.monotonic() < end:
            self.link.wait(0.005)
            # An ELM327 says when every answer is in; with a panda, stop once the expected modules answered
            if getattr(self.link, "answers_complete", False) or (expect and all(m in self._answers for m in expect)):
                break
        answers, self._answers = self._answers, None
        return answers

    def _ensure_rpm(self):
        """Fresh engine speed before a request: from the broadcast (listening for it first, so a stale
        reading never costs a request), else by asking for OBD RPM. Fresh enough means still fresh at the
        check after the request, which may wait its whole ANSWER_WAIT (a panda can't tell when every module
        has answered); found by the FrostBYTE Android port."""
        ahead = ANSWER_WAIT * 1.25
        if not self.guard.fresh(ahead=ahead) and self.guard.signal is not None:
            end = time.monotonic() + RPM_FRESH
            while not self.guard.fresh(ahead=ahead) and time.monotonic() < end:
                self.link.wait(0.01)
        for bus in self.buses:
            if self.guard.fresh(ahead=ahead):
                break
            # Once a module has answered RPM, wait only for it: the reading then starts the next request fresh
            answered = self._ask(bus, 0x0C, self._rpm_modules.get(bus, ()))
            if answered:
                self._rpm_modules[bus] = tuple(answered)
        self.guard.check_scan()
        self.result.max_rpm = max(self.result.max_rpm, self.guard.rpm)

    def query(self, bus, pid, expect=()) -> dict:
        self._ensure_rpm()
        answers = self._ask(bus, pid, expect)
        self.guard.check_scan()   # stop if the engine sped up while we waited
        return answers

    def scan(self) -> ScanResult:
        r = self.result
        for bus in self.buses:
            for base in SUPPORT_PIDS:
                for module, data in self.query(bus, base).items():
                    r.supported[(bus, module)] |= supported_from(base, data)
                if not any(base + 0x20 in pids for (b, _), pids in r.supported.items() if b == bus):
                    break
            wanted = r.wanted(bus)
            self.log(f"Bus {bus}: {len(wanted)} PIDs supported" + ("; reading each once..." if wanted else ""))
            for pid in wanted:
                for module, data in self.query(bus, pid).items():
                    r.values[(bus, module)][pid] = data
        return r

    def poll(self, keep, should_stop, on_answer):
        """Asks for each kept PID in turn until should_stop(); on_answer(bus, module, pid, data) for
        every answer. Unlike the scan, it runs at any engine speed: these PIDs are known to answer."""
        while keep and not should_stop():
            for f in keep:
                if should_stop():
                    return
                for module, data in self._ask(f.bus, f.pid, f.modules).items():
                    on_answer(f.bus, module, f.pid, data)
