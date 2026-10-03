# Red Panda USB protocol (for the Android OTG port)

What PandaCapture says to a Red Panda over USB, and everything the FrostBYTE Android app would need
to capture from one over USB OTG. `pandacapture/protocol.py` holds the same constants and the packet
code in a form that ports line for line to Kotlin.

## Device

| | Vendor | Product |
|---|---|---|
| Panda running firmware | `0xBBAA` (or comma's `0x3801`) | `0xDDCC` |
| Panda bootstub (its flasher) | same | `0xDDEE` |
| STM32 ROM bootloader (DFU) | `0x0483` | `0xDF11` |

- The USB serial number is the MCU's unique id, 24 hex digits.
- Interface 0 carries the panda's own vendor requests and three bulk endpoints:
  - `0x81` IN: CAN frames from the panda.
  - `0x03` OUT: CAN frames to send.
  - `0x02` OUT: firmware, bootstub only.
- The panda reports WinUSB compatible ids, so Windows binds a driver by itself. Android needs a
  `device_filter.xml` entry for vendor 48042 (`0xBBAA`) and 14337 (`0x3801`), product 56780 (`0xDDCC`).

## Control requests

All vendor requests to the device: `bmRequestType` `0xC0` to read, `0x40` to write. On Android:
`UsbDeviceConnection.controlTransfer(type, request, value, index, buffer, length, timeout)`.

| Request | Direction | value | index | What |
|---|---|---|---|---|
| `0xC1` | in, 1 byte | | | hardware type: `7` = Red Panda |
| `0xD6` | in, ≤64 bytes | | | firmware version string. PandaCapture's starts `PANDACAPTURE-` |
| `0xDD` | in, 8 bytes | | | health and CAN packet layout hashes (two little-endian u32). Older firmware, including the Black Panda's F4 build, answers 3 bytes: health, CAN and CAN-health versions (16, 4, 5) |
| `0xD2` | in | | | health packet (`health_t` in `board/health.h`; the F4 build's layout differs, see `LEGACY_HEALTH_V16`) |
| `0xC2` | in | bus | | CAN health of one bus (`can_health_t`): error counters, bit rates |
| `0xF8` | out | | | turn off openpilot's heartbeat check |
| `0xE7` | out | 0 | | power saving off. Power saving switches transceivers off |
| `0xDC` | out | mode | param | safety mode: `0` silent, `19` ACK only, `17` transmit (see below) |
| `0xDE` | out | bus | kbit/s × 10 | nominal bit rate: 10, 20, 50, 100, 125, 250, 500, 1000 kbit/s |
| `0xF9` | out | bus | kbit/s × 10 | CAN FD data rate. At or above the nominal rate, it also turns FD reception on |
| `0xE8` | out | bus | 0/1 | automatic CAN FD switching. PandaCapture sets 0 |
| `0xDB` | out | 0/1 | | put CAN3 (the multiplexed bus) on bus 1 instead of CAN1 |
| `0xC0` | out | | | reset the USB packet reassembly (send after connecting) |
| `0xF1` | out | `0xFFFF` | | empty the panda's receive queue |
| `0xF3` | out | 1 | | heartbeat. Needed every second while transmit is armed |

## Listening

The firmware boots silent. Silent means the CAN controllers are in bus-monitoring mode: they never
ACK, never send error frames, never transmit. To capture:

1. `0xF8`, then `0xE7` with value 0.
2. `0xDC` with mode 0 (silent).
3. `0xDE` for each bus, then `0xF9` for each bus (PandaCapture uses 2000 kbit/s for the FD data rate).
4. `0xC0`, then `0xF1` with value `0xFFFF`.
5. Read `0x81` in a loop with a 16 KiB buffer. The panda answers at once, with an empty transfer
   when it has nothing. Wait about 1 ms after an empty read.

To find a bit rate, stay silent and try rates: set the rate, clear, and read for about 0.4 s. Then
compare the bus's `total_errors` in `0xC2` before and after. The right rate brings frames and no errors.
The wrong rate brings errors and no frames, and silent mode means the bus never notices.

## CAN packets (endpoint 0x81 and 0x03)

The packets are back to back, and one can span two transfers: keep the tail for the next read.

```
byte 0     DLC (bits 7-4) | bus (bits 3-1) | FD (bit 0)
bytes 1-4  little-endian u32: id << 3 | extended << 2 | returned << 1 | rejected
byte 5     XOR of the header bytes 0-4 and all data bytes
bytes 6..  data: length from the DLC: 0-8, then 12, 16, 20, 24, 32, 48, 64
```

- A packet's XOR including the checksum byte is 0. If it isn't, the stream has lost sync: drop
  the buffer.
- `returned` marks the panda's echo of a frame it sent.
- `rejected` marks a frame the firmware refused to send.
- Packets carry no timestamp: stamp them when the transfer arrives.

## Transmitting (PandaCapture firmware only)

PandaCapture's firmware only transmits in mode `17` with param `0x4654` (`0xDC`, value 17, index
0x4654). Any other mode request falls back to silent. While armed, the firmware needs `0xF3` at
least every second, and returns to silent by itself within about 2 seconds without it. The green
LED is on while armed. To disarm, send `0xDC` with mode 0.

An app should only arm after its own warning and acknowledgement, the way `pandacapture send` does.

## Android notes

- Use `UsbManager.requestPermission`, `openDevice`, and `claimInterface(interface 0, force=true)`.
- Read endpoint `0x81` with `bulkTransfer` on a background thread, or with `UsbRequest` for queued reads.
- **Cabling:** the panda's computer port is a USB-A socket, and the panda is always the USB device.
  - A plain USB-C-to-A cable doesn't work from a phone or a USB-C computer port. Its USB-C plug
    tells the phone that the A end is a host, so the phone stays a device too, and nothing
    enumerates. This was seen on a PC with a oneclone board.
  - What works: an OTG adapter (USB-C plug to USB-A socket, which makes the phone the host) plus a
    USB A-to-A cable, or a USB-C OTG hub.
- Check how the Red Panda is powered before relying on a phone: it isn't verified yet whether a
  phone's OTG port can power it alone. A powered OTG hub avoids the question.
- The FrostBYTE app's CAN log parsers already read PandaCapture's candump output. For multi-bus
  logs they would need to keep the `canN` interface, which they currently ignore.
