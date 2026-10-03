# Status

## Verified
- Unit tests (`python -m pytest`):
  - Packet encoding and decoding, including packets split across USB transfers and corrupted ones.
  - The candump output matches the FrostBYTE Android parser's patterns.
  - The flash workflow against a simulated panda: first install through DFU, update, resuming from
    DFU, refusing comma-device and F4 pandas.
  - The transmit gate: acknowledgement, firmware check, arm, heartbeat, disarm after errors.
  - The host's constants and structs match the pinned firmware source, and the patches apply to it.
- Simulated capture with a dropout and reconnect (`pandacapture --simulate-dropout 1`).
- Both firmware builds compile natively on Windows with Arm GNU Toolchain 13.2.rel1, the GCC
  version comma uses, with comma's `-Werror`:
  - Red Panda (STM32H7)
  - Black Panda (STM32F4), from comma's pin `f849893b`
  - The signed app verifies against the development key the bootstub checks.
  - The version reads `PANDACAPTURE-…`, and the transmit-gate code is in the binary.
- The one-file Windows program runs `selftest` and finds its bundled firmware.

## Not yet tested on hardware
- No Red Panda has been flashed or captured from yet. The protocol follows comma's library for the
  pinned firmware, but these are unconfirmed:
  - auto bit rate detection on a real bus
  - DFU on Windows with Zadig's WinUSB driver
  - the green LED while armed
  - the heartbeat timeout
- No Black Panda has been flashed yet. Its flash layout, DFU block size, DFU serial formula and
  health layout come from comma's last F4-capable library and firmware, with tests against those
  pinned sources.
- A Black Panda on old stock firmware: capture needs CAN packet format 4 (2023 or later).
  Anything older is refused with a hint to flash.
