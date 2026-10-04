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
  - Black/Grey/White Panda (STM32F4), from comma's pin `e462c34d`
  - The signed app verifies against the development key the bootstub checks.
  - The version reads `PANDACAPTURE-…`, and the transmit-gate code is in the binary.
- The one-file Windows program runs `selftest` and finds its bundled firmware.

## Tested on hardware (2026-10-02, oneclone mini blackpanda)
- The board reports hardware type 2 (Grey Panda) and runs a community fork's firmware,
  `DEV-192f74aa-DEBUG`, which reports packet versions 16/4/5.
- `list` and `info` work, and Windows binds WinUSB to it by itself.
- A listen-only capture works: setup, silent mode (confirmed in its health packet), bit rate
  detection on all three buses, recording and closing.
  - USB power only, no bus connected, so no frames yet.
- Two fixes came from this run: CAN health bit rates (10x too low) and the FD display on F4 boards.
- Backup: the whole 1.5 MB flash read back through the STM32 bootloader in about 1.5 minutes. The
  bootloader started in its error state; PandaCapture now clears it first. The F4 DFU serial formula
  matched the real bootloader.
  - The board held a debug bootstub (`v1.0.0-DEV-6e96d044-DEBUG`) and the fork firmware.
- First flash through DFU: backup, bootstub, firmware. It runs `PANDACAPTURE-…-e462c34d-DEBUG` and boots
  silent.
  - It exposed the F4 version string being one character short; fixed in the F4 build patch.
- Update through the bootstub: 3.5 s, signature verified.
  - It exposed Windows briefly refusing access to a panda that had just restarted; opens now retry.
- Transmit gate on the real firmware, with no bus connected and no frames sent:
  - refused, staying silent: fork mode 29, openpilot's Hyundai mode, ELM327, ALLOUTPUT without
    the arm code, ALLOUTPUT with a wrong code
  - allowed: noOutput (ACK only)
  - armed: ALLOUTPUT with the arm code (controls_allowed on)
  - Stopping the heartbeats, after a refused request to disable heartbeat checks, disarmed it after 1.6 s.

## Not yet tested on hardware
- `pandacapture obd` (2026-10-04): tested against a simulated car (tests/test_obd.py). Covered:
  supported-PID discovery across modules and buses, discarding with reasons, polling, and the
  900 rpm interlock (blocked before any request, stopped mid-scan, a brief spike). Not yet run on
  a car.
- RP1210 and J2534 adapters (2026-10-03). Tested against a fake driver DLL (tests/fake_driver.c),
  64-bit, for both APIs: frames, transmit echoes skipped, lost-adapter errors, refused bit rates.
  NEXIQ's real 32-bit RP1210 and J2534 drivers load in the 32-bit bridge and report
  ERR_OPENING_PORT with the adapter unplugged. Not yet tried with an adapter on a bus. The RP1210
  frame decoding is FrostCapture's, which recorded the Stinger's P-CAN through a USB-Link 2.
- No Red Panda has been flashed or captured from yet. The protocol follows comma's library for the
  pinned firmware, but these are unconfirmed:
  - auto bit rate detection on a real bus
  - DFU on Windows with Zadig's WinUSB driver
  - the green LED while armed, on a panda that has status LEDs (the oneclone mini blackpanda has none)
  - the heartbeat timeout
- First real-bus capture (reported by the user's tuning session, 2026-10-03): Kia Stinger 3.3T
  P-CAN at 500k through the oneclone board's 26-pin, on PandaCapture firmware.
  - 400,330 frames in 164.9 s (about 2,430 frames/s), 64 IDs, no dropouts or stalls.
  - Bit rate auto-detected on bus 0 (980 frames during detection), recorded silent / listen-only.
  - For comparison, the NEXIQ USB-Link 2 FrostCapture managed about 1,870 frames/s, with an
    adapter dropout and a 10.3 s stall.
- Signal matching (2026-10-03): a panda capture and a JB4 log of the same short drive (no boost).
  - The logs lined up at RPM r = 0.9998, which shows the JB4's timestamp counts tenths of a second.
  - The JB4 had been polling the ECU over OBD, and the ECU's answers in the capture confirmed these
    against the ECU's own values:
    - lambda per bank (0x5CF b0/b2, 0.5 + raw/128)
    - short-term trims (0x557 b0/b2, OBD encoding)
    - OBD throttle (0x556 b5)
    - fuel rail pressure (0x557 b5-6, gauge bar)
    - coolant (0x556 b1, OBD encoding)
  - Long-term trims (0x557 b1/b3) are likely but rest on only 12 samples.
  - Load, boost, wastegate, and per-cylinder ignition didn't match: they were constant in this log,
    or aren't broadcast.
- Restore hasn't been run on hardware yet. The backup of the board's original firmware is kept
  locally.
- Transmitting frames on a real bus: the transmit gate is verified, but no frame has been sent yet.
- A genuine comma Black Panda. The F4 flash layout, DFU block size, DFU serial formula and health
  layout come from comma's last F4-capable library and firmware, with tests against those pinned
  sources; the oneclone board confirmed the DFU serial and health layout.
- A Black Panda on old stock firmware: capture needs CAN packet format 4 (2023 or later).
  Anything older is refused with a hint to flash.
