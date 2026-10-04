"""Address maps: which CAN IDs and bits of a vehicle carry which values, and how to show them.

A map is a JSON file (see docs/address-maps.md):

  {"name": "...", "vehicle": "...", "bitrate": 500,
   "signals": [{"key": "rpm", "label": "Engine speed", "id": "0x316", "byte": 2, "bits": 16,
                "scale": 0.25, "unit": "rpm", "display": "gauge", "min": 0, "max": 7000}, ...]}

A signal is either decoded from a frame (id, byte, bit, bits ...) or derived from other signals with
an expression ("expr": "(tqi_acor - tqfr) * 5"). Expressions allow numbers, signal keys,
+ - * / and comparisons, abs/min/max, and windowed p2p/lo/hi/avg(key, seconds) over any signal above, derived
ones included. They're checked when
the map loads and evaluated by walking the syntax tree, never with eval().

Maps are found by file name without ".json" in a "maps" folder next to the program (the current
folder when running from source), then among any shipped with PandaCapture (pandacapture/maps/).
"""

import ast
import collections
import json
import operator
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as p
from .firmware import bundled_dir

DISPLAYS = ("gauge", "number", "light")
COLORS = ("red", "amber", "green", "blue")
SOURCES = ("verified", "dbc", "observed", "unconfirmed")
WINDOW_FUNCS = ("p2p", "lo", "hi", "avg")
MAX_WINDOW = 60.0


class MapError(ValueError):
    pass


@dataclass
class Signal:
    key: str
    label: str
    short: str = ""             # what tiles show when the label is too long for them; the label stays the description
    can_id: int = None          # None for derived signals
    byte: int = None
    bits: int = 8
    bit: int = 0                # bit offset of the field's least significant bit
    order: str = "little"       # "little" (Intel) or "big" (Motorola, starting at `byte`)
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    raw_max: int = None         # raw values above this mean "no data" (e.g. 255 = sensor fault)
    raw_invalid: list = None    # raw values that mean "no data" (e.g. [128] = no target)
    expr: str = None            # derived signals
    unit: str = ""
    bus: int = None             # None: any bus
    display: str = "number"
    group: str = ""
    labels: dict = None         # value -> text, e.g. {"0": "P", "14": "R"}
    min: float = None
    max: float = None
    decimals: int = None
    warn_above: float = None
    alert_above: float = None
    warn_below: float = None
    alert_below: float = None
    on_above: float = None      # lights: on above / below this; neither = on when nonzero
    on_below: float = None
    color: str = "red"          # lights
    source: str = "unconfirmed"
    note: str = ""
    compiled: object = field(default=None, repr=False)

    @property
    def derived(self) -> bool:
        return self.expr is not None

    def decode(self, data: bytes):
        """The signal's value from a frame's data, or None if the frame is too short or the raw
        value means "no data"."""
        if self.order == "little":
            need = (self.byte * 8 + self.bit + self.bits + 7) // 8
            if len(data) < need:
                return None
            raw = (int.from_bytes(data, "little") >> (self.byte * 8 + self.bit)) & ((1 << self.bits) - 1)
        else:
            n = (self.bit + self.bits + 7) // 8
            if len(data) < self.byte + n:
                return None
            raw = (int.from_bytes(data[self.byte:self.byte + n], "big") >> self.bit) & ((1 << self.bits) - 1)
        if (self.raw_max is not None and raw > self.raw_max) or (self.raw_invalid and raw in self.raw_invalid):
            return None
        if self.signed and raw & (1 << (self.bits - 1)):
            raw -= 1 << self.bits
        return raw * self.scale + self.offset

    def text(self, value):
        """The label for this value, if the map gives one."""
        if not self.labels or value is None or not float(value).is_integer():
            return None
        return self.labels.get(str(int(value)))

    def light_on(self, value) -> bool:
        if value is None:
            return False
        if self.on_above is not None or self.on_below is not None:
            return ((self.on_above is not None and value > self.on_above)
                    or (self.on_below is not None and value < self.on_below))
        return value != 0

    def level(self, value) -> str:
        """'alert', 'warn' or 'ok' for gauges and numbers."""
        if value is None:
            return "ok"
        if (self.alert_above is not None and value > self.alert_above) or \
           (self.alert_below is not None and value < self.alert_below):
            return "alert"
        if (self.warn_above is not None and value > self.warn_above) or \
           (self.warn_below is not None and value < self.warn_below):
            return "warn"
        return "ok"

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if v is not None and k not in ("can_id", "compiled")}
        if self.can_id is not None:
            d["id"] = f"0x{self.can_id:X}"
        else:
            for k in ("bits", "bit", "order", "signed", "scale", "offset"):
                d.pop(k, None)
        if self.decimals is None:
            if not self.derived and float(self.scale).is_integer() and float(self.offset).is_integer():
                d["decimals"] = 0
            else:
                d["decimals"] = 1 if self.derived or abs(self.scale) >= 0.1 else 2
        return d


# ---- derived-signal expressions ----

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_CMPOPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
           ast.Eq: operator.eq, ast.NotEq: operator.ne}


class Expression:
    """A checked expression over other signals' values."""

    def __init__(self, text: str, known: set, where: str):
        try:
            self.tree = ast.parse(text, mode="eval").body
        except SyntaxError:
            raise MapError(f"{where}: expr {text!r} isn't a valid expression") from None
        self.deps = set()
        self.windows = {}   # key -> longest window in seconds
        self._check(self.tree, known, where)
        if not self.deps:
            raise MapError(f"{where}: expr uses no signals")

    def _check(self, n, known, where):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return
        if isinstance(n, ast.Name):
            if n.id not in known:
                raise MapError(f"{where}: expr uses {n.id!r}, which isn't a signal defined above it")
            self.deps.add(n.id)
            return
        if isinstance(n, ast.BinOp) and type(n.op) in _BINOPS:
            self._check(n.left, known, where)
            self._check(n.right, known, where)
            return
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            self._check(n.operand, known, where)
            return
        if isinstance(n, ast.Compare) and all(type(o) in _CMPOPS for o in n.ops):
            for x in [n.left, *n.comparators]:
                self._check(x, known, where)
            return
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.keywords:
            name, args = n.func.id, n.args
            if name in ("abs",) and len(args) == 1 or name in ("min", "max") and len(args) >= 2:
                for a in args:
                    self._check(a, known, where)
                return
            if name in WINDOW_FUNCS:
                if (len(args) != 2 or not isinstance(args[0], ast.Name) or not isinstance(args[1], ast.Constant)
                        or not isinstance(args[1].value, (int, float)) or not 0 < args[1].value <= MAX_WINDOW):
                    raise MapError(f"{where}: {name}() takes a signal key and a window of up to {MAX_WINDOW:g} s")
                self._check(args[0], known, where)
                self.windows[args[0].id] = max(self.windows.get(args[0].id, 0), float(args[1].value))
                return
        raise MapError(f"{where}: expr can only use numbers, signal keys, + - * /, comparisons, "
                       f"abs/min/max and {'/'.join(WINDOW_FUNCS)}(key, seconds)")

    def evaluate(self, values: dict, history: dict, now: float):
        """The value, or None while an input has no value yet (or on division by zero)."""
        try:
            return self._eval(self.tree, values, history, now)
        except (_Missing, ZeroDivisionError):
            return None

    def _eval(self, n, values, history, now):
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            v = values.get(n.id)
            if v is None:
                raise _Missing
            return v
        if isinstance(n, ast.BinOp):
            return _BINOPS[type(n.op)](self._eval(n.left, values, history, now), self._eval(n.right, values, history, now))
        if isinstance(n, ast.UnaryOp):
            v = self._eval(n.operand, values, history, now)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.Compare):
            left = self._eval(n.left, values, history, now)
            for op, right_node in zip(n.ops, n.comparators):
                right = self._eval(right_node, values, history, now)
                if not _CMPOPS[type(op)](left, right):
                    return 0.0
                left = right
            return 1.0
        name = n.func.id
        if name in WINDOW_FUNCS:
            key, secs = n.args[0].id, float(n.args[1].value)
            pts = [v for t, v in history.get(key, ()) if now - t <= secs]
            if not pts:
                raise _Missing
            if name == "avg":
                return sum(pts) / len(pts)
            return max(pts) - min(pts) if name == "p2p" else min(pts) if name == "lo" else max(pts)
        args = [self._eval(a, values, history, now) for a in n.args]
        return abs(args[0]) if name == "abs" else min(args) if name == "min" else max(args)


class _Missing(Exception):
    pass


@dataclass
class AddressMap:
    name: str
    signals: list
    vehicle: str = ""
    bitrate: int = None
    notes: str = ""
    path: str = ""
    by_id: dict = field(default_factory=dict)
    derived: list = field(default_factory=list)
    windows: dict = field(default_factory=dict)   # key -> seconds of history the expressions need

    def __post_init__(self):
        for s in self.signals:
            if s.derived:
                self.derived.append(s)
                for k, secs in s.compiled.windows.items():
                    self.windows[k] = max(self.windows.get(k, 0), secs)
            else:
                self.by_id.setdefault(s.can_id, []).append(s)

    def to_json(self) -> dict:
        return {"name": self.name, "vehicle": self.vehicle, "bitrate": self.bitrate, "notes": self.notes,
                "signals": [s.to_json() for s in self.signals]}


class Evaluator:
    """Keeps the history windowed expressions need, and recomputes derived signals when an input changes."""

    def __init__(self, address_map: AddressMap):
        self.map = address_map
        self.history = {k: collections.deque() for k in address_map.windows}

    def update(self, values: dict, changed: set, now: float) -> dict:
        """values: key -> latest value (updated in place). changed: keys just updated. Returns the
        derived values that were recomputed."""
        for k in changed & self.history.keys():
            h = self.history[k]
            h.append((now, values[k]))
            limit = self.map.windows[k]
            while h and now - h[0][0] > limit:
                h.popleft()
        out = {}
        for s in self.map.derived:  # in map order, so a derived signal can use ones above it
            if s.compiled.deps & changed:
                v = s.compiled.evaluate(values, self.history, now)
                if v is not None:
                    values[s.key] = v
                    out[s.key] = v
                    changed = changed | {s.key}
                    # Windows over a derived signal: map order means it's computed before anything windows it
                    if s.key in self.history:
                        h = self.history[s.key]
                        h.append((now, v))
                        while h and now - h[0][0] > self.map.windows[s.key]:
                            h.popleft()
        return out


# ---- loading ----

def _number(d, key, where, default=None, integer=False):
    v = d.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise MapError(f"{where}: {key} must be a number")
    if integer and not float(v).is_integer():
        raise MapError(f"{where}: {key} must be a whole number")
    return int(v) if integer else float(v)


FRAME_FIELDS = ("id", "byte", "bit", "bits", "order", "signed", "scale", "offset", "raw_max", "raw_invalid", "bus")


def parse_signal(d: dict, index: int, known: set) -> Signal:
    where = f"signal {index + 1}" + (f" ({d.get('key')})" if isinstance(d, dict) and d.get("key") else "")
    if not isinstance(d, dict):
        raise MapError(f"{where}: must be an object")
    allowed = set(Signal.__dataclass_fields__) - {"can_id", "compiled"} | {"id"}
    unknown = set(d) - allowed
    if unknown:
        raise MapError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    if "key" not in d:
        raise MapError(f"{where}: needs 'key'")
    derived = "expr" in d
    if derived:
        extra = [f for f in FRAME_FIELDS if f in d]
        if extra:
            raise MapError(f"{where}: a derived signal (expr) can't also have {', '.join(extra)}")
        can_id = None
    else:
        for req in ("id", "byte"):
            if req not in d:
                raise MapError(f"{where}: needs '{req}' (or 'expr' for a derived signal)")
        try:
            can_id = int(d["id"], 0) if isinstance(d["id"], str) else int(d["id"])
        except ValueError:
            raise MapError(f"{where}: id {d['id']!r} isn't a number (use \"0x316\")") from None
        if not 0 <= can_id <= 0x1FFFFFFF:
            raise MapError(f"{where}: id out of range")
    raw_invalid = d.get("raw_invalid")
    if raw_invalid is not None and (not isinstance(raw_invalid, list)
                                    or not all(isinstance(v, int) and not isinstance(v, bool) for v in raw_invalid)):
        raise MapError(f"{where}: raw_invalid must be a list of whole numbers")
    labels = d.get("labels")
    if labels is not None and (not isinstance(labels, dict) or not all(isinstance(v, str) for v in labels.values())):
        raise MapError(f"{where}: labels must map values to text, e.g. {{\"0\": \"P\"}}")
    s = Signal(
        key=str(d["key"]), label=str(d.get("label", d["key"])), short=str(d.get("short", "")), can_id=can_id,
        byte=None if derived else _number(d, "byte", where, integer=True),
        bits=_number(d, "bits", where, 8, integer=True), bit=_number(d, "bit", where, 0, integer=True),
        order=d.get("order", "little"), signed=bool(d.get("signed", False)),
        scale=_number(d, "scale", where, 1.0), offset=_number(d, "offset", where, 0.0),
        raw_max=_number(d, "raw_max", where, integer=True), raw_invalid=raw_invalid, expr=d.get("expr"), unit=str(d.get("unit", "")),
        bus=_number(d, "bus", where, integer=True), display=d.get("display", "number"), group=str(d.get("group", "")),
        labels={str(k): v for k, v in labels.items()} if labels else None,
        min=_number(d, "min", where), max=_number(d, "max", where), decimals=_number(d, "decimals", where, integer=True),
        warn_above=_number(d, "warn_above", where), alert_above=_number(d, "alert_above", where),
        warn_below=_number(d, "warn_below", where), alert_below=_number(d, "alert_below", where),
        on_above=_number(d, "on_above", where), on_below=_number(d, "on_below", where),
        color=d.get("color", "red"), source=d.get("source", "unconfirmed"), note=str(d.get("note", "")),
    )
    if derived:
        if not isinstance(s.expr, str):
            raise MapError(f"{where}: expr must be text")
        s.compiled = Expression(s.expr, known, where)
    else:
        if s.order not in ("little", "big"):
            raise MapError(f"{where}: order must be \"little\" or \"big\"")
        if not 1 <= s.bits <= 64 or s.byte < 0 or not 0 <= s.bit < 8 * 64:
            raise MapError(f"{where}: byte/bit/bits out of range")
        if s.byte * 8 + s.bit + s.bits > 64 * 8:
            raise MapError(f"{where}: the field runs past 64 bytes")
        if s.bus is not None and not 0 <= s.bus < p.CAN_BUSES:
            raise MapError(f"{where}: bus must be 0-{p.CAN_BUSES - 1}")
    if s.display not in DISPLAYS:
        raise MapError(f"{where}: display must be one of {', '.join(DISPLAYS)}")
    if s.display == "gauge" and (s.min is None or s.max is None or s.max <= s.min):
        raise MapError(f"{where}: a gauge needs min and max, with max above min")
    if s.color not in COLORS:
        raise MapError(f"{where}: color must be one of {', '.join(COLORS)}")
    if s.source not in SOURCES:
        raise MapError(f"{where}: source must be one of {', '.join(SOURCES)}")
    return s


def parse_map(d: dict, path="") -> AddressMap:
    if not isinstance(d, dict) or not isinstance(d.get("signals"), list) or not d["signals"]:
        raise MapError("an address map is an object with a non-empty \"signals\" list")
    signals, known = [], set()
    for i, sd in enumerate(d["signals"]):
        s = parse_signal(sd, i, known)
        if s.key in known:
            raise MapError(f"duplicate signal key: {s.key}")
        known.add(s.key)
        signals.append(s)
    bitrate = d.get("bitrate")
    if bitrate is not None and bitrate not in p.CAN_SPEEDS:
        raise MapError(f"bitrate must be one of {', '.join(map(str, p.CAN_SPEEDS))}")
    return AddressMap(name=str(d.get("name", Path(path).stem or "map")), vehicle=str(d.get("vehicle", "")),
                      bitrate=bitrate, notes=str(d.get("notes", "")), signals=signals, path=str(path))


def builtin_dir() -> Path:
    return bundled_dir().parent / "maps"


def user_dir() -> Path:
    """Your own maps: a "maps" folder next to the program, or in the current folder from source."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "maps"


def builtin_maps() -> dict:
    """Map name -> path: the maps shipped with PandaCapture, and yours (which win on a name clash)."""
    found = {}
    for folder in (builtin_dir(), user_dir()):
        if folder.is_dir():
            found.update({f.stem: f for f in sorted(folder.glob("*.json"))})
    return dict(sorted(found.items()))


def load_map(name_or_path) -> AddressMap:
    path = Path(name_or_path)
    if not path.exists():
        builtin = builtin_maps()
        if str(name_or_path) not in builtin:
            raise MapError(f"No address map {name_or_path!r}: not a file, and not in {user_dir()} "
                           f"({', '.join(builtin) or 'no maps there'}). See docs/address-maps.md.")
        path = builtin[str(name_or_path)]
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MapError(f"Can't read {path}: {e}") from None
    try:
        return parse_map(d, path)
    except MapError as e:
        raise MapError(f"{path.name}: {e}") from None
