"""UDS reads (ISO 14229), module by module: a module's own codes (19), and data by identifier (22),
including the standard identification ones. Reads only, through the diagnostic policy.

Commands: pandacapture modules (find modules and say what they are), pandacapture did (read data
identifiers), and pandacapture codes --uds (each module's own codes)."""

import argparse
import json
import time

from .codes import dtc, dtc_kind
from .diag import nrc_text
from .diag_run import add_connection, run_client
from .keys import KeyReader
from .policy import PolicyRefused, is_physical

PROBE_DID = 0xF187          # spare part number: asked of every id when finding modules
PROBE_WAIT = 0.08           # s: a module answers a request within 50 ms (UDS P2)

STATUS = [(0x08, "confirmed"), (0x04, "pending"), (0x01, "failing now"), (0x80, "warning light"),
          (0x02, "failed this drive"), (0x20, "failed since cleared")]

IDENTIFIERS = {
    0xF197: "System name",
    0xF187: "Part number",
    0xF18A: "Supplier",
    0xF191: "Hardware number",
    0xF193: "Hardware version",
    0xF195: "Software version",
    0xF18C: "Serial number",
    0xF18B: "Manufacturing date",
}
VIN_DID = 0xF190

WARNING = """\
This sends UDS read requests onto the vehicle's bus, one module at a time, the same ones a dealer tool
uses to read a module's codes and data. Nothing is cleared or changed.

- Modules answer every request: pause other diagnostic tools (a tuner's datalogger, a scan tool) meanwhile.
- Key on (engine running or not).
"""


# ---------------------------------------------------------------- decoding

def uds_dtc(b) -> str:
    """Three bytes: the code, then its failure type (what kind of fault), e.g. P0301-00."""
    return f"{dtc(b[0], b[1])}-{b[2]:02X}"


def parse_dtc_report(payload) -> list:
    """(code, status) from a 19 02 answer: 59 02 availability-mask, then code (3 bytes) + status."""
    recs = payload[3:]
    return [(uds_dtc(recs[i:i + 3]), recs[i + 3]) for i in range(0, len(recs) - 3, 4)]


def status_text(status) -> str:
    return ", ".join(name for bit, name in STATUS if status & bit) or "stored"


def did_text(data) -> str:
    """Text when it's text, else hex."""
    if data and sum(32 <= c < 127 for c in data.rstrip(b"\0 \xff")) >= 0.9 * len(data.rstrip(b"\0 \xff")) > 0:
        return bytes(data).rstrip(b"\0 \xff").decode("ascii", "replace").strip()
    return data.hex(" ").upper() if data else "(empty)"


def module_name(target) -> str:
    return {0x7E0: "engine", 0x7E1: "transmission"}.get(target, f"module {target:03X}")


# ---------------------------------------------------------------- reading

def read_did(client, bus, target, did):
    """(data, None) or (None, why not)."""
    a = client.request(0x22, did.to_bytes(2, "big"), bus, target, expect=(target + 8,))
    answer = a.positive.get(target + 8)
    if answer is not None:
        if answer[1:3] == did.to_bytes(2, "big"):
            return answer[3:], None
        return None, "answered a different identifier"
    if target + 8 in a.negative:
        return None, nrc_text(a.negative[target + 8])
    return None, a.broken.get(target + 8, "no answer")


def obd_modules(client, bus) -> list:
    """The request ids of the OBD modules (those that answer 01 00 on 7DF)."""
    return sorted(m - 8 for m in client.request(0x01, b"\x00", bus).positive)


def free(client, bus, target) -> bool:
    """A module id that may be asked: physical, and neither it nor its answer id carries other traffic."""
    traffic = client.state.traffic.get(bus, ())
    return is_physical(target) and target not in traffic and target + 8 not in traffic


def find_modules(client, bus, ids=range(0x700, 0x7F8), log=print) -> list:
    """Every id that answers a read request (positive or not): one request per free id."""
    found = []
    candidates = [i for i in ids if free(client, bus, i)]
    log(f"Bus {bus}: asking {len(candidates)} ids (700-7F7) for their part number...")
    for target in candidates:
        a = client.request(0x22, PROBE_DID.to_bytes(2, "big"), bus, target, expect=(target + 8,), wait=PROBE_WAIT)
        if a.modules():
            found.append(target)
    return found


def identify(client, bus, target, with_vin=False) -> dict:
    out = {}
    for did, name in list(IDENTIFIERS.items()) + ([(VIN_DID, "VIN")] if with_vin else []):
        data, why = read_did(client, bus, target, did)
        if data is not None:
            out[name] = did_text(data)
    return out


def read_module_codes(client, bus, target) -> dict:
    """{"codes": [(code, status)], "refused": why} from 19 02 (every status)."""
    a = client.request(0x19, b"\x02\xff", bus, target, expect=(target + 8,))
    module = target + 8
    if module in a.positive:
        return {"codes": parse_dtc_report(a.positive[module])}
    if module in a.negative:
        return {"refused": nrc_text(a.negative[module])}
    return {"refused": a.broken.get(module, "no answer")}


def codes_report(results) -> str:
    lines = []
    for (bus, target), r in sorted(results.items()):
        lines.append(f"Bus {bus}, {module_name(target)} ({target:03X}), its own codes (UDS):")
        if "refused" in r:
            lines.append(f"  not read: {r['refused']}")
        elif not r["codes"]:
            lines.append("  none")
        for code, status in r.get("codes", []):
            lines.append(f"  {code} ({dtc_kind(code[:5])}): {status_text(status)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- the commands

def module_ids(values) -> list:
    out = []
    for v in values or []:
        for part in v.replace(" ", "").split(","):
            try:
                target = int(part, 16)
            except ValueError:
                raise PolicyRefused(f"--module {part}: give the module's request id in hex, e.g. 7E0 or 7D1") from None
            if not is_physical(target):
                raise PolicyRefused(f"--module {part}: request ids are 700-7F7 (not 7D7, 7DF or 7E8-7EF)")
            out.append(target)
    return out


def modules_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="pandacapture modules", description=(
        "Lists the modules that answer diagnostic requests and what they are (system name, part, hardware and "
        "software numbers). By default the OBD modules; --find asks every id from 700 to 7F7. Reads only."))
    add_connection(ap)
    ap.add_argument("--find", action="store_true", help="ask every id from 700 to 7F7, not only the OBD modules")
    ap.add_argument("--module", action="append", metavar="ID", help="also these request ids (hex, e.g. 7D1)")
    ap.add_argument("--vin", action="store_true", help="also read each module's VIN")
    args = ap.parse_args(argv)
    try:
        extra = module_ids(args.module)
    except PolicyRefused as e:
        print(f"ERROR: {e}")
        return 2

    def body(client, buses, out_dir, stamp):
        found = {}
        for bus in buses:
            targets = set(obd_modules(client, bus)) | set(extra)
            if args.find:
                targets |= set(find_modules(client, bus))
            for target in sorted(targets):
                found[(bus, target)] = identify(client, bus, target, args.vin)
        print()
        for (bus, target), info in sorted(found.items()):
            print(f"Bus {bus}, {module_name(target)} ({target:03X}, answers on {target + 8:03X})")
            for name, value in info.items():
                print(f"  {name + ':':<20} {value}")
            if not info:
                print("  (answered, but gave none of the identification data)")
        if not found:
            print("No module answered.")
        path = out_dir / f"modules-{stamp}.json"
        path.write_text(json.dumps([{"bus": b, "request_id": f"{t:03X}", "answer_id": f"{t + 8:03X}", "info": info}
                                    for (b, t), info in sorted(found.items())], indent=2), encoding="utf-8")
        print(f"\nSaved: {path}")

    return run_client(args, "finding modules", WARNING, "ask modules what they are" +
                      (" (every id from 700 to 7F7)" if args.find else ""), body)


def did_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="pandacapture did", description=(
        "Reads data identifiers (UDS 22) from one module, e.g. pandacapture did --module 7E0 F190 E001. "
        "With --every, keeps reading them until Q. Reads only."))
    add_connection(ap)
    ap.add_argument("dids", nargs="+", metavar="DID", help="2-byte identifiers in hex, e.g. F187 E001")
    ap.add_argument("--module", required=True, metavar="ID", help="the module's request id (hex, e.g. 7E0)")
    ap.add_argument("--every", type=float, metavar="SECONDS", help="repeat until Q, this often")
    args = ap.parse_args(argv)
    try:
        target = module_ids([args.module])[0]
        dids = [int(d, 16) for d in args.dids]
        if any(not 0 <= d <= 0xFFFF for d in dids):
            raise ValueError
    except PolicyRefused as e:
        print(f"ERROR: {e}")
        return 2
    except ValueError:
        print("ERROR: identifiers are 4 hex digits, e.g. F187 E001")
        return 2

    def body(client, buses, out_dir, stamp):
        bus = buses[0]
        rows = []

        def once():
            for did in dids:
                data, why = read_did(client, bus, target, did)
                rows.append({"time": round(time.time(), 3), "did": f"{did:04X}",
                             "hex": data.hex() if data is not None else None, "refused": why})
                shown = f"{data.hex(' ').upper()}   {did_text(data)}" if data is not None else f"not read: {why}"
                print(f"  {did:04X}  {shown}")

        print(f"Bus {bus}, {module_name(target)} ({target:03X}):")
        once()
        if args.every:
            print(f"Every {args.every:g} s; Q to stop.")
            with KeyReader() as keys:
                while not any(k.lower() == "q" or k == "\x1b" for k in keys.poll()):
                    end = time.monotonic() + args.every
                    while time.monotonic() < end:
                        client.link.wait(0.02)   # a panda keeps getting its heartbeat meanwhile
                    once()
        path = out_dir / f"did-{stamp}.json"
        path.write_text(json.dumps({"module": f"{target:03X}", "bus": bus, "reads": rows}, indent=2), encoding="utf-8")
        print(f"\nSaved: {path}")

    return run_client(args, "reading data identifiers", WARNING,
                      f"read {', '.join(f'{d:04X}' for d in dids)} from module {target:03X}", body)

