"""pandacapture obd: scan for the standard OBD PIDs every module answers, then poll the ones that give
real values, recording a capture and a CSV of the decoded answers. See obd.py for the rules."""

import argparse
import csv
import datetime as dt
import json
import time
from pathlib import Path

from . import protocol as p
from .capture import RollingLog, candump_line, default_out_dir
from .keys import KeyReader
from .obd import MAX_SCAN_RPM, PIDS, RpmGuard, ScanBlocked, Scanner, describe, pid_name
from .panda import Panda
from .sources import detect_rates
from .transmit import ArmedPanda, TransmitRefused, TxLog, acknowledge
from .usbdev import UsbError

LISTEN_FIRST = 1.0   # s of silent listening for the broadcast engine speed before anything is sent


def parser():
    ap = argparse.ArgumentParser(prog="pandacapture obd", description=(
        "Asks every OBD module which standard (mode 01) PIDs it supports, reads each once, then polls the ones "
        f"that give real values until you press Q. Read-only requests; blocked above {MAX_SCAN_RPM} rpm, so it "
        "only runs key-on or at idle. Needs PandaCapture firmware and the transmit acknowledgement."))
    ap.add_argument("--serial", help="which panda, when several are connected (see: pandacapture list)")
    ap.add_argument("--bus", type=int, action="append", choices=range(p.CAN_BUSES),
                    help="bus to scan (repeatable); default: every bus with traffic")
    ap.add_argument("--bitrate", type=int, help="kbit/s for the scanned buses; default: detected by listening")
    ap.add_argument("--map", help="address map with the engine's broadcast RPM (default: the built-in map, "
                                  "if there's one); without it, RPM is read over OBD first")
    ap.add_argument("--rpm-key", default="rpm", help="the map's engine speed signal (default rpm)")
    ap.add_argument("--scan-only", action="store_true", help="stop after the scan, without polling")
    ap.add_argument("--seconds", type=float, default=0, help="stop polling after this many seconds")
    ap.add_argument("--out", help="folder for the results (default: captures next to the program)")
    ap.add_argument("--i-accept-transmit-risk", action="store_true",
                    help="skip typing TRANSMIT (the warning is still shown); for scripts")
    return ap


def rpm_signal(args):
    from .signals import MapError, builtin_maps, load_map
    name = args.map or (next(iter(builtin_maps())) if len(builtin_maps()) == 1 else None)
    if not name:
        return None, ""
    try:
        m = load_map(name)
    except MapError as e:
        raise TransmitRefused(str(e)) from None
    s = next((s for s in m.signals if s.key == args.rpm_key and not s.derived), None)
    return s, m.name


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


def run(argv) -> int:
    args = parser().parse_args(argv)
    try:
        signal, map_name = rpm_signal(args)
    except TransmitRefused as e:
        print(f"ERROR: {e}")
        return 2
    out_dir = Path(args.out) if args.out else default_out_dir()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    guard = RpmGuard(signal)
    try:
        pd = Panda.open(args.serial, bootstub_ok=False)
    except UsbError as e:
        print(f"ERROR: {e}")
        return 1
    log = rec = None
    try:
        with pd:
            # 1. Listen only: bit rates, which buses have traffic, and the engine's speed
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
            buses = [b for b in wanted if b in rates]
            if not buses:
                raise TransmitRefused("No traffic heard on any bus: is the key on?")
            for b in buses:
                pd.set_can_speed(b, rates[b])
            unpacker = p.CanUnpacker()
            pd.reset_comms()
            pd.clear_rx()
            end = time.monotonic() + LISTEN_FIRST
            while time.monotonic() < end:
                for f in unpacker.feed(pd.read_can(10)):
                    guard.frame(f)
            if guard.fresh():
                print(f"Engine speed: {guard.rpm:.0f} rpm ({map_name}'s {signal.key} signal).")
                guard.check()
            else:
                print("No broadcast engine speed heard" + (f" ({map_name}'s {args.rpm_key} signal)" if signal else "")
                      + ": it will be read over OBD (one request) before anything else is asked.")
            version = pd.version()
            if not p.is_pandacapture_version(version):
                raise TransmitRefused(f"The panda runs {version!r}, not PandaCapture firmware, so it can't transmit. "
                                      "Flash it with: pandacapture flash")

            # 2. The warning and the user's acknowledgement
            rate_text = ", ".join(f"bus {b} at {rates[b]} kbit/s" for b in buses)
            acknowledge(args.i_accept_transmit_risk, (
                f"About to send OBD mode 01 requests (read-only: 02 01 PID) to 0x7DF on {rate_text}, one at a "
                f"time, then poll the PIDs that answer{' (scan only: no polling)' if args.scan_only else ' until you press Q'}.\n"
                f"It stops by itself if the engine goes above {MAX_SCAN_RPM} rpm or its speed stops being known.\n"
                "If a JB4 or scan tool is logging over OBD, pause it: both would get the same answers."))

            # 3. Scan, then poll, recording everything received
            log = TxLog(out_dir, f"{pd.serial}, {version}, {rate_text}")
            rec = RollingLog(out_dir, ["# PandaCapture candump log (OBD scan and poll)",
                                       f"# source: {pd.serial}, firmware {version}, acknowledging frames, sending "
                                       "OBD mode 01 requests", *[f"# bus {b} (can{b}): {rates[b]} kbit/s" for b in buses]],
                             stamp=stamp)
            columns = Columns(out_dir / f"obd-{stamp}.csv")
            scanner = None

            def received(f):
                rec.write(candump_line(time.time(), f) + "\n")
                scanner.frame(f)

            def sent(f):
                log.sent(f)
                rec.write(candump_line(time.time(), f) + "\n")

            stopped = None
            with ArmedPanda(pd, on_frame=received) as armed:
                scanner = Scanner(armed, guard, buses, on_sent=sent)
                print("Transmit armed. Scanning...")
                try:
                    result = scanner.scan()
                except ScanBlocked as e:
                    stopped, result = str(e), scanner.result
                print()
                print(result.report(buses))
                keep, _ = result.assess(buses)
                report = out_dir / f"obd-scan-{stamp}.json"
                report.write_text(json.dumps(result.to_json(buses), indent=2), encoding="utf-8")
                print(f"\nScan saved: {report}")
                if stopped is None and keep and not args.scan_only:
                    stopped = poll(scanner, keep, columns, args.seconds, rec.maybe_rotate)
            print("Disarmed.")
            if stopped:
                print(f"Stopped: {stopped}")
            print(f"  {scanner.result.requests} requests sent, logged in {log.path}")
            print(f"  Everything received: {rec.path}")
            if columns.save():
                print(f"  Decoded answers: {columns.path}  (a reference for: pandacapture match {rec.path.name} "
                      f"{columns.path.name}, or match --obd)")
    except KeyboardInterrupt:
        print("\nStopped; disarmed.")
        return 130
    except ScanBlocked as e:
        print(f"Not scanning: {e}")
        return 1
    except (TransmitRefused, UsbError) as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        for f in (log, rec):
            if f:
                f.close()
    return 0


def poll(scanner, keep, columns, seconds, rotate=lambda: None) -> str:
    """Polls until Q, the time limit, or the RPM interlock; returns why it stopped, if not by request."""
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

        try:
            scanner.poll(keep, should_stop, on_answer)
        except ScanBlocked as e:
            print()
            return str(e)
    print()
    return ""
