"""Each capture's speed range, as a tag for the capture lists ("0–64 mph", "Stationary", "No speed").

Ground speed comes from the address map's signals by key: the slower axle's average (wheel_fl ... wheel_rr), as
the run engine takes it, so wheelspin doesn't count as speed; else the ECU's vehicle speed (speed_kmh). Raw map
values, no tire calibration.

A recording writes the range as a comment line just before its summary, `# speed range: 0.0-102.2 km/h` (or
`# speed range: not heard`), the same line PandaCapture Android writes, so either reads the other's logs. An
older log without one is read whole, once, and the result cached by file name and size.
"""

import json
import re
import threading
from pathlib import Path

from . import protocol as p

KMH_PER_MPH = 1.609344
MOVING_KMH = 2.0                  # under this the car never moved (wheel speed sensors read a little at a crawl)
MAX_KMH = 400.0                   # anything over this is a misread, not a speed
TAIL_BYTES = 256 * 1024           # how much of a log's end to look in: the summary's per-id table comes after
WHEELS = ("wheel_fl", "wheel_fr", "wheel_rl", "wheel_rr")
NOT_HEARD = "speed range: not heard"
NOTE = re.compile(r"^# speed range: ([0-9.]+)-([0-9.]+) km/h")
CACHE_NAME = "capture-speeds.json"


class SpeedTag:
    """The lowest and highest ground speed a capture heard, km/h, or heard False when the map's speed signals
    never came up in it."""

    def __init__(self, min_kmh: float, max_kmh: float, heard: bool = True):
        self.min_kmh, self.max_kmh, self.heard = min_kmh, max_kmh, heard

    def label(self) -> str:
        if not self.heard:
            return "No speed"
        if self.max_kmh < MOVING_KMH:
            return "Stationary"
        return f"{self.min_kmh / KMH_PER_MPH:.0f}–{self.max_kmh / KMH_PER_MPH:.0f} mph"

    def note(self) -> str:
        """The comment (without "# ") a recording ends with, before its summary."""
        return f"speed range: {self.min_kmh:.1f}-{self.max_kmh:.1f} km/h" if self.heard else NOT_HEARD

    def to_json(self) -> dict:
        return {"min": self.min_kmh, "max": self.max_kmh, "heard": self.heard, "label": self.label()}

    @staticmethod
    def parse(line: str):
        if line.startswith("# " + NOT_HEARD):
            return SpeedTag(0.0, 0.0, heard=False)
        m = NOTE.match(line)
        return SpeedTag(float(m.group(1)), float(m.group(2))) if m else None

    @staticmethod
    def from_tail(path):
        """The tag a recording wrote at its end, or None if it has none (older logs)."""
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            text = f.read().decode("latin-1")
        found = None
        for line in text.splitlines():
            found = SpeedTag.parse(line.strip()) or found
        return found

    @staticmethod
    def scan(path, address_map):
        """Reads a whole log for its speed range with the map's speed signals; None if the map has none."""
        tracker = SpeedTracker(address_map)
        if not tracker.available:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith("("):
                    continue
                try:
                    _stamp, iface, text = line.split(None, 2)
                    ident = text.split("#", 1)[0]
                    if len(ident) != 3 or int(ident, 16) not in tracker.by_id:
                        continue
                    frame = p.parse_frame_text(text.split()[0])
                except (ValueError, p.PacketError):
                    continue
                m = re.search(r"(\d+)$", iface)
                tracker.frame(int(m.group(1)) if m else 0, frame.addr, frame.extended, frame.data)
        return tracker.tag()


class SpeedTracker:
    """Follows ground speed through frames, from the map's speed signals."""

    def __init__(self, address_map):
        keys = set(WHEELS) | {"speed_kmh"}
        self.by_id = {}
        for s in address_map.signals:
            if not s.derived and s.key in keys:
                self.by_id.setdefault(s.can_id, []).append(s)
        have = {s.key for group in self.by_id.values() for s in group}
        self.use_wheels = all(k in have for k in WHEELS)
        self.available = self.use_wheels or "speed_kmh" in have
        self.wheels = [None] * 4
        self.min = float("inf")
        self.max = float("-inf")

    def frame(self, bus, addr, extended, data):
        if extended:
            return
        signals = self.by_id.get(addr)
        if not signals:
            return
        for s in signals:
            if s.bus is not None and s.bus != bus:
                continue
            v = s.decode(data)
            if v is None:
                continue
            if s.key == "speed_kmh":
                if not self.use_wheels:
                    self._see(v)
            else:
                self.wheels[WHEELS.index(s.key)] = v
        if self.use_wheels and None not in self.wheels:
            w = self.wheels
            self._see(min((w[0] + w[1]) / 2, (w[2] + w[3]) / 2))

    def feed(self, f):
        """A received Frame (returned and rejected ones aren't traffic)."""
        if not (f.returned or f.rejected):
            self.frame(f.bus, f.addr, f.extended, f.data)

    def _see(self, kmh):
        if kmh != kmh or kmh < 0 or kmh > MAX_KMH:
            return
        self.min = min(self.min, kmh)
        self.max = max(self.max, kmh)

    def tag(self) -> SpeedTag:
        return SpeedTag(self.min, self.max) if self.max >= self.min else SpeedTag(0.0, 0.0, heard=False)


class SpeedTags:
    """Tags for a captures folder: a log's own line, else the whole log read once in the background with the
    current map, cached in the folder's capture-speeds.json by name and size ({name: {bytes, min, max, heard}})."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.cache = None
        self.queue = []
        self.worker = None

    def _load(self):
        if self.cache is None:
            try:
                raw = json.loads((self.folder / CACHE_NAME).read_text(encoding="utf-8"))
                self.cache = {n: (int(v["bytes"]), SpeedTag(float(v["min"]), float(v["max"]), bool(v["heard"])))
                              for n, v in raw.items()}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.cache = {}

    def _save(self):
        data = {n: {"bytes": b, "min": t.min_kmh, "max": t.max_kmh, "heard": t.heard} for n, (b, t) in self.cache.items()}
        try:
            (self.folder / CACHE_NAME).write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError:
            pass

    def get(self, path, address_map):
        """(tag or None, pending): pending while an older log waits to be read."""
        path = Path(path)
        try:
            size = path.stat().st_size
            tag = SpeedTag.from_tail(path)
        except OSError:
            return None, False
        if tag:
            return tag, False
        with self.lock:
            self._load()
            hit = self.cache.get(path.name)
            if hit and hit[0] == size:
                return hit[1], False
            if not SpeedTracker(address_map).available:
                return None, False
            if path not in self.queue:
                self.queue.append(path)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, args=(address_map,), daemon=True,
                                               name="pandacapture-speed-tags")
                self.worker.start()
        return None, True

    def _work(self, address_map):
        while True:
            with self.lock:
                if not self.queue:
                    self.worker = None
                    return
                path = self.queue.pop(0)
            try:
                size = path.stat().st_size
                tag = SpeedTag.scan(path, address_map)
            except OSError:
                continue
            if tag is None:
                continue
            with self.lock:
                self.cache[path.name] = (size, tag)
                self._save()
