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
  /replay  {"capture": "..."}      replay a capture from the captures folder, with a timeline
           {"action": "play"|"pause"|"stop"} or {"action": "seek", "s": seconds}
and GET /maps, /captures, /match (the running or last match: progress and results), /replay.

While a replay runs the page shows it alone (live frames aren't shown, though a recording keeps them), and
values are judged fresh by the replay's clock, which stops while it's paused.
"""

import collections
import datetime as dt
import itertools
import json
import queue
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .capture import RollingLog, candump_line, default_out_dir, restart_note
from .firmware import bundled_dir
from .logs import replay_file
from .policy import PolicyRefused, Sender, VehicleState
from .replay import FAILED, STOPPED, ReplayPlayer
from .signals import Evaluator, MapError, Signal, builtin_maps, load_map
from .sources import SourceError
from .speedrange import SpeedTags
from .udsread import CYLINDERS, BusWatch, UdsPoller, Watch as UdsWatch, knock_target, transmission_target, uds_text
from .usbdev import UsbError

STALE_SECONDS = 1.5
PUSH_EVERY = 0.1        # normal mode
STATUS_EVERY = 0.5      # high-resolution mode: status alongside the samples
SAMPLE_BUFFER = 50000   # samples kept for high-resolution clients that fall behind
RECONNECT_EVERY = 2.0
QUEUE_BATCHES = 200000  # USB reads waiting to be decoded (minutes of a busy bus) before any are dropped
MAX_UPLOAD = 20 * 1024 * 1024


# The transmission's 01A0 answers (whoever asked, see udsread.py), shown as signals of their own once heard
TCU_GROUP = "Transmission (01A0)"
_TCU_NOTE = "From the transmission's answers to 22 01A0 (another tester's or pandacapture obd --transmission's)."
TCU_SIGNALS = [
    Signal("tcu_slip", "Converter slip", short="Slip", unit="rpm", display="gauge", group=TCU_GROUP, min=-500,
           max=2000, warn_above=600, alert_above=1200, decimals=0, source="observed",
           note="Engine speed less turbine speed: near 0 with the converter locked, hundreds of rpm on a launch. " + _TCU_NOTE),
    Signal("tcu_turbine", "Turbine speed", short="Turbine", unit="rpm", display="gauge", group=TCU_GROUP, min=0,
           max=7000, decimals=0, source="observed", note=_TCU_NOTE),
    Signal("tcu_atf", "ATF temperature", short="ATF", unit="°C", display="gauge", group=TCU_GROUP, min=0, max=150,
           warn_above=110, alert_above=125, decimals=0, source="observed", note=_TCU_NOTE),
    Signal("tcu_output", "Output shaft speed", short="Output shaft", unit="rpm", group=TCU_GROUP, decimals=0,
           source="observed", note="Turbine speed over this is the gear's ratio. " + _TCU_NOTE),
    Signal("tcu_gear", "Gear (01A0)", group=TCU_GROUP, decimals=0, source="observed", note=_TCU_NOTE),
    Signal("tcu_ratio", "Ratio (coarse)", short="Ratio", group=TCU_GROUP, decimals=2, source="observed",
           note="The transmission's own ratio figure, in quarters. " + _TCU_NOTE),
]
_TCU_VALUES = {"tcu_slip": "slip_rpm", "tcu_turbine": "turbine_rpm", "tcu_atf": "atf_c", "tcu_output": "output_rpm",
               "tcu_gear": "gear", "tcu_ratio": "ratio"}
KNOCK_STALE = 3.0   # s without an E019 answer before the knock tile dims
READS_FILE = ".pandacapture-reads.json"   # the Read switches, kept in the captures folder across launches
CONFIRM_WORD = "TRANSMIT"


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
        self.dropped = 0       # USB reads dropped because decoding fell minutes behind
        self.map_version = 0
        self._rate_mark = (time.monotonic(), 0)
        self.fps = 0.0
        self.seq = 0
        self.samples = collections.deque(maxlen=SAMPLE_BUFFER)  # (sequence, key, value, unix time)
        # The clock values are judged fresh by: the wall clock, or a replay's (it stops while paused)
        self.clock = time.monotonic
        self.replay = None     # the replay's state for the page, while one runs
        self.epoch = 0         # counts the times shown values were forgotten (a replay's start, rewind, end)
        # Per-cylinder knock and the transmission's data, from E019 and 01A0 answers whoever asked (udsread.py)
        self.uds = UdsWatch()
        self.knock = None      # (latest reading, the clock's time)
        self.knock_peak = [0.0] * CYLINDERS   # since the dashboard started, a replay began or went back, or a reset
        self.tcu_heard = False   # 01A0 answers have come: its signals show (pages rebuild once to add them)
        self.reads = None      # the Read switches (ActiveReads), set by the Reader
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

    # ---- replays: the replay's frames alone, by its clock ----

    def _forget(self):
        self.values, self.current = {}, {}
        self.evaluator = Evaluator(self.map)
        self.uds = UdsWatch()
        self.knock, self.knock_peak = None, [0.0] * CYLINDERS
        self.epoch += 1

    def reset_knock(self):
        """Forgets the knock reading and its peaks (the tile's tap)."""
        with self.lock:
            self.knock, self.knock_peak = None, [0.0] * CYLINDERS

    def signals(self):
        """The map's signals, and the transmission's once its answers have come."""
        return self.map.signals + (TCU_SIGNALS if self.tcu_heard else [])

    def map_json(self) -> dict:
        with self.lock:
            d = self.map.to_json()
            if self.tcu_heard:
                d["signals"] += [s.to_json() for s in TCU_SIGNALS]
            return d

    def _uds(self, f, now, unix) -> bool:
        """An E019 or 01A0 answer this frame completes: the knock reading, or the transmission's values."""
        got = self.uds.frame(f)
        if got is None:
            return False
        kind, r = got
        if kind == "knock":
            self.knock = (r, now)
            self.knock_peak = [max(a, b) for a, b in zip(self.knock_peak, r["retard"])]
            return False
        if not self.tcu_heard:
            self.tcu_heard = True
            self.map_version += 1      # pages rebuild with the Transmission section
        for key, field_ in _TCU_VALUES.items():
            self._sample(key, r[field_], now, unix)
        return True

    def begin_replay(self, clock, name):
        with self.lock:
            self.clock = clock
            self.replay = {"name": name, "phase": "indexing", "position": 0.0, "duration": 0.0, "speed": 1.0,
                           "message": None}
            self._forget()

    def set_replay(self, st):
        with self.lock:
            if self.replay is not None:
                self.replay = dict(self.replay, **st)

    def rewind(self):
        """The replay went back: what's shown belongs to later."""
        with self.lock:
            self._forget()

    def end_replay(self):
        with self.lock:
            self.clock = time.monotonic
            self.replay = None
            self._forget()

    def feed(self, frames, now, unix=None, replay=False):
        unix = time.time() if unix is None else unix
        with self.lock:
            if self.replay is not None and not replay:
                return             # live frames while a replay runs: the page shows the replay alone
            added = False
            for f in frames:
                if f.returned or f.rejected:
                    continue
                self.frames += 1
                self.bus_frames[f.bus] = self.bus_frames.get(f.bus, 0) + 1
                if 0x7E8 <= f.addr <= 0x7E9 and self._uds(f, now, unix):
                    added = True
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
            new = self.seq - seq
            take = min(new, len(self.samples))
            # walk back from the newest: only the samples this client hasn't had, never the whole buffer
            items = list(itertools.islice(reversed(self.samples), take))
            last = self.seq
        items.reverse()
        return [(k, round(v, 4), round(t, 4)) for _, k, v, t in items], last, new - take

    def info(self) -> dict:
        now = time.monotonic()
        with self.lock:
            t0, n0 = self._rate_mark
            if now - t0 >= 1.0:
                self.fps = (self.frames - n0) / (now - t0)
                self._rate_mark = (now, self.frames)
            return {"frames": self.frames, "fps": round(self.fps), "buses": dict(self.bus_frames),
                    "status": self.status, "error": self.error, "source": self.source, "dropped": self.dropped,
                    "recording": self.recording,
                    "recording_for": round(now - self.recording_since) if self.recording_since else None,
                    "markers": self.markers, "map_version": self.map_version, "map_name": self.map.name,
                    "epoch": self.epoch, "replay": dict(self.replay, epoch=self.epoch) if self.replay else None,
                    "knock": self._knock_json(), "reads": self.reads.info() if self.reads else None}

    def _knock_json(self):
        if self.knock is None:
            return None
        r, t = self.knock
        age = self.clock() - t
        return {"retard": [round(x, 2) for x in r["retard"]], "peak": [round(x, 2) for x in self.knock_peak],
                "noise": r["noise"], "memory": r["memory"], "age": round(age, 2), "stale": age > KNOCK_STALE}

    def snapshot(self) -> dict:
        status = self.info()
        now = self.clock()
        with self.lock:
            out = {}
            for s in self.signals():
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

    def __init__(self, out_dir, description, split_mb=100.0, speed_map=None):
        self.log = RollingLog(out_dir, ["# PandaCapture candump log (recorded by the dashboard)",
                                        f"# source: {description}"], split_mb, speed_map=speed_map)
        self._last_flush = time.monotonic()

    @property
    def path(self):
        return self.log.path

    @property
    def paths(self):
        return self.log.paths

    def write(self, frames, t):
        for f in frames:
            # Returned frames went out on the bus: the Read switches' requests (listening, nothing else is sent)
            if not f.rejected:
                self.log.write(candump_line(t, f) + "\n")
                self.log.track(f, t)
        if time.monotonic() - self._last_flush > 1.0:
            self.log.flush()
            self._last_flush = time.monotonic()
        return self.log.maybe_rotate()

    def note(self, text):
        self.log.write(f"# {text}\n")
        self.log.flush()

    def close(self):
        self.log.end_notes()
        self.log.close()


class Reader(threading.Thread):
    """Drains the source in a thread of its own, and decodes and records in a second one.

    The first thread only reads the panda and queues what it gets, so nothing else (pages being
    served, the recording's disk writes, a slow browser) can hold up reading the panda. The second
    takes from the queue in order: it updates the LiveState and writes the recording. Notes for the
    recording (markers, errors) go through the same queue, so the recording keeps its order.
    """

    def __init__(self, open_source, state: LiveState, record_dir=None, record=False, split_mb=100.0, log=print):
        super().__init__(daemon=True, name="pandacapture-reader")
        self.split_mb = split_mb
        self.open_source = open_source
        self.state = state
        self.log = log
        self.stop_event = threading.Event()
        self.queue = queue.Queue(maxsize=QUEUE_BATCHES)
        self.record_dir = Path(record_dir) if record_dir else default_out_dir()
        self.recorder = None
        self.rec_lock = threading.Lock()
        self.description = "waiting for the source"
        self.saved = []   # captures recorded this session
        self.reads = ActiveReads(self.record_dir)
        state.reads = self.reads
        self.processor = threading.Thread(target=self._process, daemon=True, name="pandacapture-decoder")
        if record:
            self.start_recording()

    # ---- recording controls (called from web threads) ----

    def start_recording(self):
        with self.rec_lock:
            if self.recorder is None:
                with self.state.lock:
                    speed_map = self.state.map
                self.recorder = Recorder(self.record_dir, self.description, self.split_mb, speed_map)
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
        self._note(f"marker {n} ({time.time():.6f})")
        return n

    def _note(self, text):
        """A comment line for the recording, in order with the frames around it."""
        self._put(("note", text))

    def _put(self, item):
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            with self.state.lock:
                self.state.dropped += 1

    # ---- reading the source (this thread) ----

    def run(self):
        self.processor.start()
        source, lost_at = None, None
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
                why = restart_note(source, time.monotonic() - lost_at) if lost_at is not None else ""
                if lost_at is not None:
                    self.log(f"[{_clock()}] Reconnected after {time.monotonic() - lost_at:.1f} s. {why}")
                    lost_at = None
                self._note(f"source: {source.description}" + (f". {why}" if why else ""))
                self._status("idle" if getattr(source, "idle", False) else "live", "")
            try:
                frames = source.read()
            except SourceError as e:
                self.reads.lost(f"the source failed: {e}")
                lost_at = time.monotonic()
                self.log(f"[{_clock()}] {getattr(source, 'kind', 'Panda')} error: {e}. Reconnecting every {RECONNECT_EVERY:g} s...")
                self._note(f"adapter error: {e} ({time.time():.6f})")
                source.close()
                source = None
                self._status("disconnected", f"{e} (reconnecting)")
                continue
            if frames:
                self._put(("frames", frames, time.monotonic(), time.time()))
            # The Read switches' requests, sent from here: this thread owns the panda
            self.reads.step(source, frames, self.recorder, self._note, self.state)
            if not frames:
                time.sleep(0.001)
        self.reads.stop(source, self._note)
        if source is not None:
            source.close()
        self._put(("stop",))
        self.processor.join(timeout=5)
        self.stop_recording()

    # ---- decoding and recording (the second thread) ----

    def _process(self):
        while True:
            item = self.queue.get()
            if item[0] == "stop":
                return
            if item[0] == "frames":
                _, frames, mono, unix = item
                self.state.feed(frames, mono, unix)
                with self.rec_lock:
                    if self.recorder and self.recorder.write(frames, unix):
                        with self.state.lock:
                            self.state.recording = str(self.recorder.path)
            else:
                with self.rec_lock:
                    if self.recorder:
                        self.recorder.note(item[1])

    def _status(self, status, error):
        with self.state.lock:
            self.state.status = status
            self.state.error = error

    def stop(self):
        self.stop_event.set()


def _clock():
    return dt.datetime.now().strftime("%H:%M:%S")


class _SourceLink:
    """What the policy's Sender sends through: the panda the dashboard reads."""

    def __init__(self, source):
        self.source = source

    def send(self, frames):
        self.source.send(frames)


class ActiveReads:
    """The dashboard's Read switches: its own reads of per-cylinder knock (22 E019) and the transmission (22 01A0),
    with every recording. Through a panda with PandaCapture firmware, once TRANSMIT has been typed on this computer
    this session; the same requests, scheduling and coexistence as pandacapture obd --knock --transmission
    (udsread.UdsPoller), and the same cut-off: a bus going bus-off, or counting over 50 errors in a second, ends
    all sending. Sending ends with the recording, and the panda goes back to listening. The switches are kept
    across launches; TRANSMIT isn't. Driven by the reader thread, which owns the panda."""

    def __init__(self, record_dir):
        self.path = Path(record_dir) / READS_FILE
        self.lock = threading.Lock()
        self.want = self._load()
        self.confirmed = False
        self.poller = self.watch = self.vehicle = None
        self.running = ()          # the targets' names while sending
        self.watch_at = 0.0
        self.status = ""
        self.problem = None        # why sending stopped during this recording (it waits for the next one)
        self.problem_rec = None

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            return {"knock": bool(d.get("knock")), "transmission": bool(d.get("transmission"))}
        except (OSError, ValueError, AttributeError):
            return {"knock": False, "transmission": False}

    def set(self, knock=None, transmission=None, confirm=None):
        with self.lock:
            if knock is not None:
                self.want["knock"] = bool(knock)
            if transmission is not None:
                self.want["transmission"] = bool(transmission)
            if confirm is not None:
                if confirm.strip().upper() != CONFIRM_WORD:
                    raise ValueError(f"Type {CONFIRM_WORD} to let the dashboard send the reads.")
                self.confirmed = True
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.want), encoding="utf-8")
            except OSError:
                pass
            return self._info()

    def info(self):
        with self.lock:
            return self._info()

    def _info(self):
        return dict(self.want, confirmed=self.confirmed, running=bool(self.running), status=self.status,
                    problem=self.problem)

    def step(self, source, frames, recorder, note, state):
        """One pass of the reader: the frames it just read, the recording (or None), note(text) into it."""
        with self.lock:
            names = tuple(k for k in ("knock", "transmission") if self.want[k])
            confirmed = self.confirmed
        if recorder is not None and recorder is not self.problem_rec:
            self.problem = None                     # a new recording: try again
        why = (None if names else "") if recorder is not None else ("waiting for a recording" if names else "")
        if why is None and not confirmed:
            why = f"waiting for {CONFIRM_WORD} to be typed (on this computer)"
        if why is None and not getattr(source, "can_transmit", False):
            why = ("listening only: sending needs PandaCapture firmware (pandacapture flash)" if hasattr(source, "arm")
                   else "listening only: no panda to send through")
        if why is None and self.problem:
            why = f"stopped: {self.problem}"
        if why is None and self.running and self.running != names:
            self._end(source, note, "the switches changed")    # starts again with the new set below
        if why is not None:
            if self.running:
                self._end(source, note, why or "switched off")
            self.status = why
            return
        try:
            if not self.running:
                self._begin(source, note, names, state)
            now = time.monotonic()
            for f in frames:
                if not (f.returned or f.rejected):
                    self.vehicle.frame(f)
                self.poller.frame(f, now)
            self.poller.tick(now)
            source.beat()
            if now - self.watch_at >= 1.0:
                self.watch_at = now
                trouble = self.watch.check()
                if trouble:
                    raise SourceError(trouble)
            if self.poller.stopped:
                raise SourceError(f"every read stopped ({self.poller.status})")
            self.status = self.poller.status
        except (SourceError, UsbError, PolicyRefused) as e:
            self.problem, self.problem_rec = str(e), recorder
            self._end(source, note, str(e))
            self.status = f"stopped: {e}"

    def _begin(self, source, note, names, state):
        targets = [{"knock": knock_target, "transmission": transmission_target}[n]() for n in names]
        with state.lock:
            busiest = sorted(state.bus_frames.items(), key=lambda x: -x[1])
        bus = busiest[0][0] if busiest else 0
        self.vehicle = VehicleState()
        self.poller = UdsPoller(Sender(_SourceLink(source), self.vehicle), bus, targets)
        self.watch = BusWatch(source.panda, [bus])
        self.watch_at = time.monotonic()
        source.arm()
        self.running = names
        note(f"active polling, not silent: transmit armed (PandaCapture firmware), {uds_text(targets)}, one request "
             f"on the bus at a time, with the flow control for their long answers, on bus {bus} ({time.time():.6f})")

    def _end(self, source, note, why):
        if not self.running:
            return
        self.running, self.poller = (), None
        source.disarm()
        note(f"reads stopped ({why}): transmit disarmed, listening again ({time.time():.6f})")

    def lost(self, why):
        """The source failed: the panda (and its armed state) is gone; it disarms itself without a heartbeat."""
        if self.running:
            self.running, self.poller = (), None
            self.status = f"stopped: {why}"

    def stop(self, source, note):
        if self.running and source is not None:
            self._end(source, note, "the dashboard stopped")


class ReplayJob:
    """The dashboard's replay: one at a time, with a timeline (replay.py). Its frames go to the LiveState by the
    replay's clock; stopping it goes back to the live source."""

    def __init__(self, state: LiveState):
        self.state = state
        self.player = None

    def start(self, path, name=None, speed=1.0, loop=False) -> dict:
        file = replay_file(path)              # a zip's log is copied out, so it can be seeked in
        temp = file if file != Path(path) else None
        self.stop()
        name = name or Path(path).name

        def on_frames(frames):
            if self.player is player:
                self.state.feed(frames, player.clock(), time.time(), replay=True)

        def on_rewind():
            if self.player is player:
                self.state.rewind()

        def on_state(st):
            if st["phase"] in (STOPPED, FAILED) and temp is not None:
                temp.unlink(missing_ok=True)
            if self.player is not player:
                return
            if st["phase"] == STOPPED:
                self.player = None
                self.state.end_replay()
            else:
                self.state.set_replay(st)
        player = ReplayPlayer(file, on_frames, on_rewind, on_state, speed=speed, loop=loop)
        self.player = player
        self.state.begin_replay(player.clock, name)
        player.start()
        return self.state.info()["replay"]

    def control(self, action, s=None) -> dict:
        player = self.player
        if player is None:
            raise ValueError("No replay is running.")
        if action == "play":
            player.play()
        elif action == "pause":
            player.pause()
        elif action == "seek":
            player.seek(float(s))
        elif action == "stop":
            self.stop()
        else:
            raise ValueError(f"unknown replay action {action!r}")
        return self.state.info()["replay"]

    def stop(self):
        player, self.player = self.player, None
        if player is not None:
            player.stop()
            self.state.end_replay()


class MatchJob:
    """One `match` run at a time, in the background, for the page's Find signals panel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle"}

    def start(self, capture, reference, address_map, label, keep=False):
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
                if label != "obd" and reference and not keep:
                    Path(reference).unlink(missing_ok=True)

        threading.Thread(target=work, daemon=True, name="pandacapture-match").start()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))


def list_captures(folder: Path, recording: str, tags=None, address_map=None):
    """The folder's newest 50 logs; with tags (a SpeedTags), each one's speed range as far as it's known."""
    if not folder.is_dir():
        return []
    out = []
    for f in sorted(folder.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:50]:
        st = f.stat()
        item = {"name": f.name, "size": st.st_size,
                "modified": dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "recording": str(f) == recording}
        if tags is not None and not item["recording"]:   # the one recording gets its tag as it ends
            tag, pending = tags.get(f, address_map)
            item["speed"] = tag.to_json() if tag else None
            item["speed_pending"] = pending
        out.append(item)
    return out


LOCAL = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def make_handler(dash):
    state, reader, stopping = dash.state, dash.reader, dash.stopping
    page = (web_dir() / "dashboard.html").read_bytes()
    captures_page = (web_dir() / "captures.html").read_bytes()
    runs_page = (web_dir() / "runs.html").read_bytes()
    devtools_page = (web_dir() / "devtools.html").read_bytes()
    icons = {"/icon.svg": ((web_dir() / "icon.svg").read_bytes(), "image/svg+xml"),
             "/units.js": ((web_dir() / "units.js").read_bytes(), "text/javascript; charset=utf-8"),
             "/devtools.js": ((web_dir() / "devtools.js").read_bytes(), "text/javascript; charset=utf-8"),
             "/icon-512.png": ((web_dir() / "icon-512.png").read_bytes(), "image/png")}

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

        def _local(self) -> bool:
            """Claude runs (they spend money on your key) and bundles only from this computer, not --lan devices."""
            if self.client_address[0] in LOCAL:
                return True
            self._json({"error": "Only from the computer running PandaCapture."}, 403)
            return False

        def _capture(self, name):
            names = {c["name"] for c in list_captures(reader.record_dir, state.recording)}
            if name not in names:
                raise ValueError("Pick a capture from the list.")
            return reader.record_dir / name

        def do_GET(self):
            url = urlsplit(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            elif path == "/captures.html":
                self._send(captures_page, "text/html; charset=utf-8")
            elif path == "/runs.html":
                self._send(runs_page, "text/html; charset=utf-8")
            elif path == "/devtools.html":
                self._send(devtools_page, "text/html; charset=utf-8")
            elif path == "/runs/saved":
                from .runs import runs_dir, saved_summaries
                units = (parse_qs(url.query).get("units") or ["us"])[0]
                self._json({"folder": str(runs_dir(reader.record_dir)),
                            "runs": saved_summaries(runs_dir(reader.record_dir), units)})
            elif path == "/runs/run":
                from .runs import open_saved, runs_dir
                q = parse_qs(url.query)
                self._json(open_saved(runs_dir(reader.record_dir), (q.get("id") or [""])[0], (q.get("units") or ["us"])[0]))
            elif path in icons:
                self._send(*icons[path])
            elif path == "/references":
                self._json({"references": dash.references.listing()})
            elif path == "/claude":
                from .ai import MODELS
                from .workbench import key_status
                self._json({**dash.claude.snapshot(), "key": key_status(), "models": MODELS,
                            "local": self.client_address[0] in LOCAL})
            elif path == "/bundle":
                if not self._local():
                    return
                q = parse_qs(url.query)
                try:
                    capture = self._capture((q.get("capture") or [""])[0])
                    ref, ref_name = dash.references.get((q.get("ref") or [""])[0])
                    from .workbench import bundle_bytes
                    with state.lock:
                        current = state.map
                    data = bundle_bytes(capture, current, ref, ref_name)
                except (ValueError, OSError) as e:
                    self._json({"error": str(e)}, 400)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{capture.stem}-bundle.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/map":
                self._json(state.map_json())
            elif path == "/state":
                self._json(state.snapshot())
            elif path == "/maps":
                with state.lock:
                    current = state.map
                names = list(builtin_maps())
                self._json({"maps": names, "current": current.name,
                            "current_file": Path(current.path).stem if current.path else ""})
            elif path == "/captures":
                with state.lock:
                    current = state.map
                self._json({"folder": str(reader.record_dir),
                            "captures": list_captures(reader.record_dir, state.recording, dash.speed_tags, current)})
            elif path == "/match":
                self._json(dash.match.snapshot())
            elif path == "/replay":
                self._json({"replay": state.info()["replay"]})
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
                if url.path == "/reads":
                    # The Read switches: turning one on (and typing TRANSMIT) only on this computer
                    req = json.loads(body or b"{}")
                    if (req.get("knock") or req.get("transmission") or req.get("confirm")) and not self._local():
                        return
                    self._json({"reads": reader.reads.set(req.get("knock"), req.get("transmission"), req.get("confirm"))})
                elif url.path == "/knock/reset":
                    state.reset_knock()
                    self._json({"ok": True})
                elif url.path == "/replay":
                    # A capture from the captures folder, or play / pause / seek / stop the one running
                    req = json.loads(body or b"{}")
                    if req.get("capture"):
                        self._json({"replay": dash.replay.start(self._capture(req["capture"]))})
                    else:
                        self._json({"replay": dash.replay.control(req.get("action", ""), req.get("s", 0))})
                elif url.path == "/record":
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
                elif url.path == "/reference":
                    if not body:
                        raise ValueError("Choose a CSV file.")
                    name = (parse_qs(url.query).get("filename") or ["reference.csv"])[0]
                    self._json({"id": dash.references.add(body, name), "name": name})
                elif url.path == "/runs/analyze":
                    # Runs in a capture: found, timed and coached, and kept for later comparisons
                    from .runs import DEFAULT_MASS_KG, analyze_capture, runs_dir, save_runs, saved_runs
                    req = json.loads(body or b"{}")
                    capture = self._capture(req.get("capture", ""))
                    mass = float(req.get("mass_kg") or DEFAULT_MASS_KG)
                    if not 500 <= mass <= 5000:
                        raise ValueError("The weight should be the car with driver and fuel.")
                    with state.lock:
                        current = state.map
                    folder = runs_dir(reader.record_dir)
                    # The units set on the page: they pick what a run is compared by, the coaching's speed
                    # marks, and whether a launch is timed to 60 mph or 100 km/h
                    result = analyze_capture(capture, current, mass, saved_runs(folder), units=req.get("units", "us"))
                    save_runs(result, folder)
                    self._json(result)
                elif url.path == "/claude/use-map":
                    # The user's choice: add the map Claude built to their maps and switch the dashboard to it
                    if not self._local():
                        return
                    from .signals import user_dir
                    path = dash.claude.use_built_map(user_dir())
                    state.set_map(load_map(path))
                    self._json({"map": state.map.name, "file": str(path)})
                elif url.path in ("/claude/prepare", "/claude/start", "/claude/cancel"):
                    if not self._local():
                        return
                    if url.path == "/claude/prepare":
                        req = json.loads(body or b"{}")
                        capture = self._capture(req.get("capture", ""))
                        ref, ref_name = dash.references.get(req.get("ref", ""))
                        with state.lock:
                            current = state.map
                        dash.claude.prepare(capture, ref, ref_name, current, req.get("model", "opus"),
                                            req.get("effort", "high"), float(req.get("max_cost", 2.0)))
                    elif url.path == "/claude/start":
                        dash.claude.start()
                    else:
                        dash.claude.cancel()
                    self._json(dash.claude.snapshot())
                elif url.path == "/match":
                    q = parse_qs(url.query)
                    capture = self._capture((q.get("capture") or [""])[0])
                    rid = (q.get("ref") or ["obd"])[0]
                    if rid == "obd":
                        dash.match.start(capture, None, state.map, "obd")
                    elif rid in {r["id"] for r in dash.references.listing()}:
                        path, name = dash.references.get(rid)
                        dash.match.start(capture, str(path), state.map, name, keep=True)
                    else:
                        if not body:
                            self._json({"error": "Choose the reference CSV (e.g. a tuner's datalog)."}, 400)
                            return
                        fd, tmp = tempfile.mkstemp(prefix="pandacapture-ref-", suffix=".csv")
                        with open(fd, "wb") as f:
                            f.write(body)
                        dash.match.start(capture, tmp, state.map, (q.get("filename") or ["reference CSV"])[0])
                    self._json({"started": True})
                else:
                    self.send_error(404)
            except (ValueError, MapError, OSError, json.JSONDecodeError) as e:
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
                 split_mb=100.0, log=print, replay=None):
        self.state = LiveState(address_map)
        self.replay = ReplayJob(self.state)
        self._replay_at_start = replay   # {"path", "speed", "loop"}: --replay
        self.stopping = threading.Event()
        self.reader = Reader(open_source, self.state, record_dir=record_dir, record=record, split_mb=split_mb,
                             log=log)
        self.match = MatchJob()
        self.speed_tags = SpeedTags(self.reader.record_dir)
        from .workbench import ClaudeJob, References
        self.references = References()
        self.claude = ClaudeJob(self.reader.record_dir)
        self.server = ThreadingHTTPServer((host, port), make_handler(self))
        self.server.daemon_threads = True
        # 127.0.0.1, not localhost: the server is IPv4-only, and Windows tries IPv6 first for localhost
        self.url = f"http://{'127.0.0.1' if host in ('127.0.0.1', '0.0.0.0') else host}:{self.server.server_address[1]}/"

    def start(self):
        if self._replay_at_start:
            self.replay.start(**self._replay_at_start)   # a bad zip says why before anything starts
        self.reader.start()
        threading.Thread(target=self.server.serve_forever, daemon=True, name="pandacapture-web").start()

    def stop(self):
        self.stopping.set()
        self.replay.stop()
        self.reader.stop()
        self.server.shutdown()
        self.server.server_close()
        self.reader.join(timeout=3)
        self.claude.cancel()
        self.references.close()
