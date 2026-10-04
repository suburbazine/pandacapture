"""Command line: pandacapture [capture options] | list | info | dashboard | maps | match | flash | backup | restore | send |
replay | selftest"""

import argparse
import sys
import time

from . import __version__
from . import protocol as p
from .capture import CaptureOptions, Console, capture
from .dfu import list_dfu
from .firmware import Firmware, FirmwareError, bundled_versions
from .backup import default_dir as default_backup_dir
from .flasher import FlashError, flash, make_backup, restore
from .panda import Panda, list_pandas
from .keys import KeyReader
from .sources import BusSetup, PandaSource, SimulatedSource, SourceError, detect_rates
from .transmit import ArmedPanda, TransmitRefused, TxLog, acknowledge, read_replay
from .usbdev import UsbError

COMMANDS = ("capture", "list", "info", "dashboard", "maps", "match", "obd", "codes", "modules", "did", "analyze", "apikey", "bundle", "flash", "backup", "restore", "send", "replay",
            "selftest")


def parse_rates(values) -> dict:
    """--bitrate 500 (every bus) or --bitrate 0=500 --bitrate 1=125."""
    rates = {}
    for v in values or []:
        if "=" in v:
            bus, rate = v.split("=", 1)
            buses = [int(bus)]
        else:
            rate, buses = v, range(p.CAN_BUSES)
        if rate.lower() == "auto":
            continue
        kbps = int(rate)
        if kbps not in p.CAN_SPEEDS:
            raise argparse.ArgumentTypeError(f"--bitrate {kbps}: the panda supports {', '.join(map(str, p.CAN_SPEEDS))}")
        for b in buses:
            if not 0 <= b < p.CAN_BUSES:
                raise argparse.ArgumentTypeError(f"bus {b}: the Red Panda has buses 0-{p.CAN_BUSES - 1}")
            rates[b] = kbps
    return rates


def add_common(ap):
    ap.add_argument("--serial", help="which panda, when several are connected (see: pandacapture list)")


def add_adapter(ap):
    ap.add_argument("--adapter", metavar="NAME", help=(
        "record through an RP1210 or J2534 adapter instead of a panda (Windows), e.g. --adapter \"USB-Link 2,USB\". "
        "pandacapture list shows the installed ones. They acknowledge frames; J2534 ones need --bitrate"))


def adapter_rate(rates):
    """An adapter has one bus: --bitrate 500, or bus 0's rate."""
    return rates.get(0) or (next(iter(rates.values())) if rates else None)


def adapter_opener(spec, rates):
    """open_source for --adapter, or an error message."""
    from .adapters import AdapterSource, bridge_args, find_adapter
    try:
        adapter = find_adapter(spec)
        bridge_args(adapter, adapter_rate(rates))   # a J2534 adapter without a bit rate fails here, not later
    except SourceError as e:
        return None, str(e)
    if adapter.problem:
        return None, f"{adapter.label()}: {adapter.problem}"
    return (lambda: AdapterSource(adapter, adapter_rate(rates))), None


def capture_parser():
    ap = argparse.ArgumentParser(prog="pandacapture", description=(
        "Records a car's CAN buses through a comma Red Panda into a candump log, listen-only by default. "
        "Other commands: list, info, dashboard, match, analyze, bundle, obd, codes, modules, did, flash, send, replay, selftest (pandacapture COMMAND --help)."))
    add_common(ap)
    add_adapter(ap)
    ap.add_argument("--bitrate", action="append", metavar="RATE",
                    help="auto (default), or kbit/s for every bus (500), or per bus (0=500); repeatable")
    ap.add_argument("--data-bitrate", type=int, default=2000, metavar="KBPS",
                    help="CAN FD data phase rate (default 2000)")
    ap.add_argument("--bus", type=int, action="append", choices=range(p.CAN_BUSES),
                    help="record only this bus (repeatable); default all three")
    ap.add_argument("--ack", action="store_true",
                    help="acknowledge frames like a normal node (needed when the panda is the only other node, "
                         "e.g. an ECU on the bench). Never transmits frames. Needs --bitrate")
    ap.add_argument("--obd", action="store_true", help="record CAN3 (the multiplexed OBD bus, OBD-C pins B10/B11) as bus 1 instead of CAN1")
    ap.add_argument("--out", help="folder for capture files (default: captures next to the program)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after this many seconds")
    ap.add_argument("--split-mb", type=float, default=100,
                    help="start a new capture file past this size (default 100, about 15 minutes of a busy bus; 0 = never)")
    ap.add_argument("--no-reconnect", action="store_true", help="stop on a panda error instead of reconnecting")
    ap.add_argument("--reconnect-seconds", type=float, default=60, help="how long to keep trying (default 60)")
    ap.add_argument("--simulate", action="store_true", help="fake traffic, to try the tool without a panda")
    ap.add_argument("--simulate-dropout", type=float, metavar="N",
                    help="fake traffic that drops out after N seconds, to try reconnecting")
    ap.add_argument("--list", action="store_true", help="same as: pandacapture list")
    ap.add_argument("--selftest", action="store_true", help="same as: pandacapture selftest")
    ap.add_argument("--version", action="version", version=f"PandaCapture {__version__}")
    return ap


def cmd_capture(argv) -> int:
    args = capture_parser().parse_args(argv)
    if args.list:
        return cmd_list([])
    if args.selftest:
        return cmd_selftest([])
    try:
        rates = parse_rates(args.bitrate)
    except (argparse.ArgumentTypeError, ValueError) as e:
        print(f"ERROR: {e}")
        return 2
    if args.ack and len(rates) < p.CAN_BUSES:
        print("ERROR: --ack needs --bitrate for every bus (e.g. --bitrate 500): a node acknowledging at the "
              "wrong rate disturbs the bus.")
        return 2
    if args.data_bitrate not in p.DATA_SPEEDS:
        print(f"ERROR: --data-bitrate must be one of {', '.join(map(str, p.DATA_SPEEDS))}")
        return 2

    opts = CaptureOptions(out_dir=args.out, seconds=args.seconds, reconnect=not args.no_reconnect,
                          reconnect_seconds=args.reconnect_seconds, buses=tuple(args.bus or ()), split_mb=args.split_mb)
    if args.adapter and not (args.simulate or args.simulate_dropout):
        open_source, error = adapter_opener(args.adapter, rates)
        if error:
            print(f"ERROR: {error}")
            return 2
    elif args.simulate or args.simulate_dropout:
        opened = []

        def open_source():
            # Only the first simulated source drops out, so a reconnect gets steady traffic
            opened.append(1)
            return SimulatedSource(args.simulate_dropout if len(opened) == 1 and args.simulate_dropout else 0)
    else:
        setup = BusSetup(mode="ack" if args.ack else "silent", rates=rates, data_rate=args.data_bitrate, obd=args.obd)
        serial = [args.serial]

        def open_source():
            src = PandaSource(serial[0], setup, log=print)
            # Reconnects reopen this panda with the rates already found
            serial[0] = src.serial
            setup.rates = dict(src.rates)
            return src
    return capture(open_source, opts, Console())


def cmd_list(argv) -> int:
    argparse.ArgumentParser(prog="pandacapture list", description=(
        "Connected pandas and STM32 bootloaders, and installed RP1210 and J2534 adapters.")).parse_args(argv)
    from .adapters import list_adapters
    status = 0
    try:
        devices = list_pandas() + list_dfu()
    except Exception as e:  # noqa: BLE001 - libusb missing etc.
        print(f"ERROR: can't list USB devices: {e}")
        devices, status = [], 1
    if not devices and not status:
        print("No panda found. Connect it by USB (and see the README's driver notes).")
    for d in devices:
        kind = {"panda": "panda (firmware running)", "bootstub": "panda bootstub (flasher)",
                "dfu": "STM32 bootloader (DFU)"}[d.kind]
        print(f"  --serial {d.serial:<26} {kind}" + (f"\n      {d.note}" if d.note else ""))
    adapters = list_adapters()
    if adapters:
        # Drivers are listed whether or not their adapter is plugged in
        print("\nRP1210 and J2534 adapter drivers with CAN (use with --adapter):")
        for a in adapters:
            print(f"  --adapter \"{a.key}\"\n      {a.label()}"
                  + (f"\n      can't be used: {a.problem}" if a.problem else ""))
    from .elm import list_ports
    ports = list_ports()
    if ports:
        print("\nSerial ports (an ELM327 for pandacapture obd --elm PORT; WiFi ones: --elm socket://192.168.0.10:35000):")
        for device, description in ports:
            print(f"  --elm {device:<14} {description}")
    return status


def cmd_info(argv) -> int:
    ap = argparse.ArgumentParser(prog="pandacapture info", description="Firmware, health and bus settings of a panda.")
    add_common(ap)
    args = ap.parse_args(argv)
    builds = bundled_versions()
    print("Bundled firmware: " + (", ".join(f"{t} {v}" for t, v in builds.items()) if builds
                                  else "none (build it with firmware/build.py)"))
    try:
        with Panda.open(args.serial) as pd:
            hw = pd.hw_type()
            print(f"Panda {pd.serial}: {p.HW_NAMES.get(hw, f'type 0x{hw:02X}')}")
            if pd.bootstub:
                print("  In its bootstub (flasher): no firmware running. Run: pandacapture flash")
                return 0
            version = pd.version()
            ours = p.is_pandacapture_version(version)
            print(f"  Firmware: {version}" + ("" if ours else "  (not PandaCapture: capture only, no transmit)"))
            mcu = p.MCU_BY_HW.get(hw)
            if mcu and ours and builds.get(mcu.target) not in (None, version):
                print("  A different PandaCapture build than the bundled one: pandacapture flash updates it.")
            try:
                h = pd.health()
                temp = f", {h['temperature_c']:.0f} C" if h["temperature_c"] is not None else ""
                print(f"  Supply {h['voltage_mv'] / 1000:.2f} V{temp}, up {h['uptime_s']} s, "
                      f"mode {p.SAFETY_NAMES.get(h['safety_mode'], h['safety_mode'])}")
                for b in range(p.CAN_BUSES):
                    c = pd.can_health(b)
                    fd = f" (FD data {c['data_speed_kbps']:g})" if (mcu.fd if mcu else hw not in p.HW_F4) else ""
                    print(f"  bus {b}: {c['speed_kbps']:g} kbit/s{fd}, {c['total_rx']} received, "
                          f"{c['total_errors']} errors, last error {c['last_stored_error']}")
            except UsbError as e:
                print(f"  Health: {e}")
    except UsbError as e:
        print(f"ERROR: {e}")
        return 1
    return 0


def cmd_flash(argv) -> int:
    ap = argparse.ArgumentParser(prog="pandacapture flash", description=(
        "Puts the PandaCapture firmware on a Red Panda. The first time, this also replaces comma's bootstub "
        "through the STM32 bootloader (DFU)."))
    add_common(ap)
    ap.add_argument("--firmware", metavar="DIR", help="firmware folder (default: the one bundled with this program)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--recover", action="store_true", help="always rewrite the bootstub through DFU")
    g.add_argument("--no-recover", action="store_true", help="never go through DFU (update the app only)")
    ap.add_argument("--force", action="store_true", help="flash a panda that doesn't report itself as a Red Panda")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--backup-dir", metavar="DIR",
                    help="where the whole-flash backup goes before the bootstub is replaced (default: backups "
                         "next to the program)")
    ap.add_argument("--no-backup", action="store_true",
                    help="don't back up the flash first (if the chip won't read back, e.g. read protection)")
    args = ap.parse_args(argv)

    def load_firmware(mcu):
        try:
            return Firmware.load(mcu, args.firmware)
        except FirmwareError as e:
            raise FlashError(str(e)) from None

    def confirm():
        if args.yes:
            return True
        try:
            return input("Flash it? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    recover = True if args.recover else False if args.no_recover else None
    try:
        version = flash(load_firmware, serial=args.serial, recover=recover, force=args.force, confirm=confirm,
                        log=lambda s: print("  " + s),
                        backup_dir=None if args.no_backup else (args.backup_dir or default_backup_dir()))
    except (FlashError, UsbError) as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Done: the panda runs {version}")
    return 0


def cmd_backup(argv) -> int:
    ap = argparse.ArgumentParser(prog="pandacapture backup", description=(
        "Saves the panda's whole flash (bootstub, firmware, settings) through the STM32 bootloader, then "
        "restarts it. Nothing is written to the panda. pandacapture restore puts a backup back."))
    add_common(ap)
    ap.add_argument("--dir", help="where to save it (default: backups next to the program)")
    args = ap.parse_args(argv)
    try:
        path = make_backup(args.dir or default_backup_dir(), serial=args.serial, log=lambda s: print("  " + s))
    except (FlashError, UsbError) as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Done: {path}")
    return 0


def cmd_restore(argv) -> int:
    ap = argparse.ArgumentParser(prog="pandacapture restore", description=(
        "Writes a whole-flash backup (made by pandacapture flash) back onto the panda, through the STM32 "
        "bootloader: its bootstub, firmware and settings, as they were."))
    add_common(ap)
    ap.add_argument("backup", help="the backup's .bin file (its .json must sit next to it)")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args(argv)

    def confirm():
        if args.yes:
            return True
        try:
            return input("Restore it? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    try:
        version = restore(args.backup, serial=args.serial, confirm=confirm, log=lambda s: print("  " + s))
    except (FlashError, UsbError) as e:
        print(f"ERROR: {e}")
        return 1
    print(f"Done: the panda runs {version}")
    return 0


def tx_parser(prog, description):
    ap = argparse.ArgumentParser(prog=prog, description=description)
    add_common(ap)
    ap.add_argument("--bitrate", action="append", metavar="RATE",
                    help="kbit/s for every bus or per bus (0=500); default: detected by listening first")
    ap.add_argument("--out", help="folder for the transmit log (default: captures next to the program)")
    ap.add_argument("--i-accept-transmit-risk", action="store_true",
                    help="skip typing TRANSMIT (the warning is still shown); for scripts")
    return ap


def prepare_tx(args, buses):
    """Opens the panda listen-only and sets the rates of the buses about to be used."""
    rates = parse_rates(args.bitrate)
    pd = Panda.open(args.serial, bootstub_ok=False)
    try:
        pd.disable_heartbeat()
        pd.set_power_save(False)
        pd.set_safety(p.SAFETY_SILENT)
        missing = [b for b in buses if b not in rates]
        if missing:
            print("Finding bit rates (listening only)...")
            found = detect_rates(pd, missing, 0.4, print)
            for b in missing:
                if not found[b][0]:
                    raise TransmitRefused(f"No traffic heard on bus {b}, so its bit rate is unknown: pass --bitrate.")
                rates[b] = found[b][0]
        for b in buses:
            pd.set_can_speed(b, rates[b])
        return pd, rates
    except BaseException:
        pd.close()
        raise


def cmd_send(argv) -> int:
    ap = tx_parser("pandacapture send", "Transmits frames, after a warning you have to acknowledge.")
    ap.add_argument("frames", nargs="+", metavar="FRAME", help="candump notation: 7DF#02010C, 18DB33F1#0201")
    ap.add_argument("--bus", type=int, default=0, choices=range(p.CAN_BUSES), help="bus to send on (default 0)")
    ap.add_argument("--count", type=int, default=1, help="send the frames this many times (default 1)")
    ap.add_argument("--interval-ms", type=float, default=100, help="between repeats (default 100)")
    args = ap.parse_args(argv)
    try:
        frames = [p.parse_frame_text(t, args.bus) for t in args.frames]
    except p.PacketError as e:
        print(f"ERROR: {e}")
        return 2
    return run_tx(args, [args.bus], lambda armed, log: _send(armed, log, frames, args),
                  f"{len(frames)} frame(s) x {args.count} on bus {args.bus}: " + " ".join(args.frames))


def _send(armed, log, frames, args):
    for i in range(args.count):
        armed.send(frames)
        for f in frames:
            log.sent(f)
        if i + 1 < args.count:
            armed.wait(args.interval_ms / 1000)
    armed.wait(0.2)


def cmd_replay(argv) -> int:
    ap = tx_parser("pandacapture replay", "Transmits the frames of a candump log with their original timing, "
                   "after a warning you have to acknowledge.")
    ap.add_argument("log", help="candump log, e.g. a PandaCapture capture")
    ap.add_argument("--bus-map", action="append", metavar="FROM:TO",
                    help="send the log's bus FROM on panda bus TO (repeatable); default each bus to itself")
    ap.add_argument("--ids", help="only these ids, comma separated hex (316,329)")
    ap.add_argument("--speed", type=float, default=1.0, help="playback speed (default 1.0)")
    args = ap.parse_args(argv)
    try:
        bus_map = {int(a): int(b) for a, b in (m.split(":") for m in args.bus_map)} if args.bus_map else None
        ids = {int(x, 16) for x in args.ids.split(",")} if args.ids else None
        frames = read_replay(args.log, bus_map, ids)
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}")
        return 2
    if not frames:
        print("ERROR: no frames to send in that log (check --bus-map and --ids).")
        return 2
    buses = sorted({f.bus for _, f in frames})
    detail = (f"{len(frames)} frames from {args.log} over {frames[-1][0] / args.speed:.1f} s on bus "
              f"{', '.join(map(str, buses))}, {len({f.addr for _, f in frames})} ids")
    return run_tx(args, buses, lambda armed, log: _replay(armed, log, frames, args.speed), detail)


def _replay(armed, log, frames, speed):
    start = time.monotonic()
    i = 0
    while i < len(frames):
        due = start + frames[i][0] / speed
        if time.monotonic() < due:
            armed.wait(due - time.monotonic())
        # Everything due by now goes in one batch
        now = time.monotonic()
        batch = []
        while i < len(frames) and start + frames[i][0] / speed <= now:
            batch.append(frames[i][1])
            i += 1
        armed.send(batch)
        for f in batch:
            log.sent(f)
    armed.wait(0.2)


def run_tx(args, buses, body, details) -> int:
    try:
        pd, rates = prepare_tx(args, buses)
    except (TransmitRefused, UsbError, argparse.ArgumentTypeError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1
    log = None
    try:
        with pd:
            version = pd.version()
            if not p.is_pandacapture_version(version):
                raise TransmitRefused(f"The panda runs {version!r}, not PandaCapture firmware, so it can't "
                                      "transmit. Flash it with: pandacapture flash")
            rate_text = ", ".join(f"bus {b} at {rates[b]} kbit/s" for b in buses)
            acknowledge(args.i_accept_transmit_risk, f"About to send {details}\n({rate_text})")
            log = TxLog(args.out, f"{pd.serial}, {version}, {rate_text}")
            with ArmedPanda(pd) as armed:
                print("Transmit armed.")
                body(armed, log)
                log.note(f"{armed.returned} frames confirmed on the bus, {armed.rejected} refused by the panda")
                print(f"Done: {armed.returned} frames confirmed on the bus"
                      + (f", {armed.rejected} refused by the panda" if armed.rejected else ""))
                if armed.returned == 0:
                    print("  None confirmed: nothing acknowledged them. Check the wiring and the bit rate.")
            print(f"Disarmed. Sent frames are logged in {log.path}")
    except KeyboardInterrupt:
        print("\nStopped; disarmed.")
        return 130
    except (TransmitRefused, UsbError) as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        if log:
            log.close()
    return 0


def cmd_obd(argv) -> int:
    from .obd_run import run
    return run(argv)


def cmd_codes(argv) -> int:
    from .codes import run
    return run(argv)


def cmd_modules(argv) -> int:
    from .uds import modules_main
    return modules_main(argv)


def cmd_did(argv) -> int:
    from .uds import did_main
    return did_main(argv)


def cmd_analyze(argv) -> int:
    from .ai import main
    return main(argv)


def cmd_apikey(argv) -> int:
    from .ai import apikey_main
    return apikey_main(argv)


def cmd_bundle(argv) -> int:
    from .bundle import main
    return main(argv)


def cmd_maps(argv) -> int:
    argparse.ArgumentParser(prog="pandacapture maps", description=(
        "Address maps found in the maps folder next to the program (or the current folder from source).")).parse_args(argv)
    from .signals import MapError, builtin_maps, load_map, user_dir
    maps = builtin_maps()
    if not maps:
        print(f"No address maps yet. Put .json maps in {user_dir()}, or pass a file with --map.")
    for name in maps:
        try:
            m = load_map(name)
            print(f"  {name:<28} {m.name}: {len(m.signals)} signals" + (f", {m.bitrate} kbit/s" if m.bitrate else ""))
        except MapError as e:
            print(f"  {name:<28} ERROR: {e}")
    print("Use one with: pandacapture dashboard --map NAME, or pass your own .json (see docs/address-maps.md).")
    return 0


def open_app_window(url) -> bool:
    """Opens [url] in a Chromium-based browser's app mode: one window, no tabs or address bar, sized to
    the screen. Returns False if no such browser was found."""
    import os
    import shutil
    import subprocess
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                               os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    else:
        candidates += [shutil.which(n) for n in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")]
    for exe in candidates:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--start-maximized"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return False


def cmd_dashboard(argv) -> int:
    from .dashboard import Dashboard
    from .signals import MapError, builtin_maps, load_map
    from .sources import ReplaySource

    ap = argparse.ArgumentParser(prog="pandacapture dashboard", description=(
        "Live gauges, numbers and status lights in your browser, decoded from the panda's traffic (listen-only) "
        "with an address map. Normal mode updates 10 times a second; high resolution streams every sample."))
    add_common(ap)
    ap.add_argument("--map", help="a map's name from your maps folder (see: pandacapture maps) or a .json file")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--simulate", action="store_true", help="fake traffic, no panda needed")
    src.add_argument("--replay", metavar="LOG", help="play back a candump log instead of reading the panda")
    src.add_argument("--adapter", metavar="NAME", help=(
        "read an RP1210 or J2534 adapter instead of a panda (Windows; see: pandacapture list). It acknowledges "
        "frames like any CAN node"))
    ap.add_argument("--speed", type=float, default=1.0, help="replay speed (default 1.0)")
    ap.add_argument("--no-loop", action="store_true", help="stop the replay at the end instead of looping")
    ap.add_argument("--bitrate", action="append", metavar="RATE",
                    help="kbit/s for every bus or per bus (0=500); default: the map's bit rate, else detected")
    ap.add_argument("--record", action="store_true", help="also record every frame to a candump log")
    ap.add_argument("--out", help="folder for recordings (default: captures next to the program)")
    ap.add_argument("--split-mb", type=float, default=100,
                    help="start a new capture file past this size (default 100, about 15 minutes; 0 = never)")
    ap.add_argument("--port", type=int, default=8765, help="web server port (default 8765)")
    ap.add_argument("--lan", action="store_true",
                    help="serve to other devices on this network too (e.g. a phone), not only this computer")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ap.add_argument("--mode", choices=("normal", "high"), help="the page's starting update rate (default: as last used)")
    ap.add_argument("--app", action="store_true",
                    help="open in a borderless app window (Edge or Chrome) instead of a browser tab")
    args = ap.parse_args(argv)

    maps = builtin_maps()
    name = args.map or (next(iter(maps)) if len(maps) == 1 else None)
    if name is None:
        print("ERROR: choose an address map with --map: a .json file, or a name from your maps folder "
              "(see: pandacapture maps, and docs/address-maps.md)")
        return 2
    try:
        address_map = load_map(name)
    except MapError as e:
        print(f"ERROR: {e}")
        return 2
    try:
        rates = parse_rates(args.bitrate)
    except (argparse.ArgumentTypeError, ValueError) as e:
        print(f"ERROR: {e}")
        return 2
    if not rates and address_map.bitrate:
        rates = {b: address_map.bitrate for b in range(p.CAN_BUSES)}

    if args.simulate:
        def open_source():
            return SimulatedSource()
    elif args.replay:
        def open_source():
            return ReplaySource(args.replay, speed=args.speed, loop=not args.no_loop)
    elif args.adapter:
        open_source, error = adapter_opener(args.adapter, rates)
        if error:
            print(f"ERROR: {error}")
            return 2
    else:
        setup = BusSetup(mode="silent", rates=rates)
        serial = [args.serial]

        def open_source():
            src = PandaSource(serial[0], setup, log=lambda s: None)
            serial[0] = src.serial
            setup.rates = dict(src.rates)
            return src

    try:
        dash = Dashboard(open_source, address_map, host="0.0.0.0" if args.lan else "127.0.0.1", port=args.port,
                         record=args.record, record_dir=args.out, split_mb=args.split_mb)
    except OSError as e:
        print(f"ERROR: can't serve on port {args.port}: {e}. Try --port with another number.")
        return 1
    dash.start()
    print(f"Dashboard for {address_map.name}: {dash.url}")
    if args.lan:
        print("  Serving to this network too: open http://<this computer's address>:%d/ on the other device." % args.port)
    print("  The adapter acknowledges frames; nothing is sent. Press Q or Ctrl+C to stop." if args.adapter
          else "  Listen-only. Press Q or Ctrl+C to stop.")
    if not args.no_browser:
        url = dash.url + (f"?mode={args.mode}" if args.mode else "")
        if not (args.app and open_app_window(url)):
            import webbrowser
            webbrowser.open(url)
    try:
        with KeyReader() as keys:
            while True:
                if any(k.lower() == "q" or k == "\x1b" for k in keys.poll()):
                    break
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        dash.stop()
        print("\nStopped.")
        for path in dash.reader.saved:
            print(f"  Recorded {path}")
    return 0


def cmd_match(argv) -> int:
    from . import match
    from .signals import MapError, builtin_maps, load_map

    ap = argparse.ArgumentParser(prog="pandacapture match", description=(
        "Finds which CAN fields carry which values: lines a capture up with a reference recorded at the same "
        "time and ranks every candidate field against each reference column. The reference is a CSV from "
        "another tool (e.g. a JB4 log), or with --obd, the ECU's OBD answers inside the capture (when a JB4 or "
        "scan tool was polling during the capture)."))
    ap.add_argument("capture", help="the PandaCapture candump log")
    ap.add_argument("reference", nargs="?", help="CSV recorded at the same time (e.g. a JB4 log)")
    ap.add_argument("--obd", action="store_true", help="use the OBD answers in the capture as the reference")
    ap.add_argument("--map", help="address map with an RPM signal for lining the logs up, and to label known fields")
    ap.add_argument("--rpm-key", default="rpm", help="the map's RPM signal (default rpm)")
    ap.add_argument("--ref-rpm", default="RPM", help="the reference's RPM column (default RPM)")
    ap.add_argument("--column", action="append", help="match only this reference column (repeatable)")
    ap.add_argument("--top", type=int, default=3, help="fields to show per column (default 3)")
    ap.add_argument("--min-r", type=float, default=0.9, help="weakest correlation to show (default 0.9)")
    ap.add_argument("--bus", type=int, default=0, choices=range(p.CAN_BUSES), help="bus to search (default 0)")
    args = ap.parse_args(argv)
    if bool(args.reference) == args.obd:
        print("ERROR: give a reference CSV, or --obd to use the capture's own OBD answers")
        return 2
    maps = builtin_maps()
    name = args.map or (next(iter(maps)) if len(maps) == 1 else None)
    if name is None:
        print("ERROR: choose an address map with --map (it supplies the RPM signal for lining the logs up)")
        return 2
    try:
        address_map = load_map(name)
        results, _ = match.run(args.capture, None if args.obd else args.reference, address_map,
                               ref_rpm=args.ref_rpm, rpm_key=args.rpm_key, columns=args.column, top=args.top,
                               bus=args.bus, min_r=args.min_r)
    except (MapError, match.MatchError, OSError) as e:
        print(f"ERROR: {e}")
        return 1
    match.report(results)
    print("\nr: correlation with the reference column. changes: correlation of row-to-row changes (low = maybe "
          "just a shared drift). RPM held: partial correlation with RPM fixed (low = maybe both just follow RPM). "
          "A match is evidence, not proof: add it to a map as \"observed\" until checked.")
    return 0


def cmd_selftest(argv) -> int:
    from .selftest import run
    return run()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    command = argv.pop(0) if argv and argv[0] in COMMANDS else "capture"
    try:
        return {"capture": cmd_capture, "list": cmd_list, "info": cmd_info, "dashboard": cmd_dashboard, "maps": cmd_maps, "match": cmd_match, "flash": cmd_flash, "backup": cmd_backup, "restore": cmd_restore,
                "send": cmd_send, "replay": cmd_replay, "selftest": cmd_selftest, "obd": cmd_obd, "codes": cmd_codes, "modules": cmd_modules, "did": cmd_did, "analyze": cmd_analyze, "apikey": cmd_apikey, "bundle": cmd_bundle}[command](argv)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
