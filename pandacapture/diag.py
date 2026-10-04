"""Diagnostic requests and their answers, single- or multi-frame (ISO-TP), sent through the policy's
Sender (policy.py), so only what the policy table allows goes out.

A long answer starts with a first frame; the tester answers with flow control (unless the link does it
itself, as an ELM327 does) and the module sends the rest as consecutive frames.
"""

import time
from dataclasses import dataclass, field

from .policy import FUNCTIONAL, Sender, answer_id, build

ANSWER_WAIT = 0.25    # s for answers to a request to start arriving
LONG_WAIT = 0.5       # s a long answer may pause between frames before it's given up
PENDING_WAIT = 2.5    # s more when a module says it needs time (7F xx 78, "response pending")

NRC = {
    0x10: "general reject", 0x11: "service not supported", 0x12: "not supported", 0x13: "wrong length",
    0x14: "answer too long", 0x21: "busy, repeat request", 0x22: "conditions not correct",
    0x24: "request sequence error", 0x31: "out of range", 0x33: "security access denied",
    0x72: "programming failure", 0x78: "response pending", 0x7E: "not supported in this session",
    0x7F: "service not supported in this session",
}


@dataclass
class Answers:
    """What each module said to one request."""
    positive: dict = field(default_factory=dict)    # module id -> payload (service id + 0x40, data)
    negative: dict = field(default_factory=dict)    # module id -> negative response code
    broken: dict = field(default_factory=dict)      # module id -> why a long answer didn't complete

    def modules(self):
        return sorted(set(self.positive) | set(self.negative) | set(self.broken))


def nrc_text(code) -> str:
    return f"{NRC.get(code, 'refused')} ({code:02X})"


class Client:
    """Sends policy requests on a link and collects the answers. The link calls Client.frame with every
    frame it receives (ArmedPanda's on_frame, or ElmLink's)."""

    def __init__(self, link, state, on_sent=None, table=None):
        self.link = link
        self.state = state
        self.sender = Sender(link, state, on_sent, table)
        self.table = table
        self._want = None             # (bus, service id, answer ids) of the request in progress
        self._reset()

    def _reset(self):
        self._answers = Answers()
        self._assembling = {}         # module -> [total length, bytes so far, next sequence number]
        self._flow = []               # modules waiting for flow control
        self._progress = 0.0
        self._pending_until = 0.0

    def frame(self, f):
        self.state.frame(f)
        if self._want is None or f.extended or f.bus != self._want[0] or f.addr not in self._want[2]:
            return
        d = bytes(f.data)
        if not d:
            return
        kind, module = d[0] >> 4, f.addr
        if kind == 0 and 1 <= d[0] <= 7:                              # single frame
            self._finish(module, d[1:1 + d[0]])
        elif kind == 1 and len(d) >= 3:                               # first frame of a long answer
            total = ((d[0] & 0x0F) << 8) | d[1]
            self._assembling[module] = [total, bytearray(d[2:]), 1]
            self._progress = time.monotonic()
            if not getattr(self.link, "handles_flow_control", False):
                self._flow.append(module)
        elif kind == 2 and module in self._assembling:                 # consecutive frame
            total, buf, seq = self._assembling[module]
            if d[0] & 0x0F != seq:
                del self._assembling[module]
                self._answers.broken[module] = f"frame {d[0] & 0x0F} arrived where {seq} was due"
                return
            buf += d[1:]
            self._progress = time.monotonic()
            if len(buf) >= total:
                del self._assembling[module]
                self._finish(module, bytes(buf[:total]))
            else:
                self._assembling[module][2] = (seq + 1) & 0x0F

    def _finish(self, module, payload):
        sid = self._want[1]
        if len(payload) >= 3 and payload[0] == 0x7F and payload[1] == sid:
            if payload[2] == 0x78:
                self._pending_until = time.monotonic() + PENDING_WAIT
            else:
                self._answers.negative[module] = payload[2]
        elif payload[:1] == bytes([(sid + 0x40) & 0xFF]):
            self._answers.positive[module] = payload

    def request(self, sid, params=b"", bus=0, target=FUNCTIONAL, expect=(), wait=None) -> Answers:
        """One request; the answers once every expected module has answered, or nothing more comes
        (after wait seconds, ANSWER_WAIT by default, for answers to start)."""
        req = build(sid, params, target, self.table)
        self._reset()
        self._want = (bus, sid, range(0x7E8, 0x7F0) if target == FUNCTIONAL else (answer_id(target),))
        try:
            self.sender.send(req, bus)
            deadline = time.monotonic() + (ANSWER_WAIT if wait is None else wait)
            while True:
                self.link.wait(0.005)
                while self._flow:
                    self.sender.flow_control(self._flow.pop(0), bus)
                now = time.monotonic()
                if self._assembling:
                    deadline = max(deadline, self._progress + LONG_WAIT)
                deadline = max(deadline, self._pending_until)
                answered = set(self._answers.positive) | set(self._answers.negative)
                if not self._assembling and now >= self._pending_until:
                    if getattr(self.link, "answers_complete", False):
                        break
                    if expect and all(m in answered for m in expect):
                        break
                if now >= deadline:
                    break
            for module in self._assembling:
                self._answers.broken[module] = "long answer stopped part way"
            return self._answers
        finally:
            self._want = None
