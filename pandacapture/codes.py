"""pandacapture codes: read the standard OBD codes (stored, pending, permanent), the check-engine light,
the freeze frame and vehicle information from every OBD module. Reads only: nothing is cleared.
Through a panda (PandaCapture firmware) or an ELM327."""

import argparse
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol as p
from .capture import RollingLog, candump_line, default_out_dir
from .diag import Client, nrc_text
from .obd import MODULES, describe, pid_name, supported_from
from .policy import VehicleState
from .transmit import TransmitRefused, TxLog, acknowledge
from .usbdev import UsbError

CODE_MODES = {0x03: "stored", 0x07: "pending", 0x0A: "permanent"}
INFO_TYPES = {0x04: "Calibration ID", 0x06: "Calibration verification number", 0x0A: "ECU name"}

WARNING = """\
This sends standard OBD-II read requests onto the vehicle's bus, the same ones a code reader sends:
codes (stored, pending, permanent), the check-engine light, the freeze frame and vehicle information.
Nothing is cleared or changed.

- Modules answer every request: pause other OBD tools (a JB4's logging, a scan tool) meanwhile.
- Key on (engine running or not).
"""


# ---------------------------------------------------------------- decoding

def dtc(hi, lo) -> str:
    """Two bytes as a code: P0301, U0100..."""
    return f"{'PCBU'[hi >> 6]}{(hi >> 4) & 3}{hi & 0x0F:X}{lo >> 4:X}{lo & 0x0F:X}"


def dtc_kind(code) -> str:
    """Whose definition it follows: SAE's (the same on every car) or the manufacturer's."""
    system, d1, d2 = code[0], code[1], code[2]
    if system == "P":
        if d1 in "02" or (d1 == "3" and d2 in "456789ABCDEF"):
            return "generic"
        return "manufacturer"
    return {"0": "generic", "1": "manufacturer", "2": "manufacturer"}.get(d1, "reserved")


def parse_dtcs(payload) -> list:
    """Codes from a mode 03/07/0A answer (43 NN code code...). On CAN the count comes first."""
    body = payload[1:]
    if len(body) % 2:
        body = body[1:]
    return [dtc(body[i], body[i + 1]) for i in range(0, len(body) - 1, 2) if body[i] or body[i + 1]]


def mil_status(data) -> dict:
    """From PID 01 (monitor status): the check-engine light and how many codes it counts."""
    return {"mil_on": bool(data[0] & 0x80), "codes": data[0] & 0x7F} if data else {}


def info_text(info_type, payload) -> list:
    """Mode 09 items: (49 type count data...). Text items as text, others as hex."""
    data = payload[3:] if len(payload) > 2 else b""
    if info_type in (0x02, 0x04, 0x0A):
        size = {0x02: 17, 0x04: 16, 0x0A: 20}[info_type]
        items = [data[i:i + size] for i in range(0, len(data), size)]
        return [bytes(c for c in item if 32 <= c < 127).decode().strip() for item in items if any(item)]
    if info_type == 0x06:
        return [data[i:i + 4].hex().upper() for i in range(0, len(data) - 3, 4)]
    return [data.hex().upper()]


# ---------------------------------------------------------------- reading

@dataclass
class ModuleCodes:
    bus: int
    module: int
    codes: dict = field(default_factory=dict)        # "stored"/"pending"/"permanent" -> list, or None = no answer
    refused: dict = field(default_factory=dict)      # what -> why the module refused
    mil: dict = field(default_factory=dict)
    freeze_dtc: str = ""
    freeze: dict = field(default_factory=dict)       # pid -> raw bytes
    info: dict = field(default_factory=dict)         # name -> list of strings

    def to_json(self) -> dict:
        return {"bus": self.bus, "module": f"{self.module:03X}", "name": MODULES.get(self.module, "module"),
                "check_engine_light": self.mil, "codes": {k: v for k, v in self.codes.items()},
                "refused": self.refused,
                "freeze_frame": {"code": self.freeze_dtc,
                                 "values": {f"{pid:02X}": {"name": pid_name(pid), "value": describe(pid, d),
                                                           "raw": d.hex()} for pid, d in sorted(self.freeze.items())}},
                "info": self.info}


def read_codes(client: Client, buses, with_vin=False, log=print) -> list:
    out = {}

    def entry(bus, module):
        return out.setdefault((bus, module), ModuleCodes(bus, module))

    for bus in buses:
        for module, payload in client.request(0x01, b"\x01", bus).positive.items():
            if len(payload) >= 3:
                entry(bus, module).mil = mil_status(payload[2:])
        for mode, what in CODE_MODES.items():
            answers = client.request(mode, b"", bus)
            for module, payload in answers.positive.items():
                entry(bus, module).codes[what] = parse_dtcs(payload)
            for module, code in answers.negative.items():
                entry(bus, module).refused[what] = nrc_text(code)
            for module, why in answers.broken.items():
                entry(bus, module).refused[what] = why
        if not any(b == bus for b, _ in out):
            log(f"Bus {bus}: no OBD module answered.")
            continue
        # The freeze frame: which code stored it, then the values it holds
        for module, payload in client.request(0x02, b"\x02\x00", bus).positive.items():
            if len(payload) >= 5 and (payload[3] or payload[4]):
                entry(bus, module).freeze_dtc = dtc(payload[3], payload[4])
        for (b, module), mc in out.items():
            if b != bus or not mc.freeze_dtc:
                continue
            supported = set()
            for base in (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
                a = client.request(0x02, bytes([base, 0]), bus, target=module - 8, expect=(module,))
                data = a.positive.get(module, b"")
                supported |= supported_from(base, data[3:]) if len(data) >= 7 else set()
                if base + 0x20 not in supported:
                    break
            for pid in sorted(supported - {0x02, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0}):
                a = client.request(0x02, bytes([pid, 0]), bus, target=module - 8, expect=(module,))
                data = a.positive.get(module)
                if data and len(data) > 3:
                    mc.freeze[pid] = data[3:]
        # Vehicle information
        for module, payload in client.request(0x09, b"\x00", bus).positive.items():
            types = supported_from(0x00, payload[2:]) if len(payload) >= 6 else set()
            wanted = [t for t in (0x02, 0x04, 0x06, 0x0A) if t in types and (t != 0x02 or with_vin)]
            for t in wanted:
                a = client.request(0x09, bytes([t]), bus, target=module - 8, expect=(module,))
                if module in a.positive:
                    entry(bus, module).info["VIN" if t == 0x02 else INFO_TYPES[t]] = info_text(t, a.positive[module])
    return [out[k] for k in sorted(out)]


def report(modules) -> str:
    if not modules:
        return "No OBD module answered."
    lines = []
    for m in modules:
        lines.append(f"Bus {m.bus}, {MODULES.get(m.module, 'module')} ({m.module:03X})")
        if m.mil:
            lines.append(f"  Check-engine light: {'ON' if m.mil['mil_on'] else 'off'}, {m.mil['codes']} codes counted")
        for what in CODE_MODES.values():
            if what in m.codes:
                codes = m.codes[what]
                lines.append(f"  {what.capitalize():<10} " + (", ".join(f"{c} ({dtc_kind(c)})" for c in codes)
                                                                if codes else "none"))
            elif what in m.refused:
                lines.append(f"  {what.capitalize():<10} not read: {m.refused[what]}")
        if m.freeze_dtc:
            lines.append(f"  Freeze frame, stored by {m.freeze_dtc}:")
            for pid, d in sorted(m.freeze.items()):
                lines.append(f"    {pid_name(pid):<46} {describe(pid, d)}")
        for name, values in m.info.items():
            lines.append(f"  {name}: {', '.join(values) or '(empty)'}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the command

def parser():
    ap = argparse.ArgumentParser(prog="pandacapture codes", description=(
        "Reads the standard OBD codes (stored, pending, permanent), the check-engine light, the freeze frame "
        "and vehicle information from every OBD module. Reads only: nothing is cleared. Through a panda with "
        "PandaCapture firmware, or an ELM327 (--elm)."))
    ap.add_argument("--elm", metavar="PORT", help="use an ELM327: COM5, /dev/rfcomm0, socket://192.168.0.10:35000")
    ap.add_argument("--elm-protocol", default="0", metavar="N", help="ELM327 OBD protocol (0 = automatic)")
    ap.add_argument("--serial", help="which panda, when several are connected")
    ap.add_argument("--bus", type=int, action="append", choices=range(p.CAN_BUSES),
                    help="panda bus to read (repeatable); default: every bus with traffic")
    ap.add_argument("--bitrate", type=int, help="kbit/s for the panda's buses; default: detected by listening")
    ap.add_argument("--vin", action="store_true", help="also read the VIN (left out by default: it identifies the car)")
    ap.add_argument("--out", help="folder for the results (default: captures next to the program)")
    ap.add_argument("--i-accept-transmit-risk", action="store_true",
                    help="skip typing TRANSMIT (the warning is still shown); for scripts")
    return ap


def run(argv) -> int:
    from .elm import ElmError
    from .obd_run import ElmSession, PandaSession, state_signals
    args = parser().parse_args(argv)
    args.map, args.rpm_key = None, "rpm"
    try:
        rpm_sig, speed_sig, gear_sig, map_name = state_signals(args) if not args.elm else (None, None, None, "")
    except TransmitRefused as e:
        print(f"ERROR: {e}")
        return 2
    state = VehicleState(rpm_sig, speed_sig, gear_sig)
    session = (ElmSession if args.elm else PandaSession)(args, state, False)
    out_dir = Path(args.out) if args.out else default_out_dir()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = rec = None
    try:
        session.open(rpm_sig, map_name)
        acknowledge(args.i_accept_transmit_risk,
                    f"About to read codes through {session.kind}: {session.rate_text}.", warning=WARNING)
        log = TxLog(out_dir, f"{session.kind}: {session.description}")
        rec = RollingLog(out_dir, ["# PandaCapture candump log (reading codes)", *session.header], stamp=stamp)
        client = None

        def received(f):
            rec.write(candump_line(time.time(), f) + "\n")
            client.frame(f)

        def sent(f):
            log.sent(f)
            rec.write(candump_line(time.time(), f) + "\n")

        with session.link(received) as link:
            client = Client(link, state, on_sent=sent)
            print("Reading codes...")
            modules = read_codes(client, session.buses, with_vin=args.vin)
        print()
        print(report(modules))
        path = out_dir / f"codes-{stamp}.json"
        path.write_text(json.dumps({"read": dt.datetime.now().isoformat(timespec="seconds"),
                                    "through": f"{session.kind}: {session.description}",
                                    "modules": [m.to_json() for m in modules]}, indent=2), encoding="utf-8")
        print(f"\nSaved: {path}")
        print(f"  Requests logged in {log.path}; everything received in {rec.path}")
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except (TransmitRefused, UsbError, ElmError) as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        for f in (log, rec):
            if f:
                f.close()
        session.close()
    return 0
