"""Performance runs in a capture: found, timed, and coached.

Ported from PandaCapture Android's run processing (perf/PerfEngine, RunAnalysis, Coach and the panda
Detectors), without its certification: no GPS, road grade, tire calibration, fingerprint or signing. Speeds
are the car's own wheel speeds, so times are as the car measured them.

- RunFinder reads a capture's frames, keeps a sample row at each wheel-speed frame (about 50 a second) of
  the signals runs are judged on (COLUMNS, by map key), and picks out each full-throttle pull, with up to
  8 s before it (a standing start's wait and brake stand), and each stop from 60 mph.
- analyze() times each one (standing 0-60 mph, 60 ft to 1/4 mile; rolling 40-100 mph and the like;
  braking 60-0), finds the full-throttle upshifts and each gear's pull (an estimated wheel power curve),
  checks the data, and coaches: where time went in this run, and against your best earlier run.
- Each pointer comes with a chart: what to plot, over which window, and what to highlight. Measurements in
  its text, the checks' and the events' are units tokens (units.py), shown in the units set when read.
- events() finds brake stands, launches and pop windows (overrun without fuel cut) over the whole log.
"""

import bisect
import datetime as dt
import json
import math
import re
from pathlib import Path

from . import protocol as p
from . import units as U

COLUMNS = [
    "wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr", "speed_kmh", "accel_long_g", "accel_lat_g", "rpm", "gear",
    "pedal", "throttle", "boost_psi", "spark", "iat", "lambda_b1", "lambda_b2", "tqi_acor", "tqfr", "tqi_target",
    "tqi_tcs", "tcs_request", "tcu_torque_limit", "brake_switch", "brake_psi", "lowside_kpa", "lowside_target_kpa",
    "awd_torque_nm", "steer_angle", "fuel_cut",
]
NAN = float("nan")
T = 0
C = {k: i + 1 for i, k in enumerate(COLUMNS)}
SIZE = 1 + len(COLUMNS)

G = 9.80665
SIXTY_KMH = 96.56064
STILL_KMH = 0.5
WOT = 90.0
LIFT = 70.0
LIFT_S = 0.3
TAIL_S = 0.5
PRE_S = 8.0               # time kept before a pull: a standing start's wait and brake stand
PRE_BRAKE_S = 1.0
MIN_PULL_S = 1.0
MIN_GAIN_KMH = 10.0
STALE_S = 0.25
KEEP_S = 90.0
BIAS_SAMPLES = 30_000
CLOCK_PERIOD = 0.02

ROLLOUT_M = 0.3048
TRAP_M = 20.1168
MIN_STILL_S = 0.5
CRR = 0.012
RHO = 1.2
CDA = 0.66                # drag area, m²: a Stinger's Cd about 0.30 on about 2.2 m²
DEFAULT_MASS_KG = 1940.0  # a Stinger GT AWD with a driver

STANDING_SPEEDS = [("0-30 mph", 48.28032), ("0-60 mph", 96.56064), ("0-100 km/h", 100.0), ("0-100 mph", 160.9344),
                   ("0-200 km/h", 200.0)]
STANDING_DISTANCES = [("60 ft", 18.288), ("1/8 mile", 201.168), ("1/4 mile", 402.336)]
ROLLING = [("40-100 mph", 64.37376, 160.9344), ("60-130 mph", 96.56064, 209.21472), ("100-200 km/h", 100.0, 200.0)]
BRAKING = [("60-0 mph", 96.56064), ("100-0 km/h", 100.0)]
# What a run is headlined and compared by, first first, in each system's units: 0-60 mph or 0-100 km/h for a
# standing start (the 60 ft and 0-30 come before it, but they're splits), then the distances, the longer speeds,
# the rolling ones and the stops. 0-60 mph and 0-100 km/h are different runs, not one in two units: each system
# shows its own (shown_in); the drag-strip distances are the same in both. The same order as PandaCapture Android.
HEADLINE_ORDER = {
    U.US: ["0-60 mph", "1/4 mile", "1/8 mile", "0-100 mph", "60-130 mph", "40-100 mph", "60-0 mph", "0-30 mph", "60 ft"],
    U.METRIC: ["0-100 km/h", "1/4 mile", "1/8 mile", "0-200 km/h", "100-200 km/h", "100-0 km/h", "60 ft"],
}


def shown_in(name: str, system) -> bool:
    """Whether a result goes with the system's units: mph runs in US, km/h runs in metric, distances in both."""
    if "mph" in name:
        return U.system_of(system) == U.US
    if "km/h" in name:
        return U.system_of(system) == U.METRIC
    return True


def shown(metrics, system):
    """The results to show in the system's units (all of them, if none is in them)."""
    return [m for m in metrics if shown_in(m["name"], system)] or list(metrics)


def headline(run, system=U.US):
    """The leading standard result shown in the system's units by HEADLINE_ORDER, else the first shown."""
    order = HEADLINE_ORDER[U.system_of(system)]
    ms = shown(run.get("metrics", []), system)
    std = [m for m in ms if m["standard"]]
    if std:
        return min(std, key=lambda m: order.index(m["name"]) if m["name"] in order else len(order))
    return ms[0] if ms else None


def ok(v) -> bool:
    return v is not None and v == v


def front(r):
    return (r[C["wheel_fl"]] + r[C["wheel_fr"]]) / 2 if ok(r[C["wheel_fl"]]) and ok(r[C["wheel_fr"]]) else r[C["speed_kmh"]]


def rear(r):
    return (r[C["wheel_rl"]] + r[C["wheel_rr"]]) / 2 if ok(r[C["wheel_rl"]]) and ok(r[C["wheel_rr"]]) else r[C["speed_kmh"]]


def braking_now(r) -> bool:
    return ok(r[C["brake_switch"]]) and r[C["brake_switch"]] >= 1.5


def ground(r):
    """Ground speed, km/h: the slower axle while driving (the other may be spinning), the faster under
    braking (the other may be locking up)."""
    f, b = front(r), rear(r)
    if not ok(f) or not ok(b):
        return f if ok(f) else b
    return max(f, b) if braking_now(r) else min(f, b)


def fastest(r):
    """The fastest wheel: the first to turn on a launch."""
    w = [r[C[k]] for k in ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr") if ok(r[C[k]])]
    return max(w) if w else r[C["speed_kmh"]]


def mean(xs):
    xs = [x for x in xs if ok(x)]
    return sum(xs) / len(xs) if xs else None


def rnd(v, places):
    return v if v is None or not math.isfinite(v) else round(v, places)


def correlation(x, y):
    if len(x) != len(y) or len(x) < 3:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return None if sxx <= 0 or syy <= 0 else sxy / math.sqrt(sxx * syy)


def interpolate(rows, t, value):
    """value at t, linearly between the rows either side; None outside them."""
    for i in range(1, len(rows)):
        a, b = rows[i - 1], rows[i]
        if a[T] <= t <= b[T]:
            va, vb = value(a), value(b)
            if not ok(va) or not ok(vb):
                return None
            span = b[T] - a[T]
            return va if span <= 0 else va + (vb - va) * (t - a[T]) / span
    return None


def slope_g(t, v, i):
    """The speed's slope at row i, g: across ±0.1 s by time, None where out of reach or over 2 g."""
    j0 = i
    while j0 > 0 and t[i] - t[j0] < 0.1:
        j0 -= 1
    j1 = i
    while j1 < len(t) - 1 and t[j1] - t[i] < 0.1:
        j1 += 1
    span = t[j1] - t[j0]
    if span < 0.15:
        return None
    g = (v[j1] - v[j0]) / 3.6 / span / G
    return g if abs(g) <= 2.0 else None


# ---------------------------------------------------------------- reading a capture

def read_frames(path):
    """(unix seconds, bus, id, extended, data) for each frame in a candump log."""
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("("):
            continue
        try:
            stamp, iface, text = line.split(None, 2)
            t = float(stamp.strip("()"))
            m = re.search(r"(\d+)$", iface)
            f = p.parse_frame_text(text.split()[0])
        except (ValueError, p.PacketError):
            continue
        yield t, int(m.group(1)) if m else 0, f.addr, f.extended, f.data


class RawRun:
    def __init__(self, kind, rows, accel_bias, bias_samples):
        self.kind, self.rows, self.accel_bias, self.bias_samples = kind, rows, accel_bias, bias_samples


class RunFinder:
    """Picks runs out of a stream of frames (see the module docstring). Times are the log's own."""

    def __init__(self, address_map):
        self.by_id = {}
        for s in address_map.signals:
            if not s.derived and s.key in C:
                self.by_id.setdefault(s.can_id, []).append((s, C[s.key]))
        clock = next((s for s in address_map.signals if s.key == "wheel_fl" and not s.derived), None) or \
            next((s for s in address_map.signals if s.key == "speed_kmh" and not s.derived), None)
        self.clock_id = clock.can_id if clock else None
        self.clock_bus = clock.bus if clock else None
        self.latest = [NAN] * SIZE
        self.latest_at = [-math.inf] * SIZE
        self.rows = []
        self.runs = []
        self.accel_bias, self.bias_samples = 0.0, 0
        self.pull_from = self.pull_low_since = self.pull_end = self.pull_start = NAN
        self.brake_from = self.brake_off_since = self.brake_end = self.start_brake = NAN
        self.last_end = -math.inf
        self.burst, self.burst_t, self.before_burst = [], NAN, NAN

    @property
    def usable(self):
        return self.clock_id is not None

    def frame(self, t, bus, can_id, extended, data):
        if extended:
            return
        for s, col in self.by_id.get(can_id, ()):
            if s.bus is not None and s.bus != bus:
                continue
            v = s.decode(data)
            if v is None:
                continue
            self.latest[col] = v
            self.latest_at[col] = t
        if can_id == self.clock_id and (self.clock_bus is None or self.clock_bus == bus):
            self._row(t)

    def flush(self):
        """End of the log: a pull still running is judged as it stands."""
        if not self.rows:
            return
        t = self.rows[-1][T]
        if ok(self.pull_from):
            self.pull_start, self.pull_end, self.pull_from = self.pull_from, t, NAN
        if ok(self.pull_end):
            self._emit_pull(t, force=True)
        self.brake_from = NAN

    def _row(self, t):
        r = [t] + [self.latest[c] if t - self.latest_at[c] <= STALE_S else NAN for c in range(1, SIZE)]
        prev = self.rows[-1] if self.rows else None
        # Frames read together share one timestamp: spread a burst of clock frames back over their period
        if t == self.burst_t:
            self.burst.append(r)
            n = len(self.burst)
            step = CLOCK_PERIOD if not ok(self.before_burst) else min(CLOCK_PERIOD, (t - self.before_burst) / n)
            for k, b in enumerate(self.burst):
                b[T] = t - (n - 1 - k) * step
        else:
            self.before_burst = prev[T] if prev else NAN
            self.burst_t = t
            self.burst = [r]
        self.rows.append(r)
        if len(self.rows) > 1 and t - self.rows[0][T] > KEEP_S + 5:
            cut = bisect.bisect_left([x[T] for x in self.rows[:500]], t - KEEP_S)
            del self.rows[:max(cut, 1)]
        self._learn(r, prev)
        self._follow(r)

    def _learn(self, r, prev):
        """The accelerometer's bias while cruising (not in a pull or a stop): averaged over driving, slopes cancel."""
        f = front(r)
        if prev is None or not ok(r[C["accel_long_g"]]) or not ok(f) or f <= 20 or ok(self.pull_from) or ok(self.brake_from):
            return
        dt_ = r[T] - prev[T]
        fp = front(prev)
        if 0.015 <= dt_ <= 0.2 and ok(fp):
            dvdt = (f - fp) / 3.6 / dt_ / G
            if abs(dvdt) < 0.6:
                w = 1.0 / min(self.bias_samples + 1, BIAS_SAMPLES)
                self.accel_bias += (r[C["accel_long_g"]] - dvdt - self.accel_bias) * w
                self.bias_samples += 1

    def _follow(self, r):
        t = r[T]
        pedal = r[C["pedal"]]
        brake = braking_now(r)
        speed = ground(r)
        wot = ok(pedal) and pedal >= WOT
        # A full-throttle pull: until the pedal has been under 70 % for 0.3 s, or the brake
        if not ok(self.pull_from) and not ok(self.pull_end):
            if wot:
                self.pull_from, self.pull_low_since = t, NAN
        elif ok(self.pull_from):
            low = not ok(pedal) or pedal < LIFT
            if low or brake:
                if not ok(self.pull_low_since):
                    self.pull_low_since = t
            else:
                self.pull_low_since = NAN
            if brake or (ok(self.pull_low_since) and t - self.pull_low_since >= LIFT_S):
                self.pull_end = t if brake else self.pull_low_since
                self.pull_start, self.pull_from = self.pull_from, NAN
        if ok(self.pull_end) and t >= self.pull_end + TAIL_S:
            self._emit_pull(t, force=False)
        # A stop from 60 mph: the brake on at 60 or more, until standing still
        if not ok(self.brake_from) and not ok(self.brake_end):
            if brake and ok(speed) and speed >= SIXTY_KMH:
                self.brake_from, self.brake_off_since = t, NAN
        elif ok(self.brake_from):
            if not brake:
                if not ok(self.brake_off_since):
                    self.brake_off_since = t
            else:
                self.brake_off_since = NAN
            if ok(speed) and speed < STILL_KMH:
                self.brake_end, self.start_brake, self.brake_from = t, self.brake_from, NAN
            elif ok(self.brake_off_since) and t - self.brake_off_since > 0.5:
                self.brake_from = NAN
        if ok(self.brake_end) and t >= self.brake_end + TAIL_S:
            self._emit("braking", self.start_brake - PRE_BRAKE_S, self.brake_end + TAIL_S)
            self.brake_end = NAN

    def _emit_pull(self, t, force):
        end, start = self.pull_end, self.pull_start
        self.pull_end = NAN
        if not ok(start):
            return
        speeds = [ground(r) for r in self.rows if start <= r[T] <= end]
        speeds = [s for s in speeds if ok(s)]
        gain = max(speeds) - min(speeds) if speeds else 0.0
        if end - start < MIN_PULL_S or gain < MIN_GAIN_KMH:
            return
        self._emit("pull", max(self.last_end, start - PRE_S), t if force else end + TAIL_S)

    def _emit(self, kind, frm, to):
        window = [list(r) for r in self.rows if frm <= r[T] <= to]
        if len(window) < 10:
            return
        self.last_end = to
        self.runs.append(RawRun(kind, window, self.accel_bias, self.bias_samples))


# ---------------------------------------------------------------- judging a run

def _crossing_up(t, v, level, frm, to):
    for i in range(1, len(t)):
        if t[i] < frm or t[i - 1] > to:
            continue
        if v[i - 1] < level <= v[i]:
            return t[i - 1] + (t[i] - t[i - 1]) * (level - v[i - 1]) / (v[i] - v[i - 1])
    return None


def _crossing_down(t, v, level, frm, to):
    for i in range(1, len(t)):
        if t[i] < frm or t[i - 1] > to:
            continue
        if v[i - 1] > level >= v[i]:
            return t[i - 1] + (t[i] - t[i - 1]) * (v[i - 1] - level) / (v[i - 1] - v[i])
    return None


def _time_at_distance(t, d, m):
    for i in range(1, len(t)):
        if d[i - 1] < m <= d[i]:
            return t[i - 1] + (t[i] - t[i - 1]) * (m - d[i - 1]) / (d[i] - d[i - 1])
    return None


class Analysis:
    def __init__(self, raw: RawRun, mass_kg=DEFAULT_MASS_KG):
        self.raw = raw
        self.mass = mass_kg
        rows = raw.rows
        self.rows = rows
        self.t = [r[T] for r in rows]
        v, last = [], 0.0
        for r in rows:                                  # ground speed, gaps carried over
            g = ground(r)
            if ok(g):
                last = g
            v.append(last)
        self.v = v
        self.fast = [fastest(r) if ok(fastest(r)) else v[i] for i, r in enumerate(rows)]
        self.pedal = [r[C["pedal"]] for r in rows]
        self.brake = [braking_now(r) for r in rows]

    def col(self, key, i):
        return self.rows[i][C[key]]

    def idx(self, frm, to):
        return [i for i, x in enumerate(self.t) if frm <= x <= to]

    def accel(self):
        """Acceleration, g: the car's accelerometer less its bias where it has one, else the speed's slope."""
        out = []
        for i, r in enumerate(self.rows):
            a = r[C["accel_long_g"]]
            out.append(a - self.raw.accel_bias if ok(a) else (slope_g(self.t, self.v, i) or 0.0))
        return out

    def run(self):
        rows, t, v, pedal = self.rows, self.t, self.v, self.pedal
        if len(rows) < 10:
            return None
        if self.raw.kind == "braking":
            kind, zero = "braking", t[0]
        else:
            wot = next((i for i, x in enumerate(pedal) if ok(x) and x >= WOT), None)
            if wot is None:
                return None
            launch = self._launch_index(wot)
            if launch is not None:
                kind = "standing"
                a = launch - 1
                f = (STILL_KMH - self.fast[a]) / max(self.fast[launch] - self.fast[a], 1e-9)
                zero = t[a] + (t[launch] - t[a]) * min(max(f, 0.0), 1.0)
            else:
                kind, zero = "rolling", t[wot]
        self.kind, self.zero = kind, zero
        d = [0.0] * len(rows)
        for i in range(1, len(rows)):
            d[i] = d[i - 1] + (0.0 if t[i] <= zero else (v[i] + v[i - 1]) / 2 / 3.6 * min(t[i] - t[i - 1], t[i] - zero))
        self.d = d
        wot_idx = [i for i, x in enumerate(pedal) if ok(x) and x >= WOT]
        pull_end = t[wot_idx[-1]] if wot_idx else t[-1]
        self.pull_end = pull_end

        metrics, checks = [], []
        if kind == "standing":
            t_roll = _time_at_distance(t, d, ROLLOUT_M)
            for name, kmh in STANDING_SPEEDS:
                at = _crossing_up(t, v, kmh, zero, pull_end + 0.3)
                if at is not None:
                    metrics.append(_metric(name, at - zero, "s", True, 0.0, at - zero,
                                           rollout=at - t_roll if t_roll is not None else None))
            for name, m in STANDING_DISTANCES:
                at = _time_at_distance(t, d, m)
                if at is None or at > pull_end + 0.3:
                    continue
                trap = None
                if m > 30:
                    a0 = _time_at_distance(t, d, m - TRAP_M)
                    trap = TRAP_M / (at - a0) * 2.236936 if a0 is not None else None
                metrics.append(_metric(name, at - zero, "s", True, 0.0, at - zero, distance_m=m, trap_mph=trap,
                                       rollout=at - t_roll if t_roll is not None else None))
            still = self._still_before(zero)
            checks.append(_check("standing_start", "Started from a stop", still >= MIN_STILL_S,
                                 f"Stood still {still:.1f} s before the first wheel turned (at least {MIN_STILL_S:.1f} s)",
                                 "standing"))
        if kind != "braking":
            for name, a, b in ROLLING:
                ta = _crossing_up(t, v, a, t[0], pull_end)
                if ta is None:
                    continue
                tb = _crossing_up(t, v, b, ta, pull_end + 0.3)
                if tb is None:
                    continue
                inside = self.idx(ta, tb)
                lifted = any(not ok(pedal[i]) or pedal[i] < 85 for i in inside) or any(self.brake[i] for i in inside)
                pedal_a = interpolate(rows, ta, lambda r: r[C["pedal"]]) or 0.0
                full = not lifted and pedal_a >= 85
                metrics.append(_metric(name, tb - ta, "s", True, ta - zero, tb - zero))
                checks.append(_check("full_throttle", "Full throttle throughout", full,
                                     "Pedal at 85 % or more from start to finish" if full
                                     else "The pedal came off (or the brake went on) inside the window", name))
        if kind == "braking":
            for name, frm in BRAKING:
                ta = _crossing_down(t, v, frm, t[0], t[-1])
                if ta is None:
                    continue
                stop = next((t[i] for i in range(len(t)) if t[i] > ta and v[i] < STILL_KMH), None)
                if stop is None:
                    continue
                da = interpolate([[t[i], d[i]] for i in range(len(t))], ta, lambda r: r[1])
                if da is None:
                    continue
                db = d[next(i for i in range(len(t)) if t[i] >= stop)]
                held = all(self.brake[i] and (not ok(pedal[i]) or pedal[i] < 5) for i in self.idx(ta, stop))
                metrics.append(_metric(name, db - da, "m", True, ta - zero, stop - zero, distance_m=db - da))
                metrics.append(_metric(f"{name} time", stop - ta, "s", False, ta - zero, stop - zero))
                checks.append(_check("braking", "Brake on, throttle off throughout", held,
                                     "Brake held to a stop with the pedal up" if held
                                     else "The brake came off or the throttle went on", name))

        shifts = self.shifts()
        pulls = self.gear_pulls()
        stats = self.stats()
        checks = self.data_checks() + checks
        started = dt.datetime.fromtimestamp(zero, dt.timezone.utc)
        return {
            "id": "run-" + started.strftime("%Y%m%dT%H%M%S") + "-" + kind,
            "kind": kind,
            "started": started.isoformat().replace("+00:00", "Z"),
            "zero_unix": round(zero, 3),
            "metrics": metrics,
            "checks": checks,
            "stats": stats,
            "shifts": shifts,
            "pulls": pulls,
            "mass_kg": self.mass,
            "columns": ["t"] + COLUMNS,
            "samples": [[round(r[T] - zero, 3)] + [round(x, 4) if ok(x) else None for x in r[1:]] for r in rows],
        }

    def _launch_index(self, wot):
        """The row where a standing start's first wheel turns: still for MIN_STILL_S, then moving, near full throttle."""
        t, fast = self.t, self.fast
        still_from, found = NAN, None
        for i in range(len(t)):
            if t[i] > t[wot] + 2.0:
                break
            if fast[i] < STILL_KMH:
                if not ok(still_from):
                    still_from = t[i]
            else:
                if ok(still_from) and i > 0 and t[i] - still_from >= MIN_STILL_S and t[i] >= t[wot] - 5.0:
                    found = i
                still_from = NAN
        return found

    def _still_before(self, zero):
        t, fast = self.t, self.fast
        i = max((k for k in range(len(t)) if t[k] < zero), default=-1)
        if i < 0:
            return 0.0
        end = t[i]
        while i >= 0 and fast[i] < STILL_KMH:
            i -= 1
        return end - t[min(i + 1, len(t) - 1)]

    def shifts(self):
        rows, t, v = self.rows, self.t, self.v
        a = self.accel()
        out = []
        gear, pedal_c, limit_c, rpm_c = C["gear"], C["pedal"], C["tcu_torque_limit"], C["rpm"]
        for i in range(1, len(rows)):
            g0, g1 = rows[i - 1][gear], rows[i][gear]
            if not ok(g0) or not ok(g1) or g1 != g0 + 1 or g0 < 1 or g1 > 8:
                continue
            if not (rows[i][pedal_c] >= WOT):
                continue
            near = [k for k in range(len(rows)) if abs(t[k] - t[i]) <= 0.8]

            def limited_at(x):
                return ok(rows[x][limit_c]) and rows[x][limit_c] < 95
            # The transmission holding torque back around the gear change, else the dip in acceleration. The gear
            # number comes up to half a second after the hold, so look further back than ahead
            seeds = [k for k in near if limited_at(k) and t[i] - 0.7 <= t[k] <= t[i] + 0.3]
            if seeds:
                seed = min(seeds, key=lambda k: abs(t[k] - t[i]))
                a0 = a1 = seed
                while a0 > 0 and limited_at(a0 - 1) and t[seed] - t[a0 - 1] <= 0.8:
                    a0 -= 1
                while a1 < len(rows) - 1 and limited_at(a1 + 1) and t[a1 + 1] - t[seed] <= 0.8:
                    a1 += 1
                s0, s1 = t[a0], t[a1]
            else:
                before = mean([a[k] for k in range(len(rows)) if t[i] - 0.6 <= t[k] <= t[i] - 0.3])
                if before is None:
                    continue
                dip = [k for k in near if a[k] < before * 0.75]
                s0, s1 = (t[dip[0]], t[dip[-1]]) if dip else (t[i], t[i])
            rpms = [rows[k][rpm_c] for k in range(len(rows)) if s0 - 0.4 <= t[k] <= t[i] and ok(rows[k][rpm_c])]
            if not rpms:
                continue
            # The old gear at its very end, and the new one once settled (past the boost spike and spark recovery,
            # up to the next torque hold or a lift), so the two compare the gears at nearly the same speed
            g_before = mean([a[k] for k in range(len(rows)) if s0 - 0.12 <= t[k] <= s0 - 0.02])
            stop = next((t[k] for k in range(len(rows)) if t[k] > s1 + 0.1
                         and (limited_at(k) or not (rows[k][pedal_c] >= WOT))), math.inf)
            top = min(s1 + 0.8, stop)
            g_after = None if top - (s1 + 0.3) < 0.2 else mean([a[k] for k in range(len(rows)) if s1 + 0.3 <= t[k] <= top])
            cross = self._cross_rpm(a, s0, g_before, g_after) if g_before is not None and g_after is not None else None
            out.append({"from": int(g0), "to": int(g1), "t": round(s0 - self.zero, 3), "rpm": max(rpms),
                        "kmh": round(v[i], 1), "duration_s": round(s1 - s0, 3),
                        "g_before": rnd(g_before, 3), "g_after": rnd(g_after, 3),
                        "cross_rpm": round(cross) if cross is not None else None})
        return out

    def _cross_rpm(self, a, s0, end, settled):
        """Where the old gear's acceleration met `settled`, the new gear's, in old-gear rpm. Already below at the
        end: the last moment (over 1.5 s back, smoothed over ±0.05 s) it still pulled `settled`. Still above: its
        trend over the last 0.3 s, carried forward; None if it's flat or rising (it wouldn't meet)."""
        rows, t, rpm_c = self.rows, self.t, C["rpm"]
        back = [k for k in range(len(rows)) if s0 - 1.5 <= t[k] <= s0 - 0.02 and ok(rows[k][rpm_c])]
        if not back:
            return None
        if end <= settled:
            def smooth(x):
                m = mean([a[k] for k in range(len(rows)) if abs(t[k] - t[x]) <= 0.05 and t[k] < s0])
                return m if m is not None else a[x]
            hit = next((k for k in reversed(back) if smooth(k) >= settled), None)
            return rows[hit][rpm_c] if hit is not None else None
        recent = [k for k in back if t[k] >= s0 - 0.3]
        if len(recent) < 5:
            return None
        rpms = [rows[k][rpm_c] for k in recent]
        gs = [a[k] for k in recent]
        mr, mg = sum(rpms) / len(rpms), sum(gs) / len(gs)
        sxx = sum((x - mr) ** 2 for x in rpms)
        if sxx <= 0:
            return None
        slope = sum((x - mr) * (y - mg) for x, y in zip(rpms, gs)) / sxx
        if slope >= -1e-5:
            return None
        return rpms[-1] + (settled - end) / slope

    def gear_pulls(self):
        """Each full-throttle stretch in one gear (2nd and up, 1.5 s or more): wheel power from mass x
        acceleration plus rolling and air drag, at each 100 rpm, with boost, spark and AFR there. An estimate:
        it depends on the mass, the road's slope and the drag guesses."""
        rows, t, v = self.rows, self.t, self.v
        out = []
        i = 0
        while i < len(rows):
            g = rows[i][C["gear"]]
            if not ok(g) or g < 2 or not (rows[i][C["pedal"]] >= WOT):
                i += 1
                continue
            j = i
            while j + 1 < len(rows) and rows[j + 1][C["gear"]] == g and rows[j + 1][C["pedal"]] >= WOT:
                j += 1
            if t[j] - t[i] >= 1.5:
                span = list(range(i, j + 1))
                bins = {}
                for x in span:
                    rpm = rows[x][C["rpm"]]
                    if ok(rpm):
                        bins.setdefault(int(math.floor(rpm / 100 + 0.5)) * 100, []).append(x)
                if len(bins) >= 8:
                    axis, hp, tq, boost, spark, afr = [], [], [], [], [], []
                    for rpm in sorted(bins):
                        xs = bins[rpm]
                        mid = xs[len(xs) // 2]
                        x0 = next((k for k in reversed(span) if t[k] <= t[mid] - 0.15), span[0])
                        x1 = next((k for k in span if t[k] >= t[mid] + 0.15), span[-1])
                        dt_ = t[x1] - t[x0]
                        speed = v[mid] / 3.6
                        power = NAN
                        if dt_ > 0.1:
                            acc = (v[x1] - v[x0]) / 3.6 / dt_
                            force = self.mass * acc + CRR * self.mass * G + 0.5 * RHO * CDA * speed * speed
                            power = force * speed
                        good = math.isfinite(power) and power > 0
                        axis.append(rpm)
                        hp.append(round(power / 745.7, 1) if good else None)
                        tq.append(round(power / (rpm * 2 * math.pi / 60) * 0.737562, 1) if good else None)
                        boost.append(rnd(mean([rows[x][C["boost_psi"]] for x in xs]), 2))
                        spark.append(rnd(mean([rows[x][C["spark"]] for x in xs]), 2))
                        lam = mean([mean([rows[x][C["lambda_b1"]], rows[x][C["lambda_b2"]]]) or NAN for x in xs])
                        afr.append(round(lam * 14.7, 2) if lam is not None else None)
                    i_hp = max((k for k in range(len(hp)) if hp[k] is not None), key=lambda k: hp[k], default=None)
                    i_tq = max((k for k in range(len(tq)) if tq[k] is not None), key=lambda k: tq[k], default=None)
                    out.append({"gear": int(g), "t0": round(t[i] - self.zero, 3), "t1": round(t[j] - self.zero, 3),
                                "rpm": axis, "hp": hp, "torque_lbft": tq, "boost_psi": boost, "spark": spark,
                                "afr": afr,
                                "peak_hp": hp[i_hp] if i_hp is not None else None,
                                "peak_hp_rpm": axis[i_hp] if i_hp is not None else None,
                                "peak_torque_lbft": tq[i_tq] if i_tq is not None else None,
                                "peak_torque_rpm": axis[i_tq] if i_tq is not None else None})
            i = j + 1
        return out

    def stats(self):
        rows, t, v = self.rows, self.t, self.v
        a = self.accel()
        run = self.idx(self.zero, self.pull_end if self.kind != "braking" else t[-1])
        moving = [i for i in run if v[i] > 5]
        launch = next((i for i in range(len(t)) if t[i] >= self.zero), None)
        standing = self.kind == "standing" and launch is not None

        def val(key, i):
            x = rows[i][C[key]]
            return x if ok(x) else None
        peak = None
        if self.kind != "braking" and moving:
            peak = round(max(a[i] for i in moving), 3)
        elif run:
            peak = round(min(a[i] for i in run), 3)
        boosts = [rows[i][C["boost_psi"]] for i in run if ok(rows[i][C["boost_psi"]])]
        return {
            "peak_g": peak,
            "average_g": round((v[run[-1]] - v[run[0]]) / 3.6 / max(t[run[-1]] - t[run[0]], 0.1) / G, 3) if len(run) > 2 else None,
            "max_kmh": round(max(v[i] for i in run), 1) if run else None,
            "distance_m": round(self.d[run[-1]], 1) if run else None,
            "launch_rpm": val("rpm", launch) if standing else None,
            "launch_boost_psi": rnd(val("boost_psi", launch), 1) if standing else None,
            "intake_c": val("iat", launch) if launch is not None else None,
            "peak_boost_psi": round(max(boosts), 1) if boosts else None,
        }

    def data_checks(self):
        """Whether the data can be trusted: wheel speeds against the ECU's, the accelerometer against the
        speed, and gaps."""
        rows, t, v = self.rows, self.t, self.v
        out = []
        pairs = [r for r in rows if ok(r[C["speed_kmh"]]) and ok(front(r)) and ok(r[C["wheel_fl"]])]
        if len(pairs) > 20:
            off = sum(1 for r in pairs if abs(r[C["speed_kmh"]] - front(r)) > 3)
            out.append(_check("speeds_agree", "Wheel speeds match the ECU's speed", off <= len(pairs) // 20,
                              f"{off} of {len(pairs)} samples more than {U.kmh(3.0)} apart"))
        ai = [i for i in range(len(rows)) if ok(rows[i][C["accel_long_g"]]) and v[i] > 3 and slope_g(t, v, i) is not None]
        if len(ai) > 40:
            r = correlation([rows[i][C["accel_long_g"]] for i in ai], [slope_g(t, v, i) for i in ai])
            out.append(_check("accel_agrees", "Accelerometer matches the speed", r is not None and r >= 0.85,
                              f"Correlation {r or 0.0:.2f} (0.85 or more)"))
        timed = [x for x in t if x >= self.zero - 1.0]
        gap = max((b - a for a, b in zip(timed, timed[1:])), default=0.0)
        out.append(_check("continuous", "No gaps in the data", gap <= 0.08, f"Longest gap {gap * 1000:.0f} ms (80 or less)"))
        return out


def _metric(name, value, unit, standard, frm, to, rollout=None, trap_mph=None, distance_m=None):
    return {"name": name, "value": round(value, 3), "unit": unit, "standard": standard, "from": round(frm, 3),
            "to": round(to, 3), "rollout": rnd(rollout, 3), "trap_mph": rnd(trap_mph, 1),
            "distance_m": rnd(distance_m, 2)}


def _check(id_, label, passed, detail, scope="run"):
    return {"id": id_, "label": label, "pass": bool(passed), "detail": detail, "scope": scope}


# ---------------------------------------------------------------- coaching

class Coach:
    """Where the time went: plain pointers from one run's samples (launch, traction, the driver's pedal,
    shifts, boost, knock, fuel, heat), then against the best earlier run of the same kind, the speed range
    where most time was lost and what was different there. Each pointer carries a chart: what to plot, the
    window, and the stretch to highlight (times from the run's zero). Measurements in the text are units tokens;
    `units` (the setting when it's analyzed) picks the result it's compared by and the comparison's speed marks."""

    SHIFT_LIMIT_RPM = 6300.0    # shifts at or over this are at the top of the rev range already: never called early
    MIN_SHIFT_GAIN_RPM = 150.0  # a shift point is only called off by this much or more: less is noise, and little time

    def __init__(self, a: Analysis, run: dict, units=U.US):
        self.a, self.run, self.units = a, run, U.system_of(units)
        self.rows, self.t, self.v, self.zero = a.rows, a.t, a.v, a.zero
        self.shifts, self.stats, self.kind = run["shifts"], run["stats"], run["kind"]

    def _rel(self, x):
        return round(x - self.zero, 3)

    def advise(self, best=None):
        out = []
        rows, t, v, zero = self.rows, self.t, self.v, self.zero
        if self.kind == "braking":
            return self._braking(out)
        wot_rows = [i for i in range(len(rows)) if rows[i][C["pedal"]] >= WOT]
        end = t[wot_rows[-1]] if wot_rows else t[-1]
        span = [self._rel(t[0]), self._rel(end + 0.5)]

        if self.kind == "standing":
            full = next((t[i] for i in range(len(rows)) if t[i] >= zero - 1.0 and rows[i][C["pedal"]] >= 95), None)
            if full is not None and full - zero > 0.15:
                out.append(_tip("launch", f"Full throttle came {full - zero:.2f} s after the car moved: floor it as "
                                "you release the brake.",
                                _chart("Pedal at the launch", ["pedal", "speed_mph"], [-1.5, 3.0],
                                       [[0.0, self._rel(full)]], "late throttle")))
            b = self.stats.get("launch_boost_psi")
            if b is not None and b < 2.0:
                rpm = self.stats.get("launch_rpm")
                out.append(_tip("launch", f"Launched at {U.psi(b)}" + (f" and {rpm:,.0f} rpm" if rpm else "") +
                                ": a brake stand to build boost first would cut the 60 ft.",
                                _chart("Boost and rpm before the launch", ["boost_psi", "rpm"], [-4.0, 2.0],
                                       [[-0.2, 0.2]], "launch")))
            slip = []
            for i in self.a.idx(zero, zero + 3.0):
                f, r = front(rows[i]), rear(rows[i])
                if ok(f) and ok(r) and f > 3:
                    slip.append(((r / f - 1) * 100, t[i]))
            if slip and max(s for s, _ in slip) > 10:
                over = [x for s, x in slip if s > 8]
                out.append(_tip("traction", f"Rear wheels spun up to {max(s for s, _ in slip):.0f} % faster than the "
                                f"fronts in the first 3 s ({len(over) * 0.02:.1f} s over 8 %): less launch boost or "
                                "rpm, or more care with tire pressures.",
                                _chart("Wheelspin off the line", ["rear_slip", "tqi_tcs", "awd_torque_nm"], [-0.5, 3.5],
                                       [[self._rel(over[0]), self._rel(over[-1])]] if over else [], "spinning")))
        tcs = [i for i in self.a.idx(zero, end) if ok(rows[i][C["tqi_tcs"]]) and rows[i][C["tqi_tcs"]] < 95]
        if tcs:
            worst = min(rows[i][C["tqi_tcs"]] for i in tcs)
            out.append(_tip("traction", f"Traction control cut torque to {worst:.0f} % for {len(tcs) * 0.02:.1f} s "
                            f"(from {U.kmh(v[tcs[0]])} to {U.kmh(v[tcs[-1]])}): try the traction setting your "
                            "track allows, or ease the launch.",
                            _chart("Traction control", ["tqi_tcs", "rear_slip", "speed_mph"], span,
                                   [[self._rel(t[tcs[0]]), self._rel(t[tcs[-1]])]], "torque cut")))
        slow = [s for s in self.shifts if s["duration_s"] > 0.35]
        if slow:
            out.append(_tip("shifts", "Slow shifts: " + "; ".join(f"{s['from']}-{s['to']} took {s['duration_s']:.2f} s"
                                                                   for s in slow) + ".",
                            _chart("Shifts", ["accel_g", "rpm"], span,
                                   [[s["t"], s["t"] + s["duration_s"]] for s in slow], "slow shift")))
        # A shift is right where the old gear's acceleration has fallen to what the new one gives at that speed: the
        # old gear at its very end against the new one settled. Only worth saying when it's clearly off
        for s in self.shifts:
            text = self._shift_advice(s)
            if text:
                out.append(_tip("shifts", text, _chart(f"The {s['from']}-{s['to']} shift", ["accel_g", "rpm"],
                                                       [s["t"] - 2.0, s["t"] + 2.0], [[s["t"], s["t"] + s["duration_s"]]],
                                                       "shift")))
        peak = self.stats.get("peak_boost_psi")
        if peak is not None and peak > 5:
            wot_at = next((t[i] for i in range(len(rows)) if rows[i][C["pedal"]] >= WOT), zero)
            reached = next((t[i] for i in range(len(rows)) if t[i] >= wot_at and ok(rows[i][C["boost_psi"]])
                            and rows[i][C["boost_psi"]] >= 0.9 * peak), None)
            if reached is not None and reached - wot_at > 1.5:
                out.append(_tip("boost", f"Boost took {reached - wot_at:.1f} s to reach 90 % of its {U.psi(peak)} peak.",
                                _chart("Boost build", ["boost_psi", "rpm"], [self._rel(wot_at) - 1, self._rel(reached) + 2],
                                       [[self._rel(wot_at), self._rel(reached)]], "spooling")))
        # Knock: spark pulled back while the throttle stayed down. Not where something asked for less torque: a
        # shift's torque hold (the transmission's limit) and traction control both retard spark on purpose
        def cutting(k):
            r = rows[k]
            return ((ok(r[C["tcu_torque_limit"]]) and r[C["tcu_torque_limit"]] < 95)
                    or (ok(r[C["tqi_tcs"]]) and r[C["tqi_tcs"]] < 95) or not (r[C["pedal"]] >= WOT))
        # ...nor around a gear change: the shift's spark retard can show before the gear signal changes
        changes = [t[k] for k in range(1, len(rows)) if ok(rows[k][C["gear"]]) and ok(rows[k - 1][C["gear"]])
                   and rows[k][C["gear"]] != rows[k - 1][C["gear"]]]
        knock, worst, worst_rpm, worst_t = 0, 0.0, 0.0, None
        for i in self.a.idx(zero, end):
            j = bisect.bisect_left(t, t[i] + 0.3)
            if j >= len(rows):
                break
            if any(cutting(k) for k in range(i, j + 1)) or any(t[i] - 0.5 <= x <= t[j] + 0.5 for x in changes):
                continue
            s0, s1 = rows[i][C["spark"]], rows[j][C["spark"]]
            r0, r1 = rows[i][C["rpm"]], rows[j][C["rpm"]]
            if all(ok(x) for x in (s0, s1, r0, r1)) and r1 >= r0 and s0 - s1 >= 3 and rows[j][C["gear"]] == rows[i][C["gear"]]:
                knock += 1
                if s0 - s1 > worst:
                    worst, worst_rpm, worst_t = s0 - s1, r1, t[j]
        if knock > 3:
            out.append(_tip("knock", f"Spark dropped by up to {worst:.1f}° at {worst_rpm:,.0f} rpm in the same gear: "
                            "likely knock. Fuel quality, intake heat or the tune.",
                            _chart("Spark under load", ["spark", "rpm", "boost_psi"], span,
                                   [[self._rel(worst_t) - 0.4, self._rel(worst_t) + 0.1]], "spark pulled")))
        deficit = []
        for i in self.a.idx(zero, end):
            act, tgt = rows[i][C["lowside_kpa"]], rows[i][C["lowside_target_kpa"]]
            if ok(act) and ok(tgt):
                deficit.append(((tgt - act) / 6.894757, t[i]))
        if deficit and max(d for d, _ in deficit) > 10:
            low = [x for d, x in deficit if d > 10]
            out.append(_tip("fuel", f"Low-side fuel pressure fell {U.psi(max(d for d, _ in deficit), 0)} under its target: "
                            "the low-pressure pump is struggling.",
                            _chart("Low-side fuel pressure", ["lowside_psi", "lowside_target_psi"], span,
                                   [[self._rel(low[0]), self._rel(low[-1])]], "under target")))
        lean = []
        for i in self.a.idx(zero, end):
            if ok(rows[i][C["boost_psi"]]) and rows[i][C["boost_psi"]] > 8:
                lam = [x for x in (rows[i][C["lambda_b1"]], rows[i][C["lambda_b2"]]) if ok(x)]
                if lam:
                    lean.append((max(lam), t[i]))
        if lean and max(x for x, _ in lean) > 0.9:
            worst_t = max(lean)[1]
            out.append(_tip("fuel", f"A bank ran as lean as {max(x for x, _ in lean) * 14.7:.1f} AFR under boost.",
                            _chart("Mixture under boost", ["afr_b1", "afr_b2", "boost_psi"], span,
                                   [[self._rel(worst_t) - 0.3, self._rel(worst_t) + 0.3]], "lean")))
        iat = self.stats.get("intake_c")
        if iat is not None and iat > 45:
            out.append(_tip("heat", f"Intake air was {U.celsius(iat)} at the start: heat soak costs power. Let it cool "
                            "between runs.", _chart("Intake air", ["iat", "boost_psi"], span, [], "")))
        if best is not None:
            tip = self._compare(best)
            if tip:
                out.insert(0, tip)
        if not out:
            out.append(_tip("clean", "Clean run: no wheelspin, traction-control cuts, slow shifts or knock found.",
                            _chart("The run", ["speed_mph", "accel_g", "rpm"], span, [], "")))
        return out

    def _shift_advice(self, s):
        before, after, cross, rpm = s.get("g_before"), s.get("g_after"), s.get("cross_rpm"), s["rpm"]
        if before is None or after is None:
            return None
        name, old, new = f"{s['from']}-{s['to']}", _ordinal(s["from"]), _ordinal(s["to"])
        if before > after * 1.05:
            if rpm >= self.SHIFT_LIMIT_RPM:
                return None
            if cross is None or cross >= self.SHIFT_LIMIT_RPM:
                return (f"{name} at {rpm:,.0f} rpm was early: {old} still pulled {before:.2f} g at the end against "
                        f"{after:.2f} g in {new} once it settled, and wasn't fading. Holding it toward the limiter "
                        "would be faster.")
            if cross - rpm >= self.MIN_SHIFT_GAIN_RPM:
                return (f"{name} at {rpm:,.0f} rpm was early: {old} still pulled {before:.2f} g at the end against "
                        f"{after:.2f} g in {new} once it settled. They'd meet at about {cross:,.0f} rpm: holding it "
                        "to there would be faster.")
            return None
        if before < after * 0.95 and cross is not None and rpm - cross >= self.MIN_SHIFT_GAIN_RPM:
            return (f"{name} at {rpm:,.0f} rpm came late: {old} had fallen to {before:.2f} g, under the {after:.2f} g "
                    f"{new} gives once settled, from about {cross:,.0f} rpm. Shifting there would be faster.")
        return None

    def _braking(self, out):
        rows, t = self.rows, self.t
        psi = [(rows[i][C["brake_psi"]], t[i]) for i in self.a.idx(self.zero, t[-1]) if ok(rows[i][C["brake_psi"]])]
        if psi:
            peak = max(x for x, _ in psi)
            reached = next((x_t for x, x_t in psi if x >= 0.8 * peak), None)
            if reached is not None and reached - t[0] > 0.4:
                out.append(_tip("braking", f"Brake pressure took {reached - t[0]:.2f} s to build: hit the pedal harder "
                                "at once.",
                                _chart("Brake pressure", ["brake_psi", "speed_mph"], [self._rel(t[0]), self._rel(t[-1])],
                                       [[self._rel(t[0]), self._rel(reached)]], "building")))
        if not out:
            out.append(_tip("clean", "No obvious losses in the stop.",
                            _chart("The stop", ["speed_mph", "brake_psi", "accel_g"], [self._rel(t[0]), self._rel(t[-1])],
                                   [], "")))
        return out

    def _compare(self, best):
        """Against the best earlier run: where along the speed range this one fell behind, and why."""
        theirs = _rows_of(best)
        if not theirs:
            return None
        mine = [[r[T] - self.zero] + r[1:] for r in self.rows]

        def time_at(rs, kmh):
            for i in range(1, len(rs)):
                if rs[i][T] < 0:
                    continue
                a, b = ground(rs[i - 1]), ground(rs[i])
                if ok(a) and ok(b) and a < kmh <= b:
                    return rs[i - 1][T] + (rs[i][T] - rs[i - 1][T]) * (kmh - a) / (b - a)
            return None
        deltas = []
        # Every 10 mph, or every 10 km/h with metric units, so the stretch it names reads round
        step = 10.0 if self.units == U.METRIC else 10 * U.KMH_PER_MPH
        for m in range(1, 21):
            kmh = m * step
            a, b = time_at(mine, kmh), time_at(theirs, kmh)
            if a is not None and b is not None:
                deltas.append((kmh, a - b))
        if len(deltas) < 2:
            return None
        total = deltas[-1][1]
        steps = [(a[0], b[0], b[1] - a[1]) for a, b in zip(deltas, deltas[1:])]
        worst = max(steps, key=lambda s: s[2])
        head = headline(self.run, self.units)
        sign = "behind" if total >= 0 else "ahead of"
        chart = _chart("Time against your best", ["gap"], None, [[worst[0], worst[1]]] if worst[2] >= 0.03
                       else [], "lost here", x="kmh")
        if worst[2] < 0.03:
            return _tip("compare", f"{abs(total):.2f} s {sign} your best by {U.kmh(deltas[-1][0])}, and no one "
                        "stretch stands out.", chart)
        lo, hi = worst[0], worst[1]
        reasons = []
        for s in self.shifts:
            if lo <= s["kmh"] <= hi:
                theirs_s = next((x for x in best.get("shifts", []) if x["from"] == s["from"]), None)
                if theirs_s is not None and s["duration_s"] > theirs_s["duration_s"] + 0.05:
                    reasons.append(f"the {s['from']}-{s['to']} shift took {s['duration_s']:.2f} s against "
                                   f"{theirs_s['duration_s']:.2f}")
                elif theirs_s is None:
                    reasons.append(f"a {s['from']}-{s['to']} shift (none there in your best)")
        my_boost = mean([r[C["boost_psi"]] for r, x in zip(self.rows, self.v) if lo <= x <= hi])
        b_boost = mean([r[C["boost_psi"]] for r in theirs if ok(ground(r)) and lo <= ground(r) <= hi])
        if my_boost is not None and b_boost is not None and b_boost - my_boost > 1.0:
            reasons.append(f"boost averaged {U.psi(my_boost)} against {U.psi(b_boost)}")
        my_slip = [(rear(r) / front(r) - 1) * 100 for r, x in zip(self.rows, self.v)
                   if lo <= x <= hi and ok(front(r)) and front(r) > 3 and ok(rear(r))]
        if my_slip and max(my_slip) > 8:
            reasons.append(f"the rears spun up to {max(my_slip):.0f} %")
        if any(lo <= x <= hi and ok(r[C["tqi_tcs"]]) and r[C["tqi_tcs"]] < 95 for r, x in zip(self.rows, self.v)):
            reasons.append("traction control cut torque")
        return _tip("compare", f"{abs(total):.2f} s {sign} your best" + (f" ({head['name']})" if head else "") +
                    f"; most of it ({worst[2]:.2f} s) between {U.kmh(lo)} and {U.kmh(hi)}" +
                    (", where " + ", ".join(reasons) if reasons else "") + ".", chart)


def _ordinal(n):
    return f"{n}" + ("th" if n % 100 in (11, 12, 13) else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _tip(topic, text, chart):
    return {"topic": topic, "text": text, "chart": chart}


def _chart(title, series, window, highlight, label, x="t"):
    """What the page plots for a pointer: series (column keys, or derived: speed_mph, rear_slip, accel_g,
    afr_b1/b2, lowside_psi, lowside_target_psi, gap), the x window, and the stretches to shade. x is "t" (seconds
    from the run's zero) or "kmh" (the speed, with highlights in km/h; runs saved before units came have "mph")."""
    return {"title": title, "series": series, "x": x, "window": window, "highlight": highlight, "label": label}


def _rows_of(run):
    """A saved run's samples back as rows (times from its zero)."""
    cols = run.get("columns") or []
    if not cols or cols[0] != "t":
        return []
    index = {k: i for i, k in enumerate(cols)}
    out = []
    for s in run.get("samples", []):
        r = [s[0]] + [NAN] * len(COLUMNS)
        for k, c in C.items():
            i = index.get(k)
            if i is not None and s[i] is not None:
                r[c] = s[i]
        out.append(r)
    return out


# ---------------------------------------------------------------- events over the whole log

class Events:
    """Brake stands, launches (to 60 mph, or 100 km/h with metric units, within 20 s) and pop windows (fuel still
    on with the pedal up over 2,000 rpm: an overrun that isn't cutting fuel), as the phone's detectors find them.
    Measurements in the summaries are units tokens."""

    FRESH_S = 1.0
    WHEELS = ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr")

    def __init__(self, keys, units=U.US):
        self.has = set(keys)
        # To 60 mph, or 100 km/h with metric units: the benchmark people know in each
        self.launch_to, self.launch_name = (100.0, "0-100 km/h") if U.system_of(units) == U.METRIC else (SIXTY_KMH, "0-60 mph")
        self.value, self.at = {}, {}
        self.out = []
        self.stand_from = self.still_since = self.launch_from = self.pop_from = None
        self.last_tick = None
        self.g_peak, self.g_sum, self.g_time = None, 0.0, 0.0
        self.stand_brake = self.stand_boost = None
        self.pop_high = self.pop_low = 0.0
        self.pop_spark = None

    def set(self, key, v, t):
        self.value[key], self.at[key] = v, t

    def v(self, key, t):
        x = self.value.get(key)
        return x if x is not None and self.at.get(key, -1e18) >= t - self.FRESH_S else None

    def speed(self, t):
        w = [self.v(k, t) for k in self.WHEELS]
        return sum(w) / 4 if all(x is not None for x in w) else self.v("speed_kmh", t)

    def first_wheel(self, t):
        w = [self.v(k, t) for k in self.WHEELS]
        return max(w) if all(x is not None for x in w) else self.v("speed_kmh", t)

    def tick(self, t):
        dt_ = min(max(t - self.last_tick, 0.0), 0.5) if self.last_tick is not None else 0.0
        self.last_tick = t
        if {"brake_switch", "pedal"} <= self.has:
            self._stand(t)
        self._launch(t, dt_)
        if {"fuel_cut", "pedal", "rpm"} <= self.has:
            self._pop(t)

    def _add(self, kind, frm, to, summary):
        self.out.append({"kind": kind, "from_unix": round(frm, 3), "to_unix": round(to, 3), "summary": summary})

    def _stand(self, t):
        brake, pedal, speed = self.v("brake_switch", t), self.v("pedal", t), self.speed(t)
        on = brake is not None and brake >= 1.5 and pedal is not None and pedal > 80 and speed is not None and speed < STILL_KMH
        if on:
            if self.stand_from is None:
                self.stand_from, self.stand_brake = t, None
            b = self.v("brake_psi", t)
            if b is not None:
                self.stand_brake = max(self.stand_brake or b, b)
            boost = self.v("boost_psi", t)
            if boost is not None:
                self.stand_boost = boost
        elif self.stand_from is not None:
            held = t - self.stand_from
            if held >= 0.5:
                self._add("brake stand", self.stand_from, t, f"held {held:.1f} s" +
                          (f", peak brake {U.psi(self.stand_brake, 0)}" if self.stand_brake is not None else "") +
                          (f", {U.psi(self.stand_boost)} boost at release" if self.stand_boost is not None else ""))
            self.stand_from = None

    def _launch(self, t, dt_):
        speed = self.speed(t)
        if speed is None:
            return
        moving = self.first_wheel(t)
        moving = speed if moving is None else moving
        if self.launch_from is None:
            if moving < STILL_KMH:
                if self.still_since is None:
                    self.still_since = t
            else:
                if self.still_since is not None and t - self.still_since >= 0.5:
                    self.launch_from, self.g_peak, self.g_sum, self.g_time = t, None, 0.0, 0.0
                self.still_since = None
            return
        g = self.v("accel_long_g", t)
        if g is not None:
            self.g_peak = max(self.g_peak if self.g_peak is not None else g, g)
            self.g_sum += g * dt_
            self.g_time += dt_
        if speed >= self.launch_to:
            self._add("launch", self.launch_from, t, f"{self.launch_name} {t - self.launch_from:.2f} s from the first wheel turning" +
                      (f", peak {self.g_peak:.2f} g" if self.g_peak is not None else "") +
                      (f", average {self.g_sum / self.g_time:.2f} g" if self.g_time > 0 else ""))
            self.launch_from = None
        elif t - self.launch_from > 20.0 or moving < STILL_KMH:
            self.launch_from = None
            if moving < STILL_KMH:
                self.still_since = t

    def _pop(self, t):
        cut, pedal, rpm = self.v("fuel_cut", t), self.v("pedal", t), self.v("rpm", t)
        on = cut is not None and cut < 0.5 and pedal is not None and pedal < 1.0 and rpm is not None and rpm > 2000
        if on:
            if self.pop_from is None:
                self.pop_from, self.pop_high, self.pop_low, self.pop_spark = t, rpm, rpm, None
            self.pop_high, self.pop_low = max(self.pop_high, rpm), min(self.pop_low, rpm)
            s = self.v("spark", t)
            if s is not None:
                self.pop_spark = min(self.pop_spark if self.pop_spark is not None else s, s)
        elif self.pop_from is not None:
            length = t - self.pop_from
            if length >= 0.3:
                self._add("pop window", self.pop_from, t, f"{length:.1f} s, {self.pop_high:,.0f} to {self.pop_low:,.0f} rpm" +
                          (f", minimum spark {self.pop_spark:.1f}°" if self.pop_spark is not None else ""))
            self.pop_from = None


# ---------------------------------------------------------------- the whole job

def runs_dir(out_dir) -> Path:
    return Path(out_dir) / "runs"


def saved_runs(folder: Path):
    out = []
    if folder.is_dir():
        for f in sorted(folder.glob("run-*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def saved_summaries(folder: Path, system=U.US):
    """The saved runs, newest first: what the Runs page lists (it picks each one's headline in its own units)."""
    out = []
    for run in saved_runs(folder):
        out.append({"id": run["id"], "kind": run["kind"], "started": run["started"], "capture": run.get("capture", ""),
                    "map": run.get("map", ""), "headline": headline(run, system), "max_kmh": run.get("stats", {}).get("max_kmh"),
                    "metrics": [{k: m.get(k) for k in ("name", "value", "unit", "standard")} for m in run.get("metrics", [])]})
    return sorted(out, key=lambda r: r["started"], reverse=True)


def open_saved(folder: Path, run_id: str, system=U.US):
    """A saved run with the best one it compares with, as the page shows a freshly found one."""
    saved = saved_runs(folder)
    run = next((r for r in saved if r.get("id") == run_id), None)
    if run is None:
        raise ValueError("No saved run by that name.")
    best = best_of([r for r in saved if r["started"] < run["started"]], run, system)
    return {"capture": run.get("capture", ""), "map": run.get("map", ""), "mass_kg": run.get("mass_kg"),
            "runs": [run], "events": [], "bests": {best["id"]: best} if best else {}}


def best_of(saved, run, system=U.US):
    """The best saved run of the same kind and map by this run's headline, other than this one (or an earlier
    reading of it, which has the same id): comparing a run with itself says "0.00 s behind your best"."""
    head = headline(run, system)
    if head is None:
        return None
    found = []
    for r in saved:
        if r.get("id") == run["id"] or r.get("kind") != run["kind"] or r.get("map") != run.get("map"):
            continue
        m = next((m for m in r.get("metrics", []) if m["name"] == head["name"]), None)
        if m is not None:
            found.append((m["value"], r))
    return min(found, key=lambda x: x[0])[1] if found else None


def analyze_capture(path, address_map, mass_kg=DEFAULT_MASS_KG, saved=(), log=lambda s: None, units=U.US):
    """{"runs": [...], "events": [...], "bests": {id: run}, "read_before": n} for a capture; each run judged and
    coached against the best of `saved` (and of the runs before it in this capture) by its headline in `units`.
    read_before counts the runs `saved` already had (the capture read again): saving them replaces those."""
    finder = RunFinder(address_map)
    if not finder.usable:
        raise ValueError(f"the map {address_map.name!r} decodes no wheel or vehicle speed, so runs can't be found")
    keys = {s.key for s in address_map.signals if not s.derived}
    events = Events(keys, units)
    event_sigs = {}
    for s in address_map.signals:
        if not s.derived and s.key in ("brake_switch", "brake_psi", "pedal", "speed_kmh", "boost_psi", "accel_long_g",
                                       "fuel_cut", "rpm", "spark") + Events.WHEELS:
            event_sigs.setdefault(s.can_id, []).append(s)
    n = 0
    for t, bus, can_id, ext, data in read_frames(path):
        n += 1
        finder.frame(t, bus, can_id, ext, data)
        if not ext:
            for s in event_sigs.get(can_id, ()):
                if s.bus is None or s.bus == bus:
                    val = s.decode(data)
                    if val is not None:
                        events.set(s.key, val, t)
            if can_id == finder.clock_id:
                events.tick(t)
    finder.flush()
    log(f"Read {n:,} frames; {len(finder.runs)} candidate runs.")
    pool = list(saved)
    runs, bests = [], {}
    for raw in finder.runs:
        a = Analysis(raw, mass_kg)
        run = a.run()
        if run is None:
            continue
        run["map"] = address_map.name
        run["capture"] = Path(path).name
        best = best_of(pool, run, units)
        run["insights"] = Coach(a, run, units).advise(best)
        run["best_id"] = best["id"] if best else None
        if best:
            bests[best["id"]] = best
        runs.append(run)
        pool.append(run)
    known = {r.get("id") for r in saved}
    return {"capture": Path(path).name, "map": address_map.name, "mass_kg": mass_kg, "runs": runs,
            "events": events.out, "bests": bests, "read_before": sum(1 for r in runs if r["id"] in known)}


def save_runs(result, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    for run in result["runs"]:
        (folder / f"{run['id']}.json").write_text(json.dumps(run, separators=(",", ":")), encoding="utf-8")


def fmt_metric(m, system=U.US):
    v = f"{m['value']:.2f} s" if m["unit"] == "s" else U.show(m["value"], "m", 0 if U.system_of(system) == U.US else 1, system)
    extra = []
    if m.get("trap_mph"):
        extra.append("trap " + U.show(m["trap_mph"] * U.KMH_PER_MPH, "km/h", 1, system))
    if m.get("rollout") is not None:
        extra.append(f"{m['rollout']:.2f} s with 1 ft rollout")
    return f"{m['name']}: {v}" + (f" ({', '.join(extra)})" if extra else "")


def found_text(result) -> str:
    """How many runs a capture gave, and how many replaced an earlier reading of the same run."""
    n, again = len(result["runs"]), result.get("read_before", 0)
    if not n:
        return f"No full-throttle pulls or hard stops in {result['capture']}."
    runs = f"{n} run{'' if n == 1 else 's'}"
    if again == n:
        return f"{result['capture']}: {runs} read again (updated)."
    return f"{result['capture']}: {runs} found" + (f", {again} of them read before (updated)." if again else ".")


def report(result, system=U.US) -> str:
    """The runs as text, in the system's units (results that go with them, measurements converted)."""
    lines = []
    for r in result["runs"]:
        st = r["stats"]
        lines.append(f"{r['kind'].capitalize()} run at {r['started']}: up to {U.show(st['max_kmh'] or 0, 'km/h', 0, system)}"
                     + (f", peak {st['peak_g']:.2f} g" if st.get("peak_g") is not None else ""))
        for m in shown(r["metrics"], system):
            lines.append("  " + fmt_metric(m, system))
        for s in r["shifts"]:
            lines.append(f"  shift {s['from']}-{s['to']} at {s['rpm']:,.0f} rpm, {U.show(s['kmh'], 'km/h', 0, system)}, "
                         f"{s['duration_s']:.2f} s")
        for pl in r["pulls"]:
            if pl.get("peak_hp"):
                lines.append(f"  gear {pl['gear']}: about {U.show(pl['peak_hp'], 'hp', 0, system)} at the wheels at "
                             f"{pl['peak_hp_rpm']:,.0f} rpm")
        for c in r["checks"]:
            if not c["pass"]:
                lines.append(f"  [check] {c['label']}: {U.render(c['detail'], system)}")
        for tip in r["insights"]:
            lines.append(f"  > {U.render(tip['text'], system)}")
    for e in result["events"]:
        lines.append(f"{e['kind']}: {U.render(e['summary'], system)}")
    return "\n".join(lines) if lines else "No full-throttle pulls or hard stops in this capture."


def main(argv) -> int:
    import argparse
    from .capture import default_out_dir
    from .signals import MapError, builtin_maps, load_map
    ap = argparse.ArgumentParser(prog="pandacapture runs", description=(
        "Finds the full-throttle pulls and stops from 60 mph in a capture, times them (0-60, 60 ft to 1/4 mile, "
        "40-100, 60-0 and the like), and points out where time went: launch, wheelspin, traction control, shifts, "
        "boost, knock, fuel and heat, and against your best earlier run. Times use the car's own wheel speeds."))
    ap.add_argument("capture")
    ap.add_argument("--map", help="the vehicle's address map (default: the built-in one)")
    ap.add_argument("--weight-lb", type=float, default=DEFAULT_MASS_KG / 0.45359237,
                    help="car with driver and fuel, lb, for the power estimates (default %(default).0f)")
    ap.add_argument("--out", help="folder for saved runs (default: captures/runs next to the program)")
    ap.add_argument("--no-save", action="store_true", help="don't keep the runs for later comparisons")
    ap.add_argument("--units", choices=U.SYSTEMS, default=U.US,
                    help="show results in US (mph, psi, °F, ft, hp) or metric units (km/h, bar, °C, m, kW); default us")
    args = ap.parse_args(argv)
    try:
        maps = builtin_maps()
        name = args.map or (next(iter(maps)) if len(maps) == 1 else None)
        if not name:
            raise MapError("choose the vehicle's address map with --map (see: pandacapture maps)")
        address_map = load_map(name)
        folder = Path(args.out) if args.out else runs_dir(default_out_dir())
        result = analyze_capture(args.capture, address_map, args.weight_lb * 0.45359237, saved_runs(folder), print,
                                 args.units)
    except (MapError, ValueError, OSError) as e:
        print(f"ERROR: {e}")
        return 1
    print(report(result, args.units))
    if not args.no_save and result["runs"]:
        save_runs(result, folder)
        print(f"\n{found_text(result)} Saved in {folder} for later comparisons.")
    return 0
