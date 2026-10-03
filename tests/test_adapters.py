"""RP1210 and J2534 adapters: finding drivers, the bridge's record stream, and (on Windows with gcc) the
real bridge against a fake driver DLL (tests/fake_driver.c)."""

import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from pandacapture import adapters as ad
from pandacapture.sources import SourceError


def pe_file(path, machine):
    """A file with just enough of a PE header for pe_machine()."""
    head = bytearray(512)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)
    head[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", head, 0x84, machine)
    Path(path).write_bytes(bytes(head))


def test_pe_machine(tmp_path):
    pe_file(tmp_path / "a.dll", 0x14C)
    pe_file(tmp_path / "b.dll", 0x8664)
    (tmp_path / "c.dll").write_bytes(b"not a dll")
    assert ad.pe_machine(tmp_path / "a.dll") == "x86"
    assert ad.pe_machine(tmp_path / "b.dll") == "x64"
    assert ad.pe_machine(tmp_path / "c.dll") == ""
    assert ad.pe_machine(tmp_path / "missing.dll") == ""


def make_windir(tmp_path):
    (tmp_path / "SysWOW64").mkdir()
    (tmp_path / "RP121032.ini").write_text("[RP1210Support]\nAPIImplementations=NULN2R32, GONE\n")
    (tmp_path / "NULN2R32.ini").write_text(
        "[VendorInformation]\nName=NEXIQ Technologies USB-Link 2\n"
        "[DeviceInformation1]\nDeviceID=1\nDeviceDescription=USB-Link 2,USB\n"
        "[DeviceInformation2]\nDeviceID=2\nDeviceDescription=USB-Link 2,Bluetooth\n"
        "[DeviceInformation4]\nDeviceID=4\nDeviceDescription=J1708 only\n"
        "; a comment\n[ProtocolInformation1]\nProtocolString=J1939\nDevices=1,2,4\n"
        "[ProtocolInformation2]\nProtocolString=CAN\nDevices=1, 2\n")
    pe_file(tmp_path / "SysWOW64" / "NULN2R32.dll", 0x14C)
    return tmp_path


def test_list_rp1210(tmp_path):
    found = ad.list_rp1210(make_windir(tmp_path))
    keys = {a.key: a for a in found}
    assert set(keys) == {"rp1210:NULN2R32:1", "rp1210:NULN2R32:2", "rp1210:GONE"}
    usb = keys["rp1210:NULN2R32:1"]
    assert (usb.name, usb.device, usb.arch, usb.problem) == ("USB-Link 2,USB", 1, "x86", "")
    assert "missing" in keys["rp1210:GONE"].problem
    assert ad.list_rp1210(tmp_path / "nowhere") == []


def test_find_adapter(tmp_path):
    found = ad.list_rp1210(make_windir(tmp_path))
    assert ad.find_adapter("rp1210:nuln2r32:2", found).device == 2
    assert ad.find_adapter("usb-link 2,usb", found).device == 1
    with pytest.raises(SourceError, match="Several"):
        ad.find_adapter("USB-Link 2", found)
    with pytest.raises(SourceError, match="No RP1210"):
        ad.find_adapter("Mongoose", found)


def test_bridge_args():
    rp = ad.Adapter("rp1210", "rp1210:X:1", "X", "", "X", 1, "x86")
    assert ad.bridge_args(rp, None)[-2:] == ["CAN:Channel=1;Baud=Auto", "CAN:Baud=Auto"]
    assert ad.bridge_args(rp, 500)[-1] == "CAN:Baud=500"
    j = ad.Adapter("j2534", "j2534:Y", "Y", "", r"C:\y.dll", arch="x86")
    assert ad.bridge_args(j, 250) == ["j2534", r"C:\y.dll", "250"]
    with pytest.raises(SourceError, match="bit rate"):
        ad.bridge_args(j, None)


def test_records_split_anywhere():
    stream = (b"I" + struct.pack("<H", 5) + b"hello"
              + b"F" + struct.pack("<IIB", 1000, 0x316, 8) + bytes(range(8))
              + b"F" + struct.pack("<IIB", 2000, 0x18FEF100 | 0x80000000, 2) + b"\xAA\xBB"
              + b"E" + struct.pack("<H", 4) + b"gone")
    for cut in range(len(stream)):
        r = ad.Records()
        got = r.feed(stream[:cut]) + r.feed(stream[cut:])
        assert [k for k, _ in got] == ["I", "F", "F", "E"]
        assert got[1][1].addr == 0x316 and got[1][1].data == bytes(range(8)) and not got[1][1].extended
        assert got[2][1].addr == 0x18FEF100 and got[2][1].extended and got[2][1].data == b"\xAA\xBB"
        assert got[3][1] == "gone"
    with pytest.raises(SourceError):
        ad.Records().feed(b"Z")


def test_adapter_with_a_problem_is_refused():
    a = ad.Adapter("j2534", "j2534:Z", "Z", "", "", problem="its registration names no driver DLL")
    with pytest.raises(SourceError, match="no driver DLL"):
        ad.AdapterSource(a, 500)


# ---- the real bridge, against a fake driver ----

def fake_driver(tmp_path_factory):
    if os.name != "nt" or not shutil.which("gcc"):
        pytest.skip("needs Windows and gcc")
    if not (ad.bridge_dir() / "pandacapture-adapter-x64.exe").exists():
        pytest.skip("bridge not built (python adapter/build.py)")
    out = tmp_path_factory.mktemp("fake") / "fake_driver.dll"
    subprocess.run(["gcc", "-shared", "-O2", "-o", str(out), str(Path(__file__).with_name("fake_driver.c"))], check=True)
    if ad.pe_machine(out) != "x64":
        pytest.skip("gcc doesn't build 64-bit DLLs here")
    return out


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    return fake_driver(tmp_path_factory)


def drain(src, reads=200):
    frames = []
    for _ in range(reads):
        frames += src.read()
    return frames


@pytest.mark.parametrize("kind", ["j2534", "rp1210"])
def test_bridge_reads_frames(driver, kind):
    a = ad.Adapter(kind, f"{kind}:fake", "Fake", "Test", str(driver), 1, "x64")
    src = ad.AdapterSource(a, 500)
    try:
        assert "Fake" in src.description and "64-bit driver" in src.description
        frames = drain(src, 50)
    finally:
        src.close()
    assert frames
    ids = {(f.addr, f.extended) for f in frames}
    assert ids == {(0x316, False), (0x18FEF100, True)}   # the J2534 transmit echo (0x7E0) is skipped
    rpm = next(f for f in frames if f.addr == 0x316)
    assert rpm.data == bytes([0, 0x10, 0x40, 0x1F, 0, 0, 0, 0]) and rpm.bus == 0
    if kind == "rp1210":
        assert '"CAN:Baud=500"' in src.description   # fell back from the Channel= form


@pytest.mark.parametrize("kind", ["j2534", "rp1210"])
def test_bridge_reports_a_lost_adapter(driver, kind, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_AFTER", "3")
    src = ad.AdapterSource(ad.Adapter(kind, f"{kind}:fake", "Fake", "", str(driver), 1, "x64"), 500)
    try:
        with pytest.raises(SourceError, match="not connected|142"):
            drain(src)
    finally:
        src.close()


def test_bridge_reports_a_refused_rate(driver):
    with pytest.raises(SourceError, match="1000 kbit/s.*unsupported baud"):
        ad.AdapterSource(ad.Adapter("j2534", "j2534:fake", "Fake", "", str(driver), arch="x64"), 1000)
