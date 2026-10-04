"""Matches unknown CAN fields against a reference recorded at the same time, to find which bytes
carry which values and how they're scaled. The reference is either a log from another tool (a
logger's CSV with a time column), or the OBD answers in the capture itself: when a logger or scan
tool polls the ECU on the same bus, its requests and the ECU's replies are in the capture.

1. Align: the reference log's clock is fitted to the capture's, by finding the time unit and offset
   at which its RPM column best follows the capture's RPM signal (from an address map).
2. Candidates: on every broadcast ID, every byte-aligned 8- and 16-bit field (both byte orders,
   signed and unsigned) and every nibble-aligned 12-bit field.
3. Rank: for each reference column that varies, each candidate's value at the reference's sample
   times is correlated with the column. A least-squares fit gives scale and offset. Two checks
   catch coincidences:
   - the correlation of the changes from row to row, which a slow drift (warming up) can't fake
   - the partial correlation with RPM held fixed, for matches that only both follow engine speed

Diagnostic IDs (0x7xx) are left out: they carry polled request/response traffic, not fields.
A strong match is evidence, not proof: check it, and mark it "observed" in a map until verified.
"""

import bisect
import csv
import math
from dataclasses import dataclass
from pathlib import Path

from .logs import read_replay

UNITS = (0.1, 1.0, 0.01, 0.001)   # reference time units to try, in seconds
MIN_DISTINCT = 5                  # reference columns need this many distinct values to be matched


class MatchError(Exception):
    pass


@dataclass
class Reference:
    path: str
    columns: list          # names, without the time column
    times: list            # in the reference's own unit
    values: dict           # column -> list of floats (None where missing)


def read_reference(path, time_column=None) -> Reference:
    """A CSV whose header row starts with a time column (some loggers put settings rows first)."""
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    names = (time_column,) if time_column else ("timestamp", "time", "time_s", "t")
    hi = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() in names), None)
    if hi is None:
        raise MatchError(f"{path}: no header row starting with a time column ({', '.join(names)})")
    header = [h.strip() for h in rows[hi]]
    times, values = [], {h: [] for h in header[1:] if h}
    for r in rows[hi + 1:]:
        if not r or not r[0].strip():
            continue
        try:
            t = float(r[0])
        except ValueError:
            continue
        times.append(t)
        for i, h in enumerate(header[1:], 1):
            if not h:
                continue
            try:
                values[h].append(float(r[i]))
            except (ValueError, IndexError):
                values[h].append(None)
    if len(times) < 10:
        raise MatchError(f"{path}: only {len(times)} rows")
    return Reference(str(path), [h for h in header[1:] if h], times, values)


# SAE J1979 mode 01 PIDs: (bytes, name, conversion)
OBD_PIDS = {
    0x04: (1, "OBD load %", lambda d: d[0] * 100 / 255),
    0x05: (1, "OBD coolant C", lambda d: d[0] - 40),
    0x06: (1, "OBD STFT bank 1 %", lambda d: d[0] / 1.28 - 100),
    0x07: (1, "OBD LTFT bank 1 %", lambda d: d[0] / 1.28 - 100),
    0x08: (1, "OBD STFT bank 2 %", lambda d: d[0] / 1.28 - 100),
    0x09: (1, "OBD LTFT bank 2 %", lambda d: d[0] / 1.28 - 100),
    0x0B: (1, "OBD MAP kPa", lambda d: d[0]),
    0x0C: (2, "OBD RPM", lambda d: ((d[0] << 8) | d[1]) / 4),
    0x0D: (1, "OBD speed km/h", lambda d: d[0]),
    0x0E: (1, "OBD timing advance deg", lambda d: d[0] / 2 - 64),
    0x0F: (1, "OBD IAT C", lambda d: d[0] - 40),
    0x10: (2, "OBD MAF g/s", lambda d: ((d[0] << 8) | d[1]) / 100),
    0x11: (1, "OBD throttle %", lambda d: d[0] * 100 / 255),
    0x23: (2, "OBD fuel rail gauge kPa", lambda d: ((d[0] << 8) | d[1]) * 10),
    0x33: (1, "OBD baro kPa", lambda d: d[0]),
    0x34: (4, "OBD lambda sensor 1", lambda d: ((d[0] << 8) | d[1]) / 32768),
    0x38: (4, "OBD lambda sensor 5", lambda d: ((d[0] << 8) | d[1]) / 32768),
    0x42: (2, "OBD module voltage V", lambda d: ((d[0] << 8) | d[1]) / 1000),
    0x43: (2, "OBD absolute load %", lambda d: ((d[0] << 8) | d[1]) * 100 / 255),
    0x44: (2, "OBD commanded lambda", lambda d: ((d[0] << 8) | d[1]) / 32768),
    0x45: (1, "OBD relative throttle %", lambda d: d[0] * 100 / 255),
    0x46: (1, "OBD ambient C", lambda d: d[0] - 40),
    0x49: (1, "OBD pedal D %", lambda d: d[0] * 100 / 255),
    0x4A: (1, "OBD pedal E %", lambda d: d[0] * 100 / 255),
    0x4C: (1, "OBD commanded throttle %", lambda d: d[0] * 100 / 255),
    0x59: (2, "OBD fuel rail absolute kPa", lambda d: ((d[0] << 8) | d[1]) * 10),
    0x5C: (1, "OBD oil temp C", lambda d: d[0] - 40),
}


def obd_answers(frames):
    """Mode 01 answers from ECUs (0x7E8-0x7EF), reassembled from ISO-TP: name -> [(time, value)]."""
    out, partial = {}, {}
    for t, f in frames:
        if not 0x7E8 <= f.addr <= 0x7EF or not f.data:
            continue
        d, kind = f.data, f.data[0] >> 4
        msg = None
        if kind == 0:
            msg, start = d[1:1 + (d[0] & 0x0F)], t
        elif kind == 1 and len(d) >= 2:
            partial[f.addr] = [((d[0] & 0x0F) << 8) | d[1], bytearray(d[2:]), t]
        elif kind == 2 and f.addr in partial:
            need, buf, start = partial[f.addr]
            buf += d[1:]
            if len(buf) >= need:
                msg = bytes(buf[:need])
                del partial[f.addr]
        if not msg or msg[0] != 0x41:
            continue
        i = 1
        while i < len(msg) and msg[i] in OBD_PIDS:
            n, name, conv = OBD_PIDS[msg[i]]
            if i + 1 + n > len(msg):
                break
            if f.addr != 0x7E8:
                name += f" ({f.addr:03X})"
            out.setdefault(name, []).append((start, conv(msg[i + 1:i + 1 + n])))
            i += 1 + n
    return out


def obd_reference(path, frames) -> "Reference":
    """The capture's own OBD answers, as a reference on the capture's clock: a row per RPM answer, at the
    time it arrived. Other values take the answer nearest that time (a poller asks for them in the same
    cycle, just before or after)."""
    answers = obd_answers(frames)
    if "OBD RPM" not in answers:
        raise MatchError("no OBD mode 01 RPM answers in this capture: nothing was polling the ECU")
    times = [t for t, _ in answers["OBD RPM"]]
    values = {"OBD RPM": [v for _, v in answers["OBD RPM"]]}
    for name, series in answers.items():
        if name == "OBD RPM":
            continue
        ts = [t for t, _ in series]
        col = []
        for t in times:
            i = bisect.bisect_left(ts, t)
            near = [j for j in (i - 1, i) if 0 <= j < len(ts)]
            col.append(series[min(near, key=lambda j: abs(ts[j] - t))][1] if near else None)
        values[name] = col
    columns = ["OBD RPM"] + sorted(n for n in values if n != "OBD RPM")
    return Reference(f"{path} (OBD answers)", columns, times, values)


class Capture:
    """Frames by ID, with the data of every frame and its time."""

    def __init__(self, path, bus=None):
        frames = read_replay(path)   # (seconds from the first frame, Frame)
        if not frames:
            raise MatchError(f"{path}: no frames")
        self.frames = frames
        self.duration = frames[-1][0]
        self.by_id = {}
        for t, f in frames:
            if bus is not None and f.bus != bus:
                continue
            times, datas = self.by_id.setdefault(f.addr, ([], []))
            times.append(t)
            datas.append(f.data)

    def sample(self, can_id, decode, at_times):
        """decode() of the latest frame at or before each time (None before the first)."""
        times, datas = self.by_id[can_id]
        out = []
        for t in at_times:
            i = bisect.bisect_right(times, t) - 1
            out.append(decode(datas[i]) if i >= 0 else None)
        return out


def pearson(xs, ys):
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 8:
        return None, n
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx <= 0 or syy <= 0:
        return None, n
    return sum((x - mx) * (y - my) for x, y in pairs) / math.sqrt(sxx * syy), n


def pearson_changes(xs, ys):
    """Correlation of the row-to-row changes: a shared slow drift doesn't make these agree."""
    dx, dy = [], []
    for i in range(1, len(xs)):
        if None in (xs[i], xs[i - 1], ys[i], ys[i - 1]):
            continue
        dx.append(xs[i] - xs[i - 1])
        dy.append(ys[i] - ys[i - 1])
    return pearson(dx, dy)[0]


def fit(xs, ys):
    """Least squares y = a*x + b."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    a = sum((x - mx) * (y - my) for x, y in pairs) / sxx
    return a, my - a * mx


def partial(r_xy, r_xz, r_yz):
    """Correlation of x and y with z held fixed."""
    if None in (r_xy, r_xz, r_yz) or abs(r_xz) >= 1 or abs(r_yz) >= 1:
        return None
    return (r_xy - r_xz * r_yz) / math.sqrt((1 - r_xz ** 2) * (1 - r_yz ** 2))


def align(capture, ref, ref_rpm, rpm_signal, log=print):
    """(unit, offset): capture time = offset + unit * reference time, best RPM correlation."""
    can_rpm = rpm_signal.decode
    if rpm_signal.can_id not in capture.by_id:
        raise MatchError(f"the capture has no 0x{rpm_signal.can_id:X} frames for {rpm_signal.key}")
    rpm = ref.values[ref_rpm]
    best = (-2, None, None)
    for unit in UNITS:
        span = (ref.times[-1] - ref.times[0]) * unit
        if span > capture.duration * 1.5 or span < 1:
            continue
        lo, hi = -span, capture.duration
        for step in (0.5, 0.05, 0.005):
            centre = best[2] if best[1] == unit else None
            if centre is not None:
                lo, hi = centre - step * 20, centre + step * 20
            for k in range(int((hi - lo) / step) + 1):
                off = lo + k * step
                at = [off + unit * t for t in ref.times]
                r, n = pearson(capture.sample(rpm_signal.can_id, can_rpm, at), rpm)
                if r is not None and n >= len(rpm) * 0.5 and r > best[0]:
                    best = (r, unit, off)
    r, unit, off = best
    if unit is None or r < 0.8:
        raise MatchError(f"couldn't line the logs up: best RPM correlation {r:.2f}. Were they recorded at the same time?")
    log(f"Aligned: reference time unit {unit:g} s, offset {off:+.3f} s into the capture, RPM r = {r:.4f}")
    return unit, off


@dataclass
class Candidate:
    can_id: int
    byte: int
    bit: int
    bits: int
    order: str
    signed: bool

    def decode(self, data):
        if self.order == "little":
            if len(data) * 8 < self.byte * 8 + self.bit + self.bits:
                return None
            raw = (int.from_bytes(data, "little") >> (self.byte * 8 + self.bit)) & ((1 << self.bits) - 1)
        else:
            n = (self.bit + self.bits + 7) // 8
            if len(data) < self.byte + n:
                return None
            raw = (int.from_bytes(data[self.byte:self.byte + n], "big") >> self.bit) & ((1 << self.bits) - 1)
        if self.signed and raw & (1 << (self.bits - 1)):
            raw -= 1 << self.bits
        return raw

    def describe(self):
        s = f"0x{self.can_id:03X} byte {self.byte}"
        if self.bit:
            s += f" bit {self.bit}"
        s += f", {self.bits} bits"
        if self.bits > 8:
            s += " big-endian" if self.order == "big" else ""
        return s + (", signed" if self.signed else "")

    def map_fields(self):
        d = {"id": f"0x{self.can_id:X}", "byte": self.byte}
        if self.bit:
            d["bit"] = self.bit
        if self.bits != 8:
            d["bits"] = self.bits
        if self.order != "little":
            d["order"] = self.order
        if self.signed:
            d["signed"] = True
        return d


def candidates(capture):
    for can_id, (_, datas) in sorted(capture.by_id.items()):
        if 0x700 <= can_id <= 0x7FF:
            continue
        n = max(len(d) for d in datas[:50])
        for b in range(n):
            for signed in (False, True):
                yield Candidate(can_id, b, 0, 8, "little", signed)
                if b + 1 < n:
                    yield Candidate(can_id, b, 0, 16, "little", signed)
                    yield Candidate(can_id, b, 0, 16, "big", signed)
        for nib in range(0, n * 2 - 2):
            yield Candidate(can_id, nib // 2, (nib % 2) * 4, 12, "little", False)


@dataclass
class Match:
    column: str
    candidate: Candidate
    r: float
    r_changes: float
    r_partial: float
    scale: float
    offset: float
    known: str


def run(capture_path, ref_path, address_map, ref_rpm="RPM", rpm_key="rpm", columns=None, top=5, bus=0,
        min_r=0.9, log=print):
    """ref_path None: use the capture's own OBD answers as the reference."""
    capture = Capture(capture_path, bus)
    if ref_path is None:
        ref = obd_reference(capture_path, capture.frames)
        ref_rpm = "OBD RPM"
        unit, off = 1.0, 0.0
        log(f"Reference: the ECU's OBD answers in the capture ({len(ref.times)} answers, "
            f"{len(ref.columns)} values: {', '.join(ref.columns)})")
    else:
        ref = read_reference(ref_path)
        if ref_rpm not in ref.values:
            raise MatchError(f"the reference log has no {ref_rpm!r} column; choose one with --ref-rpm")
        rpm_signal = next((s for s in address_map.signals if s.key == rpm_key and not s.derived), None)
        if rpm_signal is None:
            raise MatchError(f"the map has no decoded signal {rpm_key!r} to align with")
        unit, off = align(capture, ref, ref_rpm, rpm_signal, log)
    at = [off + unit * t for t in ref.times]

    # Reference columns worth matching: they vary
    wanted = []
    for c in columns or ref.columns:
        vals = [v for v in ref.values.get(c, []) if v is not None]
        if len(set(vals)) >= MIN_DISTINCT:
            wanted.append(c)
        elif columns:
            log(f"  {c}: skipped, it barely varies in this log")
    skipped = [c for c in ref.columns if c not in wanted]
    log(f"Matching {len(wanted)} columns that vary: {', '.join(wanted)}")
    if skipped and not columns:
        log(f"  (constant or nearly so in this log, so not matchable: {', '.join(skipped)})")

    # RPM at every row, carried forward from its last answer: a pandacapture obd CSV has one answer per row
    rpm_at, last = [], None
    for v in ref.values[ref_rpm]:
        last = v if v is not None else last
        rpm_at.append(last)
    known = {(s.can_id, s.byte, s.bit, s.bits, s.order): s.key for s in address_map.signals if not s.derived}
    series = []
    for cand in candidates(capture):
        vals = capture.sample(cand.can_id, cand.decode, at)
        present = [v for v in vals if v is not None]
        if len(set(present)) < 3:
            continue
        series.append((cand, vals))
    log(f"Testing {len(series)} candidate fields on {len(capture.by_id)} IDs against {len(ref.times)} reference rows")

    results = {}
    for c in wanted:
        # Only this column's own rows, so the changes from one answer to the next have neighbours to compare
        rows = [i for i, v in enumerate(ref.values[c]) if v is not None]
        col = [ref.values[c][i] for i in rows]
        rpm_c = [rpm_at[i] for i in rows]
        r_col_rpm, _ = pearson(col, rpm_c)
        found = []
        for cand, all_vals in series:
            vals = [all_vals[i] for i in rows]
            r, n = pearson(vals, col)
            if r is None or abs(r) < min_r:
                continue
            a, b = fit(vals, col)
            key = known.get((cand.can_id, cand.byte, cand.bit, cand.bits, cand.order), "")
            held = None
            if c != ref_rpm:
                r_rpm, _ = pearson(vals, rpm_c)
                held = partial(r, r_rpm, r_col_rpm)
            found.append(Match(c, cand, r, pearson_changes(vals, col), held, a, b, key))
        # Strongest first. Near-ties (within 0.001) go to a field the map already has, then the simplest
        # layout: byte-aligned, 8 bits, little-endian, unsigned. A wider field that merely contains the
        # real byte correlates just as well, so it mustn't win.
        found.sort(key=lambda m: (-round(abs(m.r), 3), not m.known, m.candidate.bit != 0, m.candidate.bits,
                                  m.candidate.order != "little", m.candidate.signed))
        results[c] = dedupe(found, top)
    return results, (unit, off)


def _span(cand):
    start = cand.byte * 8 + cand.bit
    return start, start + cand.bits


def dedupe(found, top):
    """Keeps one field per overlapping bit range of an ID: the same byte read as 8, 12 or 16 bits,
    signed or not, is one finding."""
    kept = []
    for m in found:
        a0, a1 = _span(m.candidate)
        if any(k.candidate.can_id == m.candidate.can_id and a0 < _span(k.candidate)[1] and _span(k.candidate)[0] < a1
               for k in kept):
            continue
        kept.append(m)
        if len(kept) == top:
            break
    return kept


def verdict(m) -> str:
    if m.r_changes is not None and abs(m.r_changes) < 0.5:
        return "drift only?"
    if m.r_partial is not None and abs(m.r_partial) < 0.5:
        return "follows RPM?"
    return ""


def report(results, log=print):
    for column, found in results.items():
        log(f"\n{column}")
        if not found:
            log("  no field tracks it (it may not be broadcast on this bus)")
            continue
        log(f"  {'r':>7} {'changes':>7} {'RPM held':>8}  field                                 value = raw x scale + offset")
        for m in found:
            ch = "" if m.r_changes is None else f"{m.r_changes:+.2f}"
            part = "" if m.r_partial is None else f"{m.r_partial:+.2f}"
            note = " ".join(x for x in (f"(map: {m.known})" if m.known else "", verdict(m)) if x)
            log(f"  {m.r:+7.4f} {ch:>7} {part:>8}  {m.candidate.describe():<37} x{m.scale:.6g} {m.offset:+.6g}  {note}")
