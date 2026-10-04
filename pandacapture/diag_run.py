"""Running a diagnostic command: the panda or ELM327 session, the acknowledgement, the logs, and a Client
for the command's work. Used by pandacapture codes, modules and did."""

import datetime as dt
import time
from pathlib import Path

from . import protocol as p
from .capture import RollingLog, candump_line, default_out_dir
from .diag import Client
from .policy import PolicyRefused, VehicleState
from .transmit import TransmitRefused, TxLog, acknowledge
from .usbdev import UsbError


def add_connection(ap):
    ap.add_argument("--elm", metavar="PORT", help="use an ELM327: COM5, /dev/rfcomm0, socket://192.168.0.10:35000")
    ap.add_argument("--elm-protocol", default="0", metavar="N", help="ELM327 OBD protocol (0 = automatic)")
    ap.add_argument("--serial", help="which panda, when several are connected")
    ap.add_argument("--bus", type=int, action="append", choices=range(p.CAN_BUSES),
                    help="panda bus to use (repeatable); default: every bus with traffic")
    ap.add_argument("--bitrate", type=int, help="kbit/s for the panda's buses; default: detected by listening")
    ap.add_argument("--out", help="folder for the results (default: captures next to the program)")
    ap.add_argument("--i-accept-transmit-risk", action="store_true",
                    help="skip typing TRANSMIT (the warning is still shown); for scripts")


def run_client(args, title, warning, details, body) -> int:
    """Opens the connection, asks for the acknowledgement, and runs body(client, buses, out_dir, stamp)
    with every request and answer logged. body returns nothing; its errors stop the command cleanly."""
    from .elm import ElmError
    from .obd_run import ElmSession, PandaSession, state_signals
    args.map, args.rpm_key = getattr(args, "map", None), getattr(args, "rpm_key", "rpm")
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
        acknowledge(args.i_accept_transmit_risk, f"About to {details} through {session.kind}: {session.rate_text}.",
                    warning=warning)
        log = TxLog(out_dir, f"{session.kind}: {session.description}")
        rec = RollingLog(out_dir, [f"# PandaCapture candump log ({title})", *session.header], stamp=stamp)
        client = None

        def received(f):
            rec.write(candump_line(time.time(), f) + "\n")
            client.frame(f)

        def sent(f):
            log.sent(f)
            rec.write(candump_line(time.time(), f) + "\n")

        with session.link(received) as link:
            client = Client(link, state, on_sent=sent)
            client.note = log.note   # e.g. the vehicle's state when codes are cleared
            body(client, session.buses, out_dir, stamp)
        print(f"  Requests logged in {log.path}; everything received in {rec.path}")
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except (TransmitRefused, UsbError, ElmError, PolicyRefused) as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        for f in (log, rec):
            if f:
                f.close()
        session.close()
    return 0
