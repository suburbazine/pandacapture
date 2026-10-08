"""Per-cylinder knock (E019) and the transmission's data (01A0): the decodes, ISO-TP, the CSVs a recording writes,
and the poller against a simulated car (round robin, flow control, the rate, another tester, refusals, timeouts),
through the policy's Sender. The same cases as PandaCapture Android's UdsPollerTest."""

from pathlib import Path

import pytest

from pandacapture import protocol as p
from pandacapture.capture import RollingLog
from pandacapture.logs import read_replay
from pandacapture.policy import Sender, VehicleState
from pandacapture.signals import load_map
from pandacapture import udsread as U

CAPTURE = Path(__file__).resolve().parents[2] / "PandaCapture Android" / "captures" / "panda-20261004-153417.log"


def knock_payload(retard_raw=(0xFF,) * 6, mem=(0xFF,) * 6, noise=(10, 11, 12, 13, 14, 15)):
    b = bytearray(72)
    b[13:19], b[21:27], b[29:35] = bytes(retard_raw), bytes(mem), bytes(noise)
    return b"\x62\xe0\x19" + bytes(b)


def tcu_payload(engine=2400, kmh=50, turbine=2300, output=1000, atf=80, ratio=2.5, gear=2):
    b = bytearray(39)
    b[4:6] = int(engine * 4).to_bytes(2, "big")
    b[6] = kmh
    b[9:11] = int(turbine * 4).to_bytes(2, "big")
    b[11:13] = int(output * 4).to_bytes(2, "big")
    b[13] = atf + 40
    b[14] = int(ratio * 4)
    b[21], b[22] = gear, 0x50 | gear
    return b"\x62\x01\xa0" + bytes(b)


def isotp(addr, payload, bus=0):
    """Frames of one ISO-TP message: a single frame, or a first frame and its consecutive frames."""
    if len(payload) <= 7:
        return [p.Frame(bus, addr, bytes([len(payload)]) + payload + bytes(7 - len(payload)))]
    out = [p.Frame(bus, addr, bytes([0x10 | len(payload) >> 8, len(payload) & 0xFF]) + payload[:6])]
    rest, seq = payload[6:], 1
    while rest:
        out.append(p.Frame(bus, addr, bytes([0x20 | seq]) + rest[:7] + bytes(7 - len(rest[:7]))))
        rest, seq = rest[7:], (seq + 1) & 0x0F
    return out


def test_decodes():
    r = U.decode_knock(knock_payload(retard_raw=(0xFF, 0xF7, 0xFF, 0xFF, 0xFD, 0xFF), mem=(255, 253, 255, 255, 252, 255)))
    assert r["retard"] == [0, 6.0, 0, 0, 1.5, 0] and r["memory"][1] == 253 and r["noise"] == [10, 11, 12, 13, 14, 15]
    t = U.decode_tcu(tcu_payload())
    assert (t["engine_rpm"], t["turbine_rpm"], t["output_rpm"], t["slip_rpm"]) == (2400, 2300, 1000, 100)
    assert (t["atf_c"], t["ratio"], t["gear"], t["byte22_lo"], t["speed_kmh"]) == (80, 2.5, 2, 2, 50)
    assert U.decode_knock(b"\x62\xe0\x19" + bytes(10)) is None and U.decode_tcu(knock_payload()) is None


def test_isotp_and_the_watch():
    w = U.Watch()
    got = [w.frame(f) for f in isotp(0x7E8, knock_payload(retard_raw=(0xFB,) * 6))]
    assert got[:-1] == [None] * (len(got) - 1) and got[-1][0] == "knock" and got[-1][1]["retard"] == [3.0] * 6
    frames = isotp(0x7E9, tcu_payload())
    frames[2] = p.Frame(0, 0x7E9, bytes([0x23]) + frames[2].data[1:])   # out of sequence: dropped
    assert all(w.frame(f) is None for f in frames)
    assert w.frame(p.Frame(0, 0x316, bytes(8))) is None


def test_a_recording_writes_the_csvs(tmp_path):
    amap = load_map("kia-stinger-33t-pcan")
    spark = next(s for s in amap.signals if s.key == "spark")
    log = RollingLog(tmp_path, ["# test"], split_mb=0, stamp="20261008-120000", speed_map=amap)
    t = 1791000000.0
    raw = int(round((14.5 - spark.offset) / spark.scale))
    sf = bytearray(8)
    word = int.from_bytes(sf, "little") | ((raw & ((1 << spark.bits) - 1)) << (spark.byte * 8 + spark.bit))
    log.track(p.Frame(0, spark.can_id, word.to_bytes(8, "little")), t)
    for i in range(3):
        for f in isotp(0x7E8, knock_payload(retard_raw=(0xFF, 0xFE, 0xFF, 0xFF, 0xFF, 0xFF))):
            log.track(f, t + i * 0.2)
    for f in isotp(0x7E9, tcu_payload()):
        log.track(f, t + 1)
    log.end_notes()
    log.close()
    knock = (tmp_path / "obd-knock-20261008-120000.csv").read_text().splitlines()
    assert knock[0] == "time," + ",".join(U.KNOCK_COLUMNS) and len(knock) == 4
    spark_deg = spark.decode(word.to_bytes(8, "little"))
    assert knock[1].startswith("1791000000.000,0.00,0.75,0.00") and knock[1].endswith(f",{spark_deg:.2f}")
    tcu = (tmp_path / "obd-tcu-20261008-120000.csv").read_text().splitlines()
    assert tcu[1] == "1791000001.000,2400,50,2300,1000,100,80,2.50,2,2,0"
    text = log.path.read_text()
    assert "# per-cylinder knock (E019): 3 readings in obd-knock-20261008-120000.csv" in text
    assert "# transmission (01A0): 1 readings in obd-tcu-20261008-120000.csv" in text
    # A log with no answers makes no files
    quiet = RollingLog(tmp_path / "q", ["# test"], split_mb=0, stamp="20261008-130000")
    quiet.end_notes()
    quiet.close()
    assert not list((tmp_path / "q").glob("obd-*.csv"))


@pytest.mark.skipif(not CAPTURE.is_file(), reason="capture not here")
def test_the_counts_in_a_real_capture():
    w, n, worst = U.Watch(), {"knock": 0, "tcu": 0}, 0.0
    for _, f in read_replay(CAPTURE):
        got = w.frame(f)
        if got:
            n[got[0]] += 1
            if got[0] == "knock":
                worst = max(worst, max(got[1]["retard"]))
    assert n == {"knock": 437, "tcu": 440} and worst == 6.75


# ---------------------------------------------------------------- asking, against a simulated car

class Car:
    """The ECU (E019 on 0x7E0) and the transmission (01A0 on 0x7E1): each answers a request after `delay` s,
    a long answer's first frame, then the rest once it gets flow control. `refuse` maps a request id to an NRC."""

    def __init__(self, delay=0.01):
        self.delay = delay
        self.sent = []          # frames we sent, with the time
        self.queue = []         # (due time, frame) for the poller
        self.refuse = {}
        self.silent = set()
        self.waiting = {}       # answer id -> the rest of its long answer, until flow control
        self.t = 0.0

    def send(self, frames):     # the link: what the Sender lets out
        for f in frames:
            self.sent.append((self.t, f))
            d = f.data
            if d[0] == 0x30:
                rest = self.waiting.pop(f.addr + 8, [])
                self.queue += [(self.t + 0.001 * (i + 1), x) for i, x in enumerate(rest)]
                continue
            assert d[:2] == b"\x03\x22", f"only 22 reads go out: {d.hex()}"
            if f.addr in self.silent:
                continue
            if f.addr in self.refuse:
                self.queue.append((self.t + self.delay, isotp(f.addr + 8, bytes([0x7F, 0x22, self.refuse[f.addr]]))[0]))
                continue
            payload = knock_payload() if d[2:4] == b"\xe0\x19" else tcu_payload()
            frames_ = isotp(f.addr + 8, payload)
            self.queue.append((self.t + self.delay, frames_[0]))
            self.waiting[f.addr + 8] = frames_[1:]


def run(car, poller, seconds, step=0.002, extra=()):
    """Drives the poller for `seconds` of simulated time; extra: (time, frame) of another tester."""
    extra = sorted(extra, key=lambda x: x[0])
    end = car.t + seconds
    while car.t < end:
        car.t = round(car.t + step, 6)
        while extra and extra[0][0] <= car.t:
            poller.frame(extra.pop(0)[1], car.t)
        due = [x for x in car.queue if x[0] <= car.t]
        car.queue = [x for x in car.queue if x[0] > car.t]
        for _, f in sorted(due, key=lambda x: x[0]):
            poller.frame(f, car.t)
        poller.tick(car.t)


def make(car, *targets):
    return U.UdsPoller(Sender(car, VehicleState()), 0, targets or (U.knock_target(), U.transmission_target()))


def requests(car, addr):
    return [t for t, f in car.sent if f.addr == addr and f.data[0] == 0x03]


def test_round_robin_flow_control_and_the_rate():
    car = Car(delay=0.01)
    poller = make(car)
    run(car, poller, 1.0)
    k, tr = poller.targets
    assert k.answers >= 4 and tr.answers >= 4 and abs(k.answers - tr.answers) <= 1    # turn about
    assert all(f.data[:3] == b"\x30\x00\x00" for _, f in car.sent if f.data[0] == 0x30)   # flow control, nothing else
    assert sum(1 for _, f in car.sent if f.data[0] == 0x30) == k.answers + tr.answers
    times = sorted(requests(car, 0x7E0) + requests(car, 0x7E1))
    assert all(b - a >= 0.01 for a, b in zip(times, times[1:]))                  # one request on the bus at a time
    run(car, poller, 10.0)                                                          # quick answers: faster, to 10 a second
    assert k.period == pytest.approx(U.UdsPoller.MIN_PERIOD) and "a second" in k.status
    first, last = requests(car, 0x7E0)[-11], requests(car, 0x7E0)[-1]
    assert (last - first) / 10 == pytest.approx(0.1, abs=0.01)


def test_another_tester_pauses_that_module():
    car = Car()
    poller = make(car)
    foreign = [(1.0 + i * 0.1, p.Frame(0, 0x7E0, bytes([3, 0x22, 0xE0, 0x19, 0, 0, 0, 0]))) for i in range(30)]   # 1.0-3.9 s
    run(car, poller, 8.0, extra=foreign)
    asked = requests(car, 0x7E0)
    assert not [t for t in asked if 1.0 < t < 3.9 + U.UdsPoller.QUIET]           # none from it starting to 2 s after
    assert [t for t in asked if t > 5.9] and [t for t in requests(car, 0x7E1) if 1.0 < t < 5.9]   # the other module goes on


def test_refusals_back_off_and_stop():
    car = Car()
    car.refuse = {0x7E0: 0x31, 0x7E1: 0x21}          # out of range: stop; busy: slow down
    poller = make(car)
    run(car, poller, 3.0)
    k, tr = poller.targets
    assert k.stopped == "refused: out of range (31)" and len(requests(car, 0x7E0)) == 1
    assert tr.stopped is None and tr.period > U.UdsPoller.START_PERIOD      # busy: slowed down, not stopped
    assert not poller.stopped


def test_silence_gives_up_after_eight_timeouts():
    car = Car()
    car.silent = {0x7E0}
    poller = make(car, U.knock_target())
    run(car, poller, 30.0)
    assert poller.stopped and poller.targets[0].stopped == "no answer 8 times in a row"
    assert len(requests(car, 0x7E0)) == 8


def test_response_pending_waits_longer():
    car = Car()
    poller = make(car, U.knock_target())
    car.refuse = {0x7E0: 0x78}
    run(car, poller, 1.0)
    assert len(requests(car, 0x7E0)) == 1 and poller.in_flight      # still waiting, 2.5 s more each time


def test_only_these_two_reads_are_built():
    assert U.knock_target().request.frame(0).data == bytes([3, 0x22, 0xE0, 0x19, 0, 0, 0, 0])
    assert U.transmission_target().request.frame(0).data == bytes([3, 0x22, 0x01, 0xA0, 0, 0, 0, 0])
    assert U.knock_target().request.target == 0x7E0 and U.transmission_target().request.target == 0x7E1


# ---------------------------------------------------------------- pandacapture obd's poll: PIDs and the reads together

class Link:
    """A panda link for the poll: mode 01 answers from 0x7E8, the reads from Car, another tester on 0x7E0 between
    `foreign` seconds of the poll, and every frame handed to `received` while waiting."""

    def __init__(self, foreign=(0.3, 0.6)):
        import time as _t
        self.time = _t
        self.car = Car(delay=0.005)
        self.t0 = _t.monotonic()
        self.foreign = foreign
        self.received = None
        self.log = []           # (seconds into the poll, frame) sent

    def now(self):
        return self.time.monotonic() - self.t0

    def send(self, frames):
        for f in frames:
            self.log.append((self.now(), f))
            if f.addr == 0x7DF:
                self.car.queue.append((self.now() + 0.003, p.Frame(0, 0x7E8, bytes([4, 0x41, f.data[2], 0x10, 0x20, 0, 0, 0]))))
            else:
                self.car.t = self.now()
                self.car.send([f])

    def wait(self, seconds):
        end = self.now() + seconds
        while True:
            t = self.now()
            a, b = self.foreign
            if a <= t <= b:
                self.received(p.Frame(0, 0x7E0, bytes([2, 0x01, 0x0C, 0, 0, 0, 0, 0])))
            due = [x for x in self.car.queue if x[0] <= t]
            self.car.queue = [x for x in self.car.queue if x[0] > t]
            for _, f in sorted(due, key=lambda x: x[0]):
                self.received(f)
            if t >= end:
                return
            self.time.sleep(0.001)


def test_the_poll_shares_the_bus_and_holds_for_another_tester(monkeypatch, tmp_path):
    import time
    from pandacapture import obd, obd_run
    from pandacapture.obd import Found, Scanner
    monkeypatch.setattr(obd, "ANSWER_WAIT", 0.02)
    monkeypatch.setattr(U.UdsPoller, "QUIET", 0.3)
    link = Link(foreign=(0.4, 0.7))
    scanner = Scanner(link, VehicleState(), (0,), log=lambda s: None)
    uds = U.UdsPoller(scanner.sender, 0, [U.knock_target(), U.transmission_target()])
    tester = {"until": 0.0}

    def received(f):                       # as obd_run.run's: the PID scanner, the tester watch, the reads
        scanner.frame(f)
        now = time.monotonic()
        if 0x7E0 <= f.addr <= 0x7E7:
            tester["until"] = now + U.UdsPoller.QUIET
        uds.frame(f, now)
    link.received = received
    columns = obd_run.Columns(tmp_path / "obd.csv")
    why = obd_run.poll(scanner, [Found(0, 0x0C, (0x7E8,))], columns, 1.6, uds=uds, tester=tester)
    assert why is None
    pids = [t for t, f in link.log if f.addr == 0x7DF]
    knock = [t for t, f in link.log if f.addr == 0x7E0 and f.data[0] == 0x03]
    trans = [t for t, f in link.log if f.addr == 0x7E1 and f.data[0] == 0x03]
    assert pids and knock and trans and uds.answers >= 4
    # Nothing to 0x7E0 and no PIDs from just after the other tester starts until QUIET after it stops ...
    hold = (0.45, 0.7 + 0.3 - 0.05)    # the last of its frames comes just before 0.7 s; timers on Windows slip a little
    assert not [t for t in pids + knock if hold[0] < t < hold[1]]
    # ... while the transmission is still read, and both go on afterwards
    assert [t for t in trans if hold[0] < t < hold[1]] and [t for t in pids if t > 1.05] and [t for t in knock if t > 1.05]
    # Only these ever go out: mode 01 to 7DF, 22 E019 / 22 01A0, and flow control
    for _, f in link.log:
        assert (f.addr == 0x7DF and f.data[1] == 0x01) or f.data[:4] in (b"\x03\x22\xe0\x19", b"\x03\x22\x01\xa0") \
            or f.data[:3] == b"\x30\x00\x00"


# ---------------------------------------------------------------- the dashboard's Read switches

class FakePanda:
    def __init__(self):
        self.errors, self.bus_off = 0, False

    def can_health(self, bus):
        return {"total_errors": self.errors, "bus_off": self.bus_off}


class FakeSource:
    """A panda with PandaCapture firmware: arm/disarm, and a car answering what's sent."""

    def __init__(self, can_transmit=True):
        import time as _t
        self.time = _t
        self.can_transmit = can_transmit
        self.panda = FakePanda()
        self.armed = False
        self.arms = 0
        self.sent = []
        self.car = Car(delay=0.005)

    def arm(self):
        self.armed, self.arms = True, self.arms + 1

    def disarm(self):
        self.armed = False

    def beat(self):
        pass

    def send(self, frames):
        assert self.armed, "nothing goes out unarmed"
        self.sent += frames
        self.car.t = self.time.monotonic()
        self.car.send(frames)

    def read(self):
        now = self.time.monotonic()
        due = [f for t, f in sorted(self.car.queue, key=lambda x: x[0]) if t <= now]
        self.car.queue = [x for x in self.car.queue if x[0] > now]
        return due


class Stand:
    """What the reader hands ActiveReads: the state (bus counts) and a note() into the recording."""
    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.bus_frames = {0: 1000, 1: 10}
        self.notes = []


def steps(reads, source, recorder, stand, seconds):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        reads.step(source, source.read(), recorder, stand.notes.append, stand)
        time.sleep(0.001)


def test_the_read_switches(tmp_path):
    from pandacapture.dashboard import READS_FILE, ActiveReads
    reads, source, stand, rec = ActiveReads(tmp_path), FakeSource(), Stand(), object()
    assert reads.info() == {"knock": False, "transmission": False, "confirmed": False, "running": False,
                            "status": "", "problem": None}
    steps(reads, source, rec, stand, 0.05)
    assert not source.arms                                     # off by default: nothing sent
    reads.set(knock=True, transmission=True)
    steps(reads, source, rec, stand, 0.05)
    assert not source.arms and reads.status.startswith("waiting for TRANSMIT")
    with pytest.raises(ValueError):
        reads.set(confirm="yes")
    reads.set(confirm="transmit")
    assert ActiveReads(tmp_path).want == {"knock": True, "transmission": True}      # kept across launches
    assert not ActiveReads(tmp_path).confirmed                                      # TRANSMIT isn't
    assert (tmp_path / READS_FILE).is_file()
    # Not recording: waits
    steps(reads, source, None, stand, 0.05)
    assert not source.arms and reads.status == "waiting for a recording"
    # Recording: armed, both read, the note says so
    steps(reads, source, rec, stand, 1.2)
    assert source.arms == 1 and source.armed and reads.info()["running"]
    assert any(n.startswith("active polling, not silent: transmit armed") for n in stand.notes)
    assert {f.addr for f in source.sent} >= {0x7E0, 0x7E1} and "a second" in reads.status
    assert all(f.data[:4] in (b"\x03\x22\xe0\x19", b"\x03\x22\x01\xa0") or f.data[:3] == b"\x30\x00\x00"
               for f in source.sent)
    # The recording ends: disarmed, listening again
    steps(reads, source, None, stand, 0.05)
    assert not source.armed and any(n.startswith("reads stopped (waiting for a recording)") for n in stand.notes)
    # Bus trouble stops it for the rest of that recording; the next recording tries again
    steps(reads, source, rec, stand, 0.2)
    assert source.armed
    source.panda.errors += 500
    steps(reads, source, rec, stand, 1.2)
    assert not source.armed and reads.status == "stopped: bus 0 counted 500 errors in a second"
    assert reads.info()["problem"] and source.arms == 2
    steps(reads, source, object(), stand, 0.2)
    assert source.armed and source.arms == 3
    reads.set(knock=False, transmission=False)                  # switched off: disarmed at once
    steps(reads, source, rec, stand, 0.05)
    assert not source.armed


def test_the_read_switches_need_pandacapture_firmware(tmp_path):
    from pandacapture.dashboard import ActiveReads
    reads, source, stand = ActiveReads(tmp_path), FakeSource(can_transmit=False), Stand()
    reads.set(knock=True, confirm="TRANSMIT")
    steps(reads, source, object(), stand, 0.1)
    assert not source.arms and reads.status.startswith("listening only: sending needs PandaCapture firmware")
    class Listening:                                            # the simulator, a replay: nothing to send through
        def read(self):
            return []
    steps(reads, Listening(), object(), stand, 0.05)
    assert reads.status == "listening only: no panda to send through"


def test_the_read_switches_over_http(tmp_path):
    import json
    import urllib.request
    from pandacapture.dashboard import Dashboard
    from pandacapture.sources import SimulatedSource
    dash = Dashboard(SimulatedSource, load_map("kia-stinger-33t-pcan"), port=0, record_dir=tmp_path)
    dash.start()
    try:
        def post(body):
            req = urllib.request.Request(dash.url + "reads", method="POST", headers={"Content-Type": "application/json"},
                                         data=json.dumps(body).encode())
            return json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert post({"knock": True, "confirm": "TRANSMIT"})["reads"]["confirmed"]
        state = json.loads(urllib.request.urlopen(dash.url + "state", timeout=10).read())
        assert state["reads"]["knock"] and not state["reads"]["running"]
        page = urllib.request.urlopen(dash.url, timeout=10).read().decode()
        assert 'id="readKnock"' in page and 'id="readTcu"' in page and "Type TRANSMIT" in page
    finally:
        dash.stop()
