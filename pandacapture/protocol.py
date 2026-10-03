"""The panda's USB protocol, as far as PandaCapture uses it.

Constants and pure functions with no USB access, so they can be tested anywhere and ported to the
FrostBYTE Android app (docs/protocol.md describes the same things). They follow comma's panda
firmware and Python library (MIT licence), pinned in firmware/panda.
"""

import hashlib
import struct
from dataclasses import dataclass

# ---- USB ids ----
PANDA_VIDS = (0xBBAA, 0x3801)  # 0x3801 is comma's registered vendor id
PID_APP = 0xDDCC               # panda firmware running
PID_BOOTSTUB = 0xDDEE          # the panda's bootstub (its own flasher)
ST_DFU_VID = 0x0483            # STM32 ROM bootloader (DFU)
ST_DFU_PID = 0xDF11

# ---- endpoints ----
EP_CAN_IN = 0x81
EP_FLASH_OUT = 0x02  # bootstub only
EP_CAN_OUT = 0x03

# ---- vendor control requests (device recipient) ----
REQUEST_IN = 0xC0
REQUEST_OUT = 0x40

REQ_FLASHER_ECHO = 0xB0       # bootstub: "is the flasher there?"
REQ_FLASH_UNLOCK = 0xB1       # bootstub
REQ_FLASH_ERASE = 0xB2        # bootstub, value = sector (never 0: that's the bootstub)
REQ_CAN_RESET_COMMS = 0xC0
REQ_HW_TYPE = 0xC1
REQ_CAN_HEALTH = 0xC2         # value = bus
REQ_UID = 0xC3
REQ_ENTER_BOOTLOADER = 0xD1   # value 0 = ST DFU, 1 = bootstub
REQ_HEALTH = 0xD2
REQ_SIGNATURE_1 = 0xD3
REQ_SIGNATURE_2 = 0xD4
REQ_VERSION = 0xD6
REQ_RESET = 0xD8
REQ_SET_OBD = 0xDB
REQ_SET_SAFETY = 0xDC         # value = mode, index = param
REQ_PACKET_VERSIONS = 0xDD
REQ_SET_CAN_SPEED = 0xDE      # value = bus, index = kbit/s x 10
REQ_SET_POWER_SAVE = 0xE7
REQ_SET_CANFD_AUTO = 0xE8
REQ_CAN_CLEAR = 0xF1          # value = bus, or 0xFFFF for the receive queue
REQ_HEARTBEAT = 0xF3          # value = engaged
REQ_DISABLE_HEARTBEAT = 0xF8
REQ_SET_DATA_SPEED = 0xF9     # value = bus, index = kbit/s x 10

# ---- safety modes (opendbc/safety/declarations.h) ----
SAFETY_SILENT = 0      # bus monitoring: no ACKs, no transmit
SAFETY_ALLOUTPUT = 17  # transmit anything (debug builds only)
SAFETY_NOOUTPUT = 19   # ACKs like any node, never transmits a frame
# The PandaCapture firmware only accepts ALLOUTPUT with this param (firmware/patches/0001-transmit-gate.patch)
PANDACAPTURE_TX_ARM = 0x4654
SAFETY_NAMES = {SAFETY_SILENT: "silent", SAFETY_ALLOUTPUT: "transmit armed", SAFETY_NOOUTPUT: "ack only"}

# The PandaCapture firmware's version string starts with this (firmware/patches/0002-build.patch)
PANDACAPTURE_BUILDER = "PANDACAPTURE"

# ---- hardware ----
HW_WHITE_PANDA = 0x01
HW_GREY_PANDA = 0x02
HW_BLACK_PANDA = 0x03
HW_UNO = 0x05
HW_DOS = 0x06
HW_F4 = (HW_WHITE_PANDA, HW_GREY_PANDA, HW_BLACK_PANDA, HW_UNO, HW_DOS)  # STM32F4: older firmware only
HW_RED_PANDA = 0x07
HW_TRES = 0x09    # inside a comma three
HW_CUATRO = 0x0A  # inside a comma 3X
HW_BODY = 0xB1
HW_NAMES = {HW_WHITE_PANDA: "White Panda", HW_GREY_PANDA: "Grey Panda", HW_BLACK_PANDA: "Black Panda",
            HW_UNO: "uno (comma two)", HW_DOS: "dos (comma two)", HW_RED_PANDA: "Red Panda", HW_TRES: "tres (comma three)", HW_CUATRO: "cuatro (comma 3X)", HW_BODY: "comma body"}
CAN_BUSES = 3

FLASH_BASE = 0x08000000
SIGNATURE_LEN = 128


@dataclass(frozen=True)
class Mcu:
    """A panda family's flash layout and firmware build, as comma's library describes it."""
    name: str
    target: str               # firmware build (firmware/build.py --target, firmware_bin/<target>)
    sector_sizes: tuple       # sector 0 is the bootstub
    dfu_block: int            # STM32 bootloader download block
    flash_chunk: int          # bulk write size to the bootstub
    dfu_recover_erase: tuple  # sectors erased before writing the bootstub
    dfu_serial_offset: int    # comma's DFU serial formula adds this to its middle term
    fd: bool                  # CAN FD

    @property
    def app_sectors(self):
        # Sectors 1-6: comma's library never writes past 6 (H7: 7 is the provisioning sector)
        return range(1, 7)

    def sector_address(self, sector):
        return FLASH_BASE + sum(self.sector_sizes[:sector])

    def sectors_for(self, size):
        """App sectors from 1 that hold [size] bytes."""
        total = 0
        for s in self.app_sectors:
            total += self.sector_sizes[s]
            if total >= size:
                return range(1, s + 1)
        raise ValueError(f"{size} bytes doesn't fit the {self.name}'s app sectors")


# STM32H7 (Red Panda): 8 sectors of 128 KiB
MCU_H7 = Mcu("STM32H7", "h7", (0x20000,) * 8, 0x400, 0x200, (0, 1), 0, True)
# STM32F413 (Black and White Panda): 4 x 16 KiB, 64 KiB, 11 x 128 KiB. From comma's last F4-capable
# library (firmware/panda-f4): it erased every sector to recover and flashed in 16-byte writes.
MCU_F4 = Mcu("STM32F4", "f4", (0x4000,) * 4 + (0x10000,) + (0x20000,) * 11, 0x800, 0x10, tuple(range(16)), 0xA, False)
MCU_BY_HW = {HW_RED_PANDA: MCU_H7, HW_BLACK_PANDA: MCU_F4, HW_WHITE_PANDA: MCU_F4}
MCU_BY_DFU_SECTORS = {len(MCU_H7.sector_sizes): MCU_H7, len(MCU_F4.sector_sizes): MCU_F4}

# Bit rates the firmware accepts, kbit/s
CAN_SPEEDS = (10, 20, 50, 100, 125, 250, 500, 1000)
DATA_SPEEDS = (10, 20, 50, 100, 125, 250, 500, 1000, 2000, 5000)

# CAN packet format 4 (6-byte header with checksum) is what PandaCapture reads. Recent firmware
# reports a hash instead of a number, always far above 4.
CAN_PACKET_V4 = 4

DLC_TO_LEN = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
LEN_TO_DLC = {n: dlc for dlc, n in enumerate(DLC_TO_LEN)}
HEADER_LEN = 6

# board/health.h health_t and can_health_t, little endian, packed
HEALTH_STRUCT = struct.Struct("<IHHIIIIIHBBHBHBBHHHHB")
CAN_HEALTH_STRUCT = struct.Struct("<BIBBBBBBBBIIIIIIIHHBBBIIII")
# Firmware before comma hashed the layouts reports small version numbers (3 bytes from 0xDD).
# Health version 16 is the F4 build's (firmware/panda-f4 board/health.h).
LEGACY_HEALTH_V16 = struct.Struct("<IIIIIIIIBBBBBHBBBHfBBHBHHB")

HEALTH_FLAG_IGNITION_LINE = 1 << 0
HEALTH_FLAG_IGNITION_CAN = 1 << 1
HEALTH_FLAG_CONTROLS_ALLOWED = 1 << 2
HEALTH_FLAG_POWER_SAVE = 1 << 3
HEALTH_FLAG_HEARTBEAT_LOST = 1 << 4

LEC_NAMES = ("none", "stuff", "form", "ack", "bit1", "bit0", "crc", "no change")


@dataclass(frozen=True)
class Frame:
    bus: int
    addr: int
    data: bytes
    extended: bool = False
    fd: bool = False
    returned: bool = False  # the panda's echo of a frame it transmitted
    rejected: bool = False  # a frame the firmware refused to transmit


class PacketError(ValueError):
    pass


def checksum(data) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


def pack_frame(f: Frame) -> bytes:
    """One CAN packet: [dlc<<4 | bus<<1 | fd][addr<<3 | ext<<2, 4 bytes LE][xor checksum][data]."""
    if len(f.data) not in LEN_TO_DLC:
        raise PacketError(f"{len(f.data)} data bytes is not a valid CAN length")
    if len(f.data) > 8 and not f.fd:
        raise PacketError("more than 8 data bytes needs CAN FD")
    if not 0 <= f.bus < CAN_BUSES:
        raise PacketError(f"bus {f.bus} out of range")
    limit = 0x1FFFFFFF if f.extended else 0x7FF
    if not 0 <= f.addr <= limit:
        raise PacketError(f"id 0x{f.addr:X} out of range")
    header = bytearray(HEADER_LEN)
    header[0] = (LEN_TO_DLC[len(f.data)] << 4) | (f.bus << 1) | int(f.fd)
    word = (f.addr << 3) | (int(f.extended) << 2)
    header[1:5] = struct.pack("<I", word)
    header[5] = checksum(header[:5] + f.data)
    return bytes(header) + bytes(f.data)


def pack_frames(frames, chunk: int = 256) -> list:
    """Packets grouped into USB transfers of about [chunk] bytes, as comma's library sends them."""
    out = [bytearray()]
    for f in frames:
        out[-1] += pack_frame(f)
        if len(out[-1]) > chunk:
            out.append(bytearray())
    return [bytes(c) for c in out if c]


class CanUnpacker:
    """Splits the CAN IN stream into frames. A packet can span two USB transfers, so the tail of one
    transfer is kept for the next. A bad checksum means the stream lost sync: the buffer is dropped."""

    def __init__(self):
        self.tail = b""
        self.bad_checksums = 0
        self.dropped_bytes = 0

    def reset(self):
        self.tail = b""

    def feed(self, data: bytes) -> list:
        buf = self.tail + bytes(data)
        frames = []
        pos = 0
        while len(buf) - pos >= HEADER_LEN:
            n = DLC_TO_LEN[buf[pos] >> 4]
            if len(buf) - pos < HEADER_LEN + n:
                break
            packet = buf[pos:pos + HEADER_LEN + n]
            if checksum(packet) != 0:
                self.bad_checksums += 1
                self.dropped_bytes += len(buf) - pos
                self.tail = b""
                return frames
            word = struct.unpack_from("<I", packet, 1)[0]
            frames.append(Frame(
                bus=(packet[0] >> 1) & 0x7,
                addr=word >> 3,
                data=packet[HEADER_LEN:],
                extended=bool(word & 0x4),
                fd=bool(packet[0] & 0x1),
                returned=bool(word & 0x2),
                rejected=bool(word & 0x1),
            ))
            pos += HEADER_LEN + n
        self.tail = buf[pos:]
        return frames


def parse_legacy_health_v16(dat: bytes) -> dict:
    a = LEGACY_HEALTH_V16.unpack(bytes(dat[:LEGACY_HEALTH_V16.size]))
    return {
        "uptime_s": a[0],
        "voltage_mv": a[1],
        "current_ma": a[2],
        "tx_blocked": a[3],
        "tx_buffer_overflow": a[5],
        "rx_buffer_overflow": a[6],
        "faults": a[7],
        "ignition": bool(a[8] or a[9]),
        "controls_allowed": bool(a[10]),
        "power_save": bool(a[15]),
        "heartbeat_lost": bool(a[16]),
        "harness_status": a[11],
        "safety_mode": a[12],
        "safety_param": a[13],
        "fault_status": a[14],
        "temperature_c": None,
    }


def parse_health(dat: bytes) -> dict:
    a = HEALTH_STRUCT.unpack(bytes(dat[:HEALTH_STRUCT.size]))
    flags = a[8]
    return {
        "uptime_s": a[0],
        "voltage_mv": a[1],
        "current_ma": a[2],
        "tx_blocked": a[3],
        "tx_buffer_overflow": a[5],
        "rx_buffer_overflow": a[6],
        "faults": a[7],
        "ignition": bool(flags & (HEALTH_FLAG_IGNITION_LINE | HEALTH_FLAG_IGNITION_CAN)),
        "controls_allowed": bool(flags & HEALTH_FLAG_CONTROLS_ALLOWED),
        "power_save": bool(flags & HEALTH_FLAG_POWER_SAVE),
        "heartbeat_lost": bool(flags & HEALTH_FLAG_HEARTBEAT_LOST),
        "harness_status": a[9],
        "safety_mode": a[10],
        "safety_param": a[11],
        "fault_status": a[12],
        "temperature_c": a[20] - 40,
    }


def parse_can_health(dat: bytes) -> dict:
    a = CAN_HEALTH_STRUCT.unpack(bytes(dat[:CAN_HEALTH_STRUCT.size]))
    return {
        "bus_off": bool(a[0]),
        "bus_off_count": a[1],
        "error_warning": bool(a[2]),
        "error_passive": bool(a[3]),
        "last_error": LEC_NAMES[a[4] & 7],
        "last_stored_error": LEC_NAMES[a[5] & 7],
        "receive_error_count": a[8],
        "transmit_error_count": a[9],
        "total_errors": a[10],
        "total_tx_lost": a[11],
        "total_rx_lost": a[12],
        "total_tx": a[13],
        "total_rx": a[14],
        "speed_kbps": a[17],  # the firmware already reports kbit/s
        "data_speed_kbps": a[18],
        "canfd": bool(a[19]),
        "brs": bool(a[20]),
    }


def version_hash(header_text: bytes) -> int:
    """How the firmware stamps its health and CAN packet layouts: sha256 of the header file."""
    return int.from_bytes(hashlib.sha256(header_text.replace(b"\r", b"")).digest()[:4], "little")


def dfu_serial(usb_serial: str, mcu: Mcu = MCU_H7):
    """The serial the STM32 ROM bootloader reports for a panda with this USB serial (its MCU UID),
    as comma's library works it out. None when it can't: then any single DFU device is taken."""
    try:
        uid = struct.unpack("<6H", bytes.fromhex(usb_serial))
        return struct.pack("!HHH", uid[1] + uid[5], uid[0] + uid[4] + mcu.dfu_serial_offset, uid[3]).hex().upper()
    except (ValueError, TypeError, struct.error):
        return None


def is_pandacapture_version(version: str) -> bool:
    return version.startswith(PANDACAPTURE_BUILDER + "-")


def parse_frame_text(text: str, default_bus: int = 0) -> Frame:
    """candump notation: ID#DATA, or ID##F + DATA for CAN FD (F = flags nibble). 3 hex digits for a
    standard id, 8 for extended."""
    text = text.strip()
    if "##" in text:
        ident, rest = text.split("##", 1)
        fd = True
        if not rest:
            raise PacketError(f"{text}: CAN FD needs a flags digit after ##")
        rest = rest[1:]
    elif "#" in text:
        ident, rest = text.split("#", 1)
        fd = False
    else:
        raise PacketError(f"{text}: expected ID#DATA")
    rest = rest.replace(".", "")
    if rest.upper().startswith("R"):
        raise PacketError("remote frames aren't supported")
    try:
        addr = int(ident, 16)
        data = bytes.fromhex(rest)
    except ValueError:
        raise PacketError(f"{text}: not hex") from None
    extended = len(ident) > 3 or addr > 0x7FF
    frame = Frame(bus=default_bus, addr=addr, data=data, extended=extended, fd=fd)
    pack_frame(frame)  # validates
    return frame
