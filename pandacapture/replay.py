"""Plays a candump log into the dashboard with a timeline: play, pause, seek, stop.

Reads the log once to index it (where in the file each moment starts, every INDEX_S), then plays from any point
at the pace it was recorded, on its own thread, streaming the file (logs run to 100 MB). Seeking lands with the
gauges already showing that moment: the PRIME_S before it go through at once, even while paused. While it runs,
clock() is the timeline's own clock for the dashboard to judge freshness by: it stops while paused, so the gauges
hold their values instead of going stale. The same player as PandaCapture Android's ReplayPlayer.
"""

import re
import threading
import time
from pathlib import Path

from . import protocol as p

INDEX_S = 0.25      # the index's step: a seek starts reading at most this far before where it needs to
PRIME_S = 2.0       # seconds before a seek's target sent at once, so every signal has a value when it lands
CLOCK_BASE = 10_000.0   # the timeline's clock, seconds: a large base keeps it clear of a fresh page's "never seen"

INDEXING, PLAYING, PAUSED, ENDED, STOPPED, FAILED = "indexing", "playing", "paused", "ended", "stopped", "failed"

_TIME = re.compile(rb"^\s*\(\s*([0-9.]+)\)\s+(\S+)\s+([0-9A-Fa-f]{1,8}#\S*)")
_BUS = re.compile(rb"(\d+)$")


def frame_time(line: bytes):
    """A frame line's time, cheaply (the index doesn't need the frame itself)."""
    if not line.startswith(b"("):
        return None
    m = _TIME.match(line)
    return float(m.group(1)) if m else None


def parse_line(line: bytes):
    """(time, Frame) from a candump frame line, or None."""
    m = _TIME.match(line)
    if not m:
        return None
    try:
        f = p.parse_frame_text(m.group(3).decode("ascii"))
    except (ValueError, p.PacketError, UnicodeDecodeError):
        return None
    b = _BUS.search(m.group(2))
    bus = int(b.group(1)) if b else 0
    if bus >= p.CAN_BUSES:
        return None
    return float(m.group(1)), p.Frame(bus, f.addr, f.data, f.extended, f.fd)


class ReplayPlayer:
    """on_frames(list of Frame), on_rewind() (the timeline moved back: what's shown belongs to later, forget it),
    on_state(dict: phase, position, duration, speed, message), each called from the player's thread. State is
    published about 10 times a second while playing, and on every phase change. speed: times the recorded pace.
    loop: at the end, start over (as a dashboard opened with --replay does), else hold there."""

    def __init__(self, path, on_frames, on_rewind=lambda: None, on_state=lambda s: None, speed=1.0, loop=False):
        self.path = Path(path)
        self.on_frames, self.on_rewind, self.on_state = on_frames, on_rewind, on_state
        self.speed = max(float(speed), 0.01)
        self.loop = loop
        self.duration = 0.0
        self.position = 0.0     # seconds from the log's first frame
        self.phase = INDEXING
        self._cond = threading.Condition()
        self._want_play = True
        self._seek_to = None
        self._stop = False
        self._index = []        # (seconds from the start, byte offset of the first frame line at or after it)
        self._start_t = 0.0
        self._thread = None

    def clock(self) -> float:
        """The timeline's clock, seconds: it moves only while playing."""
        return CLOCK_BASE + self.position

    def state(self, message=None) -> dict:
        return {"phase": self.phase, "position": round(self.position, 3), "duration": round(self.duration, 3),
                "speed": self.speed, "message": message}

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="pandacapture-replay")
        self._thread.start()

    def play(self):
        with self._cond:
            self._want_play = True
            if self.phase == ENDED:
                self._seek_to = 0.0       # play at the end starts over
            self._cond.notify_all()

    def pause(self):
        with self._cond:
            self._want_play = False
            self._cond.notify_all()

    def seek(self, s):
        with self._cond:
            self._seek_to = min(max(float(s), 0.0), self.duration)
            self._cond.notify_all()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def join(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def _publish(self, message=None):
        self.on_state(self.state(message))

    def _run(self):
        try:
            self._build_index()
            if self._stop:
                self.phase = STOPPED
                self._publish()
                return
            if not self._index:
                self.phase = FAILED
                self._publish("Nothing to replay: no candump frames in it")
                return
            frm = 0.0
            while not self._stop:
                frm = self._play_from(frm)
                if frm is None:
                    break
            self.phase = STOPPED
            self._publish()
        except Exception as e:  # noqa: BLE001 - the page shows why, rather than the replay going quiet
            self.phase = FAILED
            self._publish(f"Replay failed: {e}")

    def _build_index(self):
        self.phase = INDEXING
        self._publish()
        first, last, next_mark, offset = None, 0.0, 0.0, 0
        with open(self.path, "rb", buffering=1 << 16) as f:
            for line in f:
                if self._stop:
                    return
                t = frame_time(line)
                if t is not None:
                    if first is None:
                        first = t
                    rel = t - first
                    if rel >= next_mark:
                        self._index.append((rel, offset))
                        next_mark = rel + INDEX_S
                    last = rel
                offset += len(line)
        self._start_t = first or 0.0
        self.duration = last

    def _held(self):
        """Pause, seek or stop pressed (look between frames and while waiting)."""
        return not self._want_play or self._seek_to is not None or self._stop

    def _play_from(self, frm):
        """Plays from `frm` seconds until the end, a seek, or stop. Returns where to play from next (a seek's
        target, or 0 after the end when play is pressed again), or None to stop."""
        prime_from = max(frm - PRIME_S, 0.0)
        mark = next((m for m in reversed(self._index) if m[0] <= prime_from), self._index[0])
        self.position = frm
        batch = []

        def flush():
            if batch:
                self.on_frames(list(batch))
                batch.clear()
        with open(self.path, "rb", buffering=1 << 16) as f:
            f.seek(mark[1])
            # Paced from here: the wall clock against the timeline
            anchor_wall, anchor_pos = time.monotonic(), frm
            last_publish = 0.0
            priming = frm > 0      # the lead-up to `frm` goes through even while paused
            self.phase = PLAYING if self._want_play else PAUSED
            self._publish()
            for line in f:
                if not priming:
                    with self._cond:
                        while not self._want_play and self._seek_to is None and not self._stop:
                            flush()        # what's due by now, the moment a paused seek landed on included
                            if self.phase != PAUSED:
                                self.phase = PAUSED
                                self._publish()
                            self._cond.wait()
                            anchor_wall, anchor_pos = time.monotonic(), self.position
                if self._stop:
                    flush()
                    return None
                if self._seek_to is not None:
                    with self._cond:
                        target, self._seek_to = self._seek_to, None
                    flush()
                    if target < self.position:
                        self.on_rewind()
                    return target
                parsed = parse_line(line)
                if parsed is None:
                    continue
                t, frame = parsed
                rel = t - self._start_t
                if rel < frm:
                    # The lead-up to a seek's target: at once, so the gauges show that moment when it lands
                    if rel >= prime_from:
                        batch.append(frame)
                        if len(batch) >= 500:
                            flush()
                    continue
                if priming:
                    priming = False
                    flush()
                    anchor_wall, anchor_pos = time.monotonic(), frm
                if self._want_play and self.phase != PLAYING:
                    self.phase = PLAYING
                    self._publish()
                # Hold each frame until its moment, sending what's due in small batches
                due = (rel - anchor_pos) / self.speed
                while True:
                    wait = due - (time.monotonic() - anchor_wall)
                    if wait <= 0.002:
                        break
                    flush()
                    self.position = anchor_pos + (time.monotonic() - anchor_wall) * self.speed
                    now = time.monotonic()
                    if now - last_publish > 0.1:
                        last_publish = now
                        self._publish()
                    with self._cond:
                        if not self._held():
                            self._cond.wait(min(wait, 0.02))
                    if self._held():
                        break
                if self._held():
                    # Pressed while waiting: this frame is still due, keep it for after
                    batch.append(frame)
                    self.position = min(self.position, rel)
                    continue
                self.position = rel
                batch.append(frame)
                if len(batch) >= 200:
                    flush()
                now = time.monotonic()
                if now - last_publish > 0.1:
                    last_publish = now
                    self._publish()
            flush()
        # The end: start over (looping), else hold here until play (from the start), a seek, or stop
        self.position = self.duration
        if self.loop and not self._held():
            self.on_rewind()
            return 0.0
        self.phase = ENDED
        self._want_play = False
        self._publish()
        with self._cond:
            while not self._want_play and self._seek_to is None and not self._stop:
                self._cond.wait()
            if self._stop:
                return None
            target, self._seek_to = (self._seek_to if self._seek_to is not None else 0.0), None
        self.on_rewind()
        return target
