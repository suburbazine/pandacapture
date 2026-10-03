"""The STM32 ROM bootloader over USB DFU (ST's DfuSe commands), on an STM32H7 or STM32F4 panda.

Only used to write the bootstub, the one part of a panda's flash the panda can't rewrite itself.
Follows comma's panda library (python/usb.py, python/dfu.py; MIT licence).
"""

import struct
import sys
import time

import usb1

from . import protocol as p
from .panda import DeviceEntry
from .usbdev import UsbError, context, explain

DFU_OUT = 0x21  # class request to the interface
DFU_IN = 0xA1
DFU_DNLOAD = 1
DFU_UPLOAD = 2
DFU_GETSTATUS = 3
DFU_CLRSTATUS = 4
DFU_ABORT = 6
STATE_IDLE = 2
STATE_DNLOAD_IDLE = 5
STATE_UPLOAD_IDLE = 9
STATE_ERROR = 10

DRIVER_HINT_WINDOWS = ("The STM32 bootloader (USB 0483:DF11) is connected but Windows has no WinUSB driver "
                       "for it. Install one with Zadig (see \"Windows driver\" in the README), then run "
                       "pandacapture flash again.")


def list_dfu() -> list:
    found = []
    ctx = context()
    try:
        for dev in ctx.getDeviceList(skip_on_error=True):
            if dev.getVendorID() == p.ST_DFU_VID and dev.getProductID() == p.ST_DFU_PID:
                try:
                    h = dev.open()
                    try:
                        found.append(DeviceEntry("dfu", h.getASCIIStringDescriptor(3) or "?"))
                    finally:
                        h.close()
                except usb1.USBError as e:
                    note = DRIVER_HINT_WINDOWS if sys.platform == "win32" else str(explain(e, "opening it"))
                    found.append(DeviceEntry("dfu", "?", note))
    finally:
        ctx.close()
    return found


class StDfu:
    def __init__(self, ctx, handle, serial):
        self._ctx = ctx
        self._h = handle
        self.serial = serial
        self._check_mcu()

    @classmethod
    def open(cls, serial=None) -> "StDfu":
        """Opens the bootloader with this DFU serial, or the only one connected when serial is None."""
        ctx = context()
        try:
            matches = []
            for dev in ctx.getDeviceList(skip_on_error=True):
                if dev.getVendorID() != p.ST_DFU_VID or dev.getProductID() != p.ST_DFU_PID:
                    continue
                try:
                    h = dev.open()
                except usb1.USBError as e:
                    if sys.platform == "win32" and isinstance(e, (usb1.USBErrorNotSupported, usb1.USBErrorAccess)):
                        raise UsbError(DRIVER_HINT_WINDOWS) from None
                    raise explain(e, "opening the STM32 bootloader") from None
                this_serial = h.getASCIIStringDescriptor(3)
                if serial is None or this_serial == serial:
                    matches.append((h, this_serial))
                else:
                    h.close()
            if not matches:
                raise UsbError("No STM32 bootloader (DFU) device found.", disconnected=True)
            if len(matches) > 1:
                for h, _ in matches:
                    h.close()
                raise UsbError("Several STM32 bootloaders connected; unplug all pandas but one.")
            h, this_serial = matches[0]
            try:
                if sys.platform.startswith("linux"):
                    h.setAutoDetachKernelDriver(True)
                h.claimInterface(0)
            except usb1.USBError as e:
                h.close()
                raise explain(e, "claiming the STM32 bootloader") from None
            return cls(ctx, h, this_serial)
        except BaseException:
            ctx.close()
            raise

    def close(self):
        if self._h is not None:
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

    def _check_mcu(self):
        # e.g. "@Internal Flash   /0x08000000/8*128Kg"
        for i in range(20):
            try:
                desc = self._h.getStringDescriptor(i, 0)
            except usb1.USBError:
                continue
            if desc and desc.startswith("@Internal Flash"):
                sectors = sum(int(s.split("*")[0]) for s in desc.split("/")[-1].split(","))
                # 8 sectors: STM32H7 (Red Panda). 16: STM32F4 (Black Panda)
                self.mcu = p.MCU_BY_DFU_SECTORS.get(sectors)
                if self.mcu is None:
                    raise UsbError(f"This bootloader's flash has {sectors} sectors: not a chip PandaCapture knows.")
                return
        raise UsbError("Couldn't identify the chip behind this STM32 bootloader.")

    def _get_status(self) -> bytes:
        try:
            return bytes(self._h.controlRead(DFU_IN, DFU_GETSTATUS, 0, 0, 6, 5000))
        except usb1.USBError as e:
            raise explain(e, "DFU status") from None

    def _wait(self, timeout=10.0):
        """Polls until the bootloader has finished the last command."""
        end = time.monotonic() + timeout
        while True:
            st = self._get_status()
            if st[0] != 0:
                raise UsbError(f"DFU error status {st[0]} (state {st[4]})")
            if st[1] == 0:  # no more polling time asked for: done
                return
            if time.monotonic() > end:
                raise UsbError("DFU command timed out")
            time.sleep(max(st[1] | (st[2] << 8) | (st[3] << 16), 1) / 1000)

    def _dnload(self, block, data):
        try:
            self._h.controlWrite(DFU_OUT, DFU_DNLOAD, block, 0, data, 5000)
        except usb1.USBError as e:
            raise explain(e, "DFU download") from None

    def clear_status(self):
        st = self._get_status()
        if st[4] == STATE_ERROR:
            self._h.controlWrite(DFU_OUT, DFU_CLRSTATUS, 0, 0, b"", 5000)
        elif st[4] in (STATE_UPLOAD_IDLE, STATE_DNLOAD_IDLE):
            self._h.controlWrite(DFU_OUT, DFU_ABORT, 0, 0, b"", 5000)
            self._wait()

    def _abort(self):
        """Back to dfuIDLE, where the address pointer can be set and uploads start."""
        try:
            self._h.controlWrite(DFU_OUT, DFU_ABORT, 0, 0, b"", 5000)
        except usb1.USBError as e:
            raise explain(e, "DFU abort") from None

    def read(self, address, length, progress=None) -> bytes:
        """Reads flash back (DfuSe upload). The address pointer is set for every block, so the
        result doesn't depend on the bootloader's transfer size."""
        out = bytearray()
        size = self.mcu.dfu_block
        while len(out) < length:
            n = min(size, length - len(out))
            self._abort()
            self._dnload(0, b"\x21" + struct.pack("<I", address + len(out)))
            self._wait()
            self._abort()
            try:
                chunk = bytes(self._h.controlRead(DFU_IN, DFU_UPLOAD, 2, 0, n, 5000))
            except usb1.USBError as e:
                raise UsbError(f"The bootloader wouldn't read flash back at 0x{address + len(out):08X} ({e}). "
                               "The chip may be read-protected.") from None
            if len(chunk) != n:
                raise UsbError(f"Short read from flash at 0x{address + len(out):08X}: {len(chunk)} of {n} bytes.")
            out += chunk
            if progress:
                progress(len(out), length)
        self._abort()
        return bytes(out)

    def erase_sector(self, sector):
        self._dnload(0, b"\x41" + struct.pack("<I", self.mcu.sector_address(sector)))
        self._wait(timeout=30)

    def program(self, address, data: bytes, progress=None):
        self._dnload(0, b"\x21" + struct.pack("<I", address))
        self._wait()
        size = self.mcu.dfu_block
        data = bytes(data) + b"\xFF" * (-len(data) % size)
        blocks = len(data) // size
        for i in range(blocks):
            self._dnload(2 + i, data[i * size:(i + 1) * size])
            self._wait()
            if progress:
                progress(i + 1, blocks)

    def jump(self, address):
        """Leaves the bootloader and starts the code at [address]. The device disconnects."""
        self._dnload(0, b"\x21" + struct.pack("<I", address))
        self._wait()
        try:
            self._dnload(2, b"")
            self._get_status()
        except UsbError:
            pass
        self.close()
