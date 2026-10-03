"""pandacapture selftest: checks the packet decoder and a short simulated capture, no panda needed."""

import tempfile
from pathlib import Path

from . import protocol as p
from .capture import CaptureOptions, Console, capture, candump_line
from .sources import SimulatedSource


class _Quiet:
    def write(self, _s):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False


def run() -> int:
    failures = 0

    def check(ok, what):
        nonlocal failures
        print(("ok    " if ok else "FAIL  ") + what)
        failures += 0 if ok else 1

    std = p.Frame(0, 0x316, bytes(range(1, 9)))
    ext = p.Frame(2, 0x18DB33F1, bytes([2, 1, 0x0C]), extended=True)
    fd = p.Frame(1, 0x1A0, bytes(range(64)), fd=True)
    stream = b"".join(p.pack_frame(f) for f in (std, ext, fd))
    u = p.CanUnpacker()
    check(u.feed(stream) == [std, ext, fd], "standard, extended and CAN FD packets decode")
    u = p.CanUnpacker()
    got = u.feed(stream[:10]) + u.feed(stream[10:41]) + u.feed(stream[41:])
    check(got == [std, ext, fd], "packets split across USB transfers decode")
    bad = bytearray(p.pack_frame(std))
    bad[7] ^= 0xFF
    u = p.CanUnpacker()
    check(u.feed(bytes(bad)) == [] and u.bad_checksums == 1, "a corrupted packet is dropped")
    check(candump_line(1.5, std) == "(1.500000) can0 316#0102030405060708", "candump line")
    check(candump_line(1.5, ext) == "(1.500000) can2 18DB33F1#02010C", "candump line, extended id")
    check(p.parse_frame_text("7DF#0201050000000000") == p.Frame(0, 0x7DF, bytes([2, 1, 5, 0, 0, 0, 0, 0])),
          "frame text parses")
    check(p.HEALTH_STRUCT.size <= 64 and p.CAN_HEALTH_STRUCT.size <= 64, "health packets fit a control transfer")

    with tempfile.TemporaryDirectory() as d:
        console = Console(_Quiet())
        rc = capture(lambda: SimulatedSource(), CaptureOptions(out_dir=d, seconds=1.2, stamp="selftest"), console)
        text = (Path(d) / "capture-selftest.log").read_text(encoding="utf-8")
        frames = [line for line in text.splitlines() if line.startswith("(")]
        check(rc == 0 and len(frames) > 300 and " can0 316#" in text and " can1 18FEF100#" in text,
              f"simulated capture ({len(frames)} frames)")

    print("All checks passed." if failures == 0 else f"{failures} check(s) failed.")
    return 0 if failures == 0 else 1
