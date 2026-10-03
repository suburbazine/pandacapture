"""A panda over USB: its firmware (app) or its bootstub, which is the panda's own flasher."""

import sys
from dataclasses import dataclass

import usb1

from . import protocol as p
from .usbdev import DISCONNECT_ERRORS, UsbError, context, explain

TIMEOUT_MS = 5000


@dataclass
class DeviceEntry:
    kind: str     # "panda", "bootstub" or "dfu"
    serial: str
    note: str = ""


def list_pandas() -> list:
    """Pandas running firmware or their bootstub, by USB serial (the MCU UID)."""
    found = []
    ctx = context()
    try:
        for dev in ctx.getDeviceList(skip_on_error=True):
            if dev.getVendorID() in p.PANDA_VIDS and dev.getProductID() in (p.PID_APP, p.PID_BOOTSTUB):
                kind = "bootstub" if dev.getProductID() == p.PID_BOOTSTUB else "panda"
                try:
                    found.append(DeviceEntry(kind, dev.getSerialNumber()))
                except usb1.USBError as e:
                    found.append(DeviceEntry(kind, "?", str(explain(e, "reading its serial"))))
    finally:
        ctx.close()
    return found


class Panda:
    def __init__(self, ctx, handle, serial, bootstub):
        self._ctx = ctx
        self._h = handle
        self.serial = serial
        self.bootstub = bootstub
        self._health_parser = None

    @classmethod
    def open(cls, serial=None, bootstub_ok=True) -> "Panda":
        ctx = context()
        try:
            candidates = []
            for dev in ctx.getDeviceList(skip_on_error=True):
                if dev.getVendorID() not in p.PANDA_VIDS or dev.getProductID() not in (p.PID_APP, p.PID_BOOTSTUB):
                    continue
                try:
                    this_serial = dev.getSerialNumber()
                except usb1.USBError as e:
                    raise explain(e, "reading the panda's serial") from None
                if serial is None or this_serial == serial:
                    candidates.append((dev, this_serial))
            if not candidates:
                raise UsbError("No panda found" + (f" with serial {serial}" if serial else "") + ".", disconnected=True)
            if len(candidates) > 1:
                raise UsbError("Several pandas connected; choose one with --serial:\n    " +
                               "\n    ".join(s for _, s in candidates))
            dev, this_serial = candidates[0]
            bootstub = dev.getProductID() == p.PID_BOOTSTUB
            if bootstub and not bootstub_ok:
                raise UsbError("The panda is in its bootstub (flasher), not running firmware. "
                               "Flash it with: pandacapture flash")
            try:
                handle = dev.open()
                if sys.platform.startswith("linux"):
                    handle.setAutoDetachKernelDriver(True)
                handle.claimInterface(0)
            except usb1.USBError as e:
                raise explain(e, "opening the panda") from None
            return cls(ctx, handle, this_serial, bootstub)
        except BaseException:
            ctx.close()
            raise

    def close(self):
        if self._h is not None:
            try:
                self._h.releaseInterface(0)
            except usb1.USBError:
                pass
            try:
                self._h.close()
            except usb1.USBError:
                pass
            self._h = None
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- raw transfers ----

    def control_read(self, request, value=0, index=0, length=64, timeout=TIMEOUT_MS) -> bytes:
        try:
            return bytes(self._h.controlRead(p.REQUEST_IN, request, value, index, length, timeout))
        except usb1.USBError as e:
            raise explain(e, f"panda request 0x{request:02X}") from None

    def control_write(self, request, value=0, index=0, data=b"", timeout=TIMEOUT_MS, expect_disconnect=False):
        try:
            self._h.controlWrite(p.REQUEST_OUT, request, value, index, data, timeout)
        except usb1.USBError as e:
            if expect_disconnect and isinstance(e, DISCONNECT_ERRORS + (usb1.USBErrorTimeout,)):
                return
            raise explain(e, f"panda request 0x{request:02X}") from None

    def read_can(self, timeout_ms=20) -> bytes:
        """Whatever CAN packets the panda has queued (it answers at once, empty when it has none)."""
        try:
            return bytes(self._h.bulkRead(p.EP_CAN_IN, 16384, timeout_ms))
        except usb1.USBErrorTimeout as e:
            return bytes(getattr(e, "received", b"") or b"")
        except usb1.USBError as e:
            raise explain(e, "reading CAN from the panda") from None

    def write_can(self, chunk: bytes, timeout_ms=100):
        """Sends packed CAN packets. The panda NAKs while its transmit queue is full."""
        sent = 0
        try:
            while sent < len(chunk):
                sent += self._h.bulkWrite(p.EP_CAN_OUT, chunk[sent:], timeout_ms)
        except usb1.USBErrorTimeout:
            raise UsbError("the panda's transmit queue stayed full (is anything acknowledging on the bus?)") from None
        except usb1.USBError as e:
            raise explain(e, "sending CAN to the panda") from None

    # ---- information ----

    def version(self) -> str:
        return self.control_read(p.REQ_VERSION, length=0x40).decode("ascii", "replace").rstrip("\0")

    def hw_type(self) -> int:
        d = self.control_read(p.REQ_HW_TYPE, length=0x40)
        return d[0] if d else 0

    def packet_versions(self):
        """(health, CAN) packet layout versions. Recent firmware sends two 32-bit hashes; firmware
        from before that (like the F4 build) sends small numbers, one byte each."""
        d = self.control_read(p.REQ_PACKET_VERSIONS, length=8)
        if len(d) == 8:
            return tuple(int.from_bytes(d[i:i + 4], "little") for i in (0, 4))
        if len(d) >= 2:
            return d[0], d[1]
        return 0, 0

    def health(self) -> dict:
        """The health packet, for the layouts PandaCapture knows; UsbError for others."""
        if self._health_parser is None:
            health_version = self.packet_versions()[0]
            if health_version == 16:
                self._health_parser = (p.parse_legacy_health_v16, p.LEGACY_HEALTH_V16.size)
            elif health_version > 0xFF:
                self._health_parser = (p.parse_health, p.HEALTH_STRUCT.size)
            else:
                raise UsbError(f"this firmware's health packet (version {health_version}) isn't one PandaCapture reads")
        parse, size = self._health_parser
        d = self.control_read(p.REQ_HEALTH, length=size)
        if len(d) < size:
            raise UsbError(f"short health packet ({len(d)} of {size} bytes)")
        return parse(d)

    @property
    def mcu(self):
        """The panda's flash layout and firmware target, or None for hardware PandaCapture can't flash."""
        return p.MCU_BY_HW.get(self.hw_type())

    def can_health(self, bus) -> dict:
        d = self.control_read(p.REQ_CAN_HEALTH, bus, length=p.CAN_HEALTH_STRUCT.size)
        if len(d) < p.CAN_HEALTH_STRUCT.size:
            raise UsbError(f"short CAN health packet ({len(d)} bytes): older firmware")
        return p.parse_can_health(d)

    def signature(self) -> bytes:
        return self.control_read(p.REQ_SIGNATURE_1, length=64) + self.control_read(p.REQ_SIGNATURE_2, length=64)

    # ---- configuration ----

    def set_safety(self, mode, param=0):
        self.control_write(p.REQ_SET_SAFETY, mode, param)

    def set_can_speed(self, bus, kbps):
        if kbps not in p.CAN_SPEEDS:
            raise ValueError(f"{kbps} kbit/s isn't a rate the panda supports: {p.CAN_SPEEDS}")
        self.control_write(p.REQ_SET_CAN_SPEED, bus, int(kbps * 10))

    def set_data_speed(self, bus, kbps):
        if kbps not in p.DATA_SPEEDS:
            raise ValueError(f"{kbps} kbit/s isn't a CAN FD data rate the panda supports: {p.DATA_SPEEDS}")
        self.control_write(p.REQ_SET_DATA_SPEED, bus, int(kbps * 10))

    def set_canfd_auto(self, bus, on):
        self.control_write(p.REQ_SET_CANFD_AUTO, bus, int(on))

    def set_obd(self, on):
        self.control_write(p.REQ_SET_OBD, int(on))

    def reset_comms(self):
        self.control_write(p.REQ_CAN_RESET_COMMS)

    def clear_rx(self):
        self.control_write(p.REQ_CAN_CLEAR, 0xFFFF)

    def heartbeat(self, engaged=True):
        self.control_write(p.REQ_HEARTBEAT, int(engaged))

    def disable_heartbeat(self):
        self.control_write(p.REQ_DISABLE_HEARTBEAT)

    def set_power_save(self, on):
        self.control_write(p.REQ_SET_POWER_SAVE, int(on))

    # ---- resets and flashing ----

    def reset(self, into="firmware"):
        """Restart into the firmware, the bootstub, or the STM32 ROM bootloader (DFU). Closes this handle."""
        if into == "firmware":
            self.control_write(p.REQ_RESET, expect_disconnect=True, timeout=2000)
        else:
            self.control_write(p.REQ_ENTER_BOOTLOADER, 1 if into == "bootstub" else 0,
                               expect_disconnect=True, timeout=2000)
        self.close()

    def flasher_present(self) -> bool:
        return self.control_read(p.REQ_FLASHER_ECHO, length=0xC)[4:8] == b"\xde\xad\xd0\x0d"

    def flash_unlock(self):
        self.control_write(p.REQ_FLASH_UNLOCK)

    def flash_erase(self, sector):
        if not 1 <= sector <= 6:
            raise ValueError(f"sector {sector} isn't an app sector")
        self.control_write(p.REQ_FLASH_ERASE, sector)

    def flash_write(self, data: bytes, chunk: int):
        try:
            for i in range(0, len(data), chunk):
                self._h.bulkWrite(p.EP_FLASH_OUT, data[i:i + chunk], TIMEOUT_MS)
        except usb1.USBError as e:
            raise explain(e, "writing firmware to the bootstub") from None
