"""The diagnostic policy: every diagnostic request PandaCapture can send comes from SERVICES.

A request that isn't in the table can't be built, and a link (the panda's OBD path, an ELM327) refuses
any frame that doesn't parse back into a request the table allows. Each service has a tier:

- READ: reads values or codes; goes out whenever asked. (Discovery scans add their own limit: up to
  MAX_SCAN_RPM, in obd.py.)
- ENGINE_OFF: changes something in a module (clears codes, tests an actuator, runs a routine). Only
  sent while the engine is off and the car is stopped, from fresh readings checked right before each
  request. See docs/diagnostics-plan.md.

Some services are never in the table, whatever the engine is doing: NEVER says why.

Today the table holds reads only: the standard OBD ones (live data, freeze frame, stored, pending and
permanent codes, vehicle information) and the UDS ones (a module's own codes, data by identifier).
Requests go to 7DF (every OBD module) or one module's request id (700-7F7, answering at +8), never to
an id the bus uses for ordinary traffic. The plan adds the rest one at a time. Besides requests, the only frame
sent is ISO-TP flow control, which lets a module send the rest of a long answer (see flow_control).
"""

import enum
import time
from dataclasses import dataclass

from . import protocol as p

FRESH = 0.5                  # s: a reading older than this doesn't count
MAX_SCAN_RPM = 900           # discovery scans: key-on or idle only. Fixed on purpose: no option overrides it
FUNCTIONAL = 0x7DF           # every OBD module
PHYSICAL = range(0x7E0, 0x7E8)   # the OBD modules' own request ids (engine 7E0, transmission 7E1...)


def is_physical(can_id) -> bool:
    """A module's own request id: 700-7F7, answered from the id + 8 (the usual 11-bit convention). Not
    7DF (everyone), not an OBD answer id (7E8-7EF), and not 7D7, whose answers would land on 7DF."""
    return 0x700 <= can_id <= 0x7F7 and can_id not in (FUNCTIONAL, 0x7D7) and not 0x7E8 <= can_id <= 0x7EF


def answer_id(target) -> int:
    return target + 8


# Services a module might be asked for on a diagnostic id; a frame on an id that doesn't look like one
# of these (or ISO-TP) is ordinary traffic, and that id is never sent to (VehicleState.traffic).
_DIAG_SIDS = set(range(0x01, 0x0B)) | set(range(0x10, 0x3F)) | set(range(0x83, 0x88))


class Tier(enum.Enum):
    READ = "read"
    ENGINE_OFF = "engine off"


class PolicyRefused(Exception):
    """The request isn't one the policy allows."""


class StateRefused(Exception):
    """The vehicle isn't in the state the request needs."""


class ScanBlocked(StateRefused):
    """A discovery scan with the engine above MAX_SCAN_RPM, or its speed unknown."""


class EngineNotOff(StateRefused):
    """An engine-off request without the engine off and the car stopped."""


@dataclass(frozen=True)
class Service:
    sid: int
    name: str
    tier: Tier
    check: object                 # params (bytes) -> "" if allowed, else why not
    confirm: str = ""             # word the user types before the first request of a session
    undo: object = None           # params -> the payload that undoes it, sent if the state changes


def _one_pid(params: bytes) -> str:
    return "" if len(params) == 1 else "takes one PID"


def _pid_and_frame(params: bytes) -> str:
    return "" if len(params) == 2 and params[1] == 0 else "takes a PID and freeze frame 00"


def _no_params(params: bytes) -> str:
    return "" if not params else "takes no parameters"


def _info_type(params: bytes) -> str:
    return "" if len(params) == 1 else "takes one info type"


# UDS 19, read DTC information: the reporting sub-functions, by their parameter length. All read-only.
DTC_REPORTS = {
    0x01: 1,     # number of codes matching a status mask
    0x02: 1,     # codes matching a status mask
    0x03: 0,     # snapshot (freeze frame) identification
    0x04: 4,     # a code's snapshot record: code (3 bytes), record number
    0x06: 4,     # a code's extended data record: code (3 bytes), record number
    0x0A: 0,     # every code the module supports
}


def _dtc_report(params: bytes) -> str:
    if not params:
        return "needs a report type"
    if params[0] not in DTC_REPORTS:
        return f"report type {params[0]:02X} isn't one PandaCapture reads"
    if len(params) != 1 + DTC_REPORTS[params[0]]:
        return f"report type {params[0]:02X} takes {DTC_REPORTS[params[0]]} parameter bytes"
    return ""


def _data_ids(params: bytes) -> str:
    return "" if 2 <= len(params) <= 6 and len(params) % 2 == 0 else "takes one to three 2-byte identifiers"


SERVICES = {
    0x01: Service(0x01, "OBD current data (mode 01)", Tier.READ, _one_pid),
    0x02: Service(0x02, "OBD freeze frame (mode 02)", Tier.READ, _pid_and_frame),
    0x03: Service(0x03, "OBD stored codes (mode 03)", Tier.READ, _no_params),
    0x07: Service(0x07, "OBD pending codes (mode 07)", Tier.READ, _no_params),
    0x09: Service(0x09, "OBD vehicle information (mode 09)", Tier.READ, _info_type),
    0x0A: Service(0x0A, "OBD permanent codes (mode 0A)", Tier.READ, _no_params),
    0x19: Service(0x19, "UDS read codes (19)", Tier.READ, _dtc_report),
    0x22: Service(0x22, "UDS read data by identifier (22)", Tier.READ, _data_ids),
}

NEVER = {
    0x27: "security access needs each manufacturer's seed-to-key algorithm, which PandaCapture doesn't include",
    0x34: "request download (flashing) can leave a module unable to start",
    0x35: "request upload (reading a module's memory) is part of flashing tools",
    0x36: "transfer data (flashing) can leave a module unable to start",
    0x37: "request transfer exit (flashing)",
}


@dataclass(frozen=True)
class Request:
    service: Service
    payload: bytes                # service id, then its parameters
    target: int = FUNCTIONAL

    def frame(self, bus=0) -> p.Frame:
        """One ISO-TP single frame: length, payload, zero padding."""
        return p.Frame(bus, self.target, bytes([len(self.payload)]) + self.payload + bytes(7 - len(self.payload)))


def build(sid, params=b"", target=FUNCTIONAL, table=None) -> Request:
    """The request for this service and parameters, if the policy allows it."""
    table = SERVICES if table is None else table
    params = bytes(params)
    if sid in NEVER:
        raise PolicyRefused(f"Service {sid:02X} is never sent: {NEVER[sid]}.")
    service = table.get(sid)
    if service is None:
        raise PolicyRefused(f"Service {sid:02X} isn't in PandaCapture's diagnostic policy.")
    why = service.check(params)
    if why:
        raise PolicyRefused(f"{service.name}: {why}.")
    if target != FUNCTIONAL and not is_physical(target):
        raise PolicyRefused(f"{target:03X} isn't a diagnostic request address (7DF, or 700-7F7 answering at +8).")
    if 1 + len(params) > 7:
        raise PolicyRefused(f"{service.name}: longer than one frame.")
    return Request(service, bytes([sid]) + params, target)


def recognise(frame, table=None) -> Request:
    """The request a frame carries, if the policy allows it; links use this to refuse anything else."""
    d = frame.data
    if frame.extended or len(d) < 2 or not 1 <= d[0] <= 7 or len(d) < 1 + d[0]:
        raise PolicyRefused("Not a single-frame diagnostic request.")
    return build(d[1], d[2:1 + d[0]], frame.addr, table)


# ISO-TP flow control: the one transport frame a tester sends. When a module starts a long answer (a
# first frame), the tester tells it to send the rest: "continue, no block limit, no gap". It carries no
# request, so it's allowed on its own terms: exactly this frame, to the module that's answering.
FLOW_CONTROL = bytes([0x30, 0x00, 0x00, 0, 0, 0, 0, 0])


def flow_control(answering, bus=0) -> p.Frame:
    """The flow control for a long answer from the module answering on this id: to its request id."""
    if not is_physical(answering - 8):
        raise PolicyRefused(f"{answering:03X} isn't a diagnostic answer address.")
    return p.Frame(bus, answering - 8, FLOW_CONTROL)


def is_flow_control(frame) -> bool:
    return not frame.extended and is_physical(frame.addr) and bytes(frame.data) == FLOW_CONTROL


def looks_diagnostic(data) -> bool:
    """Whether a frame's data could be ISO-TP diagnostic traffic (a request, an answer, flow control)."""
    d = bytes(data)
    if not d:
        return False
    kind = d[0] >> 4
    if kind == 0:
        return 1 <= d[0] <= 7 and len(d) > 1 and (d[1] in _DIAG_SIDS or d[1] - 0x40 in _DIAG_SIDS or d[1] == 0x7F)
    return kind in (1, 2, 3)


class VehicleState:
    """Engine speed, vehicle speed and gear, from the address map's broadcast signals and from OBD answers
    (41 0C, 41 0D). Readings count while under FRESH seconds old."""

    def __init__(self, rpm_signal=None, speed_signal=None, gear_signal=None, clock=time.monotonic):
        self.signals = {"rpm": rpm_signal, "speed": speed_signal, "gear": gear_signal}
        self.clock = clock
        self.readings = {}            # name -> (value, time, source)
        self.peak = None              # highest engine speed since the last check
        self.traffic = {}             # bus -> ids carrying ordinary (non-diagnostic) traffic: never sent to

    @property
    def signal(self):                 # the broadcast RPM signal, if any
        return self.signals["rpm"]

    def frame(self, f):
        if not f.extended and 0x700 <= f.addr <= 0x7FF and not looks_diagnostic(f.data):
            self.traffic.setdefault(f.bus, set()).add(f.addr)
        for name, s in self.signals.items():
            if s is not None and f.addr == s.can_id and (s.bus is None or s.bus == f.bus):
                v = s.decode(f.data)
                if v is not None:
                    self._seen(name, v, "broadcast")
        d = f.data
        if 0x7E8 <= f.addr <= 0x7EF and len(d) >= 4 and 3 <= d[0] <= 7 and d[1] == 0x41:
            if d[2] == 0x0C and d[0] >= 4 and len(d) >= 5:
                self._seen("rpm", ((d[3] << 8) | d[4]) / 4, "OBD")
            elif d[2] == 0x0D:
                self._seen("speed", d[3], "OBD")

    def _seen(self, name, value, source):
        self.readings[name] = (value, self.clock(), source)
        if name == "rpm":
            self.peak = value if self.peak is None else max(self.peak, value)

    def fresh(self, name="rpm") -> bool:
        r = self.readings.get(name)
        return r is not None and self.clock() - r[1] <= FRESH

    def value(self, name="rpm"):
        return self.readings[name][0] if self.fresh(name) else None

    @property
    def rpm(self):
        r = self.readings.get("rpm")
        return r[0] if r else None

    @property
    def source(self):
        r = self.readings.get("rpm")
        return r[2] if r else ""

    def _worst_rpm(self):
        """The highest engine speed since the last check (not only the latest), then starts afresh."""
        worst, self.peak = max(self.peak, self.rpm), self.rpm
        return worst

    def check_scan(self):
        """For discovery scans: engine speed known, and every reading since the last check at or below
        MAX_SCAN_RPM."""
        if not self.fresh("rpm"):
            raise ScanBlocked("Engine speed isn't known, so the scan can't check it's below "
                              f"{MAX_SCAN_RPM} rpm. Turn the key on (or idle the engine) and try again.")
        worst = self._worst_rpm()
        if worst > MAX_SCAN_RPM:
            raise ScanBlocked(f"Engine at {worst:.0f} rpm: an OBD scan only runs key-on or at idle "
                              f"(up to {MAX_SCAN_RPM} rpm).")

    def check_engine_off(self):
        """For engine-off requests: engine speed exactly 0 since the last check, vehicle speed 0, and
        Park when the gear is known. Unknown counts as running or moving. The reading at a check counts
        towards the next one too, so after the engine stops it takes a whole interval at 0 to pass."""
        if not self.fresh("rpm"):
            raise EngineNotOff("Engine speed isn't known, so it counts as running.")
        worst = self._worst_rpm()
        if worst != 0:
            raise EngineNotOff(f"The engine is running ({worst:.0f} rpm): this only goes out with the engine off.")
        if not self.fresh("speed"):
            raise EngineNotOff("Vehicle speed isn't known, so the car counts as moving "
                               "(at 0 rpm a hybrid can drive, and stop-start can restart).")
        if self.value("speed") != 0:
            raise EngineNotOff(f"The car is moving ({self.value('speed'):g} km/h).")
        gear = self.signals["gear"]
        if gear is not None and gear.labels and self.fresh("gear"):
            shown = gear.text(self.value("gear"))
            if str(shown).upper() not in ("P", "PARK"):
                raise EngineNotOff(f"The car isn't in Park (gear {shown}).")

    def require(self, tier: Tier):
        if tier is Tier.ENGINE_OFF:
            self.check_engine_off()

    def describe(self) -> str:
        parts = []
        for name, unit in (("rpm", " rpm"), ("speed", " km/h"), ("gear", "")):
            if self.fresh(name):
                v = self.value(name)
                s = self.signals[name]
                parts.append(f"{s.text(v) if name == 'gear' and s is not None else f'{v:g}'}{unit}")
        return ", ".join(parts) or "vehicle state unknown"


class Sender:
    """The one way diagnostic requests reach a link: each is checked against the policy table and
    the vehicle state its tier needs, right before it goes out."""

    def __init__(self, link, state: VehicleState, on_sent=None, table=None):
        self.link = link
        self.state = state
        self.on_sent = on_sent
        self.table = SERVICES if table is None else table
        self.sent = 0

    def _free(self, can_id, bus):
        if can_id in self.state.traffic.get(bus, ()):
            raise PolicyRefused(f"{can_id:03X} carries other traffic on bus {bus}: a request there could pass "
                                "for another module's message, so nothing is sent to it.")

    def flow_control(self, answering, bus=0):
        """Lets a module send the rest of a long answer (ISO-TP). Not a request: no state needed."""
        frame = flow_control(answering, bus)
        self._free(frame.addr, bus)
        self.link.send([frame])
        if self.on_sent:
            self.on_sent(frame)

    def send(self, request: Request, bus=0):
        if self.table.get(request.service.sid) is not request.service:
            raise PolicyRefused(f"{request.service.name} isn't in this policy table.")
        self.state.require(request.service.tier)
        if request.target != FUNCTIONAL:
            self._free(request.target, bus)
        frame = request.frame(bus)
        recognise(frame, self.table)          # what goes out is exactly what the table allows
        self.link.send([frame])
        self.sent += 1
        if self.on_sent:
            self.on_sent(frame)
