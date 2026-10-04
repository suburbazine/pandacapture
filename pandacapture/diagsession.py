"""An engine-off diagnostic session: the services that change something in a module (extended session, code
setting off, communication off, module reset, actuator control, routines), held for as long as the engine stays
off and the car stopped. See docs/diagnostics-plan.md, step 4.

One worker thread owns the link for the whole session. It runs the user's commands one at a time, and between
them, several times a second:
- checks the vehicle state from new readings (engine speed 0, vehicle speed 0, Park where known)
- sends tester present to the modules in an extended session, every TESTER_PRESENT_EVERY seconds, and only
  while that check passes
The moment the check fails (the engine starts, the car moves, a reading goes missing), it sends every undo,
newest first, then puts each module back in its default session, and the session ends. If PandaCapture stops
altogether, the modules' own timeout does the same: without tester present a module drops back to its default
session a few seconds later (the S3 timer, about 5 s), which ends extended-session effects.
"""

import queue
import threading
import time
from dataclasses import dataclass, field

from .codes import ensure_engine_off
from .diag import nrc_text
from .policy import ENGINE_OFF_WORD, EngineNotOff, PolicyRefused, Tier, build

TESTER_PRESENT_EVERY = 2.0     # s: well inside a module's S3 timeout (about 5 s)
CHECK_EVERY = 0.25             # s between vehicle-state checks while anything is active
COMMAND_TIMEOUT = 30.0

WARNING = """\
An engine-off diagnostic session changes things in a module while it lasts: an extended session, code setting
off, communication off, a module reset, actuator tests, routines. Actuator tests and routines can move parts
(fans, pumps, the throttle, injectors): keep hands and tools clear, and know what each one does before you start it.

- Only with the engine off (key on), the car stopped, and Park where the map knows the gear. That's checked before
  each request and several times a second for the whole session.
- If the engine starts, the car moves or a reading goes missing, everything is undone at once (newest first) and
  the modules go back to their default session.
- Tester present (which keeps a module in its extended session) is only sent while those conditions hold. If this
  program stops, the modules drop back to their default session by themselves within about 5 seconds.
- Flashing, programming sessions and security access are never sent.
"""


class SessionEnded(Exception):
    pass


@dataclass
class Effect:
    target: int
    started: bytes                 # the request that started it
    undo: bytes                    # the request that undoes it


@dataclass
class Outcome:
    ok: bool
    text: str
    data: bytes = b""


class EngineOffSession:
    """Owns `client` (diag.Client) while open: every request goes through the worker thread."""

    def __init__(self, client, bus=0, log=print, note=lambda text: None):
        self.client = client
        self.bus = bus
        self.log = log
        self.note = note
        self.effects: list = []            # active, oldest first
        self.extended = set()              # modules in an extended session
        self.ended = ""                    # why the session ended, once it has
        self.tester_present_sent = 0
        self._jobs = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pandacapture-diag")
        self._last_tp = 0.0
        self._last_check = 0.0

    # ---- the user's side ----

    def start(self):
        self._thread.start()
        return self

    def confirm(self):
        """The user typed ENGINE OFF."""
        self.client.sender.confirm(ENGINE_OFF_WORD)

    def request(self, target, sid, params=b"") -> Outcome:
        """One request through the worker: checked, sent, and (if it started something) remembered for undoing."""
        return self._submit(lambda: self._do(target, sid, bytes(params)))

    def status(self) -> dict:
        return self._submit(lambda: {"ended": self.ended, "extended": sorted(self.extended),
                                     "effects": [(e.target, e.started.hex(" ").upper(), e.undo.hex(" ").upper())
                                                 for e in self.effects],
                                     "vehicle": self.client.state.describe()})

    def close(self, why="session ended"):
        """Undoes everything and stops the worker."""
        if self._thread.is_alive():
            try:
                self._submit(lambda: self._end(why) if not self.ended else None)
            finally:
                self._stop.set()
                self._thread.join(timeout=10)
        elif not self.ended:
            self._end(why)

    def _submit(self, fn):
        if not self._thread.is_alive():
            raise SessionEnded(self.ended or "the session isn't running")
        box, done = {}, threading.Event()
        self._jobs.put((fn, box, done))
        if not done.wait(COMMAND_TIMEOUT):
            raise SessionEnded("the session stopped answering")
        if "error" in box:
            raise box["error"]
        return box["value"]

    # ---- the worker ----

    def _run(self):
        while not self._stop.is_set():
            try:
                fn, box, done = self._jobs.get(timeout=0.02)
            except queue.Empty:
                fn = None
            if fn is not None:
                try:
                    box["value"] = fn()
                except Exception as e:  # noqa: BLE001 - handed to the caller
                    box["error"] = e
                done.set()
            self._tick()

    def _tick(self):
        """Between commands: the vehicle-state check and tester present, while anything is active."""
        if self.ended or not (self.effects or self.extended):
            self.client.link.wait(0.01)
            return
        now = time.monotonic()
        if now - self._last_check >= CHECK_EVERY:
            self._last_check = now
            try:
                ensure_engine_off(self.client, [self.bus], wait=0.15)
            except EngineNotOff as e:
                self._end(str(e))
                return
        if now - self._last_tp >= TESTER_PRESENT_EVERY:
            self._last_tp = now
            for target in sorted(self.extended):
                try:
                    # 3E 80: tester present without an answer. An engine-off request: refused unless the state holds
                    self.client.request(0x3E, b"\x80", self.bus, target, wait=0.02)
                    self.tester_present_sent += 1
                except EngineNotOff as e:
                    self._end(str(e))
                    return
        self.client.link.wait(0.01)

    def _do(self, target, sid, params) -> Outcome:
        if self.ended:
            raise SessionEnded(f"The session has ended: {self.ended}")
        req = build(sid, params, target)            # refuses what the policy doesn't allow
        if req.tier is Tier.ENGINE_OFF:
            ensure_engine_off(self.client, [self.bus])
        a = self.client.request(sid, params, self.bus, target, expect=(target + 8,))
        module = target + 8
        if module in a.negative:
            return Outcome(False, f"refused: {nrc_text(a.negative[module])}")
        if module not in a.positive:
            return Outcome(False, a.broken.get(module, "no answer"))
        answer = a.positive[module]
        self._record(target, req)
        self.note(f"{target:03X} {req.payload.hex(' ').upper()}: done; vehicle: {self.client.state.describe()}")
        return Outcome(True, "done", answer)

    def _record(self, target, req):
        """Keeps the list of what's active, and what undoes it, up to date after a request succeeded."""
        sid, params = req.payload[0], req.payload[1:]
        if sid == 0x10:
            if params[0] == 0x03:
                self.extended.add(target)
            else:                                    # back to the default session: its effects are gone
                self.extended.discard(target)
                self.effects = [e for e in self.effects if e.target != target]
            return
        if sid == 0x11:                              # a reset: the module starts afresh, in its default session
            self.extended.discard(target)
            self.effects = [e for e in self.effects if e.target != target]
            return
        # A request that undoes something ends it
        self.effects = [e for e in self.effects if not (e.target == target and e.undo == req.payload)]
        if req.undo is not None and not any(e.target == target and e.undo == req.undo for e in self.effects):
            self.effects.append(Effect(target, req.payload, req.undo))

    def _end(self, why):
        """Undoes everything, newest first, then default sessions. Undo requests are READs: they always go out."""
        undone = []
        for e in reversed(self.effects):
            try:
                a = self.client.request(e.undo[0], e.undo[1:], self.bus, e.target, expect=(e.target + 8,))
                undone.append(f"{e.target:03X} {e.undo.hex(' ').upper()}: "
                              + ("done" if e.target + 8 in a.positive else
                                 nrc_text(a.negative[e.target + 8]) if e.target + 8 in a.negative else "no answer"))
            except (PolicyRefused, OSError) as ex:
                undone.append(f"{e.target:03X} {e.undo.hex(' ').upper()}: couldn't send ({ex})")
        for target in sorted(self.extended):
            try:
                self.client.request(0x10, b"\x01", self.bus, target, expect=(target + 8,))
                undone.append(f"{target:03X} back to its default session")
            except (PolicyRefused, OSError) as ex:
                undone.append(f"{target:03X} default session: couldn't send ({ex}); it drops back within about 5 s")
        self.effects, self.extended = [], set()
        self.ended = why
        self.note(f"session ended ({why}); undone: {'; '.join(undone) or 'nothing was active'}")
        self.log(f"Session ended: {why}")
        for line in undone:
            self.log(f"  undone: {line}")
