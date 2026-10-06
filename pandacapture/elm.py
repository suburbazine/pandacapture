"""ELM327 OBD adapters (and the many clones), for pandacapture obd: USB, Bluetooth (its serial COM
port) or WiFi (socket://192.168.0.10:35000).

An ELM327 is a scan tool, not a bus recorder: it sends one OBD request at a time and returns the
answers, which is exactly what the scan and the poll need. It can't record a busy bus (its monitor
mode drops frames), so it isn't offered for capture or the dashboard.

Only CAN cars (OBD protocols 6-9, every car sold in the US since 2008 and most before): the scan
reads answers by their CAN ids. Headers are switched on so each answer says which module sent it.
"""

import time

from . import protocol as p

BAUDS = (38400, 115200, 9600, 230400, 500000)   # USB ELMs; Bluetooth and WiFi ignore the rate
CAN_PROTOCOLS = {
    "6": "ISO 15765-4 CAN, 11-bit ids, 500 kbit/s",
    "7": "ISO 15765-4 CAN, 29-bit ids, 500 kbit/s",
    "8": "ISO 15765-4 CAN, 11-bit ids, 250 kbit/s",
    "9": "ISO 15765-4 CAN, 29-bit ids, 250 kbit/s",
}
FAILURES = ("UNABLE TO CONNECT", "CAN ERROR", "BUS ERROR", "BUS INIT", "FB ERROR", "DATA ERROR", "BUFFER FULL",
            "LV RESET", "ACT ALERT", "STOPPED", "?")


class ElmError(Exception):
    pass


def hexbytes(tokens) -> bytes:
    return bytes(int(t, 16) for t in tokens)


def parse_line(line: str, extended: bool):
    """(CAN id, data) from one answer line with headers on, spaces on or off; None if it isn't one.
    29-bit OBD answers (18DAF1xx) get the matching 11-bit id (7E8 + xx - 10), so modules read the same."""
    line = line.strip().upper()
    if not line or any(c not in "0123456789ABCDEF " for c in line):
        return None
    if " " in line:
        tokens = line.split()
        if extended:
            if len(tokens) < 5 or any(len(t) != 2 for t in tokens):
                return None
            can_id, data = int("".join(tokens[:4]), 16), tokens[4:]
        else:
            if len(tokens) < 2 or len(tokens[0]) != 3 or any(len(t) != 2 for t in tokens[1:]):
                return None
            can_id, data = int(tokens[0], 16), tokens[1:]
    else:
        head = 8 if extended else 3
        if len(line) <= head or (len(line) - head) % 2:
            return None
        can_id = int(line[:head], 16)
        data = [line[i:i + 2] for i in range(head, len(line), 2)]
    if extended:
        if can_id >> 8 != 0x18DAF1 or not 0x10 <= can_id & 0xFF <= 0x17:
            return None
        can_id = 0x7E8 + (can_id & 0xFF) - 0x10
    return can_id, hexbytes(data)


class Elm:
    """One ELM327, set up for OBD with headers on. port: COM5, /dev/ttyUSB0, /dev/rfcomm0, or a pyserial
    URL such as socket://192.168.0.10:35000 for WiFi adapters."""

    def __init__(self, port, protocol="0", log=print, serial_factory=None):
        self.port = port
        self.log = log
        self.ser = self._open(port, serial_factory)
        try:
            self._setup(protocol)
        except BaseException:
            self.close()
            raise

    def _open(self, port, factory):
        import serial
        if factory is not None:
            return factory(port)
        if "://" in port:
            try:
                ser = serial.serial_for_url(port, timeout=0.05)
            except (serial.SerialException, OSError, ValueError) as e:
                raise ElmError(f"Can't open {port}: {e}") from None
            self.ser = ser
            if self._hello():
                return ser
            ser.close()
            raise ElmError(f"No ELM327 answered at {port}.")
        last = None
        for baud in BAUDS:
            try:
                ser = serial.Serial(port, baud, timeout=0.05)
            except (serial.SerialException, OSError) as e:
                raise ElmError(f"Can't open {port}: {e}") from None
            self.ser = ser
            try:
                if self._hello():
                    return ser
            except ElmError as e:
                last = e
            ser.close()
        raise ElmError(f"No ELM327 answered on {port} at {', '.join(map(str, BAUDS))} baud"
                       + (f" ({last})" if last else "") + ". Is it the adapter's port, and is the adapter powered?")

    def _hello(self) -> bool:
        self.ser.reset_input_buffer()
        self.ser.write(b"\r")              # finish anything half-typed
        self._read(0.3)
        return any("ELM" in line.upper() for line in self.command("ATZ", 2.5))

    def _read(self, timeout) -> str:
        buf = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if buf.rstrip().endswith(b">"):
                    break
        return buf.replace(b"\x00", b"").decode("ascii", "replace")

    def command(self, text, timeout=2.0) -> list:
        """Sends a command and returns the reply lines (no echo, no prompt)."""
        self.ser.write(text.encode("ascii") + b"\r")
        reply = self._read(timeout)
        if not reply.rstrip().endswith(">"):
            raise ElmError(f"No answer to {text} within {timeout:g} s")
        lines = [ln.strip() for ln in reply.replace("\r", "\n").split("\n")]
        return [ln for ln in lines if ln and ln != ">" and ln.rstrip(">").strip() != text and ln != text]

    def _setup(self, protocol):
        self.version = next((ln for ln in self.command("ATI") if "ELM" in ln.upper()), "ELM327")
        for cmd in ("ATE0", "ATL0", "ATS1", "ATH1", "ATAT1", f"ATSP{protocol}"):
            reply = self.command(cmd)
            if not any("OK" in ln.upper() for ln in reply):
                raise ElmError(f"The adapter refused {cmd}: {' '.join(reply) or 'no reply'}")
        # The first request makes an auto-protocol adapter find the car's protocol
        first = self.command("0100", 15)
        number = "".join(self.command("ATDPN")).strip().upper().lstrip("A")
        if number not in CAN_PROTOCOLS:
            if any(f in " ".join(first).upper() for f in ("UNABLE TO CONNECT", "NO DATA", "SEARCHING")):
                raise ElmError("The adapter didn't reach the car: is the key on? "
                               f"({' '.join(first) or 'no reply'})")
            raise ElmError(f"The car speaks OBD protocol {number or '?'}, not CAN: the scan reads CAN answers only.")
        self.protocol = number
        self.extended = number in ("7", "9")
        self.description = f"{self.version} on {self.port}, {CAN_PROTOCOLS[number]}"

    def request(self, payload: bytes, timeout=1.0) -> list:
        """One OBD request (e.g. 01 0C); the answer frames, as (CAN id, data with its ISO-TP byte)."""
        reply = self.command(payload.hex().upper(), timeout)
        text = " ".join(reply).upper()
        if "NO DATA" in text and len(reply) == 1:
            return []
        for f in FAILURES:
            if text.startswith(f) or f" {f}" in text and f != "?":
                raise ElmError(f"The adapter reported {text}")
        out = []
        for line in reply:
            parsed = parse_line(line, self.extended)
            if parsed:
                out.append(parsed)
        return out

    def close(self):
        ser = getattr(self, "ser", None)
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
            self.ser = None


class ElmLink:
    """The scanner's link over an ELM327: send() makes the request and waits for the prompt, wait() hands
    the answers over as CAN frames (bus 0) and says they're complete."""

    handles_flow_control = True   # an ELM327 does ISO-TP itself and prints every frame of a long answer

    def __init__(self, elm: Elm, on_frame):
        self.elm = elm
        self.on_frame = on_frame
        self.pending = []
        self.answers_complete = False

    def send(self, frames):
        from .policy import FUNCTIONAL, PolicyRefused, recognize
        for f in frames:
            try:
                request = recognize(f)        # only what the diagnostic policy allows
            except PolicyRefused as ex:
                raise ElmError(f"Refused: {ex}") from None
            other = request.target != FUNCTIONAL and not 0x7E0 <= request.target <= 0x7E7
            if request.target != FUNCTIONAL:
                self.elm.command(f"ATSH{request.target:03X}")
            if other:   # an ELM327 only listens to 7E8-7EF by itself
                self.elm.command(f"ATCRA{request.target + 8:03X}")
            self.answers_complete = False
            try:
                self.pending += [p.Frame(0, can_id, data) for can_id, data in self.elm.request(request.payload)]
            finally:
                self.answers_complete = True
                if other:
                    self.elm.command("ATAR")
                if request.target != FUNCTIONAL:
                    self.elm.command("ATSH7DF")

    def wait(self, seconds):
        frames, self.pending = self.pending, []
        for f in frames:
            self.on_frame(f)
        if not frames and not self.answers_complete:
            time.sleep(seconds)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


def list_ports() -> list:
    """(port, description) of serial ports, for pandacapture list."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(pt.device, pt.description or "") for pt in sorted(list_ports.comports(), key=lambda x: x.device)]
