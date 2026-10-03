"""Puts the PandaCapture firmware on a Red Panda.

A panda's flash has a bootstub (sector 0) that checks the app's signature before starting it. comma
ships pandas with a release bootstub that only starts comma-signed apps, so the first install goes
through the STM32's ROM bootloader (DFU) to write PandaCapture's bootstub, which also starts apps
signed with the panda project's public development key, as PandaCapture's are. After that, updates
go through the bootstub alone. Same steps as comma's Panda.recover() and Panda.flash().
"""

import time

from . import protocol as p
from .dfu import StDfu, list_dfu
from .firmware import Firmware
from .panda import Panda, list_pandas
from .usbdev import UsbError


class FlashError(Exception):
    pass


def wait_for(find, what, timeout, log):
    """Polls find() every 0.1 s until it returns something."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            found = find()
        except UsbError:
            found = None
        if found:
            return found
        time.sleep(0.1)
    raise FlashError(f"Timed out after {timeout:.0f} s waiting for {what}.")


def _panda_with(serial, kind):
    return next((d for d in list_pandas() if d.serial == serial and d.kind == kind), None)


def _dfu_device(dfu_serial):
    devices = list_dfu()
    for d in devices:
        if d.note:
            raise FlashError(d.note)
    if dfu_serial is not None:
        return next((d for d in devices if d.serial == dfu_serial), None)
    return devices[0] if len(devices) == 1 else None


def check_hardware(panda: Panda, force: bool):
    hw = panda.hw_type()
    if hw == p.HW_RED_PANDA:
        return
    name = p.HW_NAMES.get(hw, f"unknown hardware 0x{hw:02X}")
    if hw in p.HW_F4:
        raise FlashError(f"This is a {name}, which has an STM32F4. PandaCapture firmware is built for the "
                         "Red Panda's STM32H7 and can't run on it (comma's firmware no longer supports the F4). "
                         "It can still capture with its current firmware.")
    if hw in (p.HW_TRES, p.HW_CUATRO, p.HW_BODY):
        raise FlashError(f"This is a {name}, not a Red Panda. PandaCapture firmware is only for the Red Panda.")
    if not force:
        raise FlashError(f"This panda reports {name}. PandaCapture firmware is built for the Red Panda "
                         "(STM32H7); use --force only if you know it's the same hardware.")


def flash_app_via_bootstub(serial, fw: Firmware, log):
    bs = Panda.open(serial)
    try:
        if not bs.bootstub or not bs.flasher_present():
            raise FlashError("The panda's bootstub didn't answer as a flasher.")
        sectors = fw.app_sectors()
        log("Unlocking flash")
        bs.flash_unlock()
        log(f"Erasing app sectors {sectors.start}-{sectors.stop - 1}")
        for s in sectors:
            bs.flash_erase(s)
        log(f"Writing firmware ({len(fw.app) // 1024} KiB)")
        bs.flash_write(fw.app)
        log("Restarting")
        bs.reset("firmware")
    finally:
        bs.close()
    # Let the bootstub drop off the bus, so it isn't mistaken for the panda coming back
    end = time.monotonic() + 5
    while time.monotonic() < end and _panda_with(serial, "bootstub"):
        time.sleep(0.1)


def enter_dfu(serial, log):
    """From the bootstub into the STM32 ROM bootloader. Returns the bootloader's serial (or None)."""
    dfu_serial = p.dfu_serial(serial)
    bs = Panda.open(serial)
    log("Entering the STM32 bootloader (DFU)")
    bs.reset("bootloader")
    wait_for(lambda: _dfu_device(dfu_serial), "the STM32 bootloader (USB 0483:DF11)", 20, log)
    return dfu_serial


def write_bootstub(dfu_serial, fw: Firmware, log, serial=None) -> str:
    """In DFU: write PandaCapture's bootstub and start it. Returns the panda's USB serial."""
    before = {d.serial for d in list_pandas()}
    with StDfu.open(dfu_serial) as dfu:
        dfu.clear_status()
        log("Erasing the bootstub and the first app sector")
        dfu.erase_sector(0)
        dfu.erase_sector(1)
        log(f"Writing PandaCapture's bootstub ({len(fw.bootstub) // 1024} KiB)")
        dfu.program(p.FLASH_BASE, fw.bootstub)
        log("Starting the bootstub")
        dfu.jump(p.FLASH_BASE)

    # The app sector is empty, so the bootstub stays in its flasher
    def find():
        found = [d for d in list_pandas() if d.kind == "bootstub" and (d.serial == serial or
                                                                     (serial is None and d.serial not in before))]
        return found[0] if len(found) == 1 else None
    return wait_for(find, "the panda's bootstub", 20, log).serial


def flash(fw: Firmware, serial=None, recover=None, force=False, confirm=None, log=print) -> str:
    """Flashes [fw]. recover: True = always rewrite the bootstub through DFU, False = never,
    None = only when the panda isn't already running PandaCapture firmware. Returns the new version."""
    pandas = list_pandas()
    dfus = list_dfu()
    if serial:
        pandas = [d for d in pandas if d.serial == serial]
    if not pandas:
        if len(dfus) == 1:
            if dfus[0].note:
                raise FlashError(dfus[0].note)
            # e.g. the last flash stopped here for want of a Windows driver
            log("A panda is waiting in the STM32 bootloader (DFU): flashing it from there.")
            log(f"Firmware to flash: {fw.version}")
            if confirm and not confirm():
                raise FlashError("Cancelled; nothing was changed.")
            serial = write_bootstub(dfus[0].serial, fw, log)
            return finish(serial, fw, True, log)
        if dfus:
            raise FlashError("Several STM32 bootloaders connected: connect one panda at a time.")
        raise FlashError("No panda found. Connect the Red Panda by USB.")
    if len(pandas) > 1:
        raise FlashError("Several pandas connected; choose one with --serial:\n    " +
                         "\n    ".join(f"{d.serial} ({d.kind})" for d in pandas))
    serial = pandas[0].serial

    with Panda.open(serial) as panda:
        check_hardware(panda, force)
        current = "bootstub (no firmware running)" if panda.bootstub else panda.version()
    ours = p.is_pandacapture_version(current)
    via_dfu = recover if recover is not None else not ours

    log(f"Panda {serial}: {current}")
    log(f"Firmware to flash: {fw.version}")
    if via_dfu:
        log("This replaces the panda's bootstub through the STM32 bootloader, then its firmware.")
    if confirm and not confirm():
        raise FlashError("Cancelled; nothing was changed.")

    if via_dfu:
        if not pandas[0].kind == "bootstub":
            with Panda.open(serial) as panda:
                log("Entering the bootstub")
                panda.reset("bootstub")
            wait_for(lambda: _panda_with(serial, "bootstub"), "the panda's bootstub", 15, log)
        write_bootstub(enter_dfu(serial, log), fw, log, serial)
    elif pandas[0].kind != "bootstub":
        with Panda.open(serial) as panda:
            log("Entering the bootstub")
            panda.reset("bootstub")
        wait_for(lambda: _panda_with(serial, "bootstub"), "the panda's bootstub", 15, log)

    return finish(serial, fw, via_dfu, log)


def finish(serial, fw, via_dfu, log) -> str:
    """With the panda in its bootstub: write the app, restart, and check it runs."""
    flash_app_via_bootstub(serial, fw, log)
    # The bootstub checks the app's signature: it starts the app, or stays in its flasher
    came_back = wait_for(lambda: _panda_with(serial, "panda") or _panda_with(serial, "bootstub"),
                         "the panda to restart", 20, log)
    if came_back.kind == "bootstub":
        hint = "" if via_dfu else " Its bootstub is probably comma's: run pandacapture flash --recover."
        raise FlashError("The panda's bootstub refused the new firmware." + hint)

    with Panda.open(serial) as panda:
        version = panda.version()
        signature = panda.signature()
    if version != fw.version or signature != fw.signature:
        raise FlashError(f"The panda is running {version!r} after flashing, not {fw.version!r}.")
    return version
