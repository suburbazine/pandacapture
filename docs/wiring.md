# Wiring

The Red Panda and the Black Panda both connect to the car through an **OBD-C** port: a USB-C socket
that carries CAN buses, power and two sense lines instead of USB. The pin assignment below is
comma's, from [OBD-C.sch.pdf](https://github.com/commaai/hardware/blob/master/harness/OBD-C.sch.pdf)
in comma's hardware repository.

> **Not USB.** Never plug the panda's OBD-C port into a computer, a phone or a charger, and never
> plug a USB device into an OBD-C breakout. The panda's USB connection to your computer is its
> separate USB-A plug.

## OBD-C pinout

This is the harness-side socket (female), which is what a breakout board wired to the car
presents to the cable. The cable is a plain, fully wired USB-C 3.1/3.2 Gen 2 cable. Cheap
cables that leave out pins are a common cause of "no traffic".

| Pins | Signal | PandaCapture |
|---|---|---|
| A2 / A3 | CAN0_H / CAN0_L | bus 0, `can0` |
| A11 / A10 | CAN1_H / CAN1_L | bus 1, `can1` |
| B2 / B3 | CAN2_H / CAN2_L | bus 2, `can2` |
| B11 / B10 | CAN3_H / CAN3_L | bus 1 instead of CAN1, with `--obd` |
| A4, A9, B4, B9 | VIN (+12 V) | powers the panda's car side |
| A1, A12, B1, B12 | GND | ground |
| A8 | SBU1 | orientation sense: about 100 Ω to GND |
| B8 | SBU2 | ignition sense: 1 kΩ to GND = ignition on, open = off |
| A5–A7, B5–B7 | | not connected |

- **Orientation:** the socket can take the cable either way up. The panda works out which way by
  measuring SBU1 (about 100 Ω to ground) against SBU2 (about 1 kΩ). On a home-made breakout, fit
  the 100 Ω resistor from SBU1 to GND, so the panda reads the orientation and maps the buses as
  in the table.
- **Ignition:** PandaCapture doesn't need the ignition line. Leave SBU2 open, or fit 1 kΩ to GND.
- **The panda never drives the relay:** in a comma harness box, SBU1 also drives the relay that
  separates CAN0 from CAN2. PandaCapture's firmware never drives it, in any mode, so a harness box
  stays a plain pass-through.

## Option 1: a DIY breakout (tapping one bus)

For tapping a powertrain bus, such as the Kia Stinger's P-CAN, or an ECU on the bench. You need a
USB-C female breakout board that brings out all 24 pins (a "USB 3.1 full pin" breakout), and a
good USB-C cable.

| Car / bench | Breakout pin |
|---|---|
| CAN-H of the bus to record | A2 (CAN0_H) |
| CAN-L of the bus to record | A3 (CAN0_L) |
| +12 V (switched or battery) | A4 and A9 (VIN) |
| Ground | A1 and A12 (GND) |
| 100 Ω resistor to GND | A8 (SBU1) |

A second bus can go on A11/A10 (CAN1). Record with plain `pandacapture`. The status line shows
traffic on `bus0`.

- **Tapping a bus:** add no termination resistor. The bus already has its two 120 Ω terminators.
  Twist the CAN-H/CAN-L pair, and keep the stub to the panda short (well under a metre).
- **On the bench:** an ECU alone on a bench cable needs a 120 Ω resistor across CAN-H and CAN-L
  somewhere on the bench wiring. With nothing else on the bus, record with
  `pandacapture --ack --bitrate 500`, so the panda acknowledges the ECU's frames.

## Option 2: from the car's OBD-II port

The OBD-II port's CAN pins are standard. On many modern cars they only carry diagnostic traffic
behind a gateway; PandaCapture tells you when that's all it hears.

| OBD-II pin | Signal | Breakout pin |
|---|---|---|
| 6 | CAN-H | A2 (CAN0_H) |
| 14 | CAN-L | A3 (CAN0_L) |
| 4, 5 | chassis and signal ground | A1, A12 (GND) |
| 16 | +12 V battery | A4, A9 (VIN) |

Plus 100 Ω from A8 (SBU1) to GND, as above.

## Option 3: a comma car harness

A comma car harness and harness box sit between a car's ADAS camera and the rest of the car:

- **CAN0** (bus 0) is the car side.
- **CAN2** (bus 2) is the camera side.
- **CAN1** (bus 1) is an extra bus, usually the radar.

With the relay closed, which is always the case under PandaCapture, CAN0 and CAN2 are one bus,
so buses 0 and 2 show the same traffic. CAN3 is the multiplexed bus, which openpilot uses for
OBD-II diagnostics: record it as bus 1 with `--obd`.

## Black Panda

The Black Panda's OBD-C port has the same pinout, and the same three buses plus the multiplexed
one. It doesn't support CAN FD.
