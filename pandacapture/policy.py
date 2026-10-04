"""The diagnostic policy: every diagnostic request PandaCapture can send comes from SERVICES.

A request that isn't in the table can't be built, and a link (the panda's OBD path, an ELM327) refuses
any frame that doesn't parse back into a request the table allows. Each service has a tier:

- READ: reads values or codes; goes out whenever asked. (Discovery scans add their own limit: up to
  MAX_SCAN_RPM, in obd.py.)
- ENGINE_OFF: changes something in a module (clears codes, tests an actuator, runs a routine). Only
  sent while the engine is off and the car is stopped, from fresh readings checked right before each
  request. See docs/diagnostics-plan.md.

Some services are never in the table, whatever the engine is doing: NEVER says why.

Today the table holds OBD mode 01 only (pandacapture obd); the plan adds the rest one at a time.
"""

import enum
import time
from dataclasses import dataclass

from . import protocol as p

FRESH = 0.5                  # s: a reading older than this doesn't count
MAX_SCAN_RPM = 900           # discovery scans: key-on or idle only. Fixed on purpose: no option overrides it
FUNCTIONAL = 0x7DF           # every OBD module
PHYSICAL = range(0x7E0, 0x7E8)   # one module each (answers come from the id + 8)


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
    return "" if len(params) == 1 else "mode 01 takes one PID"


SERVICES = {
    0x01: Service(0x01, "OBD current data (mode 01)", Tier.READ, _one_pid),
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
    if target != FUNCTIONAL and target not in PHYSICAL:
        raise PolicyRefused(f"{target:03X} isn't an OBD request address (7DF, 7E0-7E7).")
    if 1 + len(params) > 7:
        raise PolicyRefused(f"{service.name}: longer than one frame.")
    return Request(service, bytes([sid]) + params, target)


def recognise(frame, table=None) -> Request:
    """The request a frame carries, if the policy allows it; links use this to refuse anything else."""
    d = frame.data
    if frame.extended or len(d) < 2 or not 1 <= d[0] <= 7 or len(d) < 1 + d[0]:
        raise PolicyRefused("Not a single-frame diagnostic request.")
    return build(d[1], d[2:1 + d[0]], frame.addr, table)


class VehicleState:
    """Engine speed, vehicle speed and gear, from the address map's broadcast signals and from OBD answers
    (41 0C, 41 0D). Readings count while under FRESH seconds old."""

    def __init__(self, rpm_signal=None, speed_signal=None, gear_signal=None, clock=time.monotonic):
        self.signals = {"rpm": rpm_signal, "speed": speed_signal, "gear": gear_signal}
        self.clock = clock
        self.readings = {}            # name -> (value, time, source)
        self.peak = None              # highest engine speed since the last check

    @property
    def signal(self):                 # the broadcast RPM signal, if any
        return self.signals["rpm"]

    def frame(self, f):
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

    def send(self, request: Request, bus=0):
        if self.table.get(request.service.sid) is not request.service:
            raise PolicyRefused(f"{request.service.name} isn't in this policy table.")
        self.state.require(request.service.tier)
        frame = request.frame(bus)
        recognise(frame, self.table)          # what goes out is exactly what the table allows
        self.link.send([frame])
        self.sent += 1
        if self.on_sent:
            self.on_sent(frame)
