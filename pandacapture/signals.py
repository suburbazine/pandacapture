"""Address maps: which CAN IDs and bits of a vehicle carry which values, and how to show them.

A map is a JSON file (see docs/address-maps.md):

  {"name": "...", "vehicle": "...", "bitrate": 500,
   "signals": [{"key": "rpm", "label": "Engine speed", "id": "0x316", "byte": 2, "bits": 16,
                "scale": 0.25, "unit": "rpm", "display": "gauge", "min": 0, "max": 7000}, ...]}

Built-in maps live in pandacapture/maps/ and are found by file name without ".json".
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as p
from .firmware import bundled_dir

DISPLAYS = ("gauge", "number", "light")
COLORS = ("red", "amber", "green", "blue")
SOURCES = ("verified", "dbc", "observed", "unconfirmed")


class MapError(ValueError):
    pass


@dataclass
class Signal:
    key: str
    label: str
    can_id: int
    byte: int
    bits: int = 8
    bit: int = 0                # bit offset of the field's least significant bit
    order: str = "little"       # "little" (Intel) or "big" (Motorola, starting at `byte`)
    signed: bool = False
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    bus: int = None             # None: any bus
    display: str = "number"
    group: str = ""
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

    def decode(self, data: bytes):
        """The signal's value from a frame's data, or None if the frame is too short."""
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
        if self.signed and raw & (1 << (self.bits - 1)):
            raw -= 1 << self.bits
        return raw * self.scale + self.offset

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
        d = {k: v for k, v in self.__dict__.items() if v is not None and k != "can_id"}
        d["id"] = f"0x{self.can_id:X}"
        if self.decimals is None:
            if float(self.scale).is_integer() and float(self.offset).is_integer():
                d["decimals"] = 0
            else:
                d["decimals"] = 1 if abs(self.scale) >= 0.1 else 2
        return d


@dataclass
class AddressMap:
    name: str
    signals: list
    vehicle: str = ""
    bitrate: int = None
    notes: str = ""
    path: str = ""
    by_id: dict = field(default_factory=dict)

    def __post_init__(self):
        for s in self.signals:
            self.by_id.setdefault(s.can_id, []).append(s)

    def to_json(self) -> dict:
        return {"name": self.name, "vehicle": self.vehicle, "bitrate": self.bitrate, "notes": self.notes,
                "signals": [s.to_json() for s in self.signals]}


def _number(d, key, where, default=None, integer=False):
    v = d.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise MapError(f"{where}: {key} must be a number")
    if integer and not float(v).is_integer():
        raise MapError(f"{where}: {key} must be a whole number")
    return int(v) if integer else float(v)


def parse_signal(d: dict, index: int) -> Signal:
    where = f"signal {index + 1}" + (f" ({d.get('key')})" if isinstance(d, dict) and d.get("key") else "")
    if not isinstance(d, dict):
        raise MapError(f"{where}: must be an object")
    known = set(Signal.__dataclass_fields__) - {"can_id"} | {"id"}
    unknown = set(d) - known
    if unknown:
        raise MapError(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
    for req in ("key", "id", "byte"):
        if req not in d:
            raise MapError(f"{where}: needs '{req}'")
    try:
        can_id = int(d["id"], 0) if isinstance(d["id"], str) else int(d["id"])
    except ValueError:
        raise MapError(f"{where}: id {d['id']!r} isn't a number (use \"0x316\")") from None
    if not 0 <= can_id <= 0x1FFFFFFF:
        raise MapError(f"{where}: id out of range")
    s = Signal(
        key=str(d["key"]), label=str(d.get("label", d["key"])), can_id=can_id,
        byte=_number(d, "byte", where, integer=True), bits=_number(d, "bits", where, 8, integer=True),
        bit=_number(d, "bit", where, 0, integer=True), order=d.get("order", "little"), signed=bool(d.get("signed", False)),
        scale=_number(d, "scale", where, 1.0), offset=_number(d, "offset", where, 0.0), unit=str(d.get("unit", "")),
        bus=_number(d, "bus", where, integer=True), display=d.get("display", "number"), group=str(d.get("group", "")),
        min=_number(d, "min", where), max=_number(d, "max", where), decimals=_number(d, "decimals", where, integer=True),
        warn_above=_number(d, "warn_above", where), alert_above=_number(d, "alert_above", where),
        warn_below=_number(d, "warn_below", where), alert_below=_number(d, "alert_below", where),
        on_above=_number(d, "on_above", where), on_below=_number(d, "on_below", where),
        color=d.get("color", "red"), source=d.get("source", "unconfirmed"), note=str(d.get("note", "")),
    )
    if s.order not in ("little", "big"):
        raise MapError(f"{where}: order must be \"little\" or \"big\"")
    if not 1 <= s.bits <= 64 or s.byte < 0 or not 0 <= s.bit < 8 * 64:
        raise MapError(f"{where}: byte/bit/bits out of range")
    if s.byte * 8 + s.bit + s.bits > 64 * 8:
        raise MapError(f"{where}: the field runs past 64 bytes")
    if s.display not in DISPLAYS:
        raise MapError(f"{where}: display must be one of {', '.join(DISPLAYS)}")
    if s.display == "gauge" and (s.min is None or s.max is None or s.max <= s.min):
        raise MapError(f"{where}: a gauge needs min and max, with max above min")
    if s.color not in COLORS:
        raise MapError(f"{where}: color must be one of {', '.join(COLORS)}")
    if s.source not in SOURCES:
        raise MapError(f"{where}: source must be one of {', '.join(SOURCES)}")
    if s.bus is not None and not 0 <= s.bus < p.CAN_BUSES:
        raise MapError(f"{where}: bus must be 0-{p.CAN_BUSES - 1}")
    return s


def parse_map(d: dict, path="") -> AddressMap:
    if not isinstance(d, dict) or not isinstance(d.get("signals"), list) or not d["signals"]:
        raise MapError("an address map is an object with a non-empty \"signals\" list")
    signals = [parse_signal(s, i) for i, s in enumerate(d["signals"])]
    keys = [s.key for s in signals]
    dupes = {k for k in keys if keys.count(k) > 1}
    if dupes:
        raise MapError(f"duplicate signal key(s): {', '.join(sorted(dupes))}")
    bitrate = d.get("bitrate")
    if bitrate is not None and bitrate not in p.CAN_SPEEDS:
        raise MapError(f"bitrate must be one of {', '.join(map(str, p.CAN_SPEEDS))}")
    return AddressMap(name=str(d.get("name", Path(path).stem or "map")), vehicle=str(d.get("vehicle", "")),
                      bitrate=bitrate, notes=str(d.get("notes", "")), signals=signals, path=str(path))


def builtin_dir() -> Path:
    return bundled_dir().parent / "maps"


def builtin_maps() -> dict:
    """Map name -> path, for the maps shipped with PandaCapture."""
    return {f.stem: f for f in sorted(builtin_dir().glob("*.json"))}


def load_map(name_or_path) -> AddressMap:
    path = Path(name_or_path)
    if not path.exists():
        builtin = builtin_maps()
        if str(name_or_path) not in builtin:
            raise MapError(f"No address map {name_or_path!r}: not a file, and not one of the built-in maps "
                           f"({', '.join(builtin) or 'none'}).")
        path = builtin[str(name_or_path)]
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MapError(f"Can't read {path}: {e}") from None
    try:
        return parse_map(d, path)
    except MapError as e:
        raise MapError(f"{path.name}: {e}") from None
