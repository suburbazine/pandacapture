"""Other CAN adapters, through their vendors' Windows drivers: RP1210 (NEXIQ USB-Link 2, Noregon DLA+,
DG DPA and other truck adapters) and J2534 pass-thrus (Tactrix OpenPort, Drew Tech Mongoose, VCX Nano and
most dealer and tuning tools).

The drivers are DLLs, mostly 32-bit, which this 64-bit program can't load. So a small bridge program
(adapter/PandaCaptureAdapter.cs, built 32- and 64-bit) loads the driver and streams the frames here
over a pipe. Unlike a panda, these adapters can't listen silently: they acknowledge frames like any CAN
node, which is harmless at the bus's real bit rate. Nothing is ever sent.
"""

import collections
import os
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import protocol as p
from .sources import SourceError

CONNECT_TIMEOUT = 20.0   # seconds for the driver to open the adapter
PE_MACHINES = {0x14C: "x86", 0x8664: "x64", 0xAA64: "arm64"}


@dataclass
class Adapter:
    kind: str           # "rp1210" or "j2534"
    key: str            # what --adapter takes, e.g. rp1210:NULN2R32:1
    name: str           # e.g. "USB-Link 2,USB"
    vendor: str
    library: str        # RP1210: the API name (NULN2R32); J2534: the DLL's path
    device: int = 0     # RP1210 device id
    arch: str = ""      # the driver DLL's bitness: "x86", "x64", or "" if unknown
    problem: str = ""   # why it can't be used as registered

    def label(self) -> str:
        return f"{self.vendor}: {self.name}" if self.vendor and self.vendor not in self.name else self.name


# ---------------------------------------------------------------- finding the drivers

def windows_dir() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows"))


def pe_machine(path) -> str:
    """The CPU a DLL is built for, from its PE header: "x86", "x64", "arm64" or "" if unreadable."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if head[:2] != b"MZ":
            return ""
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\0\0":
            return ""
        return PE_MACHINES.get(struct.unpack_from("<H", head, pe + 4)[0], "")
    except (OSError, struct.error):
        return ""


def read_ini(path) -> dict:
    """A lenient INI reader (vendors' RP1210 files aren't always valid for configparser):
    {section (lower case): {key (lower case): value}}."""
    sections, current = {}, None
    for raw in Path(path).read_text(encoding="latin-1").splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), {})
        elif current is not None and "=" in line:
            k, v = line.split("=", 1)
            current[k.strip().lower()] = v.strip()
    return sections


def _system_dll(name, windir) -> tuple:
    """Where Windows finds a driver DLL loaded by bare name, and its bitness: a 32-bit program looks in
    SysWOW64, a 64-bit one in System32. Prefers 32-bit, which is what RP1210 vendors ship."""
    file = name if name.lower().endswith(".dll") else name + ".dll"
    for folder in ("SysWOW64", "System32"):
        path = windir / folder / file
        if path.exists():
            return path, pe_machine(path) or ("x86" if folder == "SysWOW64" else "")
    return None, ""


def list_rp1210(windir=None) -> list:
    """RP1210 adapters whose driver lists plain CAN for them, from RP121032.ini and each vendor's INI."""
    windir = Path(windir) if windir else windows_dir()
    master = windir / "RP121032.ini"
    if not master.exists():
        return []
    out = []
    apis = read_ini(master).get("rp1210support", {}).get("apiimplementations", "")
    for api in (a.strip() for a in apis.split(",")):
        if not api:
            continue
        path = windir / f"{api}.ini"
        if not path.exists():
            out.append(Adapter("rp1210", f"rp1210:{api}", api, api, api, problem=f"{path} is missing"))
            continue
        ini = read_ini(path)
        vendor = ini.get("vendorinformation", {}).get("name", api)
        can_devices = set()
        for sec, values in ini.items():
            if sec.startswith("protocolinformation") and values.get("protocolstring", "").upper() == "CAN":
                can_devices |= {int(d) for d in values.get("devices", "").split(",") if d.strip().isdigit()}
        dll, arch = _system_dll(api, windir)
        for sec, values in ini.items():
            if not sec.startswith("deviceinformation"):
                continue
            try:
                dev = int(values.get("deviceid", ""))
            except ValueError:
                continue
            if dev not in can_devices:
                continue
            name = values.get("devicedescription") or values.get("devicename") or f"device {dev}"
            out.append(Adapter("rp1210", f"rp1210:{api}:{dev}", name, vendor, api, dev, arch,
                               "" if dll else f"{api}.dll isn't installed"))
    return out


def list_j2534() -> list:
    """J2534 (04.04) pass-thru devices registered with CAN, from both the 64-bit and 32-bit registry."""
    if os.name != "nt":
        return []
    import winreg
    windir = windows_dir()
    out, seen = [], set()
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\PassThruSupport.04.04", 0,
                                  winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(root, sub) as k:
                        values = {}
                        j = 0
                        while True:
                            try:
                                n, v, _ = winreg.EnumValue(k, j)
                            except OSError:
                                break
                            values[n.lower()] = v
                            j += 1
                except OSError:
                    continue
                if values.get("can") in (0, "0"):
                    continue
                lib = str(values.get("functionlibrary", "") or "")
                name = str(values.get("name") or sub)
                vendor = str(values.get("vendor") or "")
                if (sub, lib) in seen:
                    continue
                seen.add((sub, lib))
                arch, problem = "", ""
                if not lib:
                    problem = "its registration names no driver DLL (FunctionLibrary); give the DLL: --adapter j2534:PATH"
                else:
                    real = Path(lib)
                    # This 64-bit program sees the real System32; a 32-bit driver's "system32" is SysWOW64
                    if view == winreg.KEY_WOW64_32KEY and real.parent.name.lower() == "system32":
                        real = windir / "SysWOW64" / real.name
                    arch = pe_machine(real)
                    if not real.exists():
                        problem = f"{lib} isn't installed"
                out.append(Adapter("j2534", f"j2534:{sub}", name, vendor, lib, arch=arch, problem=problem))
    return out


def list_adapters() -> list:
    return list_rp1210() + list_j2534()


def find_adapter(spec, adapters=None) -> Adapter:
    """--adapter's value: rp1210:API:DEVICE, j2534:REGISTRY NAME, j2534:PATH.dll, or words from a name
    shown by `pandacapture list` (e.g. "USB-Link 2,USB"), matching exactly one adapter."""
    spec = spec.strip()
    if spec.lower().startswith("j2534:") and spec.lower().endswith(".dll"):
        path = spec[6:]
        if not Path(path).exists():
            raise SourceError(f"{path} doesn't exist")
        return Adapter("j2534", spec, Path(path).name, "", path, arch=pe_machine(path))
    adapters = list_adapters() if adapters is None else adapters
    exact = [a for a in adapters if a.key.lower() == spec.lower()]
    if exact:
        return exact[0]
    words = spec.lower()
    found = [a for a in adapters if words in f"{a.key} {a.label()}".lower()]
    usable = [a for a in found if not a.problem]
    if len(usable) == 1 or (len(found) == 1):
        return (usable or found)[0]
    if not found:
        raise SourceError(f"No RP1210 or J2534 adapter matches \"{spec}\". See: pandacapture list")
    choices = "\n    ".join(f"--adapter \"{a.key}\"   {a.label()}" for a in found)
    raise SourceError(f"Several adapters match \"{spec}\"; choose one:\n    {choices}")


# ---------------------------------------------------------------- the bridge

def bridge_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "pandacapture" / "adapter_bin"


def bridge_exe(arch) -> Path:
    exe = bridge_dir() / f"pandacapture-adapter-{arch}.exe"
    if not exe.exists():
        raise SourceError(f"{exe.name} is missing: build it with python adapter/build.py")
    return exe


def bridge_args(adapter: Adapter, kbps) -> list:
    if adapter.kind == "rp1210":
        baud = str(kbps) if kbps else "Auto"
        # The channel form is what NEXIQ's tools use; plain CAN suits drivers with one channel
        return ["rp1210", adapter.library, str(adapter.device), f"CAN:Channel=1;Baud={baud}", f"CAN:Baud={baud}"]
    if not kbps:
        raise SourceError("A J2534 adapter needs the bus's bit rate (e.g. --bitrate 500): it acknowledges "
                          "frames, so it can't try rates on a live bus the way a silent panda can.")
    return ["j2534", adapter.library, str(kbps)]


class Records:
    """Parses the bridge's output stream (see PandaCaptureAdapter.cs) into frames and messages."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        out, b, i = [], self.buf, 0
        while i < len(b):
            kind = b[i]
            if kind == ord("F"):
                if len(b) - i < 10 or len(b) - i < 10 + b[i + 9]:
                    break
                _, raw_id, n = struct.unpack_from("<IIB", b, i + 1)
                ext = bool(raw_id & 0x80000000)
                out.append(("F", p.Frame(0, raw_id & 0x1FFFFFFF, bytes(b[i + 10:i + 10 + n]), extended=ext, fd=n > 8)))
                i += 10 + n
            elif kind in b"INE":
                if len(b) - i < 3:
                    break
                n = struct.unpack_from("<H", b, i + 1)[0]
                if len(b) - i < 3 + n:
                    break
                out.append((chr(kind), bytes(b[i + 3:i + 3 + n]).decode("utf-8", "replace")))
                i += 3 + n
            else:
                raise SourceError(f"the adapter bridge sent garbage (byte 0x{kind:02X})")
        del self.buf[:i]
        return out


class AdapterSource:
    """Frames from an RP1210 or J2534 adapter, as bus 0 (can0)."""

    def __init__(self, adapter: Adapter, kbps=None, log=print):
        if adapter.problem:
            raise SourceError(f"{adapter.label()}: {adapter.problem}")
        if adapter.arch not in ("x86", "x64"):
            raise SourceError(f"{adapter.label()}: can't tell whether its driver ({adapter.library}) is 32- or "
                              f"64-bit" + (f" (it's {adapter.arch})" if adapter.arch else ""))
        self.adapter = adapter
        self.kind = "Adapter"   # for messages: "Adapter error: ..." rather than "Panda error: ..."
        self.kbps = kbps
        self.serial = adapter.key
        self.rates = {0: kbps} if kbps else {}
        self.uptime = self.voltage = None
        self.frames = collections.deque()
        self.ready = threading.Condition()
        self.error = None
        self.info = None
        args = [str(bridge_exe(adapter.arch))] + bridge_args(adapter, kbps)
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.thread = threading.Thread(target=self._pump, args=(self.proc.stdout,), daemon=True,
                                       name="pandacapture-adapter")
        self.thread.start()
        deadline = time.monotonic() + CONNECT_TIMEOUT
        with self.ready:
            while self.info is None and self.error is None and time.monotonic() < deadline:
                self.ready.wait(0.2)
        if self.info is None:
            self.close()
            raise SourceError(self.error or f"{adapter.label()} didn't answer within {CONNECT_TIMEOUT:g} s")
        rate = f"{kbps} kbit/s" if kbps else "bit rate found by the adapter"
        self.description = f"{adapter.label()} via {self.info}"
        self.header = [f"bus 0 (can0): {rate}",
                       "the adapter acknowledges frames like any CAN node (RP1210/J2534 adapters can't listen "
                       "silently); nothing is transmitted"]

    def _pump(self, stream):
        records = Records()
        try:
            while True:
                data = stream.read1(65536)
                if not data:
                    break
                got = records.feed(data)
                with self.ready:
                    for kind, value in got:
                        if kind == "F":
                            self.frames.append(value)
                        elif kind == "I":
                            self.info = value
                        elif kind == "E":
                            self.error = value
                    self.ready.notify_all()
        except (OSError, ValueError, SourceError) as e:
            with self.ready:
                self.error = self.error or str(e)
        with self.ready:
            if self.error is None:
                self.error = "the adapter bridge stopped"
            self.ready.notify_all()

    def read(self) -> list:
        with self.ready:
            if not self.frames and self.error is None:
                self.ready.wait(0.02)
            if self.frames:
                out = list(self.frames)
                self.frames.clear()
                return out
            if self.error is not None:
                raise SourceError(self.error)
            return []

    def health(self):
        return None

    def close(self):
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        self.proc = None
        try:
            proc.stdin.close()   # the bridge disconnects from the driver and exits
        except OSError:
            pass
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)
