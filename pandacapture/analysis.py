"""Local analysis of a capture for pandacapture analyze: statistics per CAN id, reference values, and the
read-only tools Claude may call.

Everything here runs on this computer. What leaves it is decided in ai.py: the overview and the tool results
built here, never the capture file itself. CAN ids whose frames carry text (a VIN, a part number) are
withheld entirely; diagnostic ids (700-7FF) only appear as decoded reference values; the capture's header
lines (which name the panda's serial number) are never read out, only its markers and events.

References, all on the capture's clock (seconds from its first frame):
- the engine computer's OBD answers in the capture ("OBD ..." columns, as pandacapture match --obd)
- a log recorded alongside: a logger's CSV (lined up by RPM), or a pandacapture obd CSV (by clock time)
- any frame signal of the address map, decoded from the capture ("map:<key>")
"""

import bisect
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .match import (Candidate, Capture, MatchError, align, candidates, dedupe, fit, obd_reference, partial,
                    pearson, pearson_changes, read_reference, Match)
from .signals import MapError, parse_map

TEXT_RUN = 5           # this many printable letters/digits in a row is text
TEXT_SHARE = 0.2       # an id is withheld when this share of its frames carry text
MAX_POINTS = 120       # most points a series tool returns
MAX_FRAMES = 200       # most frames frames_window returns
MAP_GRID = 0.1         # s between samples of a map signal used as a reference
COUNTER_CHANGE_RATE = 0.5   # fields changing on more than this share of frames are counters/checksums

EVENT = re.compile(r"^#\s*(marker\s+\S+|stall ended|stall|adapter error|reconnected|panda dropped|dropped)\b(.*?)\(?"
                   r"(\d{9,}\.\d+)\)?", re.I)


def _texty(data) -> bool:
    run = 0
    for b in data:
        run = run + 1 if (48 <= b <= 57 or 65 <= b <= 90) else 0
        if run >= TEXT_RUN:
            return True
    return False


def _parse_id(value) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


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


def byte_stats(datas, duration):
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


@dataclass
class Column:
    """A reference: values at times on the capture's clock."""
    name: str
    source: str
    times: list
    values: list
    rpm: str = ""             # the RPM column from the same source, for "with RPM held"


# ---------------------------------------------------------------- checksums

def _crc8(data, poly, init, xorout):
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ xorout


def _nibbles(data):
    for b in data:
        yield b >> 4
        yield b & 0x0F


CHECKSUMS = {
    "xor of the other bytes": lambda o, can_id: _xor(o),
    "sum of the other bytes": lambda o, can_id: sum(o) & 0xFF,
    "sum of the other bytes and the id": lambda o, can_id: (sum(o) + (can_id & 0xFF) + (can_id >> 8)) & 0xFF,
    "0x100 minus the sum of the other bytes": lambda o, can_id: (0x100 - sum(o)) & 0xFF,
    "CRC-8 SAE J1850 (poly 1D, init FF, xorout FF)": lambda o, can_id: _crc8(o, 0x1D, 0xFF, 0xFF),
    "CRC-8 AUTOSAR (poly 2F, init FF, xorout FF)": lambda o, can_id: _crc8(o, 0x2F, 0xFF, 0xFF),
    "CRC-8 (poly 07, init 00)": lambda o, can_id: _crc8(o, 0x07, 0x00, 0x00),
}
NIBBLE_CHECKSUMS = {
    "sum of the other nibbles": lambda n: sum(n) & 0x0F,
    "16 minus the sum of the other nibbles": lambda n: (16 - sum(n)) & 0x0F,
    "xor of the other nibbles": lambda n: _xor(n) & 0x0F,
}


def _xor(values):
    x = 0
    for v in values:
        x ^= v
    return x


# ---------------------------------------------------------------- the analysis

class Analysis:
    """The capture, its references, and the tools. bus: which bus to analyse."""

    def __init__(self, capture_path, address_map, ref_path=None, bus=0, ref_rpm="RPM", rpm_key="rpm", log=print):
        self.capture = Capture(capture_path, bus)
        self.map = address_map
        self.bus = bus
        self.log = log
        self.duration = self.capture.duration or 1.0
        self.first_unix, self.events = self._read_events(capture_path)
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
            self.stats[can_id] = IdStats(can_id, len(datas), round(len(datas) / self.duration, 1),
                                         max(len(d) for d in datas), byte_stats(datas, self.duration),
                                         mapped.get(can_id, []))
        self.columns = {}
        self._add_obd(capture_path)
        if ref_path:
            self._add_log(ref_path, ref_rpm, rpm_key)

    # ---- reading the capture's markers and events (never its header) ----

    @staticmethod
    def _read_events(path):
        first, events = None, []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("(") and first is None:
                    try:
                        first = float(line[1:line.index(")")])
                    except ValueError:
                        pass
                elif line.startswith("#"):
                    m = EVENT.match(line.strip())
                    if m:
                        events.append((m.group(1).strip().lower(), float(m.group(3))))
        return first, events

    def markers(self, a=None) -> dict:
        base = self.first_unix or 0
        return {"markers_and_events": [{"what": what, "t": round(unix - base, 2)} for what, unix in self.events],
                "duration_s": round(self.duration, 2)}

    # ---- references ----

    def _add_obd(self, capture_path):
        try:
            ref = obd_reference(capture_path, self.capture.frames)
        except MatchError:
            return
        rpm = "OBD RPM" if "OBD RPM" in ref.values else ""
        for c in ref.columns:
            self.columns[c] = Column(c, "the engine computer's OBD answers in the capture", list(ref.times),
                                     ref.values[c], rpm)

    def _add_log(self, path, ref_rpm, rpm_key):
        ref = read_reference(path)
        name = Path(path).name
        if ref.times and ref.times[0] > 1e9 and self.first_unix:
            # A pandacapture obd CSV (or any log stamped with clock time): line up by the clock
            unit, offset = 1.0, -self.first_unix
            self.log(f"Lined {name} up with the capture by clock time.")
        else:
            rpm_signal = next((s for s in self.map.signals if s.key == rpm_key and not s.derived), None)
            if ref_rpm not in ref.values or rpm_signal is None:
                raise MatchError(f"{name} can't be lined up: it needs an {ref_rpm!r} column (choose it with --ref-rpm) "
                                 f"and the map a {rpm_key!r} signal")
            unit, offset = align(self.capture, ref, ref_rpm, rpm_signal, self.log)
        times = [offset + unit * t for t in ref.times]
        keys = {c: (c if c not in self.columns else f"{c} ({name})") for c in ref.columns}
        rpm = keys.get(ref_rpm, "")
        for c in ref.columns:
            # Each column keeps only its own rows: a pandacapture obd CSV has one answer per row, and the
            # blanks between would leave r_changes and "with RPM held" no neighbouring pairs to work with
            vals = ref.values[c]
            keep = [i for i, v in enumerate(vals) if v is not None]
            self.columns[keys[c]] = Column(keys[c], name, [times[i] for i in keep], [vals[i] for i in keep], rpm)

    def column(self, name) -> Column:
        if name in self.columns:
            return self.columns[name]
        if name.startswith("map:"):
            key = name[4:]
            s = next((s for s in self.map.signals if s.key == key), None)
            if s is None or s.derived:
                raise ValueError(f"the map has no frame signal {key!r}")
            if s.can_id not in self.capture.by_id:
                raise ValueError(f"the capture has no 0x{s.can_id:03X} frames for {key}")
            times = [i * MAP_GRID for i in range(int(self.duration / MAP_GRID) + 1)]
            rpm_sig = next((x for x in self.map.signals if x.key == "rpm" and not x.derived), None)
            col = Column(name, "the address map, decoded from the capture", times,
                         self.capture.sample(s.can_id, s.decode, times), "map:rpm" if rpm_sig and key != "rpm" else "")
            self.columns[name] = col
            return col
        raise ValueError(f"no reference {name!r}; references: {', '.join(self.columns) or 'none'}, or map:<key>")

    def reference_lines(self):
        lines = []
        for c in self.columns.values():
            if c.name.startswith("map:"):
                continue
            vals = [v for v in c.values if v is not None]
            if vals:
                lines.append(f"{c.name}: {len(vals)} values, {min(vals):g} to {max(vals):g}, "
                             f"{len(set(vals))} distinct [{c.source}]")
        return lines or ["(none: no OBD answers in the capture and no log given)"]

    # ---- what Claude sees first ----

    def overview(self) -> str:
        m = self.map
        events = self.markers()["markers_and_events"]
        lines = [
            f"Vehicle map: {m.name}" + (f", {m.bitrate} kbit/s" if m.bitrate else ""),
            f"Capture: bus {self.bus}, {self.duration:.1f} s, {len(self.stats)} broadcast ids analysed"
            + (f", {len(self.withheld)} withheld because their frames carry text" if self.withheld else ""),
            "Markers and events: " + (", ".join(f"{e['what']} at {e['t']} s" for e in events) if events else "none"),
            "",
            "Broadcast ids (rate, length, then each byte: constant value, or kind[min-max, distinct values, "
            "changes per second]; byte kinds: varies, counter, noisy = changes nearly every frame):",
            *[s.line() for s in self.stats.values()],
            "",
            "Signals the map already decodes (map_signal shows an entry in full, with its notes):",
            *[f"  {s.key}: {s.label}" + (f" (0x{s.can_id:03X} byte {s.byte}, {s.bits} bits, {s.order})" if not s.derived
                                         else " (derived)") + f" [{s.source}]" for s in m.signals],
            "",
            "References (values recorded at the same time, on the capture's clock):",
            *self.reference_lines(),
            "Any of the map's frame signals can be used as a reference too, as map:<key> (e.g. map:rpm).",
        ]
        return "\n".join(lines)

    # ---- tools ----

    def _cand(self, a) -> Candidate:
        can_id = _parse_id(a["can_id"])
        self._known(can_id)
        bits = int(a["bits"])
        if bits not in range(1, 33) or not 0 <= int(a["bit"]) <= 7:
            raise ValueError("bits must be 1-32 and bit 0-7")
        return Candidate(can_id, int(a["byte"]), int(a["bit"]), bits, a["order"], bool(a["signed"]))

    def _known(self, can_id):
        if can_id not in self.stats:
            raise ValueError(f"0x{can_id:03X} isn't one of the analysed ids"
                             + (" (withheld: its frames carry text)" if can_id in self.withheld else ""))

    def _grid(self, points, start=0.0, end=None):
        end = self.duration if end is None else min(end, self.duration)
        n = max(2, min(int(points), MAX_POINTS))
        return [start + (end - start) * i / (n - 1) for i in range(n)]

    def map_signal(self, a) -> dict:
        s = next((s for s in self.map.signals if s.key == a["key"]), None)
        if s is None:
            raise ValueError(f"the map has no signal {a['key']!r}")
        return {k: v for k, v in s.to_json().items() if v not in (None, "", [], {})}

    def id_detail(self, a) -> dict:
        can_id = _parse_id(a["can_id"])
        self._known(can_id)
        times, datas = self.capture.by_id[can_id]
        idx = [round(i * (len(datas) - 1) / 15) for i in range(16)] if len(datas) > 16 else range(len(datas))
        s = self.stats[can_id]
        return {"id": f"0x{can_id:03X}", "rate_hz": s.rate, "dlc": s.dlc, "bytes": s.bytes, "mapped": s.mapped,
                "samples": [{"t": round(times[i], 3), "data": datas[i].hex(" ").upper()} for i in idx]}

    def frames_window(self, a) -> dict:
        can_id = _parse_id(a["can_id"])
        self._known(can_id)
        times, datas = self.capture.by_id[can_id]
        lo = bisect.bisect_left(times, float(a["start_s"]))
        hi = bisect.bisect_right(times, float(a["end_s"]))
        n = min(int(a["max_frames"]), MAX_FRAMES)
        idx = list(range(lo, hi))
        if len(idx) > n:
            idx = [idx[round(i * (len(idx) - 1) / (n - 1))] for i in range(n)] if n > 1 else idx[:1]
        return {"id": f"0x{can_id:03X}", "frames_in_window": hi - lo, "shown": len(idx),
                "frames": [f"{times[i]:.3f} {datas[i].hex(' ').upper()}" for i in idx]}

    def field_series(self, a) -> dict:
        cand = self._cand(a)
        end = a.get("end_s")
        at = self._grid(a.get("points", 60), float(a.get("start_s", 0)),
                        None if end is None or float(end) < 0 else float(end))
        return {"field": cand.describe(), "t": [round(t, 2) for t in at],
                "raw": self.capture.sample(cand.can_id, cand.decode, at)}

    def reference_series(self, a) -> dict:
        col = self.column(a["reference"])
        at = self._grid(a.get("points", 60))
        out = []
        for t in at:
            i = bisect.bisect_right(col.times, t) - 1
            out.append(col.values[i] if i >= 0 else None)
        return {"reference": col.name, "source": col.source, "t": [round(t, 2) for t in at], "values": out}

    def _test(self, cand, col):
        vals = self.capture.sample(cand.can_id, cand.decode, col.times)
        r, n = pearson(vals, col.values)
        return vals, r, n

    def test_field(self, a) -> dict:
        """How a field tracks a reference: correlation, its changes, with RPM held, and the fit."""
        cand = self._cand(a)
        col = self.column(a["reference"])
        vals, r, n = self._test(cand, col)
        if r is None:
            return {"field": cand.describe(), "reference": col.name, "r": None, "n": n,
                    "note": "no correlation: one of them doesn't vary over the overlap"}
        scale, offset = fit(vals, col.values)
        out = {"field": cand.describe(), "reference": col.name, "n": n, "r": round(r, 4),
               "r_changes": _round(pearson_changes(vals, col.values)), "scale": _sig(scale), "offset": _sig(offset)}
        if col.rpm and col.rpm != col.name:
            rpm = self.column(col.rpm)
            rpm_vals = [rpm.values[max(0, bisect.bisect_right(rpm.times, t) - 1)] for t in col.times]
            r_rpm, _ = pearson(vals, rpm_vals)
            r_col_rpm, _ = pearson(col.values, rpm_vals)
            out["r_with_rpm_held"] = _round(partial(r, r_rpm, r_col_rpm))
        return out

    def search_references(self, a) -> dict:
        """pandacapture match's search: every 8-, 12- and 16-bit field against one reference, best first."""
        col = self.column(a["reference"])
        top = max(1, min(int(a.get("top", 5)), 15))
        found = []
        for cand in candidates(self.capture):
            if cand.can_id not in self.stats:
                continue
            vals, r, n = self._test(cand, col)
            if r is None or abs(r) < 0.5 or len({v for v in vals if v is not None}) < 3:
                continue
            scale, offset = fit(vals, col.values)
            known = next((k for k in self.stats[cand.can_id].mapped), "")
            found.append(Match(col.name, cand, r, pearson_changes(vals, col.values), None, scale, offset, known))
        found.sort(key=lambda m: (-round(abs(m.r), 4), m.candidate.bit != 0, m.candidate.bits,
                                  m.candidate.order != "little", m.candidate.signed))
        return {"reference": col.name, "results": [
            {"field": m.candidate.describe(), "can_id": f"0x{m.candidate.can_id:03X}", "byte": m.candidate.byte,
             "bit": m.candidate.bit, "bits": m.candidate.bits, "order": m.candidate.order,
             "signed": m.candidate.signed, "r": round(m.r, 4), "r_changes": _round(m.r_changes),
             "scale": _sig(m.scale), "offset": _sig(m.offset)} for m in dedupe(found, top)]}

    def what_moved(self, a) -> dict:
        """What moved: fields that moved between start and end, scored against a still baseline
        (the start of the capture up to baseline_end_s; -1 for none). Counters and checksums are left out."""
        start, end = float(a["start_s"]), float(a["end_s"])
        base_end = float(a["baseline_end_s"])
        out = []
        for can_id, s in self.stats.items():
            times, datas = self.capture.by_id[can_id]
            varies = [b["kind"] != "constant" for b in s.bytes]
            layouts = [(b, 8, "little") for b in range(s.dlc) if varies[b]]
            layouts += [(b, 16, o) for b in range(s.dlc - 1) if varies[b] and varies[b + 1] for o in ("little", "big")]
            for byte, bits, order in layouts:
                cand = Candidate(can_id, byte, 0, bits, order, False)
                section = [cand.decode(d) for t, d in zip(times, datas) if start <= t <= end]
                section = [v for v in section if v is not None]
                if len(set(section)) < 3:
                    continue
                baseline = [v for t, d in zip(times, datas) if base_end > 0 and t <= base_end
                            for v in [cand.decode(d)] if v is not None]
                check = baseline or section
                rate = sum(1 for x, y in zip(check, check[1:]) if x != y) / max(1, len(check) - 1)
                if rate > COUNTER_CHANGE_RATE:
                    continue
                rng = max(section) - min(section)
                if rng < 3:
                    continue
                base_rng = (max(baseline) - min(baseline)) if baseline else 0
                out.append({"field": cand.describe(), "can_id": f"0x{can_id:03X}", "byte": byte, "bits": bits,
                            "order": order, "score": round(rng / (base_rng + 1), 1), "section_min": min(section),
                            "section_max": max(section), "baseline_min": min(baseline) if baseline else None,
                            "baseline_max": max(baseline) if baseline else None, "mapped": s.mapped})
        out.sort(key=lambda r: (-r["score"], r["bits"]))
        return {"window_s": [start, end], "baseline_end_s": base_end if base_end > 0 else None, "moved": out[:25]}

    def bit_stats(self, a) -> dict:
        can_id = _parse_id(a["can_id"])
        self._known(can_id)
        byte = int(a["byte"])
        times, datas = self.capture.by_id[can_id]
        rows = [(t, d[byte]) for t, d in zip(times, datas) if len(d) > byte]
        bits = []
        for bit in range(8):
            vals = [(t, (v >> bit) & 1) for t, v in rows]
            toggles = [t for (t0, x), (t, y) in zip(vals, vals[1:]) if x != y]
            bits.append({"bit": bit, "share_of_ones": round(sum(v for _, v in vals) / len(vals), 3),
                         "toggles": len(toggles), "first_toggle_s": round(toggles[0], 2) if toggles else None,
                         "last_toggle_s": round(toggles[-1], 2) if toggles else None})
        return {"id": f"0x{can_id:03X}", "byte": byte, "frames": len(rows), "bits": bits}

    def mux_check(self, a) -> dict:
        """Whether a message is multiplexed: byte 0 (or its low nibble) selecting what the rest carries."""
        can_id = _parse_id(a["can_id"])
        self._known(can_id)
        _, datas = self.capture.by_id[can_id]
        out = {"id": f"0x{can_id:03X}", "selectors": []}
        for name, sel in (("byte 0", lambda d: d[0]), ("low nibble of byte 0", lambda d: d[0] & 0x0F)):
            groups = {}
            for d in datas:
                if d:
                    groups.setdefault(sel(d), []).append(d)
            if not 2 <= len(groups) <= 32:
                out["selectors"].append({"selector": name, "values": len(groups),
                                         "verdict": "not a selector (one value, or too many)"})
                continue
            pages = []
            for value, ds in sorted(groups.items())[:16]:
                n = max(len(d) for d in ds)
                pages.append({"value": value, "frames": len(ds),
                              "bytes": [{"min": min(d[i] for d in ds if len(d) > i), "max": max(d[i] for d in ds if len(d) > i)}
                                        for i in range(1, n)]})
            # A selector if the other bytes' ranges differ clearly between its values
            spans = [[p["bytes"][i]["min"] for p in pages if len(p["bytes"]) > i] for i in range(len(pages[0]["bytes"]))]
            differs = sum(1 for col in spans if len(set(col)) > 1)
            out["selectors"].append({"selector": name, "values": len(groups), "pages": pages,
                                     "bytes_differing_between_pages": differs})
        return out

    def checksum_check(self, a) -> dict:
        """How often a byte (or one of its nibbles) equals common checksums of the rest of the frame."""
        can_id = _parse_id(a["can_id"])
        self._known(can_id)
        byte = int(a["byte"])
        _, datas = self.capture.by_id[can_id]
        frames = [d for d in datas if len(d) > byte][:2000]
        results = []
        for name, fn in CHECKSUMS.items():
            hits = sum(1 for d in frames if fn(d[:byte] + d[byte + 1:], can_id) == d[byte])
            results.append({"checksum": name, "of": "the byte", "matches": round(hits / len(frames), 3)})
        for half, get in (("high nibble", lambda b: b >> 4), ("low nibble", lambda b: b & 0x0F)):
            for name, fn in NIBBLE_CHECKSUMS.items():
                def rest(d, half=half):
                    nibs = list(_nibbles(d))
                    del nibs[byte * 2 + (0 if half == "high nibble" else 1)]
                    return nibs
                hits = sum(1 for d in frames if fn(rest(d)) == get(d[byte]))
                results.append({"checksum": name, "of": half, "matches": round(hits / len(frames), 3)})
        results.sort(key=lambda r: -r["matches"])
        return {"id": f"0x{can_id:03X}", "byte": byte, "frames": len(frames), "best": results[:5],
                "note": "a real checksum matches on nearly every frame (> 0.98); chance is about 1/256 for a byte, "
                        "1/16 for a nibble"}

    def co_changes(self, a) -> dict:
        """Which other bytes change within window_ms of this field's changes: related signals, or a message
        reacting to another."""
        cand = self._cand(a)
        window = float(a["window_ms"]) / 1000
        times, datas = self.capture.by_id[cand.can_id]
        vals = [cand.decode(d) for d in datas]
        moments = [t for t, x, y in zip(times[1:], vals, vals[1:]) if x != y]
        if not moments:
            return {"field": cand.describe(), "changes": 0, "related": []}
        related = []
        for can_id, s in self.stats.items():
            ts, ds = self.capture.by_id[can_id]
            for byte, b in enumerate(s.bytes):
                own = can_id == cand.can_id and cand.byte <= byte < cand.byte + (cand.bit + cand.bits + 7) // 8
                if b["kind"] in ("constant", "counter", "noisy") or own:
                    continue
                changes = [t for t, d0, d1 in zip(ts[1:], ds, ds[1:]) if len(d0) > byte and len(d1) > byte and d0[byte] != d1[byte]]
                if len(changes) < 3:
                    continue
                near = sum(1 for t in moments
                           if (i := bisect.bisect_left(changes, t - window)) < len(changes) and changes[i] <= t + window)
                share = near / len(moments)
                # A byte that changes all the time lands near any moment: what counts is the excess over chance
                chance = min(1.0, len(changes) / self.duration * 2 * window)
                related.append({"can_id": f"0x{can_id:03X}", "byte": byte, "share_of_changes_near": round(share, 3),
                                "by_chance": round(chance, 3), "excess": round(share - chance, 3),
                                "its_changes": len(changes)})
        related.sort(key=lambda r: -r["excess"])
        return {"field": cand.describe(), "changes": len(moments), "window_ms": a["window_ms"], "related": related[:15]}

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
        try:
            col = self.column(reference)
        except ValueError as e:
            return str(e), None
        vals = self.capture.sample(s.can_id, s.decode, col.times)
        r, n = pearson(vals, col.values)
        pairs = [(v, x) for v, x in zip(vals, col.values) if v is not None and x is not None]
        err = math.sqrt(sum((v - x) ** 2 for v, x in pairs) / len(pairs)) if pairs else None
        return "", {"reference": col.name, "r": _round(r), "n": n, "rms_error": _round(err)}


def _round(x, d=4):
    return None if x is None else round(x, d)


def _sig(x):
    return float(f"{x:.6g}")


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


# ---------------------------------------------------------------- the tools, described

def _obj(props, required=None):
    return {"type": "object", "properties": props, "required": required or list(props), "additionalProperties": False}


_ID = {"can_id": {"type": "string", "description": "hex, e.g. \"0x316\""}}
_FIELD = {**_ID, "byte": {"type": "integer"},
          "bit": {"type": "integer", "description": "start bit within the byte, 0 for byte-aligned"},
          "bits": {"type": "integer", "description": "field width, e.g. 8, 12, 16"},
          "order": {"type": "string", "enum": ["little", "big"]}, "signed": {"type": "boolean"}}
_REF = {"reference": {"type": "string", "description": "a reference column from the overview, or map:<key>"}}

TOOLS = [
    {"name": "markers", "description": "The capture's markers (dropped while recording, e.g. before key-on, idle, "
                                       "throttle blips) and events (stalls, reconnects), in seconds from the start.",
     "input_schema": _obj({})},
    {"name": "map_signal", "description": "One address-map entry in full, including its notes (where it came from, "
                                          "what was checked).",
     "input_schema": _obj({"key": {"type": "string"}})},
    {"name": "id_detail", "description": "One CAN id's statistics per byte, the map signals on it, and 16 sample "
                                         "frames spread over the capture (time in seconds, data in hex).",
     "input_schema": _obj(_ID)},
    {"name": "frames_window", "description": f"One id's raw frames between two times (at most {MAX_FRAMES}, evenly "
                                             "picked if there are more).",
     "input_schema": _obj({**_ID, "start_s": {"type": "number"}, "end_s": {"type": "number"},
                           "max_frames": {"type": "integer"}})},
    {"name": "field_series", "description": "A field's raw value (before scale and offset), evenly sampled between "
                                            "two times (end_s -1 for the end of the capture).",
     "input_schema": _obj({**_FIELD, "start_s": {"type": "number"}, "end_s": {"type": "number"},
                           "points": {"type": "integer", "description": f"up to {MAX_POINTS}"}})},
    {"name": "reference_series", "description": "A reference's values over the capture, evenly sampled.",
     "input_schema": _obj({**_REF, "points": {"type": "integer"}})},
    {"name": "test_field", "description": "How a field tracks a reference: correlation r, r of their changes (near 0: "
                                          "they only drift together), r with engine speed held (near 0: both merely "
                                          "follow RPM), and the fit reference = scale * raw + offset.",
     "input_schema": _obj({**_FIELD, **_REF})},
    {"name": "search_references", "description": "pandacapture match's search: every 8-, 12- and 16-bit field of every "
                                                 "id against one reference, strongest first, overlapping fields merged.",
     "input_schema": _obj({**_REF, "top": {"type": "integer", "description": "1-15"}})},
    {"name": "what_moved", "description": "Fields that moved between two times compared with a still baseline (from "
                                          "the start up to baseline_end_s; -1 for none), counters and checksums left "
                                          "out. Use with markers: the section after a marker against the time before "
                                          "the first one.",
     "input_schema": _obj({"start_s": {"type": "number"}, "end_s": {"type": "number"},
                           "baseline_end_s": {"type": "number"}})},
    {"name": "bit_stats", "description": "Each bit of one byte: share of ones, toggles, first and last toggle time. "
                                         "Flags and lamps live here.",
     "input_schema": _obj({**_ID, "byte": {"type": "integer"}})},
    {"name": "mux_check", "description": "Whether an id is multiplexed: byte 0 (or its low nibble) selecting what the "
                                         "rest of the frame carries, with each page's byte ranges.",
     "input_schema": _obj(_ID)},
    {"name": "checksum_check", "description": "How often a byte, or one of its nibbles, equals common checksums of the "
                                              "rest of the frame (xor, sums, CRC-8 variants, nibble sums).",
     "input_schema": _obj({**_ID, "byte": {"type": "integer"}})},
    {"name": "co_changes", "description": "Which other bytes change within window_ms of a field's changes: related "
                                          "signals, or one message reacting to another.",
     "input_schema": _obj({**_FIELD, "window_ms": {"type": "number"}})},
]
READ_TOOLS = {t["name"] for t in TOOLS}


def call(analysis: "Analysis", name, args) -> dict:
    """Runs one read-only tool by name (for the API session and for bundles)."""
    if name not in READ_TOOLS:
        raise ValueError(f"no tool {name}")
    return getattr(analysis, name)(dict(args))
