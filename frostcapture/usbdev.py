"""libusb access shared by the panda and the STM32 bootloader (DFU) handles."""

import ctypes
import sys

import usb1

_loaded = False


def context() -> usb1.USBContext:
    """A libusb context, using the libusb that ships with libusb-package when it's installed."""
    global _loaded
    if not _loaded:
        _loaded = True
        try:
            import libusb_package
            path = libusb_package.get_library_path()
            if path:
                usb1._libusb1.loadLibrary(ctypes.CDLL(str(path)))
        except (ImportError, OSError):
            pass  # libusb1's own copy (Windows) or the system's
    ctx = usb1.USBContext()
    ctx.open()
    return ctx


class UsbError(Exception):
    """A USB failure, with a hint for the usual causes."""

    def __init__(self, message, disconnected=False):
        super().__init__(message)
        self.disconnected = disconnected


DISCONNECT_ERRORS = (usb1.USBErrorNoDevice, usb1.USBErrorIO, usb1.USBErrorPipe, usb1.USBErrorNotFound)


def explain(e: Exception, what: str) -> UsbError:
    if isinstance(e, UsbError):
        return e
    if isinstance(e, usb1.USBErrorAccess):
        if sys.platform.startswith("linux"):
            return UsbError(f"{what}: permission denied. Install the udev rules: see linux/README.md")
        return UsbError(f"{what}: access denied. Is another program (cabana, openpilot tools) using it?")
    if isinstance(e, usb1.USBErrorNotSupported) and sys.platform == "win32":
        return UsbError(f"{what}: Windows has no WinUSB driver for it. See \"Windows driver\" in the README.")
    if isinstance(e, usb1.USBErrorBusy):
        return UsbError(f"{what}: busy. Close any other program using it.")
    if isinstance(e, DISCONNECT_ERRORS):
        return UsbError(f"{what}: {e} (disconnected?)", disconnected=True)
    return UsbError(f"{what}: {e}")
