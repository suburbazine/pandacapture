"""Per-cylinder knock (E019) and the transmission's speeds and temperature (01A0): decoded whenever they're on
the bus, whoever asks, and read by PandaCapture itself when asked to (UdsPoller, through pandacapture obd).

E019, the ECU's (a Stinger 3.3T's SIM2K-260): asked with 03 22 E0 19 on 0x7E0, answered on 0x7E8 as 62 E0 19 and
72 data bytes over ISO-TP. Offsets count from the first data byte after 62 E0 19:
- 13-18: knock retard, cylinders 1-6: (255 - raw) x 0.75 deg, so 0xFF is none (matched against another tool's
  per-cylinder ignition readings)
- 21-26: per cylinder, 0xFF unless that cylinder has been retarding: meaning unknown, kept raw
- 29-34: per cylinder, rising with rpm and load (about 9-13 at idle, 31-45 at full throttle): likely the knock
  sensors' noise, unverified, kept raw

01A0, the transmission's (the 8-speed): asked with 03 22 01 A0 on 0x7E1, answered on 0x7E9 as 62 01 A0 and 39
data bytes. Offsets count from the first data byte, big-endian: engine rpm 4-5 x 0.25 (matches 0x316), speed 6
km/h, torque-related 7-8 raw, turbine 9-10 x 0.25 rpm, output shaft 11-12 x 0.25 rpm (turbine / output is the
gear's ratio), ATF 13 - 40 C, the ratio 14 / 4 (coarse), gear 21, byte 22's low nibble (follows the gear) raw.
Converter slip is engine speed - turbine speed: near 0 locked, hundreds of rpm on a launch.

The same decodes, CSVs and poller as PandaCapture Android's (obd/Knock.kt, Tcu.kt, UdsPoller.kt, KnockLog.kt).
"""

import datetime as dt
from pathlib import Path

from .diag import nrc_text
from .policy import build

KNOCK_DID, KNOCK_REQUEST = 0xE019, 0x7E0
TCU_DID, TCU_REQUEST = 0x01A0, 0x7E1
CYLINDERS = 6
DEG_PER_COUNT = 0.75

KNOCK_COLUMNS = ([f"knock_cyl{i}" for i in range(1, 7)] + [f"knock_mem{i}" for i in range(1, 7)]
                 + [f"knock_noise{i}" for i in range(1, 7)] + ["spark_deg"])
TCU_COLUMNS = ["engine_rpm", "speed_kmh", "turbine_rpm", "output_rpm", "slip_rpm", "atf_c", "ratio", "gear",
               "byte22_lo", "torque_raw"]


def decode_knock(msg: bytes):
    """A complete E019 answer (62 E0 19 ..., 38 bytes or more) as {retard: [deg x6], memory: [x6], noise: [x6]}."""
    if len(msg) < 3 + 35 or msg[:3] != b"\x62\xe0\x19":
        return None
    b = msg[3:]
    return {"retard": [(255 - b[13 + i]) * DEG_PER_COUNT for i in range(CYLINDERS)],
            "memory": [b[21 + i] for i in range(CYLINDERS)],
            "noise": [b[29 + i] for i in range(CYLINDERS)]}


def decode_tcu(msg: bytes):
    """A complete 01A0 answer (62 01 A0 ..., 26 bytes or more) as a dict of the transmission's values."""
    if len(msg) < 3 + 23 or msg[:3] != b"\x62\x01\xa0":
        return None
    b = msg[3:]

    def be(i):
        return (b[i] << 8) | b[i + 1]
    engine, turbine = be(4) * 0.25, be(9) * 0.25
    return {"engine_rpm": engine, "speed_kmh": b[6], "torque_raw": be(7), "turbine_rpm": turbine,
            "output_rpm": be(11) * 0.25, "atf_c": b[13] - 40, "ratio": b[14] / 4, "gear": b[21],
            "byte22_lo": b[22] & 0x0F, "slip_rpm": engine - turbine}


def knock_row(t, r, spark):
    return (f"{t:.3f}," + ",".join(f"{x:.2f}" for x in r["retard"]) + "," + ",".join(map(str, r["memory"])) + ","
            + ",".join(map(str, r["noise"])) + "," + (f"{spark:.2f}" if spark is not None else ""))


def tcu_row(t, r):
    return (f"{t:.3f},{r['engine_rpm']:.0f},{r['speed_kmh']},{r['turbine_rpm']:.0f},{r['output_rpm']:.0f},"
            f"{r['slip_rpm']:.0f},{r['atf_c']},{r['ratio']:.2f},{r['gear']},{r['byte22_lo']},{r['torque_raw']}")


class IsoTp:
    """Puts one answer id's ISO-TP messages back together (single frames, and first frames with their
    consecutive ones)."""

    def __init__(self, answer_id):
        self.answer_id = answer_id
        self.buf = None
        self.total = self.seq = 0

    def starts(self, f) -> bool:
        """A first frame on this id: the asker owes it flow control."""
        return not f.extended and f.addr == self.answer_id and len(f.data) > 0 and f.data[0] >> 4 == 1

    def frame(self, f):
        """The message this frame completes, if any."""
        d = f.data
        if f.extended or f.addr != self.answer_id or not d:
            return None
        kind = d[0] >> 4
        if kind == 0:
            self.buf = None
            n = d[0]
            return bytes(d[1:1 + n]) if 1 <= n <= 7 and len(d) > n else None
        if kind == 1 and len(d) >= 3:
            self.total = ((d[0] & 0x0F) << 8) | d[1]
            self.buf, self.seq = bytearray(d[2:]), 1
        elif kind == 2 and self.buf is not None:
            if d[0] & 0x0F != self.seq:
                self.buf = None
                return None
            self.buf += d[1:]
            if len(self.buf) >= self.total:
                msg, self.buf = bytes(self.buf[:self.total]), None
                return msg
            self.seq = (self.seq + 1) & 0x0F
        return None


class Watch:
    """E019 and 01A0 answers on the bus, whoever asked: frame(f) returns ("knock" | "tcu", reading) or None."""

    def __init__(self):
        self.knock = IsoTp(KNOCK_REQUEST + 8)
        self.tcu = IsoTp(TCU_REQUEST + 8)

    def frame(self, f):
        if f.extended or f.addr not in (KNOCK_REQUEST + 8, TCU_REQUEST + 8):
            return None
        if f.addr == KNOCK_REQUEST + 8:
            msg = self.knock.frame(f)
            r = decode_knock(msg) if msg else None
            return ("knock", r) if r else None
        msg = self.tcu.frame(f)
        r = decode_tcu(msg) if msg else None
        return ("tcu", r) if r else None


class KnockLog:
    """The E019 and 01A0 answers heard while a log records, whoever asked, as CSVs beside the capture (times in
    Unix seconds, like the capture): obd-knock-<stamp>.csv (knock_cyl1-6 in degrees, knock_mem1-6 and
    knock_noise1-6 raw, and the map's spark then) and obd-tcu-<stamp>.csv (the transmission's speeds, slip and
    ATF). Each file is made at its first answer, so a log without any has none."""

    def __init__(self, out_dir, stamp, address_map=None):
        self.dir, self.stamp = Path(out_dir), stamp
        self.watch = Watch()
        self.spark = next((s for s in (address_map.signals if address_map else []) if s.key == "spark" and not s.derived), None)
        self.spark_deg = None
        self.files, self.counts, self._out = {}, {"knock": 0, "tcu": 0}, {}

    def frame(self, f, t):
        if f.extended or getattr(f, "returned", False) or getattr(f, "rejected", False):
            return
        s = self.spark
        if s is not None and f.addr == s.can_id and (s.bus is None or s.bus == f.bus):
            v = s.decode(f.data)
            if v is not None:
                self.spark_deg = v
        got = self.watch.frame(f)
        if got is None:
            return
        kind, r = got
        out = self._out.get(kind) or self._open(kind)
        out.write((knock_row(t, r, self.spark_deg) if kind == "knock" else tcu_row(t, r)) + "\n")
        self.counts[kind] += 1

    def _open(self, kind):
        self.dir.mkdir(parents=True, exist_ok=True)
        path, n = self.dir / f"obd-{kind}-{self.stamp}.csv", 2
        while path.exists():
            path, n = self.dir / f"obd-{kind}-{self.stamp}-{n}.csv", n + 1
        out = open(path, "w", encoding="utf-8", newline="\n")
        out.write("time," + ",".join(KNOCK_COLUMNS if kind == "knock" else TCU_COLUMNS) + "\n")
        self.files[kind], self._out[kind] = path, out
        return out

    def notes(self) -> list:
        """The lines a recording ends with: each file and how many readings it holds."""
        names = {"knock": "per-cylinder knock (E019)", "tcu": "transmission (01A0)"}
        return [f"{names[k]}: {self.counts[k]} readings in {p.name}" for k, p in self.files.items()]

    def flush(self):
        for out in self._out.values():
            out.flush()

    def close(self):
        for out in self._out.values():
            out.close()
        self._out = {}


def stamp_of(path) -> str:
    """The YYYYMMDD-HHMMSS a capture's file name carries, else now's."""
    import re
    m = re.search(r"(\d{8}-\d{6})", Path(path).name)
    return m.group(1) if m else dt.datetime.now().strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------- asking for them

class Target:
    """A data identifier to read from the module on request_id (answering on request_id + 8)."""

    def __init__(self, name, request_id, did):
        self.name, self.request_id, self.did = name, request_id, did
        self.answer_id = request_id + 8
        self.isotp = IsoTp(self.answer_id)
        self.request = build(0x22, did.to_bytes(2, "big"), request_id)   # the policy's: a READ, any engine speed
        self.next_at = 0.0
        self.period = UdsPoller.START_PERIOD
        self.foreign_until = 0.0
        self.timeouts = self.quick = self.answers = self.rate_answers = 0
        self.stopped = None
        self.status = "starting"

    def due(self, t) -> bool:
        return self.stopped is None and t >= self.next_at and t >= self.foreign_until


def knock_target():
    return Target("Knock", KNOCK_REQUEST, KNOCK_DID)


def transmission_target():
    return Target("Transmission", TCU_REQUEST, TCU_DID)


class UdsPoller:
    """Reads data identifiers itself, over and over, for logs without another tester asking. Only 03 22 HI LO to
    each target's request id and the flow control for its long answer, through the policy's Sender, one request
    on the bus at a time. Driven from the read loop: frame(f, t) for every frame received (not our echoes),
    tick(t, can_send) on every pass. Each target goes its own way:
    - Another tester on its request id (a frame there we didn't send) pauses it until QUIET s after the last of
      them: two askers on one id spoil each other's flow control. Their answers are decoded meanwhile.
    - Rate: from START_PERIOD (5 a second) toward MIN_PERIOD (10 a second) while answers come back quickly
      (ten under FAST s in a row: x 0.9), slower on a timeout or a "busy" refusal (21, 22). "Response pending"
      (78) waits PENDING s more; any other refusal stops that target, and so do MAX_TIMEOUTS timeouts in a row.
    The same as PandaCapture Android's UdsPoller."""

    START_PERIOD, MIN_PERIOD, MAX_PERIOD = 0.2, 0.1, 2.0
    TIMEOUT = 0.15      # an answer later than this is a timeout (a first frame extends it by as much)
    FAST = 0.05         # an answer quicker than this counts toward asking faster
    PENDING = 2.5       # "response pending": how much longer to wait
    QUIET = 2.0         # seconds without another tester on a module's request id before asking it again
    MAX_TIMEOUTS = 8

    def __init__(self, sender, bus, targets):
        self.sender, self.bus = sender, bus
        self.targets = list(targets)
        self.asking = None
        self.sent_at = self.deadline = 0.0
        self.turn = 0
        self.rate_from = 0.0

    @property
    def in_flight(self) -> bool:
        return self.asking is not None

    def due(self, t) -> bool:
        """One of them wants the bus now: a PID poll should hold off."""
        return self.asking is None and any(g.due(t) for g in self.targets)

    @property
    def stopped(self) -> bool:
        return all(g.stopped is not None for g in self.targets)

    @property
    def answers(self) -> int:
        return sum(g.answers for g in self.targets)

    @property
    def status(self) -> str:
        return " · ".join(f"{g.name}: {g.status}" for g in self.targets)

    def frame(self, f, t):
        if f.extended or f.bus != self.bus or getattr(f, "returned", False) or getattr(f, "rejected", False):
            return
        g = next((g for g in self.targets if g.request_id == f.addr), None)
        if g is not None:
            # Not ours (ours come back as echoes, not here): another tester is asking this module. Step aside.
            if t >= g.foreign_until and g.stopped is None:
                g.status = "another tester is asking, reading its answers"
            g.foreign_until = t + self.QUIET
            if self.asking is g:
                self.asking = None
            return
        g = self.asking
        if g is None or f.addr != g.answer_id:
            return
        if g.isotp.starts(f):
            self.sender.flow_control(g.answer_id, self.bus)
            self.deadline = max(self.deadline, t + self.TIMEOUT)   # the rest is on its way
        msg = g.isotp.frame(f)
        if msg is None:
            return
        if msg[:1] == b"\x62" and len(msg) >= 3 and int.from_bytes(msg[1:3], "big") == g.did:
            latency = t - self.sent_at
            self.asking = None
            g.timeouts = 0
            g.answers += 1
            g.rate_answers += 1
            if latency < self.FAST:
                g.quick += 1
                if g.quick >= 10:
                    g.quick, g.period = 0, max(self.MIN_PERIOD, g.period * 0.9)
            else:
                g.quick = 0
        elif msg[:1] == b"\x7f" and len(msg) >= 3 and msg[1] == 0x22:
            code = msg[2]
            if code == 0x78:
                self.deadline = t + self.PENDING
            elif code in (0x21, 0x22):
                self.asking = None
                self._back_off(g, t, 1.0)
                g.status = f"the module said {nrc_text(code)}, slowing down"
            else:
                self.asking = None
                g.stopped = f"refused: {nrc_text(code)}"
                g.status = f"stopped ({g.stopped})"

    def _back_off(self, g, t, pause):
        g.quick = 0
        g.period = min(self.MAX_PERIOD, g.period * 2)
        g.next_at = t + pause

    def tick(self, t, can_send=True):
        """One pass: a timeout, or the next request if one is due and can_send."""
        g = self.asking
        if g is not None and t > self.deadline:
            self.asking = None
            g.timeouts += 1
            if g.timeouts >= self.MAX_TIMEOUTS:
                g.stopped = f"no answer {self.MAX_TIMEOUTS} times in a row"
                g.status = f"stopped ({g.stopped})"
            else:
                self._back_off(g, t, 0.5)
        if t - self.rate_from >= 1.0:
            for x in self.targets:
                if x.stopped is None and t >= x.foreign_until:
                    x.status = f"{x.rate_answers / max(t - self.rate_from, 1e-3):.1f} a second"
                x.rate_answers = 0
            self.rate_from = t
        if not can_send or self.asking is not None:
            return
        for k in range(len(self.targets)):         # round robin among those due
            x = self.targets[(self.turn + k) % len(self.targets)]
            if not x.due(t):
                continue
            self.turn = (self.turn + k + 1) % len(self.targets)
            self.sender.send(x.request, self.bus)
            self.asking, self.sent_at, self.deadline = x, t, t + self.TIMEOUT
            x.next_at = t + x.period
            return
