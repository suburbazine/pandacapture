"""Where frames come from: a Red Panda, or simulated traffic for trying the tool without one."""

import math
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as p
from .firmware import expected_packet_versions
from .panda import Panda
from .usbdev import UsbError

AUTO_RATES = (500, 250, 125, 1000, 100, 50)
# Said only when nothing is arriving on any bus, so it doesn't clutter use in a car
BENCH_HINT = ("On a bench (the panda wired straight to an ECU or programmer, no car), the bus also needs a 120 Ω resistor across CAN-H and CAN-L: a car terminates its own bus, and pandas have no termination they can switch on.")


class SourceError(Exception):
    pass


@dataclass
class BusSetup:
    mode: str = "silent"                        # "silent" (no ACKs) or "ack"
    rates: dict = field(default_factory=dict)   # bus -> kbit/s; buses not listed are auto-detected
    data_rate: int = 2000                       # CAN FD data phase (bit rate switching), kbit/s
    obd: bool = False
    detect_seconds: float = 0.4


def bus_errors(panda, bus) -> int:
    """The bus's error count, or 0 on firmware whose CAN health PandaCapture can't read."""
    try:
        return panda.can_health(bus)["total_errors"]
    except UsbError:
        return 0


def detect_rates(panda, buses, dwell, log, candidates=AUTO_RATES) -> dict:
    """Finds each bus's bit rate by listening (the panda must be silent, so a wrong rate can't
    disturb the bus): the first rate that brings frames without errors. Returns bus -> (rate or None,
    frames, errors)."""
    unpacker = p.CanUnpacker()
    result = {b: (None, 0, 0) for b in buses}
    pending = list(buses)
    for rate in candidates:
        if not pending:
            break
        for b in pending:
            panda.set_can_speed(b, rate)
        time.sleep(0.05)
        panda.reset_comms()
        panda.clear_rx()
        unpacker.reset()
        errors_before = {b: bus_errors(panda, b) for b in pending}
        frames = {b: 0 for b in pending}
        end = time.monotonic() + dwell
        while time.monotonic() < end:
            got = unpacker.feed(panda.read_can(10))
            for f in got:
                if f.bus in frames:
                    frames[f.bus] += 1
            if not got:
                time.sleep(0.002)
        for b in list(pending):
            errors = bus_errors(panda, b) - errors_before[b]
            if frames[b] >= 3 and errors <= frames[b] // 20:
                result[b] = (rate, frames[b], errors)
                pending.remove(b)
                log(f"  bus {b}: {rate} kbit/s ({frames[b]} frames in {dwell:.1f} s)")
            elif frames[b] or errors:
                log(f"  bus {b}: not {rate} kbit/s ({frames[b]} frames, {errors} errors)")
    for b in pending:
        log(f"  bus {b}: no traffic heard")
    if pending and len(pending) == len(buses):
        log(f"  No traffic on any bus. Check CAN-H/CAN-L, ignition/ECU power, and the bit rate. {BENCH_HINT}")
    return result


class PandaSource:
    def __init__(self, serial, setup: BusSetup, log=print):
        self.setup = setup
        self.unpacker = p.CanUnpacker()
        self.header = []
        try:
            self.panda = Panda.open(serial, bootstub_ok=False)
        except UsbError as e:
            raise SourceError(str(e)) from None
        try:
            self._configure(log)
        except UsbError as e:
            self.close()
            raise SourceError(str(e)) from None
        except BaseException:
            self.close()
            raise

    def _configure(self, log):
        pd = self.panda
        hw = pd.hw_type()
        version = self.version = pd.version()
        hw_name = p.HW_NAMES.get(hw, f"panda type 0x{hw:02X}")
        self.serial = pd.serial
        mcu = p.MCU_BY_HW.get(hw)
        if mcu is None:
            self.header.append(f"note: this is a {hw_name}; PandaCapture is made for the Red and Black Panda")
        can_version = pd.packet_versions()[1]
        if can_version < p.CAN_PACKET_V4:
            raise SourceError(f"this panda's firmware ({version}) sends CAN in an older format (version "
                              f"{can_version}) than PandaCapture reads. Flash it with: pandacapture flash")
        expected = expected_packet_versions(mcu) if mcu else None
        if expected and can_version != expected[1]:
            self.header.append("note: the panda's CAN packet layout differs from this PandaCapture's firmware; "
                               "run pandacapture flash if frames look wrong")
        if not p.is_pandacapture_version(version):
            self.header.append("note: not PandaCapture firmware (capture works; transmitting needs it)")

        # Stay out of openpilot's power saving, and listen before anything else
        pd.disable_heartbeat()
        pd.set_power_save(False)
        pd.set_safety(p.SAFETY_SILENT)
        buses = range(p.CAN_BUSES)
        for b in buses:
            pd.set_canfd_auto(b, False)

        rates = dict(self.setup.rates)
        unknown = [b for b in buses if b not in rates]
        detected = {}
        if unknown:
            log("Finding bit rates (listening only)...")
            detected = detect_rates(pd, unknown, self.setup.detect_seconds, log)
        for b in buses:
            if b in rates:
                note = "set"
            elif detected[b][0]:
                rates[b] = detected[b][0]
                note = f"auto-detected ({detected[b][1]} frames)"
            else:
                rates[b] = 500
                note = "no traffic during detection, left at 500"
            pd.set_can_speed(b, rates[b])
            if mcu.fd if mcu else hw not in p.HW_F4:
                # A data rate at or above the nominal rate turns CAN FD reception on; classic frames still come in
                pd.set_data_speed(b, max(self.setup.data_rate, rates[b]))
            self.header.append(f"bus {b} (can{b}): {rates[b]} kbit/s, {note}")
        self.rates = rates

        if self.setup.mode == "ack":
            pd.set_safety(p.SAFETY_NOOUTPUT)
        if self.setup.obd:
            pd.set_obd(True)
        want = p.SAFETY_NOOUTPUT if self.setup.mode == "ack" else p.SAFETY_SILENT
        self.uptime = None   # seconds the panda had been running when opened; resets if it restarted
        self.voltage = None
        try:
            health = pd.health()
            mode = health["safety_mode"]
            self.uptime = health["uptime_s"]
            self.voltage = health["voltage_mv"] / 1000
        except UsbError:
            # Older firmware: its health packet isn't one PandaCapture reads, so trust the request
            mode = want
            self.header.append("note: can't read this firmware's health packet to confirm the safety mode")
        if mode != want:
            raise SourceError(f"the panda is in safety mode {mode}, not {want}; refusing to record")
        pd.reset_comms()
        pd.clear_rx()
        how = ("acknowledging frames, never transmitting" if self.setup.mode == "ack"
               else "silent: listen-only, no ACKs")
        self.description = f"{hw_name} {pd.serial}, firmware {version}, {how}"

    def read(self) -> list:
        try:
            return self.unpacker.feed(self.panda.read_can(10))
        except UsbError as e:
            raise SourceError(str(e)) from None

    def health(self):
        try:
            return self.panda.health()
        except UsbError:
            return None

    # ---- sending, for the dashboard's reads (dashboard.ActiveReads); the reader thread owns the panda ----

    @property
    def can_transmit(self) -> bool:
        """PandaCapture firmware: its armed transmit gate (and nothing else) lets frames out."""
        return p.is_pandacapture_version(self.version)

    def arm(self):
        """Transmit armed, with the heartbeat the firmware needs to stay armed (beat() at least every 0.25 s)."""
        try:
            self.panda.heartbeat(True)
            self.panda.set_safety(p.SAFETY_ALLOUTPUT, p.PANDACAPTURE_TX_ARM)
            self._beat_at = time.monotonic()
            mode = self.panda.health()["safety_mode"]
        except UsbError as e:
            self.disarm()
            raise SourceError(f"couldn't arm transmit: {e}") from None
        if mode != p.SAFETY_ALLOUTPUT:
            self.disarm()
            raise SourceError(f"the panda didn't arm (safety mode {mode}); nothing was sent")

    def beat(self):
        from .transmit import HEARTBEAT_EVERY
        now = time.monotonic()
        if now - getattr(self, "_beat_at", 0.0) >= HEARTBEAT_EVERY:
            self.panda.heartbeat(True)
            self._beat_at = now

    def send(self, frames):
        self.beat()
        for chunk in p.pack_frames(frames):
            self.panda.write_can(chunk)

    def disarm(self):
        """Back to listening as before (silent, or acknowledging), heartbeat off again."""
        want = p.SAFETY_NOOUTPUT if self.setup.mode == "ack" else p.SAFETY_SILENT
        try:
            self.panda.set_safety(want)
            self.panda.disable_heartbeat()
        except UsbError:
            pass   # the firmware disarms itself within about 2 s without a heartbeat

    def close(self):
        panda = getattr(self, "panda", None)
        if panda is not None:
            try:
                panda.set_safety(p.SAFETY_SILENT)
            except UsbError:
                pass
            panda.close()
            self.panda = None


class IdleSource:
    """No traffic: a dashboard opened to replay a log (--replay) with no panda to read. The replay itself is the
    dashboard's (replay.py), with its timeline."""

    idle = True

    def __init__(self):
        self.description = "no panda (replay only)"
        self.header = []
        self.serial = "none"
        self.rates = {}

    def read(self) -> list:
        time.sleep(0.05)
        return []

    def health(self):
        return None

    def close(self):
        pass


class SimulatedSource:
    """Fake P-CAN traffic (RPM on 0x316 etc.) on bus 0 and a little on bus 1. With a dropout time it
    goes silent then and fails 3 s later, to exercise the stall marks and reconnecting."""

    def __init__(self, dropout_at=0.0):
        self.start = time.monotonic()
        self.dropout_at = dropout_at
        self.next_ms = 0.0
        self.counter = 0
        self.rnd = random.Random(1)
        self.unpacker = p.CanUnpacker()
        self.description = "simulated P-CAN traffic (no panda)"
        self.header = ["bus 0 (can0): 500 kbit/s, simulated", "bus 1 (can1): 500 kbit/s, simulated"]
        self.serial = "simulated"

    def read(self) -> list:
        now = (time.monotonic() - self.start) * 1000
        if self.dropout_at and now >= self.dropout_at * 1000:
            if now >= (self.dropout_at + 3) * 1000:
                raise SourceError("simulated dropout: panda disconnected")
            return []
        out = []
        while self.next_ms <= now:
            s = self.next_ms / 1000
            rpm = int(800 + 2500 * max(0.0, math.sin(s / 4)))
            raw = rpm * 4
            out.append(p.Frame(0, 0x316, bytes([0, 0x10, raw & 0xFF, raw >> 8, 0, 0, 0, 0])))
            out.append(p.Frame(0, 0x329, bytes([0, int(160 + s / 20) & 0xFF, 0, 0, 0, 0, 0, 0])))
            out.append(p.Frame(0, 0x2A0, bytes([self.counter & 0xFF, 0x55, 1, (20 + rpm // 100) & 0xFF, 0x7F, 0, 0, 0])))
            if self.counter % 10 == 0:
                out.append(p.Frame(0, 0x5A0, bytes([self.rnd.randrange(256), 0, 0, 0])))
                out.append(p.Frame(1, 0x18FEF100, bytes([1, 2, 3, 4, 5, 6, 7, 8]), extended=True))
            self.counter += 1
            self.next_ms += 10
        # Through the real packet decoder, as frames from a panda would be
        return self.unpacker.feed(b"".join(p.pack_frame(f) for f in out))

    def health(self):
        return None

    def close(self):
        pass
