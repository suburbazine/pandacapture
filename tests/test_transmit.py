import pytest

from frostcapture import protocol as p
from frostcapture import transmit
from frostcapture.transmit import ArmedPanda, TransmitRefused, acknowledge, read_replay


def test_acknowledge_needs_the_word():
    shown = []
    acknowledge(False, "3 frames", ask=lambda _: "TRANSMIT", out=shown.append)
    assert transmit.WARNING in shown and "3 frames\n" in shown
    for answer in ("", "transmit", "yes", "TRANSMIT now"):
        with pytest.raises(TransmitRefused):
            acknowledge(False, ask=lambda _, a=answer: a, out=lambda _: None)


def test_acknowledge_eof_refuses():
    def eof(_):
        raise EOFError

    with pytest.raises(TransmitRefused):
        acknowledge(False, ask=eof, out=lambda _: None)


def test_flag_still_shows_warning():
    shown = []
    acknowledge(True, ask=lambda _: pytest.fail("asked"), out=shown.append)
    assert transmit.WARNING in shown


class FakePanda:
    def __init__(self, version="FROSTCAPTURE-a-b-DEBUG", arms=True):
        self._version = version
        self.arms = arms
        self.mode = p.SAFETY_SILENT
        self.calls = []
        self.written = b""
        self.echo = b""

    def version(self):
        return self._version

    def heartbeat(self, engaged=True):
        self.calls.append("heartbeat")

    def set_safety(self, mode, param=0):
        self.calls.append(("safety", mode, param))
        firmware_accepts = mode != p.SAFETY_ALLOUTPUT or (self.arms and param == p.FROSTCAPTURE_TX_ARM)
        self.mode = mode if firmware_accepts else p.SAFETY_SILENT

    def health(self):
        return {"safety_mode": self.mode}

    def reset_comms(self):
        pass

    def clear_rx(self):
        pass

    def write_can(self, chunk):
        assert self.mode == p.SAFETY_ALLOUTPUT, "wrote while not armed"
        self.written += chunk
        # the panda echoes what went out as 'returned'
        for f in p.CanUnpacker().feed(chunk):
            pkt = bytearray(p.pack_frame(f))
            pkt[1] |= 2
            pkt[5] ^= 2
            self.echo += bytes(pkt)

    def read_can(self, timeout_ms=10):
        out, self.echo = self.echo, b""
        return out


def test_arm_send_disarm():
    pd = FakePanda()
    frame = p.Frame(0, 0x7DF, bytes([2, 1, 0x0C]))
    with ArmedPanda(pd) as armed:
        assert ("safety", p.SAFETY_ALLOUTPUT, p.FROSTCAPTURE_TX_ARM) in pd.calls
        armed.send([frame])
        assert armed.returned == 1
    assert pd.mode == p.SAFETY_SILENT
    assert p.CanUnpacker().feed(pd.written) == [frame]


def test_disarms_after_error():
    pd = FakePanda()
    with pytest.raises(RuntimeError):
        with ArmedPanda(pd):
            raise RuntimeError("boom")
    assert pd.calls[-1] == ("safety", p.SAFETY_SILENT, 0)


def test_refuses_stock_firmware():
    pd = FakePanda(version="v1.0-RELEASE")
    with pytest.raises(TransmitRefused, match="not FrostCapture firmware"):
        with ArmedPanda(pd):
            pass
    assert not any(c[0] == "safety" and c[1] == p.SAFETY_ALLOUTPUT for c in pd.calls if isinstance(c, tuple))


def test_refuses_when_firmware_does_not_arm():
    pd = FakePanda(arms=False)
    with pytest.raises(TransmitRefused, match="didn't arm"):
        with ArmedPanda(pd):
            pass
    assert pd.mode == p.SAFETY_SILENT


def test_wait_keeps_heartbeat(monkeypatch):
    pd = FakePanda()
    with ArmedPanda(pd) as armed:
        before = pd.calls.count("heartbeat")
        armed.wait(0.6)
        assert pd.calls.count("heartbeat") - before >= 2


def test_read_replay(tmp_path):
    log = tmp_path / "c.log"
    log.write_text("# FrostCapture candump log\n"
                   "(100.000000) can0 316#0011\n"
                   "(100.010000) can1 329#22\n"
                   "# marker 1 (100.02)\n"
                   "(100.050000) can0 18FEF100#01\n"
                   "(100.060000) can5 123#01\n"
                   "garbage\n")
    frames = read_replay(log)
    assert [(round(t, 3), f.bus, f.addr) for t, f in frames] == [(0, 0, 0x316), (0.01, 1, 0x329), (0.05, 0, 0x18FEF100)]
    assert frames[2][1].extended
    mapped = read_replay(log, bus_map={1: 2})
    assert [(f.bus, f.addr) for _, f in mapped] == [(2, 0x329)]
    only = read_replay(log, ids={0x316})
    assert [f.addr for _, f in only] == [0x316]
