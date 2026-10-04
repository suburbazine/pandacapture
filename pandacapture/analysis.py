"""Local analysis of a capture for pandacapture analyze: statistics per CAN id, the reference values
(OBD answers in the capture, or a log recorded alongside), and the read-only tools Claude may call.

Everything here runs on this computer. What leaves it is decided in ai.py: the overview and tool results
built here, never the capture itself. CAN ids whose frames carry text (a VIN, a part number) are
withheld entirely, and diagnostic ids (700-7FF) only reach Claude as decoded reference values.
"""

import json
import math
from dataclasses import dataclass, field

from .match import (Candidate, Capture, MatchError, align, fit, obd_reference, partial, pearson, pearson_changes,
                    read_reference)
from .signals import MapError, parse_map

TEXT_RUN = 5           # this many printable letters/digits in a row is text
TEXT_SHARE = 0.2       # an id is withheld when this share of its frames carry text
MAX_POINTS = 120       # most points a series tool returns


def _texty(data) -> bool:
    run = 0
    for b in data:
        run = run + 1 if (48 <= b <= 57 or 65 <= b <= 90) else 0
        if run >= TEXT_RUN:
            return True
    return False


@dataclass
class IdStats:
    can_id: int
    count: int
    rate: float
    dlc: int
    bytes: list = field(default_factory=list)   # per byte: dict(min, max, distinct, changes_per_s, kind)
    mapped: list = field(default_factory=list)  # map signal keys on this id

    def line(self) -> str:
        parts = []
        for i, b in enumerate(self.bytes):
            if b["kind"] == "constant":
                parts.append(f"b{i}={b['min']:02X}")
            else:
                parts.append(f"b{i}:{b['kind']}[{b['min']:02X}-{b['max']:02X},{b['distinct']}v,{b['changes_per_s']:g}/s]")
        mapped = f"  mapped: {', '.join(self.mapped)}" if self.mapped else ""
        return f"0x{self.can_id:03X} {self.rate:6.1f} Hz dlc{self.dlc}  " + " ".join(parts) + mapped


def byte_stats(times, datas, duration):
    n = max(len(d) for d in datas)
    out = []
    for i in range(n):
        vals = [d[i] for d in datas if len(d) > i]
        changes = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        distinct = len(set(vals))
        diffs = {(b - a) & 0xFF for a, b in zip(vals, vals[1:])}
        if distinct == 1:
            kind = "constant"
        elif len(diffs) <= 2 and changes >= 0.9 * (len(vals) - 1) and len(vals) > 10:
            kind = "counter"
        elif changes >= 0.9 * (len(vals) - 1) and distinct > 64:
            kind = "noisy"            # a checksum, or a fast-moving low byte
        else:
            kind = "varies"
        out.append({"min": min(vals), "max": max(vals), "distinct": distinct,
                    "changes_per_s": round(changes / duration, 1) if duration else 0, "kind": kind})
    return out


class Analysis:
    """The capture, its reference, and the tools. bus: which bus to analyse."""

    def __init__(self, capture_path, address_map, ref_path=None, bus=0, ref_rpm="RPM", rpm_key="rpm", log=print):
        self.capture = Capture(capture_path, bus)
        self.map = address_map
        self.bus = bus
        self.log = log
        dur = self.capture.duration or 1.0
        self.duration = dur
        self.withheld = []
        self.stats = {}
        mapped = {}
        for s in address_map.signals:
            if not s.derived and s.can_id is not None:
                mapped.setdefault(s.can_id, []).append(s.key)
        for can_id, (times, datas) in sorted(self.capture.by_id.items()):
            if 0x700 <= can_id <= 0x7FF:
                continue
            if sum(_texty(d) for d in datas) >= TEXT_SHARE * len(datas):
                self.withheld.append(can_id)
                continue
            self.stats[can_id] = IdStats(can_id, len(datas), round(len(datas) / dur, 1), max(len(d) for d in datas),
                                         byte_stats(times, datas, dur), mapped.get(can_id, []))
        # The reference: the ECU's OBD answers in the capture, or a log recorded alongside
        self.ref = None
        self.ref_rpm = None
        self.unit, self.offset = 1.0, 0.0
        if ref_path:
            self.ref = read_reference(ref_path)
            self.ref_rpm = ref_rpm if ref_rpm in self.ref.values else None
            rpm_signal = next((s for s in address_map.signals if s.key == rpm_key and not s.derived), None)
            if self.ref_rpm and rpm_signal:
                self.unit, self.offset = align(self.capture, self.ref, self.ref_rpm, rpm_signal, log)
            else:
                log("Not lining the logs up by RPM (no RPM column or no map RPM signal): times taken as they are.")
        else:
            try:
                self.ref = obd_reference(capture_path, self.capture.frames)
                self.ref_rpm = "OBD RPM" if "OBD RPM" in self.ref.values else None
            except MatchError:
                self.ref = None

    # ---- what Claude sees first ----

    def reference_lines(self):
        if not self.ref:
            return ["(no reference values: no OBD answers in the capture and no log given)"]
        lines = []
        for c in self.ref.columns:
            vals = [v for v in self.ref.values[c] if v is not None]
            if vals:
                lines.append(f"{c}: {len(vals)} values, {min(vals):g} to {max(vals):g}, {len(set(vals))} distinct")
        return lines

    def overview(self) -> str:
        m = self.map
        lines = [
            f"Vehicle map: {m.name}" + (f", {m.bitrate} kbit/s" if m.bitrate else ""),
            f"Capture: bus {self.bus}, {self.duration:.1f} s, {len(self.stats)} broadcast ids analysed"
            + (f", {len(self.withheld)} withheld because their frames carry text" if self.withheld else ""),
            "",
            "Broadcast ids (rate, length, then each byte: constant value, or kind[min-max, distinct values, "
            "changes per second]; byte kinds: varies, counter, noisy = changes nearly every frame):",
            *[s.line() for s in self.stats.values()],
            "",
            "Signals the map already decodes:",
            *[f"  {s.key}: {s.label}" + (f" (0x{s.can_id:03X} byte {s.byte}, {s.bits} bits, {s.order})" if not s.derived
                                         else " (derived)") + f" [{s.source}]" for s in m.signals],
            "",
            "Reference values recorded at the same time" + (f" (aligned by RPM, {self.ref_rpm})" if self.ref_rpm else "")
            + ":",
            *self.reference_lines(),
        ]
        return "\n".join(lines)

    # ---- tools ----

    def _cand(self, a) -> Candidate:
        can_id = int(a["can_id"], 16) if isinstance(a["can_id"], str) else int(a["can_id"])
        if can_id not in self.stats:
            raise ValueError(f"0x{can_id:03X} isn't one of the analysed ids"
                             + (" (withheld: its frames carry text)" if can_id in self.withheld else ""))
        bits = int(a["bits"])
        if bits not in range(1, 33) or not 0 <= int(a["bit"]) <= 7:
            raise ValueError("bits must be 1-32 and bit 0-7")
        return Candidate(can_id, int(a["byte"]), int(a["bit"]), bits, a["order"], bool(a["signed"]))

    def _times(self, points):
        n = max(2, min(int(points), MAX_POINTS))
        return [self.duration * i / (n - 1) for i in range(n)]

    def id_detail(self, a) -> dict:
        can_id = int(a["can_id"], 16) if isinstance(a["can_id"], str) else int(a["can_id"])
        if can_id not in self.stats:
            raise ValueError(f"0x{can_id:03X} isn't one of the analysed ids"
                             + (" (withheld: its frames carry text)" if can_id in self.withheld else ""))
        times, datas = self.capture.by_id[can_id]
        idx = [round(i * (len(datas) - 1) / 15) for i in range(16)] if len(datas) > 16 else range(len(datas))
        s = self.stats[can_id]
        return {"id": f"0x{can_id:03X}", "rate_hz": s.rate, "dlc": s.dlc, "bytes": s.bytes, "mapped": s.mapped,
                "samples": [{"t": round(times[i], 2), "data": datas[i].hex(" ").upper()} for i in idx]}

    def field_series(self, a) -> dict:
        cand = self._cand(a)
        at = self._times(a.get("points", 60))
        vals = self.capture.sample(cand.can_id, cand.decode, at)
        return {"field": cand.describe(), "t": [round(t, 2) for t in at], "raw": vals}

    def reference_series(self, a) -> dict:
        col = a["reference"]
        if not self.ref or col not in self.ref.values:
            raise ValueError(f"no reference column {col!r}")
        at = self._times(a.get("points", 60))
        cap_times = [self.offset + self.unit * t for t in self.ref.times]
        out = []
        for t in at:
            i = min(range(len(cap_times)), key=lambda k: abs(cap_times[k] - t))
            out.append(self.ref.values[col][i])
        return {"reference": col, "t": [round(t, 2) for t in at], "values": out}

    def test_field(self, a) -> dict:
        """How a field tracks a reference column: correlation, its changes, with RPM held, and the fit."""
        cand = self._cand(a)
        col = a["reference"]
        if not self.ref or col not in self.ref.values:
            raise ValueError(f"no reference column {col!r}")
        at = [self.offset + self.unit * t for t in self.ref.times]
        vals = self.capture.sample(cand.can_id, cand.decode, at)
        ref = self.ref.values[col]
        r, n = pearson(vals, ref)
        if r is None:
            return {"field": cand.describe(), "reference": col, "r": None, "n": n,
                    "note": "no correlation: one of them doesn't vary over the overlap"}
        scale, offset = fit(vals, ref)
        out = {"field": cand.describe(), "reference": col, "n": n, "r": round(r, 4),
               "r_changes": _round(pearson_changes(vals, ref)), "scale": _sig(scale), "offset": _sig(offset)}
        if self.ref_rpm and col != self.ref_rpm:
            r_rpm, _ = pearson(vals, self.ref.values[self.ref_rpm])
            r_col_rpm, _ = pearson(ref, self.ref.values[self.ref_rpm])
            out["r_with_rpm_held"] = _round(partial(r, r_rpm, r_col_rpm))
        return out

    def check_signal(self, entry, reference=""):
        """(problem or "", verification): the entry against the map rules, then against the reference."""
        try:
            m = parse_map({"name": "check", "signals": [entry]})
        except MapError as e:
            return str(e), None
        s = m.signals[0]
        if s.can_id not in self.stats:
            return f"0x{s.can_id:03X} isn't one of the analysed ids", None
        if not reference:
            return "", None
        if not self.ref or reference not in self.ref.values:
            return f"no reference column {reference!r}", None
        at = [self.offset + self.unit * t for t in self.ref.times]
        vals = self.capture.sample(s.can_id, s.decode, at)
        r, n = pearson(vals, self.ref.values[reference])
        pairs = [(v, x) for v, x in zip(vals, self.ref.values[reference]) if v is not None and x is not None]
        err = math.sqrt(sum((v - x) ** 2 for v, x in pairs) / len(pairs)) if pairs else None
        return "", {"reference": reference, "r": _round(r), "n": n, "rms_error": _round(err)}


def _round(x, d=4):
    return None if x is None else round(x, d)


def _sig(x):
    return float(f"{x:.6g}")


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))
