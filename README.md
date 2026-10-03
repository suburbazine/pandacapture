# PandaCapture for comma pandas

Records a car's CAN buses through a [comma](https://comma.ai) Red Panda or Black Panda into a candump log. The log is
for finding the signals a FrostBYTE water/methanol controller should read (RPM, MAP/boost, intake
temperature), and the FrostBYTE Android app's signal finder, SavvyCAN and can-utils all read it.
PandaCapture also flashes its own firmware onto the panda, and can transmit frames once you
acknowledge a warning.

- **Listen-only by default.** The panda's CAN controllers sit in bus-monitoring mode: no ACKs, no
  error frames, nothing transmitted. The bus can't tell the panda is there.
- **Bit rates are found automatically**, by listening at each rate. A wrong rate can't disturb
  the bus while the panda is silent.
- **All three buses at once.** The log names them `can0`, `can1` and `can2`, and the status line
  shows which ones carry traffic. The Red Panda also receives CAN FD; the Black Panda is classic
  CAN only.
- **Windows and Linux** now. macOS and the FrostBYTE Android app over USB OTG are planned; see
  [docs/protocol.md](docs/protocol.md).
- **Transmitting is gated.** You type `TRANSMIT` after a warning. Only PandaCapture's firmware can
  transmit at all, and it stops by itself within about 2 seconds if this program goes away.

> **Status:** works in simulation and its unit tests. It hasn't been run against a real panda yet;
> see [STATUS.md](STATUS.md).

## Hardware

| Panda | Capture | PandaCapture firmware, transmit |
|---|---|---|
| Red Panda (STM32H7) | yes | yes |
| Black Panda (STM32F4) | yes | yes: built from comma's last F4-capable panda firmware |
| White Panda (STM32F4) | yes | the F4 build supports it; untried, needs `flash --force` |
| Panda inside a comma three / 3X | not supported | refused |

## 1. Install

Download the program for your system from [Releases](../../releases): `pandacapture-windows-x64.exe`
or `pandacapture-linux-x64`. Each release lists SHA-256 sums. Or run from source:

```bash
pip install -e .
```

- **Windows:** the panda installs its own WinUSB driver when plugged in. The first flash needs one
  more driver, for the STM32 bootloader; see "Windows driver" below.
- **Linux:** install the udev rules in [linux/](linux/README.md), or only root can open the panda.

Check it sees the panda:

```bash
pandacapture list
```

```bash
pandacapture info
```

## 2. Flash the PandaCapture firmware (once)

Capturing works on comma's firmware too. Flashing PandaCapture's firmware pins the panda to the
version this program was tested against, and is needed for transmitting.

```bash
pandacapture flash
```

- **The first time**, comma's bootstub only starts comma-signed firmware. PandaCapture puts the
  panda into the STM32's ROM bootloader (DFU), writes its own bootstub, then the firmware.
- **Later updates** only rewrite the firmware.
- **Back to openpilot:** a comma device flashes its own firmware onto a panda it's connected to.
  PandaCapture's bootstub starts comma-signed firmware too.

### Windows driver (first flash only)

The STM32 bootloader (USB `0483:DF11`) doesn't install a driver by itself. If `flash` stops with
"Windows has no WinUSB driver for it":

1. Leave the panda plugged in. It's waiting in the bootloader.
2. Open [Zadig](https://zadig.akeo.ie), choose **Options → List All Devices**, then pick
   **STM32 BOOTLOADER**.
3. Pick **WinUSB** as the driver and click **Install Driver**.
4. Run `pandacapture flash` again. It carries on from the bootloader.

## 3. Connect to the bus

Record the bus your FrostBYTE is (or will be) wired to. On many modern cars, the OBD-II port sits
behind a gateway and only carries diagnostic traffic, so tap the powertrain CAN wires directly.

**[docs/wiring.md](docs/wiring.md)** has the pinouts and three ways to connect:
- a DIY breakout tapping one bus
- the car's OBD-II port
- a comma car harness

With a comma car harness, the bus to record goes on the harness's 26-pin connector (Molex
501646-2600), which the harness box passes to the panda's OBD-C port:

| Signal | 26-pin | Bus |
|---|---|---|
| CAN-H / CAN-L, car side | 4 / 6 | bus 0 |
| CAN-H / CAN-L, radar | 8 / 10 (and 18 / 20) | bus 1 |
| CAN-H / CAN-L, camera side | 22 / 24 | bus 2 |
| +12 V | 12, 14 | |
| Ground | 1, 26 | |

The OBD-C port isn't USB: never connect it to a computer or a charger.

## 4. Record

```bash
pandacapture
```

It finds each bus's bit rate, then shows a status line every second: frame rate, message IDs,
markers, traffic per bus. On Hyundai, Kia and Genesis engines, `RPM(0x316)` appears in it.

| Key | Does |
|---|---|
| `M` | drop a numbered marker into the log |
| `1`–`9` | drop a marker with that number |
| `Q` / `Esc` / Ctrl+C | stop and save |

The log goes to a `captures` folder next to the program, as `capture-YYYYMMDD-HHMMSS.log`. A
summary of every bus and ID is added at the end.

- **Stalls** are marked: `# stall: no frames since (time)` after 2 s without frames.
- **Panda errors** are marked: `# adapter error: …`. PandaCapture then reconnects and keeps recording
  into the same file: it retries every 2 s for up to 60 s.
- **Frames the panda dropped** are marked, when its receive buffer overflowed.

### What to record

Drop a marker before each step:

1. **Key on, engine off** (`1`), about 10 s.
2. **Idle** (`2`), about 10 s.
3. **A few throttle blips** (`3`).
4. **Optional: a pull into boost** (`4`), with a reference log running, such as the FrostBYTE
   app's logger, a JB4 or an ECU log.

Then copy the log to your phone and open it in the FrostBYTE app: **CAN → Find signals → Open
capture…**.

### Bench (ECU on a bench cable)

With the panda as the only other node, nothing acknowledges the ECU's frames, so let the panda
acknowledge them. It still never transmits a frame:

```bash
pandacapture --ack --bitrate 500
```

## Transmitting

```bash
pandacapture send 7DF#02010C --bus 0 --count 5 --interval-ms 200
```

```bash
pandacapture replay captures/capture-20261002-120000.log --bus-map 0:1 --ids 316,329
```

Both commands show what is about to be sent and a warning, and wait for you to type `TRANSMIT`.
For scripts, `--i-accept-transmit-risk` skips the typing; the warning is still printed. Then:

- **Firmware check:** the panda must run PandaCapture firmware. Its transmit gate refuses every
  other way of transmitting, including openpilot's car modes.
- **Arming:** the green LED is on while transmit is armed.
- **Heartbeat:** PandaCapture sends one several times a second. Without it, the panda drops back to
  listen-only within about 2 seconds.
- **Log:** every frame sent goes to a `tx-….log` in the captures folder.

Only transmit on a bench, or with the vehicle parked and secured.

## Options

```
pandacapture [options]          record
  --bitrate RATE                auto (default), kbit/s for all buses (500), or per bus (0=500)
  --bus N                       record only bus N (repeatable)
  --data-bitrate KBPS           CAN FD data rate (default 2000)
  --ack                         acknowledge frames (bench); needs --bitrate
  --obd                         record CAN3 (the multiplexed OBD bus) as bus 1
  --out DIR                     where to save captures
  --seconds N                   stop after N seconds
  --no-reconnect                stop on a panda error
  --reconnect-seconds N         how long to keep trying (default 60)
  --serial S                    which panda (see: pandacapture list)
  --simulate                    fake traffic, no panda needed
  --simulate-dropout N          fake traffic that drops out after N seconds

pandacapture list | info | flash | send | replay | selftest      (each has --help)
```

## Privacy

A capture holds everything the car broadcasts. Check it before sharing: some cars broadcast the
VIN or odometer.

## Building

```bash
git clone --recurse-submodules https://github.com/suburbazine/pandacapture
```

- **Tests:** `pip install -e ".[dev]"`, then `python -m pytest`.
- **Firmware:** `python firmware/build.py` builds both targets: `h7` (Red Panda) and `f4` (Black
  Panda). See the top of [firmware/build.py](firmware/build.py) for the Arm toolchain and
  pycryptodome. It builds on Windows, Linux or in Docker (`--docker`).
- **One-file program:** `pip install ".[package]"`, then `python packaging/build_exe.py`.
- **CI:** [GitHub Actions](.github/workflows/build.yml) builds the firmware and the Windows and
  Linux programs on every push, and publishes a release for every `v*` tag.

The firmware is comma's panda firmware at pinned commits, plus the patches in
[firmware/patches](firmware/patches), signed with the panda project's public development key:
- **Red Panda:** a recent commit.
- **Black Panda:** comma's last commit before it removed STM32F4 support (`f849893b`, July 2025),
  with the opendbc commit it pinned. comma no longer maintains that firmware.
PandaCapture isn't made or endorsed by comma.ai. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
