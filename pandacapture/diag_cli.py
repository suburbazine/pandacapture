"""pandacapture diag: an interactive engine-off diagnostic session with one or more modules. See diagsession.py."""

import argparse

from .diagsession import WARNING, EngineOffSession, SessionEnded
from .policy import ENGINE_OFF_WORD, EngineNotOff, PolicyRefused

HELP = """\
Commands (MODULE defaults to the first --module; add @7D1 to aim one command at another):
  read DID                         read a data identifier (22)
  session extended | default       10 03 / 10 01
  dtc-setting off | on             85 02 / 85 01
  comm disable | enable            28 03 03 / 28 00 03 (normal and network messages)
  reset hard | keyoff | soft       11 01 / 11 02 / 11 03
  io DID return | default | freeze 2F DID 00 / 01 / 02
  io DID adjust HEX                2F DID 03 HEX
  routine start | stop | result RID [HEX]   31 01 / 02 / 03 RID [options]
  status                           what's active, and the vehicle's state
  end                              undo everything and leave (also Ctrl+C)
"""


def parse(line, default_target):
    """(target, sid, params) for a command line, or raises ValueError."""
    words = line.split()
    target = default_target
    if words and words[-1].startswith("@"):
        target = int(words.pop()[1:], 16)
    if not words:
        raise ValueError("empty")
    cmd, args = words[0].lower(), words[1:]

    def hx(text, n=None):
        b = bytes.fromhex(text)
        if n is not None and len(b) != n:
            raise ValueError(f"{text}: expected {n} bytes")
        return b

    if cmd == "read" and len(args) == 1:
        return target, 0x22, hx(args[0], 2)
    if cmd == "session" and args in (["extended"], ["default"]):
        return target, 0x10, b"\x03" if args[0] == "extended" else b"\x01"
    if cmd == "dtc-setting" and args in (["off"], ["on"]):
        return target, 0x85, b"\x02" if args[0] == "off" else b"\x01"
    if cmd == "comm" and args in (["disable"], ["enable"]):
        return target, 0x28, b"\x03\x03" if args[0] == "disable" else b"\x00\x03"
    if cmd == "reset" and len(args) == 1 and args[0] in ("hard", "keyoff", "soft"):
        return target, 0x11, {"hard": b"\x01", "keyoff": b"\x02", "soft": b"\x03"}[args[0]]
    if cmd == "io" and len(args) >= 2:
        did = hx(args[0], 2)
        kind = args[1].lower()
        if kind in ("return", "default", "freeze") and len(args) == 2:
            return target, 0x2F, did + {"return": b"\x00", "default": b"\x01", "freeze": b"\x02"}[kind]
        if kind == "adjust" and len(args) == 3:
            return target, 0x2F, did + b"\x03" + hx(args[2])
    if cmd == "routine" and len(args) >= 2 and args[0] in ("start", "stop", "result"):
        sub = {"start": b"\x01", "stop": b"\x02", "result": b"\x03"}[args[0]]
        return target, 0x31, sub + hx(args[1], 2) + (hx(args[2]) if len(args) > 2 else b"")
    raise ValueError(f"not a command: {line} (help lists them)")


def main(argv) -> int:
    from .diag_run import add_connection, run_client
    from .uds import module_ids
    ap = argparse.ArgumentParser(prog="pandacapture diag", description=(
        "An interactive engine-off diagnostic session with a module: extended session, code setting, communication, "
        "resets, actuator tests and routines. Only with the engine off and the car stopped, held for the whole session, "
        "and everything undone if that changes. Type help in the session."))
    add_connection(ap)
    ap.add_argument("--module", action="append", required=True, metavar="ID",
                    help="the module's request id (hex, e.g. 7E0; see pandacapture modules)")
    args = ap.parse_args(argv)
    try:
        targets = module_ids(args.module)
    except PolicyRefused as e:
        print(f"ERROR: {e}")
        return 2

    def body(client, buses, out_dir, stamp):
        session = EngineOffSession(client, buses[0], note=getattr(client, "note", lambda text: None)).start()
        confirmed = False
        print(HELP)
        try:
            while True:
                try:
                    line = input(f"diag {targets[0]:03X}> ").strip()
                except EOFError:
                    break
                if not line:
                    continue
                if line in ("end", "quit", "exit"):
                    break
                if line == "help":
                    print(HELP)
                    continue
                if line == "status":
                    st = session.status()
                    print(f"  vehicle: {st['vehicle']}")
                    print(f"  extended session: {', '.join(f'{t:03X}' for t in st['extended']) or 'none'}")
                    for target, started, undo in st["effects"]:
                        print(f"  active on {target:03X}: {started} (undone by {undo})")
                    if st["ended"]:
                        print(f"  ended: {st['ended']}")
                    continue
                try:
                    target, sid, params = parse(line, targets[0])
                    from .policy import Tier, build
                    req = build(sid, params, target)
                    if req.tier is Tier.ENGINE_OFF and req.confirm and not confirmed:
                        print(WARNING)
                        if input(f"Type {ENGINE_OFF_WORD} to go on, anything else to stop: ").strip() != ENGINE_OFF_WORD:
                            print("  Not sent.")
                            continue
                        session.confirm()
                        confirmed = True
                    out = session.request(target, sid, params)
                    print(f"  {out.text}" + (f": {out.data.hex(' ').upper()}" if out.ok and out.data else ""))
                except (ValueError, PolicyRefused, EngineNotOff) as e:
                    print(f"  {e}")
                except SessionEnded as e:
                    print(f"  {e}")
                    break
        except KeyboardInterrupt:
            print()
        finally:
            session.close("you ended the session")

    return run_client(args, "engine-off diagnostic session", WARNING,
                      f"open a diagnostic session with {', '.join(f'{t:03X}' for t in targets)} (reads now; "
                      f"anything that changes something asks for {ENGINE_OFF_WORD} first)", body)
