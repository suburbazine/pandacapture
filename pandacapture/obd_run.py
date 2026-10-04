"""pandacapture obd: scan for the standard OBD PIDs every module answers, then poll the ones that give
real values, recording what's received and a CSV of the decoded answers. Through a panda (PandaCapture
firmware) or an ELM327. See obd.py for the rules."""

import argparse
import csv
import datetime as dt
import json
import time
from pathlib import Path

from . import protocol as p
from .capture import RollingLog, candump_line, default_out_dir
from .keys import KeyReader
from .obd import MAX_SCAN_RPM, PIDS, Found, ScanBlocked, Scanner, describe, pid_name
from .policy import VehicleState
from .transmit import ArmedPanda, TransmitRefused, TxLog, acknowledge
from .usbdev import UsbError

LISTEN_FIRST = 1.0   # s of silent listening for the broadcast engine speed before anything is sent

OBD_WARNING = """\
This sends standard OBD-II requests (mode 01, "show current data") onto the vehicle's bus, the same
requests a scan tool or a logger sends. They read values; they can't change anything in the car.

- Modules answer every request: pause other OBD tools (a JB4's logging, a scan tool) meanwhile, or
  both may get confused answers.
- Set it up with the vehicle parked. The scan (finding what's supported) only runs key-on or at idle,
  up to 900 rpm. The poll that follows keeps reading at any engine speed.
"""


def parser():
    ap = argparse.ArgumentParser(prog="pandacapture obd", description=(
        "Asks every OBD module which standard (mode 01) PIDs it supports, reads each once, then polls the ones "
        f"that give real values until you press Q. Read-only requests. The scan is blocked above {MAX_SCAN_RPM} "
        "rpm (key-on or idle only); the poll runs at any engine speed. Through a panda with PandaCapture firmware, "
        "or an ELM327 (--elm)."))
    ap.add_argument("--elm", metavar="PORT", help=(
        "use an ELM327 instead of a panda: COM5, /dev/ttyUSB0, /dev/rfcomm0 (Bluetooth), or "
        "socket://192.168.0.10:35000 (WiFi). CAN cars only"))
    ap.add_argument("--elm-protocol", default="0", metavar="N",
                    help="ELM327 OBD protocol: 0 = automatic (default), 6-9 = CAN (6: 11-bit 500k)")
    ap.add_argument("--serial", help="which panda, when several are connected (see: pandacapture list)")
    ap.add_argument("--bus", type=int, action="append", choices=range(p.CAN_BUSES),
                    help="panda bus to use (repeatable); default: every bus with traffic")
    ap.add_argument("--bitrate", type=int, help="kbit/s for the panda's buses; default: detected by listening")
    ap.add_argument("--map", help="address map with the engine's broadcast RPM (default: the built-in map, "
                                  "if there's one); without it, RPM is read over OBD first")
    ap.add_argument("--rpm-key", default="rpm", help="the map's engine speed signal (default rpm)")
    what = ap.add_mutually_exclusive_group()
    what.add_argument("--scan-only", action="store_true", help="stop after the scan, without polling")
    what.add_argument("--poll", metavar="SCAN.json",
                      help="skip the scan: poll the PIDs an earlier scan kept (its obd-scan-….json)")
    what.add_argument("--pids", metavar="LIST", help="skip the scan: poll these PIDs, e.g. 0C,0D,05")
    ap.add_argument("--seconds", type=float, default=0, help="stop polling after this many seconds")
    ap.add_argument("--out", help="folder for the results (default: captures next to the program)")
    ap.add_argument("--i-accept-transmit-risk", action="store_true",
                    help="skip typing TRANSMIT (the warning is still shown); for scripts")
    return ap


def state_signals(args):
    """The map's broadcast signals for the vehicle state: (rpm, speed, gear, map name)."""
    from .signals import MapError, builtin_maps, load_map
    name = args.map or (next(iter(builtin_maps())) if len(builtin_maps()) == 1 else None)
    if not name:
        return None, None, None, ""
    try:
        m = load_map(name)
    except MapError as e:
        raise TransmitRefused(str(e)) from None

    def first(*keys):
        return next((s for k in keys for s in m.signals if s.key == k and not s.derived), None)
    return first(args.rpm_key), first("speed_kmh", "speed"), first("gear"), m.name


def planned_poll(args):
    """The PIDs to poll without scanning, from --poll or --pids; None to scan first."""
    if args.pids:
        try:
            pids = [int(x, 16) for x in args.pids.replace(" ", "").split(",") if x]
        except ValueError:
            raise TransmitRefused(f"--pids {args.pids}: give hex PIDs separated by commas, e.g. 0C,0D,05") from None
        if not pids or any(not 0 < x < 0x100 for x in pids):
            raise TransmitRefused(f"--pids {args.pids}: PIDs are 01 to FF")
        return [Found(None, x, ()) for x in pids]   # bus filled in once the buses are known
    if args.poll:
        try:
            data = json.loads(Path(args.poll).read_text(encoding="utf-8"))
            return [Found(int(e["bus"]), int(e["pid"], 16), tuple(int(m, 16) for m in e["modules"]))
                    for e in data["polled"]]
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise TransmitRefused(f"{args.poll}: not a scan saved by pandacapture obd ({e})") from None
    return None


class Columns:
    """The CSV of decoded answers: one row per answer, a column per (bus, module, PID). Engine RPM is
    called RPM so Find signals lines it up with a capture by itself."""

    def __init__(self, path):
        self.path = path
        self.names = {}
        self.rows = []

    def name(self, bus, module, pid):
        key = (bus, module, pid)
        if key not in self.names:
            unit = PIDS.get(pid, ("", "", None))[1]
            base = "RPM" if pid == 0x0C and module == 0x7E8 else f"{pid_name(pid)}" + (f" ({unit})" if unit else "")
            where = [] if module == 0x7E8 else [f"{module:03X}"]
            where += [] if bus == 0 else [f"bus {bus}"]
            self.names[key] = base + (f" [{', '.join(where)}]" if where else "")
        return self.names[key]

    def add(self, t, bus, module, pid, data):
        fn = PIDS.get(pid, ("", "", None))[2]
        if fn is None:
            return
        try:
            self.rows.append((t, self.name(bus, module, pid), round(fn(data), 4)))
        except (IndexError, ZeroDivisionError):
            pass

    def save(self):
        if not self.rows:
            return False
        names = list(self.names.values())
        col = {n: i for i, n in enumerate(names)}
        with open(self.path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time"] + names)
            for t, n, v in self.rows:
                row = [""] * len(names)
                row[col[n]] = v
                w.writerow([f"{t:.3f}"] + row)
        return True


# ---------------------------------------------------------------- the two ways to reach the bus

class PandaSession:
    kind = "panda"

    def __init__(self, args, guard, scanning):
        self.args, self.guard, self.scanning = args, guard, scanning
        self.pd = None

    def open(self, signal, map_name):
        from .panda import Panda
        from .sources import detect_rates
        args = self.args
        self.pd = pd = Panda.open(args.serial, bootstub_ok=False)
        pd.disable_heartbeat()
        pd.set_power_save(False)
        pd.set_safety(p.SAFETY_SILENT)
        wanted = sorted(set(args.bus)) if args.bus else list(range(p.CAN_BUSES))
        if args.bitrate:
            rates = {b: args.bitrate for b in wanted}
        else:
            print("Finding bit rates (listening only)...")
            found = detect_rates(pd, wanted, 0.4, print)
            rates = {b: r for b, (r, _, _) in found.items() if r}
        self.buses = [b for b in wanted if b in rates]
        if not self.buses:
            raise TransmitRefused("No traffic heard on any bus: is the key on?")
        for b in self.buses:
            pd.set_can_speed(b, rates[b])
        unpacker = p.CanUnpacker()
        pd.reset_comms()
        pd.clear_rx()
        end = time.monotonic() + LISTEN_FIRST
        while time.monotonic() < end:
            for f in unpacker.feed(pd.read_can(10)):
                self.guard.frame(f)
        if self.guard.fresh():
            print(f"Engine speed: {self.guard.rpm:.0f} rpm ({map_name}'s {signal.key} signal).")
            if self.scanning:
                self.guard.check_scan()
        elif self.scanning:
            print("No broadcast engine speed heard" + (f" ({map_name}'s {self.args.rpm_key} signal)" if signal else "")
                  + ": it will be read over OBD (one request) before anything else is asked.")
        version = pd.version()
        if not p.is_pandacapture_version(version):
            raise TransmitRefused(f"The panda runs {version!r}, not PandaCapture firmware, so it can't transmit. "
                                  "Flash it with: pandacapture flash")
        self.rate_text = ", ".join(f"bus {b} at {rates[b]} kbit/s" for b in self.buses)
        self.description = f"{pd.serial}, firmware {version}, {self.rate_text}"
        self.header = [f"# source: {pd.serial}, firmware {version}, acknowledging frames, sending OBD mode 01 requests",
                       *[f"# bus {b} (can{b}): {rates[b]} kbit/s" for b in self.buses]]

    def link(self, on_frame):
        return ArmedPanda(self.pd, on_frame=on_frame)

    def close(self):
        if self.pd is not None:
            self.pd.close()


class ElmSession:
    kind = "ELM327"

    def __init__(self, args, guard, scanning):
        self.args, self.guard, self.scanning = args, guard, scanning
        self.elm = None
        self.buses = [0]

    def open(self, signal, map_name):
        from .elm import Elm
        print(f"Connecting to the ELM327 on {self.args.elm}...")
        self.elm = Elm(self.args.elm, self.args.elm_protocol)
        self.rate_text = self.elm.description
        self.description = self.elm.description
        self.header = [f"# source: {self.elm.description}, OBD mode 01 requests and answers only "
                       "(an ELM327 doesn't record the bus)"]
        if self.scanning:
            print("Engine speed will be read over OBD (one request) before anything else is asked.")

    def link(self, on_frame):
        from .elm import ElmLink
        return ElmLink(self.elm, on_frame)

    def close(self):
        if self.elm is not None:
            self.elm.close()


# ---------------------------------------------------------------- the command

def run(argv) -> int:
    from .elm import ElmError
    args = parser().parse_args(argv)
    try:
        planned = planned_poll(args)
        rpm_sig, speed_sig, gear_sig, map_name = state_signals(args) if not args.elm else (None, None, None, "")
    except TransmitRefused as e:
        print(f"ERROR: {e}")
        return 2
    scanning = planned is None
    out_dir = Path(args.out) if args.out else default_out_dir()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    guard = VehicleState(rpm_sig, speed_sig, gear_sig)
    session = (ElmSession if args.elm else PandaSession)(args, guard, scanning)
    log = rec = None
    try:
        session.open(rpm_sig, map_name)
        if planned is not None:
            planned = [Found(b, f.pid, f.modules) for f in planned
                       for b in ([f.bus] if f.bus is not None else session.buses[:1]) if b in session.buses]
            if not planned:
                raise TransmitRefused("None of those PIDs are on a bus with traffic.")

        what = ("scan the standard PIDs (key-on or idle, up to 900 rpm), then "
                + ("stop" if args.scan_only else "poll the ones that answer until you press Q")) if scanning \
            else f"poll {len(planned)} PIDs until you press Q (no scan first)"
        acknowledge(args.i_accept_transmit_risk, (
            f"About to send OBD mode 01 requests (02 01 PID) to 0x7DF through {session.kind}: {session.rate_text}.\n"
            f"It will {what}."), warning=OBD_WARNING)

        log = TxLog(out_dir, f"{session.kind}: {session.description}")
        rec = RollingLog(out_dir, ["# PandaCapture candump log (OBD " + ("scan and poll)" if scanning else "poll)"),
                                   *session.header], stamp=stamp)
        columns = Columns(out_dir / f"obd-{stamp}.csv")
        scanner = None

        def received(f):
            rec.write(candump_line(time.time(), f) + "\n")
            scanner.frame(f)

        def sent(f):
            log.sent(f)
            rec.write(candump_line(time.time(), f) + "\n")

        stopped = None
        with session.link(received) as link:
            scanner = Scanner(link, guard, session.buses, on_sent=sent)
            if scanning:
                print(f"{'Transmit armed. ' if session.kind == 'panda' else ''}Scanning...")
                try:
                    result = scanner.scan()
                except ScanBlocked as e:
                    stopped, result = str(e), scanner.result
                print()
                print(result.report(session.buses))
                keep, _ = result.assess(session.buses)
                report = out_dir / f"obd-scan-{stamp}.json"
                report.write_text(json.dumps(result.to_json(session.buses), indent=2), encoding="utf-8")
                print(f"\nScan saved: {report}")
                if not stopped and keep and not args.scan_only:
                    print(f"  (next time, poll these without scanning: pandacapture obd --poll {report.name})")
            else:
                keep = planned
            if not stopped and keep and not args.scan_only:
                poll(scanner, keep, columns, args.seconds, rec.maybe_rotate)
        if session.kind == "panda":
            print("Disarmed.")
        if stopped:
            print(f"Stopped: {stopped}")
        print(f"  {scanner.result.requests} requests sent, logged in {log.path}")
        print(f"  Everything received: {rec.path}")
        if columns.save():
            print(f"  Decoded answers: {columns.path}  (a reference for: pandacapture match {rec.path.name} "
                  f"{columns.path.name}, or match --obd)")
    except KeyboardInterrupt:
        print("\nStopped" + ("; disarmed." if session.kind == "panda" else "."))
        return 130
    except ScanBlocked as e:
        print(f"Not scanning: {e}")
        return 1
    except (TransmitRefused, UsbError, ElmError) as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        for f in (log, rec):
            if f:
                f.close()
        session.close()
    return 0


def poll(scanner, keep, columns, seconds, rotate=lambda: None):
    """Polls until Q or the time limit."""
    print(f"\nPolling {len(keep)} PIDs. Press Q to stop.")
    start = last = time.monotonic()
    answers = {"n": 0, "at_last": 0}
    latest = {}

    def on_answer(bus, module, pid, data):
        columns.add(time.time(), bus, module, pid, data)
        answers["n"] += 1
        latest[(bus, module, pid)] = data

    with KeyReader() as keys:
        quit_ = {"q": False}

        def should_stop():
            nonlocal last
            now = time.monotonic()
            if any(k.lower() == "q" or k == "\x1b" for k in keys.poll()):
                quit_["q"] = True
            if now - last >= 1:
                rate = (answers["n"] - answers["at_last"]) / (now - last)
                answers["at_last"], last = answers["n"], now
                shown = ", ".join(f"{pid_name(pid).split(',')[0]} {describe(pid, d)}"
                                  for (b, m, pid), d in list(latest.items())[:4])
                print(f"\r  {rate:5.1f} answers/s   {shown}"[:118].ljust(118), end="", flush=True)
                rotate()
            return quit_["q"] or (seconds and now - start >= seconds)

        scanner.poll(keep, should_stop, on_answer)
    print()
