"""Reading candump logs. Pure Python (no USB), so capture bundles can carry it with the analysis tools."""

import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import protocol as p


LOG_EXTENSIONS = (".log", ".txt", ".candump")


def zip_logs(path) -> list:
    """The entries of a zip that can be candump logs, in the zip's order (CSVs, JSON, Python are something else)."""
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if not n.endswith("/") and not n.startswith("__MACOSX/")
                and n.lower().endswith(LOG_EXTENSIONS)]


def zip_log(path) -> str:
    """The log a zip is played or read from: its only log, else a bundle's capture.log; with several and no
    capture.log it says which, to unzip and pick one."""
    logs = zip_logs(path)
    named = [n for n in logs if n.rsplit("/", 1)[-1] == "capture.log"]
    pick = logs[0] if len(logs) == 1 else named[0] if len(named) == 1 else None
    if pick is None:
        raise ValueError(f"{Path(path).name} holds " + (f"several logs ({', '.join(logs)}): unzip it and pick one"
                                                         if logs else "no candump log"))
    return pick


def log_text(path) -> str:
    """A log's text, or a zip's (a shared capture with its OBD CSVs, or an exported bundle; see zip_log)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            return z.read(zip_log(path)).decode("utf-8", errors="replace")
    return Path(path).read_text(encoding="utf-8", errors="replace")


def replay_file(path) -> Path:
    """A log the replay can seek in: the file itself, or a zip's log (see zip_log) copied to a temporary file,
    which the caller deletes when done with it."""
    if not zipfile.is_zipfile(path):
        return Path(path)
    pick = zip_log(path)
    fd, tmp = tempfile.mkstemp(prefix="pandacapture-replay-", suffix=".log")
    with os.fdopen(fd, "wb") as out, zipfile.ZipFile(path) as z, z.open(pick) as src:
        shutil.copyfileobj(src, out, 1 << 20)
    return Path(tmp)


def read_replay(path, bus_map=None, ids=None) -> list:
    """(seconds from first frame, Frame) from a candump log, or a zip holding one (see log_text); buses
    remapped, optionally filtered by id."""
    out = []
    first = None
    for line in log_text(path).splitlines():
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
