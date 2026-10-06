"""Records CAN traffic to a candump log, with markers, stall and dropout marks, and a summary.

The log format is candump's, so PandaCapture Android's signal finder, SavvyCAN and can-utils read it: `(unix time) canN ID#DATA`, and `#` comment lines for markers and events.
"""

import datetime as dt
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .keys import KeyReader
from .sources import SourceError
from .speedrange import SpeedTracker

STALL_SECONDS = 2.0
RECONNECT_INTERVAL = 2.0


def default_out_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "captures"
    return Path.cwd() / "captures"


def candump_line(t: float, frame) -> str:
    ident = f"{frame.addr:08X}" if frame.extended else f"{frame.addr:03X}"
    sep = "##0" if frame.fd else "#"  # FD flags digit: the panda doesn't report bit rate switching
    return f"({t:.6f}) can{frame.bus} {ident}{sep}{frame.data.hex().upper()}"


@dataclass
class IdStats:
    count: int = 0
    first: float = 0.0
    last_t: float = 0.0
    last: bytes = b""


@dataclass
class CaptureOptions:
    out_dir: Path = None
    seconds: float = 0.0
    reconnect: bool = True
    reconnect_seconds: float = 60.0
    buses: tuple = ()  # record only these buses; empty = all
    stamp: str = None  # file name timestamp, for tests
    split_mb: float = 100.0  # start a new file past this size; 0 = one file
    speed_map: object = None  # an address map: each part ends with the speed range it heard (see speedrange.py)


class RollingLog:
    """A candump log that continues in a new file once it passes a size. Each part repeats the
    header and names the file before and after it, so the parts read as one recording. Given a map with speed
    signals, each part ends with the speed range it heard (track() each frame, speed_note() before the summary)."""

    def __init__(self, out_dir: Path, header, split_mb=100.0, stamp=None, speed_map=None):
        self.speed_map = speed_map if speed_map is not None and SpeedTracker(speed_map).available else None
        self.speed = None
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.header = list(header)
        self.limit = int(split_mb * 1024 * 1024) if split_mb else 0
        self.paths = []
        self._f = None
        self._open(stamp)

    @property
    def path(self) -> Path:
        return self.paths[-1]

    def _next_path(self, stamp=None) -> Path:
        stamp = stamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = self.out_dir / f"capture-{stamp}.log"
        n = 2
        while path.exists() or path in self.paths:
            path = self.out_dir / f"capture-{stamp}-{n}.log"
            n += 1
        return path

    def _open(self, stamp=None, continues_from=None, path=None):
        path = path or self._next_path(stamp)
        self._f = open(path, "w", encoding="utf-8", newline="\n")
        self.paths.append(path)
        self.size = 0
        self.speed = SpeedTracker(self.speed_map) if self.speed_map is not None else None
        for line in self.header:
            self.write(line + "\n")
        self.write(f"# started: {dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}\n")
        if continues_from:
            self.write(f"# continues from: {continues_from.name}\n")

    def write(self, text):
        self._f.write(text)
        self.size += len(text)

    def flush(self):
        self._f.flush()

    def track(self, frame):
        """A frame written to this part, for its speed range."""
        if self.speed is not None:
            self.speed.feed(frame)

    def speed_note(self):
        """This part's speed range, as the line it ends with (before the summary), once."""
        if self.speed is not None:
            self.write(f"# {self.speed.tag().note()}\n")
            self.speed = None

    def maybe_rotate(self) -> bool:
        """Call between frames: starts the next part if this one is past the limit."""
        if not self.limit or self.size < self.limit:
            return False
        old, new = self.path, self._next_path()
        self.speed_note()
        self._f.write(f"# continued in: {new.name}\n")
        self._f.close()
        self._open(continues_from=old, path=new)
        return True

    def close(self):
        if self._f:
            self._f.close()
            self._f = None


@dataclass
class Session:
    stats: dict = field(default_factory=dict)  # (bus, id) -> IdStats
    frames: int = 0
    markers: int = 0
    dropouts: int = 0
    panda_overflows: int = 0
    undecodable: int = 0

    def ids(self):
        return {k[1] for k in self.stats}


class Console:
    """Status line that rewrites itself on a terminal, and plain lines otherwise."""

    def __init__(self, out=None):
        self.out = out or sys.stdout
        self.tty = self.out.isatty()
        self.status_shown = False

    def line(self, text=""):
        if self.status_shown:
            self.out.write("\n")
            self.status_shown = False
        self.out.write(text + "\n")
        self.out.flush()

    def status(self, text):
        if self.tty:
            width = max(10, shutil.get_terminal_size((100, 20)).columns - 1)
            self.out.write("\r" + text[:width].ljust(width))
            self.status_shown = True
            self.out.flush()
        else:
            self.line(text)


def status_line(secs, session: Session, fps, per_bus) -> str:
    buses = "  ".join(f"bus{b} {n:.0f}/s" for b, n in sorted(per_bus.items()))
    rpm = ""
    st = next((s for (b, i), s in session.stats.items() if i == 0x316 and len(s.last) >= 4), None)
    if st:
        rpm = f"  RPM(0x316) {((st.last[3] << 8) | st.last[2]) * 0.25:.0f}"
    return (f"{secs:6.0f} s  {session.frames:9d} frames  {fps:6.0f}/s  {len(session.ids()):3d} ids  "
            f"{session.markers} markers  {buses}{rpm}")


def summary(session: Session, seconds: float) -> list:
    extra = ""
    if session.dropouts:
        extra += f", {session.dropouts} dropout{'s' if session.dropouts != 1 else ''} (see # adapter error lines)"
    if session.panda_overflows:
        extra += f", {session.panda_overflows} frames lost in the panda (receive buffer overflow)"
    if session.undecodable:
        extra += f", {session.undecodable} undecodable USB transfers"
    lines = [f"{session.frames} frames, {len(session.ids())} ids, {seconds:.1f} s, {session.markers} markers{extra}"]
    if session.stats:
        ids = session.ids()
        diag_only = all(0x600 <= i <= 0x7FF for i in ids)
        lines.append("0x316 (engine RPM) present: this is the powertrain side." if 0x316 in ids
                     else "Only diagnostic ids: this is the OBD/gateway side, not P-CAN." if diag_only
                     else "0x316 not seen.")
        lines.append("  bus  id          count     rate  len  last data")
        for (bus, ident), st in sorted(session.stats.items()):
            span = st.last_t - st.first
            rate = (st.count - 1) / span if span > 0 else 0.0
            name = f"{ident:08X}" if ident > 0x7FF else f"0x{ident:03X}"
            lines.append(f"  can{bus} {name:<10} {st.count:7d} {rate:7.1f}/s  {len(st.last):3d}  {st.last.hex(' ').upper()}")
    return lines


def restart_note(source, seconds_away) -> str:
    """After a dropout: did the panda restart (power), or did only the USB link drop?"""
    uptime = getattr(source, "uptime", None)
    if uptime is None:
        return ""
    volts = getattr(source, "voltage", None)
    supply = f", supply {volts:.2f} V" if volts else ""
    if uptime <= seconds_away + 5:
        return (f"The panda had restarted ({uptime} s since power-up{supply}): it lost power or reset. "
                "On USB power alone, check the cable and the laptop's USB power settings.")
    return f"The panda kept running ({uptime} s since power-up{supply}): only the USB link dropped."


class _Stop:
    requested = False


def capture(open_source, opts: CaptureOptions, console: Console = None, keys: KeyReader = None) -> int:
    """Records from open_source() until stopped. If the source fails and reconnecting is on, the
    failure is marked, the source reopened, and recording carries on in the same file."""
    console = console or Console()
    stop = _Stop()

    def on_sigint(_sig, _frame):
        stop.requested = True

    try:
        source = open_source()
    except SourceError as e:
        console.line(f"ERROR: {e}")
        return 1

    out_dir = Path(opts.out_dir) if opts.out_dir else default_out_dir()

    old_handler = signal.signal(signal.SIGINT, on_sigint)
    keys = keys or KeyReader()
    session = Session()
    t0_unix = time.time()
    t0 = time.monotonic()

    def unix_now():
        return t0_unix + (time.monotonic() - t0)

    last_frame = 0.0  # monotonic seconds since start
    stalled = False
    warned_silent = warned_diag = False
    last_status = 0.0
    frames_at_status = 0
    bus_counts = {}
    last_overflow = None

    console.line(f"Capturing from {source.description}")
    for h in source.header:
        console.line(f"  {h}")
    w = RollingLog(out_dir, ["# PandaCapture candump log", f"# source: {source.description}",
                             *[f"# {h}" for h in source.header]], opts.split_mb, opts.stamp, opts.speed_map)
    console.line(f"Writing {w.path}" + (f" (a new file every {opts.split_mb:g} MB)" if opts.split_mb else ""))
    console.line("Keys: M = marker, 1-9 = numbered marker, Q or Esc = stop.\n")

    with keys:
        try:
            while not stop.requested and (opts.seconds <= 0 or time.monotonic() - t0 < opts.seconds):
                try:
                    frames = source.read()
                except SourceError as e:
                    err_at = time.monotonic() - t0
                    w.write(f"# adapter error: {e} ({t0_unix + err_at:.6f}); last frame ({t0_unix + last_frame:.6f})\n")
                    w.flush()
                    console.line(f"  {getattr(source, 'kind', 'Panda')} error: {e}")
                    if not opts.reconnect:
                        raise
                    source.close()
                    source = reconnect(open_source, opts, console, keys, stop)
                    if source is None:
                        raise SourceError(f"{e}; couldn't reconnect within {opts.reconnect_seconds:g} s") from None
                    session.dropouts += 1
                    back = time.monotonic() - t0
                    why = restart_note(source, back - last_frame)
                    w.write(f"# reconnected ({t0_unix + back:.6f}), {back - last_frame:.1f} s without frames: "
                            f"{source.description}" + (f". {why}" if why else "") + "\n")
                    w.flush()
                    console.line("  Reconnected; still recording to the same file." + (f" {why}" if why else ""))
                    stalled = True  # the next frame closes the gap with a stall-ended line
                    last_overflow = None
                    continue

                if frames:
                    t = unix_now()
                    if stalled:
                        stalled = False
                        w.write(f"# stall ended ({t:.6f}), {t - t0_unix - last_frame:.1f} s without frames\n")
                    last_frame = t - t0_unix
                    for f in frames:
                        if f.returned or f.rejected or (opts.buses and f.bus not in opts.buses):
                            continue
                        session.frames += 1
                        bus_counts[f.bus] = bus_counts.get(f.bus, 0) + 1
                        w.write(candump_line(t, f) + "\n")
                        w.track(f)
                        st = session.stats.get((f.bus, f.addr))
                        if st is None:
                            st = session.stats[(f.bus, f.addr)] = IdStats(first=t)
                        st.count += 1
                        st.last_t = t
                        st.last = f.data
                if w.maybe_rotate():
                    console.line(f"  Continuing in {w.path.name}")
                undecodable = getattr(getattr(source, "unpacker", None), "bad_checksums", 0)
                if undecodable > session.undecodable:
                    w.write(f"# undecodable USB data from the panda ({undecodable - session.undecodable}), dropped\n")
                    session.undecodable = undecodable

                for k in keys.poll():
                    if k.lower() == "q" or k == "\x1b":
                        stop.requested = True
                    elif k.lower() == "m" or k.isdigit():
                        session.markers += 1
                        label = k if k.isdigit() else str(session.markers)
                        w.write(f"# marker {label} ({unix_now():.6f})\n")
                        w.flush()
                        console.line(f"  marker {label} at {time.monotonic() - t0:.1f} s")

                now = time.monotonic() - t0
                if not stalled and session.frames and now - last_frame >= STALL_SECONDS:
                    stalled = True
                    w.write(f"# stall: no frames since ({t0_unix + last_frame:.6f})\n")
                    w.flush()
                    console.line(f"  No frames for {STALL_SECONDS:g} s: the bus went quiet, or the panda's link dropped.")
                if now - last_status >= 1.0:
                    span = now - last_status
                    fps = (session.frames - frames_at_status) / span
                    per_bus = {b: n / span for b, n in bus_counts.items()}
                    bus_counts = {}
                    frames_at_status, last_status = session.frames, now
                    console.status(status_line(now, session, fps, per_bus))
                    w.flush()
                    health = source.health()
                    if health:
                        overflow = health["rx_buffer_overflow"]
                        if last_overflow is not None and overflow > last_overflow:
                            lost = overflow - last_overflow
                            session.panda_overflows += lost
                            w.write(f"# panda receive buffer overflow: {lost} frames lost ({unix_now():.6f})\n")
                        last_overflow = overflow
                    if not warned_silent and now > 5 and session.frames == 0:
                        warned_silent = True
                        console.line("  No CAN traffic yet. Check CAN-H/CAN-L, ignition/ECU power, and the bit rate.")
                    if (not warned_diag and now > 5 and session.frames
                            and all(0x600 <= i <= 0x7FF for i in session.ids())):
                        warned_diag = True
                        console.line("  Only diagnostic IDs so far: this looks like the OBD/gateway side, not P-CAN.")
                if not frames:
                    time.sleep(0.001)
        except SourceError as e:
            console.line(f"Stopped: {e}")
            w.write(f"# stopped with error: {e}\n")
        finally:
            signal.signal(signal.SIGINT, old_handler)
            source.close()
            w.speed_note()
            lines = summary(session, time.monotonic() - t0)
            if len(w.paths) > 1:
                lines.insert(1, f"Recorded in {len(w.paths)} parts: {', '.join(x.name for x in w.paths)}")
            for line in lines:
                w.write(f"# {line}\n")
            w.close()
    console.line("")
    for line in lines:
        console.line(line)
    console.line("")
    for x in w.paths:
        console.line(f"Saved {x}")
    return 0


def reconnect(open_source, opts, console, keys, stop):
    """Reopens the source every 2 s until it answers, time runs out, or Q is pressed."""
    give_up = time.monotonic() + opts.reconnect_seconds
    attempt = 0
    while not stop.requested and time.monotonic() < give_up:
        attempt += 1
        console.line(f"  Reconnecting (attempt {attempt}, Q to stop)...")
        try:
            return open_source()
        except SourceError as e:
            console.line(f"    {e}")
        end = time.monotonic() + RECONNECT_INTERVAL
        while time.monotonic() < end and not stop.requested:
            time.sleep(0.1)
            if any(k.lower() == "q" or k == "\x1b" for k in keys.poll()):
                stop.requested = True
    return None

