"""Live dashboard: decodes CAN traffic with an address map and shows it as gauges, numbers and
status lights in a browser.

One thread reads frames from the source (a panda listening silently, a candump replay, or the
simulator) and keeps the latest value of every signal in the map. A small web server on this
computer serves the page (pandacapture/web/dashboard.html) and streams the values to it as
Server-Sent Events. Optionally, every frame is recorded to a candump log too.

Two stream modes, chosen by each page:
  normal  /events            the latest value of every signal, 10 times a second
  high    /events?mode=high  every decoded sample with its receive time, sent as soon as it arrives
"""

import collections
import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .capture import candump_line, default_out_dir
from .firmware import bundled_dir
from .signals import Evaluator
from .sources import SourceError

STALE_SECONDS = 1.5
PUSH_EVERY = 0.1        # normal mode
STATUS_EVERY = 0.5      # high-resolution mode: status alongside the samples
SAMPLE_BUFFER = 50000   # samples kept for high-resolution clients that fall behind
RECONNECT_EVERY = 2.0


def web_dir() -> Path:
    return bundled_dir().parent / "web"


class LiveState:
    """Latest decoded values and source health, shared between the reader and the web clients."""

    def __init__(self, address_map):
        self.map = address_map
        self.lock = threading.Lock()
        self.values = {}       # key -> (value, monotonic time)
        self.current = {}      # key -> value, for derived signals
        self.evaluator = Evaluator(address_map)
        self.samples = collections.deque(maxlen=SAMPLE_BUFFER)  # (sequence, key, value, unix time)
        self.seq = 0
        self.new_samples = threading.Condition(self.lock)
        self.frames = 0
        self.bus_frames = {}
        self.status = "starting"
        self.error = ""
        self.source = ""
        self.recording = ""
        self._rate_mark = (time.monotonic(), 0)
        self.fps = 0.0

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
                    "recording": self.recording}

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
    """candump log of everything received while the dashboard runs."""

    def __init__(self, out_dir, description):
        out_dir = Path(out_dir) if out_dir else default_out_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"capture-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        self._f = open(self.path, "w", encoding="utf-8", newline="\n")
        self._f.write("# PandaCapture candump log (recorded by the dashboard)\n")
        self._f.write(f"# source: {description}\n")
        self._f.write(f"# started: {dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')}\n")
        self._last_flush = time.monotonic()

    def write(self, frames, t):
        for f in frames:
            if not (f.returned or f.rejected):
                self._f.write(candump_line(t, f) + "\n")
        if time.monotonic() - self._last_flush > 1.0:
            self._f.flush()
            self._last_flush = time.monotonic()

    def note(self, text):
        self._f.write(f"# {text}\n")

    def close(self):
        self._f.close()


class Reader(threading.Thread):
    """Reads the source into the LiveState, reopening it when it fails."""

    def __init__(self, open_source, state: LiveState, record_dir=None, record=False):
        super().__init__(daemon=True, name="pandacapture-reader")
        self.open_source = open_source
        self.state = state
        self.stop_event = threading.Event()
        self.record = record
        self.record_dir = record_dir
        self.recorder = None

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
                with self.state.lock:
                    self.state.source = source.description
                if self.record and self.recorder is None:
                    self.recorder = Recorder(self.record_dir, source.description)
                    with self.state.lock:
                        self.state.recording = str(self.recorder.path)
                self._status("live", "")
            try:
                frames = source.read()
            except SourceError as e:
                if self.recorder:
                    self.recorder.note(f"adapter error: {e} ({time.time():.6f})")
                source.close()
                source = None
                self._status("disconnected", f"{e} (reconnecting)")
                continue
            if frames:
                self.state.feed(frames, time.monotonic())
                if self.recorder:
                    self.recorder.write(frames, time.time())
            else:
                time.sleep(0.001)
        if source is not None:
            source.close()
        if self.recorder:
            self.recorder.close()

    def _status(self, status, error):
        with self.state.lock:
            self.state.status = status
            self.state.error = error

    def stop(self):
        self.stop_event.set()


def make_handler(state: LiveState, stopping: threading.Event):
    page = (web_dir() / "dashboard.html").read_bytes()
    map_json = json.dumps(state.map.to_json()).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep the console for the dashboard's own messages

        def _send(self, body: bytes, kind: str):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif path == "/map":
                self._send(map_json, "application/json")
            elif path == "/state":
                self._send(json.dumps(state.snapshot()).encode("utf-8"), "application/json")
            elif path == "/events":
                high = "mode=high" in self.path
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

    def __init__(self, open_source, address_map, host="127.0.0.1", port=8765, record=False, record_dir=None):
        self.state = LiveState(address_map)
        self.stopping = threading.Event()
        self.reader = Reader(open_source, self.state, record_dir=record_dir, record=record)
        self.server = ThreadingHTTPServer((host, port), make_handler(self.state, self.stopping))
        self.server.daemon_threads = True
        self.url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{self.server.server_address[1]}/"

    def start(self):
        self.reader.start()
        threading.Thread(target=self.server.serve_forever, daemon=True, name="pandacapture-web").start()

    def stop(self):
        self.stopping.set()
        self.reader.stop()
        self.server.shutdown()
        self.server.server_close()
        self.reader.join(timeout=3)
