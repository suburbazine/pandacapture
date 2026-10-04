"""Reading candump logs. Pure Python (no USB), so capture bundles can carry it with the analysis tools."""

import re
from pathlib import Path

from . import protocol as p


def read_replay(path, bus_map=None, ids=None) -> list:
    """(seconds from first frame, Frame) from a candump log; buses remapped, optionally filtered by id."""
    out = []
    first = None
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("("):
            continue
        try:
            stamp, iface, text = line.split(None, 2)
            t = float(stamp.strip("()"))
            m = re.search(r"(\d+)$", iface)
            bus = int(m.group(1)) if m else 0
            frame = p.parse_frame_text(text.split()[0])
        except (ValueError, p.PacketError):
            continue
        if ids and frame.addr not in ids:
            continue
        if bus_map:
            if bus not in bus_map:
                continue
            bus = bus_map[bus]
        if bus >= p.CAN_BUSES:
            continue
        frame = p.Frame(bus, frame.addr, frame.data, frame.extended, frame.fd)
        first = t if first is None else first
        out.append((t - first, frame))
    return out
