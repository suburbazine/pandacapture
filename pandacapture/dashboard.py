"""Live dashboard: decodes CAN traffic with an address map and shows it as gauges, numbers and
status lights in a browser, with controls for recording, markers, maps and finding signals.

One thread reads frames from the source (a panda listening silently, a candump replay, or the
simulator) and keeps the latest value of every signal in the map. A small web server on this
computer serves the page (pandacapture/web/dashboard.html) and streams the values to it as
Server-Sent Events.

Two stream modes, chosen by each page:
  normal  /events            the latest value of every signal, 10 times a second
  high    /events?mode=high  every decoded sample with its receive time, sent as soon as it arrives

Controls (POST, JSON bodies):
  /record  {"on": true|false}      start or stop recording a candump capture
  /marker  {}                      numbered marker into the recording
  /map     {"name": "..."}         switch address map
  /match?capture=NAME&ref=obd|csv  find signals in a capture (CSV reference in the body)
and GET /maps, /captures, /match (the running or last match: progress and results).
"""

import collections
import datetime as dt
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .capture import RollingLog, candump_line, default_out_dir
from .firmware import bundled_dir
from .signals import Evaluator, MapError, builtin_maps, load_map
from .sources import SourceError

STALE_SECONDS = 1.5
PUSH_EVERY = 0.1        # normal mode
STATUS_EVERY = 0.5      # high-resolution mode: status alongside the samples
SAMPLE_BUFFER = 50000   # samples kept for high-resolution clients that fall behind
RECONNECT_EVERY = 2.0
MAX_UPLOAD = 20 * 1024 * 1024


def web_dir() -> Path:
    return bundled_dir().parent / "web"


class LiveState:
    """Latest decoded values and source health, shared between the reader and the web clients."""

    def __init__(self, address_map):
        self.lock = threading.Lock()
        self.new_samples = threading.Condition(self.lock)
        self.frames = 0
        self.bus_frames = {}
        self.status = "starting"
        self.error = ""
        self.source = ""
        self.recording = ""
        self.recording_since = None
        self.markers = 0
        self.map_version = 0
        self._rate_mark = (time.monotonic(), 0)
        self.fps = 0.0
        self.seq = 0
        self.samples = collections.deque(maxlen=SAMPLE_BUFFER)  # (sequence, key, value, unix time)
        self._load(address_map)

    def _load(self, address_map):
        self.map = address_map
        self.values = {}       # key -> (value, monotonic time)
        self.current = {}      # key -> value, for derived signals
        self.evaluator = Evaluator(address_map)

    def set_map(self, address_map):
        """Switches to another map; pages notice map_version change and reload."""
        with self.lock:
            self._load(address_map)
            self.map_version += 1
            self.samples.clear()

    def feed(self, frames, now, unix=None):
        unix = time.time() if unix is None else unix
        with self.lock:
            added = False
            for f in frames:
                if f.returned or f.rejected:
                    continue
                self.frames += 1
                self.bus_frames[f.bus] = self.bus_frames.get(f.bus, 0) + 1
                changed = set()
                for s in self.map.by_id.get(f.addr, ()):
                    if s.bus is not None and s.bus != f.bus:
                        continue
                    v = s.decode(f.data)
                    if v is not None:
                        self._sample(s.key, v, now, unix)
                        self.current[s.key] = v
                        changed.add(s.key)
                if changed and self.map.derived:
                    for key, v in self.evaluator.update(self.current, changed, now).items():
                        self._sample(key, v, now, unix)
                added = added or bool(changed)
            if added:
                self.new_samples.notify_all()

    def _sample(self, key, v, now, unix):
        self.values[key] = (v, now)
        self.seq += 1
        self.samples.append((self.seq, key, v, unix))

    def samples_after(self, seq, timeout):
        """Samples newer than [seq], waiting up to [timeout] for some. Returns (samples, last seq,
        how many were lost because this client fell behind)."""
        with self.lock:
            if self.seq <= seq:
                self.new_samples.wait(timeout)
            if self.seq <= seq:
                return [], seq, 0
            oldest = self.samples[0][0] if self.samples else self.seq + 1
            lost = max(0, oldest - seq - 1)
            out = [(k, round(v, 4), round(t, 4)) for n, k, v, t in self.samples if n > seq]
            return out, self.seq, lost

    def info(self) -> dict:
        now = time.monotonic()
        with self.lock:
            t0, n0 = self._rate_mark
            if now - t0 >= 1.0:
                self.fps = (self.frames - n0) / (now - t0)
                self._rate_mark = (now, self.frames)
            return {"frames": self.frames, "fps": round(self.fps), "buses": dict(self.bus_frames),
                    "status": self.status, "error": self.error, "source": self.source,
                    "recording": self.recording,
                    "recording_for": round(now - self.recording_since) if self.recording_since else None,
                    "markers": self.markers, "map_version": self.map_version, "map_name": self.map.name}

    def snapshot(self) -> dict:
        status = self.info()
        now = time.monotonic()
        with self.lock:
            out = {}
            for s in self.map.signals:
                if s.key not in self.values:
                    continue
                v, t = self.values[s.key]
                entry = {"v": round(v, 4), "age": round(now - t, 2), "stale": now - t > STALE_SECONDS}
                if s.display == "light":
                    entry["on"] = s.light_on(v)
                else:
                    entry["level"] = s.level(v)
                text = s.text(v)
                if text is not None:
                    entry["text"] = text
                out[s.key] = entry
            return dict(status, values=out)


class Recorder:
    """candump log of everything received while recording is on, in parts of split_mb."""

    def __init__(self, out_dir, description, split_mb=100.0):
        self.log = RollingLog(out_dir, ["# PandaCapture candump log (recorded by the dashboard)",
                                        f"# source: {description}"], split_mb)
        self._last_flush = time.monotonic()

    @property
    def path(self):
        return self.log.path

    @property
    def paths(self):
        return self.log.paths

    def write(self, frames, t):
        for f in frames:
            if not (f.returned or f.rejected):
                self.log.write(candump_line(t, f) + "\n")
        if time.monotonic() - self._last_flush > 1.0:
            self.log.flush()
            self._last_flush = time.monotonic()
        return self.log.maybe_rotate()

    def note(self, text):
        self.log.write(f"# {text}\n")
        self.log.flush()

    def close(self):
        self.log.close()


class Reader(threading.Thread):
    """Reads the source into the LiveState (reopening it when it fails), and records while asked."""

    def __init__(self, open_source, state: LiveState, record_dir=None, record=False, split_mb=100.0):
        super().__init__(daemon=True, name="pandacapture-reader")
        self.split_mb = split_mb
        self.open_source = open_source
        self.state = state
        self.stop_event = threading.Event()
        self.record_dir = Path(record_dir) if record_dir else default_out_dir()
        self.recorder = None
        self.rec_lock = threading.Lock()
        self.description = "waiting for the source"
        self.saved = []   # captures recorded this session
        if record:
            self.start_recording()

    # ---- recording controls (called from web threads) ----

    def start_recording(self):
        with self.rec_lock:
            if self.recorder is None:
                self.recorder = Recorder(self.record_dir, self.description, self.split_mb)
                with self.state.lock:
                    self.state.recording = str(self.recorder.path)
                    self.state.recording_since = time.monotonic()
                    self.state.markers = 0
        return self.recorder.path

    def stop_recording(self):
        with self.rec_lock:
            rec, self.recorder = self.recorder, None
            if rec:
                rec.close()
                self.saved.extend(rec.paths)
            with self.state.lock:
                self.state.recording = ""
                self.state.recording_since = None
        return rec.path if rec else None

    def marker(self):
        with self.rec_lock:
            if self.recorder is None:
                return None
            with self.state.lock:
                self.state.markers += 1
                n = self.state.markers
            self.recorder.note(f"marker {n} ({time.time():.6f})")
            return n

    # ---- reading ----

    def run(self):
        source = None
        while not self.stop_event.is_set():
            if source is None:
                try:
                    source = self.open_source()
                except SourceError as e:
                    self._status("disconnected", f"{e} (retrying)")
                    self.stop_event.wait(RECONNECT_EVERY)
                    continue
                self.description = source.description
                with self.state.lock:
                    self.state.source = source.description
                with self.rec_lock:
                    if self.recorder:
                        self.recorder.note(f"source: {source.description}")
                self._status("live", "")
            try:
                frames = source.read()
            except SourceError as e:
                with self.rec_lock:
                    if self.recorder:
                        self.recorder.note(f"adapter error: {e} ({time.time():.6f})")
                source.close()
                source = None
                self._status("disconnected", f"{e} (reconnecting)")
                continue
            if frames:
                self.state.feed(frames, time.monotonic())
                with self.rec_lock:
                    if self.recorder and self.recorder.write(frames, time.time()):
                        with self.state.lock:
                            self.state.recording = str(self.recorder.path)
            else:
                time.sleep(0.001)
        if source is not None:
            source.close()
        self.stop_recording()

    def _status(self, status, error):
        with self.state.lock:
            self.state.status = status
            self.state.error = error

    def stop(self):
        self.stop_event.set()


class MatchJob:
    """One `match` run at a time, in the background, for the page's Find signals panel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle"}

    def start(self, capture, reference, address_map, label):
        from . import match
        with self.lock:
            if self.state.get("state") == "running":
                raise ValueError("a search is already running")
            self.state = {"state": "running", "capture": capture.name, "reference": label, "log": []}

        def work():
            lines = []

            def log(text):
                lines.append(text)
                with self.lock:
                    self.state["log"] = list(lines)
            try:
                results, (unit, off) = match.run(capture, reference, address_map, log=log)
                out = []
                for column, found in results.items():
                    out.append({"column": column, "matches": [{
                        "field": m.candidate.describe(), "r": round(m.r, 4),
                        "changes": None if m.r_changes is None else round(m.r_changes, 2),
                        "rpm_held": None if m.r_partial is None else round(m.r_partial, 2),
                        "scale": float(f"{m.scale:.6g}"), "offset": float(f"{m.offset:.6g}"),
                        "known": m.known, "verdict": match.verdict(m),
                        "entry": dict({"key": "new_signal", "label": column}, **m.candidate.map_fields(),
                                      scale=float(f"{m.scale:.6g}"), offset=float(f"{m.offset:.6g}"),
                                      source="observed", note=f"Matched {column} (r {m.r:.3f})"),
                    } for m in found]})
                with self.lock:
                    self.state = {"state": "done", "capture": capture.name, "reference": label, "log": lines,
                                  "results": out}
            except Exception as e:  # noqa: BLE001 - reported to the page
                with self.lock:
                    self.state = {"state": "error", "capture": capture.name, "reference": label, "log": lines,
                                  "error": str(e)}
            finally:
                if label != "obd" and reference:
                    Path(reference).unlink(missing_ok=True)

        threading.Thread(target=work, daemon=True, name="pandacapture-match").start()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))


def list_captures(folder: Path, recording: str):
    if not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
        st = f.stat()
        out.append({"name": f.name, "size": st.st_size,
                    "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "recording": str(f) == recording})
    return out


def make_handler(dash):
    state, reader, stopping = dash.state, dash.reader, dash.stopping
    page = (web_dir() / "dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep the console for the dashboard's own messages

        def _send(self, body: bytes, kind: str, code=200):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, code=200):
            self._send(json.dumps(payload).encode("utf-8"), "application/json", code)

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif path == "/map":
                with state.lock:
                    self._json(state.map.to_json())
            elif path == "/state":
                self._json(state.snapshot())
            elif path == "/maps":
                with state.lock:
                    current = state.map
                names = list(builtin_maps())
                self._json({"maps": names, "current": current.name,
                            "current_file": Path(current.path).stem if current.path else ""})
            elif path == "/captures":
                self._json({"folder": str(reader.record_dir),
                            "captures": list_captures(reader.record_dir, state.recording)})
            elif path == "/match":
                self._json(dash.match.snapshot())
            elif path == "/events":
                high = parse_qs(url.query).get("mode") == ["high"]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    if high:
                        self._stream_samples()
                    else:
                        while not stopping.is_set():
                            self._event("snapshot", state.snapshot())
                            time.sleep(PUSH_EVERY)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
            else:
                self.send_error(404)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ValueError(f"upload too big (over {MAX_UPLOAD // (1024 * 1024)} MB)")
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            url = urlsplit(self.path)
            try:
                body = self._body()
                if url.path == "/record":
                    on = json.loads(body or b"{}").get("on", True)
                    if on:
                        self._json({"recording": str(reader.start_recording())})
                    else:
                        path = reader.stop_recording()
                        self._json({"saved": str(path) if path else ""})
                elif url.path == "/marker":
                    n = reader.marker()
                    if n is None:
                        self._json({"error": "Start recording first: markers go into the recording."}, 409)
                    else:
                        self._json({"marker": n})
                elif url.path == "/map":
                    name = json.loads(body or b"{}").get("name", "")
                    if name not in builtin_maps():
                        self._json({"error": f"No map named {name!r}"}, 404)
                        return
                    state.set_map(load_map(name))
                    self._json({"map": state.map.name})
                elif url.path == "/match":
                    q = parse_qs(url.query)
                    name = (q.get("capture") or [""])[0]
                    names = {c["name"] for c in list_captures(reader.record_dir, state.recording)}
                    if name not in names:
                        self._json({"error": "Pick a capture from the list."}, 400)
                        return
                    capture = reader.record_dir / name
                    if (q.get("ref") or ["obd"])[0] == "obd":
                        dash.match.start(capture, None, state.map, "obd")
                    else:
                        if not body:
                            self._json({"error": "Choose the reference CSV (e.g. the JB4 log)."}, 400)
                            return
                        fd, tmp = tempfile.mkstemp(prefix="pandacapture-ref-", suffix=".csv")
                        with open(fd, "wb") as f:
                            f.write(body)
                        dash.match.start(capture, tmp, state.map, (q.get("filename") or ["reference CSV"])[0])
                    self._json({"started": True})
                else:
                    self.send_error(404)
            except (ValueError, MapError, OSError) as e:
                self._json({"error": str(e)}, 400)

        def _event(self, kind, payload):
            self.wfile.write(f"event: {kind}\ndata: ".encode("utf-8") + json.dumps(payload).encode("utf-8") + b"\n\n")
            self.wfile.flush()

        def _stream_samples(self):
            """Every sample, as soon as the reader has it: the stream runs at the bus's own pace."""
            with state.lock:
                seq = state.seq
            self._event("snapshot", state.snapshot())  # start from the current values
            last_status = time.monotonic()
            while not stopping.is_set():
                samples, seq, lost = state.samples_after(seq, STATUS_EVERY)
                if samples or lost:
                    self._event("samples", {"s": samples, "lost": lost})
                if time.monotonic() - last_status >= STATUS_EVERY:
                    self._event("status", state.info())
                    last_status = time.monotonic()

    return Handler


class Dashboard:
    """The reader thread plus the web server, started and stopped together."""

    def __init__(self, open_source, address_map, host="127.0.0.1", port=8765, record=False, record_dir=None,
                 split_mb=100.0):
        self.state = LiveState(address_map)
        self.stopping = threading.Event()
        self.reader = Reader(open_source, self.state, record_dir=record_dir, record=record, split_mb=split_mb)
        self.match = MatchJob()
        self.server = ThreadingHTTPServer((host, port), make_handler(self))
        self.server.daemon_threads = True
        # 127.0.0.1, not localhost: the server is IPv4-only, and Windows tries IPv6 first for localhost
        self.url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host}:{self.server.server_address[1]}/"

    def start(self):
        self.reader.start()
        threading.Thread(target=self.server.serve_forever, daemon=True, name="pandacapture-web").start()

    def stop(self):
        self.stopping.set()
        self.reader.stop()
        self.server.shutdown()
        self.server.server_close()
        self.reader.join(timeout=3)
