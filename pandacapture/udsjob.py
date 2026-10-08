"""The dashboard's Modules page: find every module that answers diagnostic requests, what each is (part, hardware
and software numbers) and its own trouble codes; and read data identifiers from one module. The same steps as
PandaCapture Android's Modules card, through desktop's diagnostic policy.

Reads only: 03 22 HI LO, 03 19 02 FF, the flow control 30 00 00 for long answers, and 02 01 00 / 02 01 0C to 7DF,
each built and checked by the policy (policy.Sender), and never to an id (or its answer id) that the bus uses for
its own messages. Through the panda the dashboard is reading (PandaCapture firmware's armed transmit gate), so the
gauges keep running and a recording keeps every frame; the Read switches pause meanwhile. The sweep runs key-on or
at idle only (VehicleState.check_scan: 900 rpm); identifier reads run at any engine speed. TRANSMIT is typed once a
session, on the computer running PandaCapture (dashboard.py).

Each run saves obd-modules-<stamp>.json or obd-did-<stamp>.json, and a .log of every diagnostic frame sent and
heard while it ran, in the captures folder.
"""

import datetime as dt
import json
import queue
import threading
import time
from pathlib import Path

from .capture import candump_line
from .diag import Client, nrc_text
from .policy import FUNCTIONAL, StateRefused, VehicleState, is_physical
from .uds import IDENTIFIERS, PROBE_DID, did_text, free, module_name, parse_dtc_report, status_text

LISTEN_S = 1.0     # heard before anything is sent: which ids carry the bus's own messages
MAX_DIDS = 32
LOG_HEADER = ("# PandaCapture UDS exchange (recorded by the dashboard): every diagnostic frame sent and heard "
              "while it ran")


def ensure_rpm(client, bus, ahead):
    """A fresh engine speed before a sweep probe: the broadcast if it's there, else one OBD request for it."""
    if not client.state.fresh(ahead=ahead):
        client.request(0x01, b"\x0c", bus, FUNCTIONAL)


def scan_bus(client, bus, say, probe_wait, max_rpm):
    """OBD modules (01 00 to 7DF), then every other free id 700-7F7 asked for its part number (the sweep,
    key-on or idle only), then each module's identification and its own codes."""
    targets = sorted(m - 8 for m in client.request(0x01, b"\x00", bus).positive if is_physical(m - 8) and free(client, bus, m - 8))
    candidates = [i for i in range(0x700, 0x7F8) if free(client, bus, i) and i not in targets]
    for n, target in enumerate(candidates, 1):
        ensure_rpm(client, bus, probe_wait * 2)
        client.state.check_scan()
        max_rpm[0] = max(max_rpm[0] or 0, client.state.rpm or 0)
        say(f"Bus {bus}: asking {target:03X} for its part number ({n} of {len(candidates)})…")
        a = client.request(0x22, PROBE_DID.to_bytes(2, "big"), bus, target, expect=(target + 8,), wait=probe_wait)
        if a.modules():
            targets.append(target)
        client.state.check_scan()
    out = []
    for target in sorted(set(targets)):
        if not free(client, bus, target):
            continue
        say(f"Bus {bus}: reading what {target:03X} is…")
        info = {}
        for did, name in IDENTIFIERS.items():
            a = client.request(0x22, did.to_bytes(2, "big"), bus, target, expect=(target + 8,))
            payload = a.positive.get(target + 8)
            if payload is not None and payload[1:3] == did.to_bytes(2, "big"):
                info[name] = did_text(payload[3:])
        say(f"Bus {bus}: reading {target:03X}'s codes…")
        m = {"bus": bus, "request_id": f"{target:03X}", "answer_id": f"{target + 8:03X}",
             "name": module_name(target), "info": info}
        a = client.request(0x19, b"\x02\xff", bus, target, expect=(target + 8,))
        if target + 8 in a.positive:
            m["codes"] = [{"code": code, "status": status_text(st)} for code, st in parse_dtc_report(a.positive[target + 8])]
        elif target + 8 in a.negative:
            m["codes_refused"] = nrc_text(a.negative[target + 8])
        else:
            m["codes_refused"] = a.broken.get(target + 8, "no answer")
        out.append(m)
    return out


def scan_modules(client, buses, say=lambda text: None, probe_wait=None) -> dict:
    """The whole scan: {"swept", "modules", "requests", "max_rpm", "stopped"?}. A sweep stopped by the engine
    speed keeps what it found before. probe_wait: how long each sweep probe waits (uds.PROBE_WAIT, 80 ms)."""
    from . import uds
    probe_wait = uds.PROBE_WAIT if probe_wait is None else probe_wait
    result, max_rpm = {"swept": True, "modules": []}, [None]
    try:
        for bus in buses:
            result["modules"] += scan_bus(client, bus, say, probe_wait, max_rpm)
    except StateRefused as e:
        result["stopped"] = str(e).replace("an OBD scan only runs", "the scan only runs")
    result["requests"] = client.sender.sent
    result["max_rpm"] = max_rpm[0]
    return result


def parse_did_request(module_text, dids_text):
    """(module request id, [identifiers]) from the page's two fields, or ValueError with what's wrong."""
    try:
        target = int((module_text or "").strip(), 16)
    except ValueError:
        raise ValueError("Give the module's request id in hex, e.g. 7E0") from None
    if not is_physical(target):
        raise ValueError("Request ids are 700-7F7 (not 7D7, 7DF or 7E8-7EF)")
    parts = [x for x in (dids_text or "").replace(",", " ").split() if x]
    if not parts or any(len(x) != 4 for x in parts):
        raise ValueError("Identifiers are 4 hex digits each, e.g. E019 F187")
    try:
        dids = [int(x, 16) for x in parts]
    except ValueError:
        raise ValueError("Identifiers are 4 hex digits each, e.g. E019 F187") from None
    if len(dids) > MAX_DIDS:
        raise ValueError(f"Up to {MAX_DIDS} identifiers at a time")
    return target, dids


def read_dids(client, bus, target, dids, say=lambda text: None) -> dict:
    reads = []
    for did in dids:
        say(f"Bus {bus}: reading {did:04X} from {target:03X}…")
        a = client.request(0x22, did.to_bytes(2, "big"), bus, target, expect=(target + 8,))
        payload, why = a.positive.get(target + 8), None
        if payload is not None and payload[1:3] != did.to_bytes(2, "big"):
            payload, why = None, "answered a different identifier"
        elif payload is None:
            why = nrc_text(a.negative[target + 8]) if target + 8 in a.negative else a.broken.get(target + 8, "no answer")
        reads.append({"time": round(time.time(), 3), "did": f"{did:04X}",
                      "hex": payload[3:].hex() if payload is not None else None, "refused": why,
                      "text": did_text(payload[3:]) if payload is not None else None})
    return {"module": f"{target:03X}", "bus": bus, "reads": reads, "requests": client.sender.sent,
            "max_rpm": client.state.rpm}


# ---------------------------------------------------------------- through the dashboard's panda

class ReaderLink:
    """A link for diag.Client over the panda the dashboard's reader owns: requests go out through it (armed), and
    the frames the reader reads are handed to the client while it waits. Every diagnostic frame sent and heard is
    written to the job's .log."""

    def __init__(self, source, log):
        self.source, self.log = source, log
        self.inbox = queue.Queue()
        self.on_frame = None

    def feed(self, frames):
        """From the reader's thread: what it just read."""
        self.inbox.put((frames, time.time()))

    def _logged(self, f, t):
        if not f.extended and 0x700 <= f.addr <= 0x7FF:
            self.log.write(candump_line(t, f) + "\n")

    def send(self, frames):
        self.source.send(frames)
        for f in frames:
            self._logged(f, time.time())

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while True:
            self.source.beat()
            try:
                frames, t = self.inbox.get(timeout=max(0.0, min(0.005, end - time.monotonic())))
            except queue.Empty:
                frames = None
            for f in frames or ():
                if f.returned or f.rejected:
                    continue
                self._logged(f, t)
                if self.on_frame:
                    self.on_frame(f)
            if time.monotonic() >= end and self.inbox.empty():
                return


def latest_modules(folder):
    """The last module scan saved, for the page on load (results survive a restart)."""
    files = sorted(Path(folder).glob("obd-modules-*.json"))
    for f in reversed(files):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            return dict(d, file=f.name)
        except (OSError, ValueError):
            continue
    return None


class UdsJob:
    """One module scan or identifier read at a time, in the background, through the dashboard's panda."""

    def __init__(self, reader, state):
        self.reader, self.state = reader, state
        self.lock = threading.Lock()
        self.confirmed = False
        self.job = {"state": "idle"}
        self.last_bus = {}      # module request id -> the bus the last scan found it on

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.job))

    def confirm(self, word):
        if (word or "").strip().upper() != "TRANSMIT":
            raise ValueError("Type TRANSMIT to let the dashboard send diagnostic requests.")
        self.confirmed = True

    def start(self, kind, module=None, dids=None, confirm=None):
        if confirm:
            self.confirm(confirm)
        if not self.confirmed:
            raise ValueError("Type TRANSMIT first: these send requests on the vehicle's bus.")
        target = did_list = None
        if kind == "did":
            target, did_list = parse_did_request(module, dids)
        elif kind != "scan":
            raise ValueError(f"unknown request {kind!r}")
        source = self.reader.source
        if not getattr(source, "can_transmit", False):
            raise ValueError("Sending needs a panda with PandaCapture firmware (see Firmware)." if hasattr(source, "arm")
                             else "There's no panda to send through (the simulator or a replay is running).")
        if self.state.replay is not None:
            raise ValueError("A replay is running: stop it first.")
        with self.lock:
            if self.job.get("state") == "running":
                raise ValueError("A module scan or read is already running.")
            self.job = {"state": "running", "kind": kind, "progress": "Listening to the bus first…"}
        threading.Thread(target=self._work, args=(kind, source, target, did_list), daemon=True,
                         name="pandacapture-uds").start()

    def _say(self, text):
        with self.lock:
            self.job["progress"] = text

    def _work(self, kind, source, target, dids):
        out_dir = self.reader.record_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        base = out_dir / f"obd-{'modules' if kind == 'scan' else 'did'}-{stamp}"
        armed = False
        with open(base.with_suffix(".log"), "w", encoding="utf-8", newline="\n") as log:
            log.write(LOG_HEADER + "\n")
            link = ReaderLink(source, log)
            try:
                self.reader.reads.pause("paused for the module scan" if kind == "scan" else "paused for an identifier read")
                end = time.monotonic() + 3
                while self.reader.reads.info()["running"] and time.monotonic() < end:
                    time.sleep(0.02)
                with self.state.lock:
                    amap = self.state.map
                    buses = sorted(b for b, n in self.state.bus_frames.items() if n)
                rpm = next((s for s in amap.signals if s.key == "rpm" and not s.derived), None)
                speed = next((s for s in amap.signals if s.key in ("speed_kmh", "speed") and not s.derived), None)
                client = Client(link, VehicleState(rpm, speed))
                link.on_frame = client.frame
                self.reader.uds_link = link
                source.arm()
                armed = True
                self.reader._note(f"active polling, not silent: transmit armed for the Modules page's "
                                  f"{'module scan' if kind == 'scan' else 'identifier read'} ({time.time():.6f})")
                link.wait(LISTEN_S)
                if kind == "scan":
                    result = scan_modules(client, buses or [0], self._say)
                    for m in result["modules"]:
                        self.last_bus[m["request_id"]] = m["bus"]
                    found = f"Found {len(result['modules'])} modules ({result['requests']} requests)"
                    summary = (f"{found}: stopped, {result['stopped']}" if result.get("stopped") else found)
                else:
                    bus = self.last_bus.get(f"{target:03X}", buses[0] if buses else 0)
                    result = read_dids(client, bus, target, dids, self._say)
                    summary = f"Read {sum(1 for r in result['reads'] if r['hex'] is not None)} of {len(dids)} identifiers from {target:03X}"
                base.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
                result["file"] = base.with_suffix(".json").name
                with self.lock:
                    self.job = {"state": "done", "kind": kind, "progress": summary, "result": result,
                                "log": base.with_suffix(".log").name}
            except Exception as e:  # noqa: BLE001 - refusals, a stopped sweep, USB: said on the page
                with self.lock:
                    self.job = {"state": "error", "kind": kind, "progress": str(e), "error": str(e)}
            finally:
                self.reader.uds_link = None
                if armed:
                    source.disarm()
                    self.reader._note(f"Modules page done: transmit disarmed, listening again ({time.time():.6f})")
                self.reader.reads.pause(None)
