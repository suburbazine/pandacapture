"""Transmitting, behind a warning the user has to acknowledge.

Three locks, all of which have to open before a frame reaches the bus:
1. The user acknowledges the warning: types TRANSMIT, or passes --i-accept-transmit-risk for a script.
2. The panda runs PandaCapture firmware, whose transmit gate only allows sending in ALLOUTPUT
   with PandaCapture's arm code (firmware/patches/0001-transmit-gate.patch); stock comma
   firmware can't send arbitrary frames at all.
3. While armed, the firmware needs PandaCapture's heartbeat. If this program stops, crashes or the
   USB cable comes out, the panda drops back to silent within about 2 seconds.
"""

import datetime as dt
import re
import time
from pathlib import Path

from . import protocol as p
from .capture import candump_line, default_out_dir
from .usbdev import UsbError

CONFIRM_WORD = "TRANSMIT"
HEARTBEAT_EVERY = 0.25  # s; the firmware gives up after about 2 s

WARNING = """\
WARNING: this sends frames onto a vehicle's CAN bus.

Frames a module doesn't expect can set fault codes, put modules into limp or failsafe modes, turn
off driver aids such as ABS, stability control or power steering, move actuators, or leave a
module unable to start until it's reflashed. Other modules can't tell your frames from real ones.

- Transmit on a bench, or with the vehicle parked and secured, never while driving.
- Know what each frame does before you send it.
- You are responsible for what you send.

The panda goes back to listen-only when this command ends, or within about 2 seconds if this
program stops responding. Its green LED is on while transmitting is armed.
"""


class TransmitRefused(Exception):
    pass


def acknowledge(accept_flag: bool, details: str = "", ask=input, out=print) -> None:
    """Shows the warning and what's about to be sent, and needs the user to type TRANSMIT unless
    the flag was given. Asked again on every run: nothing is remembered."""
    out(WARNING)
    if details:
        out(details + "\n")
    if accept_flag:
        out("(--i-accept-transmit-risk given)\n")
        return
    try:
        answer = ask(f"Type {CONFIRM_WORD} to continue, anything else to cancel: ")
    except EOFError:
        answer = ""
    if answer.strip() != CONFIRM_WORD:
        raise TransmitRefused("Not acknowledged; nothing was sent.")


class TxLog:
    """candump log of what was sent, next to the captures."""

    def __init__(self, out_dir, description):
        out_dir = Path(out_dir) if out_dir else default_out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"tx-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        self._f = open(self.path, "w", encoding="utf-8", newline="\n")
        self._f.write("# PandaCapture transmit log (frames sent, not received)\n")
        self._f.write(f"# panda: {description}\n")
        self._f.write(f"# started: {dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}\n")

    def sent(self, frame):
        self._f.write(candump_line(time.time(), frame) + "\n")

    def note(self, text):
        self._f.write(f"# {text}\n")

    def close(self):
        self._f.close()


class ArmedPanda:
    """Context manager: arms transmit on entry, disarms on exit (even after an error)."""

    def __init__(self, panda):
        self.panda = panda
        self._last_beat = 0.0
        self.returned = 0
        self.rejected = 0
        self._unpacker = p.CanUnpacker()

    def __enter__(self):
        version = self.panda.version()
        if not p.is_pandacapture_version(version):
            raise TransmitRefused(f"The panda runs {version!r}, not PandaCapture firmware, so it can't "
                                  "transmit. Flash it with: pandacapture flash")
        try:
            self.panda.heartbeat(True)
            self.panda.set_safety(p.SAFETY_ALLOUTPUT, p.PANDACAPTURE_TX_ARM)
            self.beat(force=True)
            mode = self.panda.health()["safety_mode"]
            if mode != p.SAFETY_ALLOUTPUT:
                raise TransmitRefused(f"The panda didn't arm (safety mode {mode}); nothing was sent.")
            self.panda.reset_comms()
            self.panda.clear_rx()
        except BaseException:
            self._disarm()
            raise
        return self

    def __exit__(self, *exc):
        self._disarm()

    def _disarm(self):
        try:
            self.panda.set_safety(p.SAFETY_SILENT)
            mode = self.panda.health()["safety_mode"]
            if mode != p.SAFETY_SILENT:
                print(f"WARNING: the panda reports safety mode {mode} after disarming; unplug it.")
        except UsbError as e:
            print(f"Couldn't disarm over USB ({e}); the panda disarms itself within about 2 s.")

    def beat(self, force=False):
        now = time.monotonic()
        if force or now - self._last_beat >= HEARTBEAT_EVERY:
            self.panda.heartbeat(True)
            self._last_beat = now

    def send(self, frames):
        self.beat()
        for chunk in p.pack_frames(frames):
            self.panda.write_can(chunk)
        self.drain()

    def drain(self):
        """Counts the panda's echoes: 'returned' = went out on the bus, 'rejected' = refused."""
        for f in self._unpacker.feed(self.panda.read_can(5)):
            if f.returned:
                self.returned += 1
            elif f.rejected:
                self.rejected += 1

    def wait(self, seconds):
        """Sleeps, keeping the heartbeat going."""
        end = time.monotonic() + seconds
        while True:
            self.beat()
            self.drain()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))


def read_replay(path, bus_map=None, ids=None) -> list:
    """(seconds from first frame, Frame) from a candump log; buses remapped, optionally filtered by id."""
    out = []
    first = None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("("):
            continue
        try:
            stamp, iface, text = line.split(None, 2)
            t = float(stamp.strip("()"))
            m = re.search(r"(\d+)$", iface)
            bus = int(m.group(1)) if m else 0
            frame = p.parse_frame_text(text.split()[0])
        except (ValueError, p.PacketError):
            continue
        if ids and frame.addr not in ids:
            continue
        if bus_map:
            if bus not in bus_map:
                continue
            bus = bus_map[bus]
        if bus >= p.CAN_BUSES:
            continue
        frame = p.Frame(bus, frame.addr, frame.data, frame.extended, frame.fd)
        first = t if first is None else first
        out.append((t - first, frame))
    return out
