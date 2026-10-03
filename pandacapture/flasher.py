"""Puts the PandaCapture firmware on a Red Panda.

A panda's flash has a bootstub (sector 0) that checks the app's signature before starting it. comma
ships pandas with a release bootstub that only starts comma-signed apps, so the first install goes
through the STM32's ROM bootloader (DFU) to write PandaCapture's bootstub, which also starts apps
signed with the panda project's public development key, as PandaCapture's are. After that, updates
go through the bootstub alone. Same steps as comma's Panda.recover() and Panda.flash().
"""

import time
from pathlib import Path

from . import backup
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


def check_hardware(panda: Panda, force: bool) -> p.Mcu:
    """Which firmware build the panda takes: H7 for the Red Panda, F4 for the Black Panda."""
    hw = panda.hw_type()
    name = p.HW_NAMES.get(hw, f"unknown hardware 0x{hw:02X}")
    # Grey: comma's grey board, and boards that detect as one, such as oneclone's mini blackpanda
    if hw in (p.HW_RED_PANDA, p.HW_BLACK_PANDA, p.HW_GREY_PANDA):
        return p.MCU_BY_HW[hw]
    if hw == p.HW_WHITE_PANDA:
        if not force:
            raise FlashError("This is a White Panda. The F4 firmware supports it, but PandaCapture hasn't been "
                             "tried on one: use --force to flash it anyway.")
        return p.MCU_F4
    if hw in (p.HW_TRES, p.HW_CUATRO, p.HW_BODY, p.HW_UNO, p.HW_DOS):
        raise FlashError(f"This is a {name}: the panda inside a comma device. PandaCapture firmware is for the "
                         "Red Panda and the Black Panda.")
    raise FlashError(f"This panda reports {name}, which none of PandaCapture's firmware builds supports. "
                     "It can still capture with its current firmware.")


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
        bs.flash_write(fw.app, fw.mcu.flash_chunk)
        log("Restarting")
        bs.reset("firmware")
    finally:
        bs.close()
    # Let the bootstub drop off the bus, so it isn't mistaken for the panda coming back
    end = time.monotonic() + 5
    while time.monotonic() < end and _panda_with(serial, "bootstub"):
        time.sleep(0.1)


def enter_dfu(serial, mcu, log):
    """From the bootstub into the STM32 ROM bootloader. Returns the bootloader's serial (or None)."""
    dfu_serial = p.dfu_serial(serial, mcu)
    bs = Panda.open(serial)
    log("Entering the STM32 bootloader (DFU)")
    bs.reset("bootloader")
    wait_for(lambda: _dfu_device(dfu_serial), "the STM32 bootloader (USB 0483:DF11)", 20, log)
    return dfu_serial


def backup_flash(dfu_serial, folder, serial, firmware, log):
    """In DFU: save the whole flash before anything is erased."""
    with StDfu.open(dfu_serial) as dfu:
        try:
            path = backup.save(dfu, folder, serial, firmware, log)
        except UsbError as e:
            raise FlashError(f"Backup failed, so nothing was erased: {e}") from None
    log(f"Backup saved: {path}")
    return path


def write_bootstub(dfu_serial, fw: Firmware, log, serial=None) -> str:
    """In DFU: write PandaCapture's bootstub and start it. Returns the panda's USB serial."""
    before = {d.serial for d in list_pandas()}
    with StDfu.open(dfu_serial) as dfu:
        if dfu.mcu != fw.mcu:
            raise FlashError(f"The bootloader is an {dfu.mcu.name}, but the firmware is for the {fw.mcu.name}.")
        dfu.clear_status()
        erase = fw.mcu.dfu_recover_erase
        log(f"Erasing flash sectors {erase[0]}-{erase[-1]}")
        for sector in erase:
            dfu.erase_sector(sector)
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


def flash(load_firmware, serial=None, recover=None, force=False, confirm=None, log=print, backup_dir=None) -> str:
    """Flashes the firmware load_firmware(mcu) returns for the panda's chip. recover: True = always
    rewrite the bootstub through DFU, False = never, None = only when the panda isn't already running
    PandaCapture firmware. With backup_dir, the whole flash is saved there before the bootstub is
    rewritten. Returns the new version."""
    pandas = list_pandas()
    dfus = list_dfu()
    if serial:
        pandas = [d for d in pandas if d.serial == serial]
    if not pandas:
        if len(dfus) == 1:
            if dfus[0].note:
                raise FlashError(dfus[0].note)
            # e.g. the last flash stopped here for want of a Windows driver
            with StDfu.open(dfus[0].serial) as dfu:
                mcu = dfu.mcu
            fw = load_firmware(mcu)
            log(f"A panda ({mcu.name}) is waiting in the STM32 bootloader (DFU): flashing it from there.")
            log(f"Firmware to flash: {fw.version}")
            if confirm and not confirm():
                raise FlashError("Cancelled; nothing was changed.")
            if backup_dir:
                backup_flash(dfus[0].serial, backup_dir, None, "unknown (found in DFU)", log)
            serial = write_bootstub(dfus[0].serial, fw, log)
            return finish(serial, fw, True, log)
        if dfus:
            raise FlashError("Several STM32 bootloaders connected: connect one panda at a time.")
        raise FlashError("No panda found. Connect it by USB.")
    if len(pandas) > 1:
        raise FlashError("Several pandas connected; choose one with --serial:\n    " +
                         "\n    ".join(f"{d.serial} ({d.kind})" for d in pandas))
    serial = pandas[0].serial

    with Panda.open(serial) as panda:
        mcu = check_hardware(panda, force)
        current = "bootstub (no firmware running)" if panda.bootstub else panda.version()
    fw = load_firmware(mcu)
    ours = p.is_pandacapture_version(current)
    via_dfu = recover if recover is not None else not ours

    log(f"Panda {serial} ({mcu.name}): {current}")
    log(f"Firmware to flash: {fw.version}")
    if via_dfu:
        log("This replaces the panda's bootstub through the STM32 bootloader, then its firmware.")
        if backup_dir:
            log(f"The whole flash is backed up first, to {backup_dir}.")
    if confirm and not confirm():
        raise FlashError("Cancelled; nothing was changed.")

    if via_dfu:
        if not pandas[0].kind == "bootstub":
            with Panda.open(serial) as panda:
                log("Entering the bootstub")
                panda.reset("bootstub")
            wait_for(lambda: _panda_with(serial, "bootstub"), "the panda's bootstub", 15, log)
        dfu_serial = enter_dfu(serial, mcu, log)
        if backup_dir:
            backup_flash(dfu_serial, backup_dir, serial, current, log)
        write_bootstub(dfu_serial, fw, log, serial)
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


def restore(path, serial=None, confirm=None, log=print) -> str:
    """Writes a whole-flash backup back through the STM32 bootloader. Returns what the panda runs after."""
    try:
        data, meta, mcu = backup.load(path)
    except backup.BackupError as e:
        raise FlashError(str(e)) from None
    pandas = [d for d in list_pandas() if serial is None or d.serial == serial]
    dfus = list_dfu()
    if len(pandas) > 1:
        raise FlashError("Several pandas connected; choose one with --serial.")
    if pandas and meta.get("serial") and pandas[0].serial != meta["serial"]:
        raise FlashError(f"This backup is of panda {meta['serial']}, not {pandas[0].serial}.")
    log(f"Backup: {Path(path).name}, {meta['mcu']}, firmware {meta.get('firmware')}, made {meta.get('created')}")
    log("This erases the panda's flash and writes the backup back.")
    if confirm and not confirm():
        raise FlashError("Cancelled; nothing was changed.")

    if pandas:
        serial = pandas[0].serial
        if pandas[0].kind != "bootstub":
            with Panda.open(serial) as panda:
                log("Entering the bootstub")
                panda.reset("bootstub")
            wait_for(lambda: _panda_with(serial, "bootstub"), "the panda's bootstub", 15, log)
        dfu_serial = enter_dfu(serial, mcu, log)
    elif len(dfus) == 1:
        if dfus[0].note:
            raise FlashError(dfus[0].note)
        dfu_serial = dfus[0].serial
    else:
        raise FlashError("No panda found. Connect it by USB.")

    with StDfu.open(dfu_serial) as dfu:
        if dfu.mcu != mcu:
            raise FlashError(f"The backup is of an {mcu.name}, but this panda is an {dfu.mcu.name}.")
        backup.write(dfu, data, log)
        log("Starting the restored firmware")
        dfu.jump(p.FLASH_BASE)
    found = wait_for(lambda: next((d for d in list_pandas() if serial is None or d.serial == serial), None),
                     "the panda to restart", 20, log)
    if found.kind == "bootstub":
        return "its bootstub (the backup's firmware didn't start)"
    with Panda.open(found.serial) as panda:
        return panda.version()


def make_backup(folder, serial=None, log=print) -> Path:
    """Saves the whole flash through the STM32 bootloader, then restarts the panda. Writes nothing."""
    pandas = [d for d in list_pandas() if serial is None or d.serial == serial]
    if not pandas:
        raise FlashError("No panda found. Connect it by USB.")
    if len(pandas) > 1:
        raise FlashError("Several pandas connected; choose one with --serial.")
    serial = pandas[0].serial
    with Panda.open(serial) as panda:
        hw = panda.hw_type()
        mcu = p.MCU_BY_HW.get(hw)
        if mcu is None:
            raise FlashError(f"PandaCapture doesn't know this panda's flash layout ({p.HW_NAMES.get(hw, hw)}).")
        current = "bootstub (no firmware running)" if panda.bootstub else panda.version()
    log(f"Panda {serial} ({mcu.name}): {current}")
    if pandas[0].kind != "bootstub":
        with Panda.open(serial) as panda:
            log("Entering the bootstub")
            panda.reset("bootstub")
        wait_for(lambda: _panda_with(serial, "bootstub"), "the panda's bootstub", 15, log)
    dfu_serial = enter_dfu(serial, mcu, log)
    try:
        path = backup_flash(dfu_serial, folder, serial, current, log)
    finally:
        # Leave the bootloader whatever happened: nothing was erased, so the panda starts as before
        with StDfu.open(dfu_serial) as dfu:
            log("Restarting the panda")
            dfu.jump(p.FLASH_BASE)
    wait_for(lambda: _panda_with(serial, "panda") or _panda_with(serial, "bootstub"), "the panda to restart", 20, log)
    return path
