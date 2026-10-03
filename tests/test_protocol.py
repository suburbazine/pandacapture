import re
import struct
from pathlib import Path

import pytest

from pandacapture import protocol as p

ROOT = Path(__file__).resolve().parent.parent
PANDA = ROOT / "firmware" / "panda"
OPENDBC = ROOT / "firmware" / "opendbc"
needs_panda = pytest.mark.skipif(not (PANDA / "board" / "health.h").exists(), reason="firmware/panda submodule not checked out")


def frames():
    return [
        p.Frame(0, 0x316, bytes(range(8))),
        p.Frame(1, 0x7DF, b""),
        p.Frame(2, 0x18DB33F1, bytes([2, 1, 0x0C]), extended=True),
        p.Frame(0, 0x1A0, bytes(range(64)), fd=True),
        p.Frame(1, 0x12345678, bytes(range(12)), extended=True, fd=True),
    ]


def test_roundtrip():
    stream = b"".join(p.pack_frame(f) for f in frames())
    assert p.CanUnpacker().feed(stream) == frames()


@pytest.mark.parametrize("cut", range(1, 60, 7))
def test_split_transfers(cut):
    stream = b"".join(p.pack_frame(f) for f in frames())
    u = p.CanUnpacker()
    assert u.feed(stream[:cut]) + u.feed(stream[cut:]) == frames()
    assert u.tail == b""


def test_header_layout_matches_comma_library():
    # comma's pack_can_buffer: header[0] = dlc<<4 | bus<<1 | fd, then (addr<<3 | ext<<2) LE, then xor
    pkt = p.pack_frame(p.Frame(2, 0x18DB33F1, bytes([2, 1, 0x0C]), extended=True))
    assert pkt[0] == (3 << 4) | (2 << 1)
    assert struct.unpack_from("<I", pkt, 1)[0] == (0x18DB33F1 << 3) | 4
    assert p.checksum(pkt) == 0


def test_returned_and_rejected_flags():
    pkt = bytearray(p.pack_frame(p.Frame(0, 0x123, b"\x01")))
    pkt[1] |= 0x2  # returned
    pkt[5] ^= 0x2
    (f,) = p.CanUnpacker().feed(bytes(pkt))
    assert f.returned and not f.rejected and f.addr == 0x123


def test_bad_checksum_drops_buffer():
    good = p.pack_frame(p.Frame(0, 0x316, bytes(8)))
    bad = bytearray(good)
    bad[6] ^= 1
    u = p.CanUnpacker()
    assert u.feed(good + bytes(bad) + good) == [p.Frame(0, 0x316, bytes(8))]
    assert u.bad_checksums == 1 and u.tail == b""


def test_chunking():
    many = [p.Frame(0, 0x100 + i, bytes(8)) for i in range(100)]
    chunks = p.pack_frames(many)
    assert len(chunks) > 1 and all(len(c) <= 256 + 14 for c in chunks)
    assert p.CanUnpacker().feed(b"".join(chunks)) == many


@pytest.mark.parametrize("bad", [
    p.Frame(0, 0x800, b""),                    # standard id too big
    p.Frame(3, 0x100, b""),                    # no bus 3
    p.Frame(0, 0x100, bytes(9)),               # 9 isn't a CAN length
    p.Frame(0, 0x100, bytes(12)),              # >8 needs FD
])
def test_pack_rejects(bad):
    with pytest.raises(p.PacketError):
        p.pack_frame(bad)


def test_parse_frame_text():
    assert p.parse_frame_text("7DF#02010C", 1) == p.Frame(1, 0x7DF, bytes([2, 1, 0x0C]))
    assert p.parse_frame_text("18DB33F1#0201") == p.Frame(0, 0x18DB33F1, bytes([2, 1]), extended=True)
    assert p.parse_frame_text("00000123#") == p.Frame(0, 0x123, b"", extended=True)
    fd = p.parse_frame_text("1A0##1" + "00" * 12)
    assert fd.fd and len(fd.data) == 12
    for bad in ("7DF", "7DF#0", "XYZ#00", "7DF#R", "7DF#" + "00" * 9):
        with pytest.raises(p.PacketError):
            p.parse_frame_text(bad)


def test_dfu_serial_matches_comma_formula():
    st = "1d0032000f51333231373438"
    uid = struct.unpack("H" * 6, bytes.fromhex(st))
    want = struct.pack("!HHH", uid[1] + uid[5], uid[0] + uid[4], uid[3]).hex().upper()
    assert p.dfu_serial(st) == want
    assert p.dfu_serial("nothex") is None


def c_struct(text, start_marker, end_marker):
    """struct.Struct from a packed C struct, the way comma's library reads health.h."""
    types = {"uint8_t": "B", "uint16_t": "H", "uint32_t": "I", "float": "f"}
    body = text.split(start_marker, 1)[1].split(end_marker, 1)[0]
    fmt = "<"
    for line in body.splitlines():
        line = line.split("//")[0].strip()
        if line:
            m = re.fullmatch(r"(\w+)\s+\w+;", line)
            fmt += types[m[1]]
    return fmt


@needs_panda
def test_health_structs_match_pinned_firmware():
    text = (PANDA / "board" / "health.h").read_text()
    assert c_struct(text, "struct __attribute__((packed)) health_t {", "};") == p.HEALTH_STRUCT.format
    assert c_struct(text, "typedef struct __attribute__((packed)) {", "} can_health_t;") == p.CAN_HEALTH_STRUCT.format


@needs_panda
def test_firmware_constants_match_pinned_sources():
    llfdcan = (PANDA / "board" / "stm32h7" / "llfdcan.h").read_text()
    speeds = re.search(r"speeds\[SPEEDS_ARRAY_SIZE\] = \{([^}]*)\}", llfdcan)[1]
    data = re.search(r"data_speeds\[DATA_SPEEDS_ARRAY_SIZE\] = \{([^}]*)\}", llfdcan)[1]
    assert tuple(int(x.strip().rstrip("U")) / 10 for x in speeds.split(",")) == p.CAN_SPEEDS
    assert tuple(int(x.strip().rstrip("U")) / 10 for x in data.split(",")) == p.DATA_SPEEDS
    decl = (OPENDBC / "opendbc" / "safety" / "declarations.h").read_text()
    for name, value in (("SILENT", p.SAFETY_SILENT), ("ALLOUTPUT", p.SAFETY_ALLOUTPUT), ("NOOUTPUT", p.SAFETY_NOOUTPUT)):
        assert re.search(rf"#define SAFETY_{name} (\d+)U", decl)[1] == str(value)
    comms = (PANDA / "board" / "main_comms.h").read_text()
    for req in (p.REQ_HEALTH, p.REQ_CAN_HEALTH, p.REQ_SET_SAFETY, p.REQ_SET_CAN_SPEED, p.REQ_SET_DATA_SPEED,
                p.REQ_HEARTBEAT, p.REQ_DISABLE_HEARTBEAT, p.REQ_VERSION, p.REQ_ENTER_BOOTLOADER, p.REQ_RESET):
        assert f"case 0x{req:02x}:" in comms


PANDA_F4 = ROOT / "firmware" / "panda-f4"


@pytest.mark.skipif(not (PANDA_F4 / "board" / "health.h").exists(), reason="firmware/panda-f4 submodule not checked out")
def test_f4_health_matches_pinned_firmware():
    text = (PANDA_F4 / "board" / "health.h").read_text()
    assert "#define HEALTH_PACKET_VERSION 16" in text
    assert c_struct(text, "struct __attribute__((packed)) health_t {", "};") == p.LEGACY_HEALTH_V16.format
    assert c_struct(text, "typedef struct __attribute__((packed)) {", "} can_health_t;") == p.CAN_HEALTH_STRUCT.format
    assert re.search(r"#define CAN_PACKET_VERSION (\d+)", (PANDA_F4 / "board" / "can_declarations.h").read_text())[1] \
        == str(p.CAN_PACKET_V4)


@pytest.mark.skipif(not (PANDA_F4 / "python" / "constants.py").exists(), reason="firmware/panda-f4 submodule not checked out")
def test_mcu_layouts_match_comma_library():
    text = (PANDA_F4 / "python" / "constants.py").read_text()
    assert "[0x4000 for _ in range(4)] + [0x10000] + [0x20000 for _ in range(11)]" in text
    assert p.MCU_F4.sector_sizes == (0x4000,) * 4 + (0x10000,) + (0x20000,) * 11
    assert p.MCU_F4.sector_address(1) == 0x8004000 and p.MCU_H7.sector_address(1) == 0x8020000
    assert "0x800,\n  0x1FFF79C0" in text and p.MCU_F4.dfu_block == 0x800


def test_dfu_serial_f4_formula():
    st = "1d0032000f51333231373438"
    uid = struct.unpack("H" * 6, bytes.fromhex(st))
    want = struct.pack("!HHH", uid[1] + uid[5], uid[0] + uid[4] + 0xA, uid[3]).hex().upper()
    assert p.dfu_serial(st, p.MCU_F4) == want


def test_legacy_health_parse():
    raw = p.LEGACY_HEALTH_V16.pack(100, 12000, 0, 0, 0, 0, 7, 0, 1, 0, 1, 1, p.SAFETY_SILENT, 0, 0, 0, 0, 0, 0.1,
                                   0, 0, 0, 0, 0, 0, 0)
    h = p.parse_legacy_health_v16(raw)
    assert h["safety_mode"] == p.SAFETY_SILENT and h["rx_buffer_overflow"] == 7 and h["temperature_c"] is None


def test_can_health_speeds_are_kbps():
    # Firmware sends bus_config.can_speed / 10 (5000 -> 500): already kbit/s. Seen on a real panda.
    raw = bytes.fromhex("0000000000000000000000000000000000000000000000000000000000000000"
                        "000000000000000000f40100000000000000000000000000000000000000000000")
    assert p.parse_can_health(raw[:p.CAN_HEALTH_STRUCT.size])["speed_kbps"] == 500
