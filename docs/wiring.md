# Wiring

Two connectors matter:
- **The panda's OBD-C port:** a USB-C socket that carries CAN buses, power and two sense lines
  instead of USB. The Red Panda and the Black Panda both have one.
- **A comma car harness's 26-pin connector:** most installs go through one (Option 3).

The pin assignments are comma's, from its [hardware repository](https://github.com/commaai/hardware):
[OBD-C.sch.pdf](https://github.com/commaai/hardware/blob/master/harness/OBD-C.sch.pdf) and
[open_pinout.sch.pdf](https://github.com/commaai/hardware/blob/master/harness/v1/open_pinout.sch.pdf).

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

## Option 3: a comma car harness (26-pin connector)

Most pandas meet the car through a comma car harness. The harness plugs in between the car and its
ADAS camera, and joins a harness box at a **26-pin Molex connector** (part 501646-2600, crimp pins
501647-1000). The box then goes to the panda's OBD-C port. The table is comma's
[open pinout](https://github.com/commaai/hardware/blob/master/harness/v1/open_pinout.sch.pdf),
looking at the wire side of the cable connector. Pins 1 and 2 are at the car end of the connector,
pins 25 and 26 at the camera end.

| Pin | Signal | Wire | Pin | Signal | Wire |
|---|---|---|---|---|---|
| 1 | GND | black | 2 | IGN (ignition) | brown |
| 3 | optional resistor loopback | | 4 | **CAN0_H** (car) | orange |
| 5 | optional resistor loopback | | 6 | **CAN0_L** (car) | green |
| 7 | PT4 | purple | 8 | **CAN1_H** (radar) | pink |
| 9 | PT3 | yellow | 10 | **CAN1_L** (radar) | blue |
| 11 | PT2 | white | 12 | 12 V in | red |
| 13 | PT1 | grey | 14 | 12 V in | red |
| 15 | PT1 | grey | 16 | IGN | brown |
| 17 | PT2 | white | 18 | **CAN1_H** (radar) | pink |
| 19 | PT3 | yellow | 20 | **CAN1_L** (radar) | blue |
| 21 | PT4 | purple | 22 | **CAN2_H** (camera) | orange |
| 23 | optional resistor loopback | | 24 | **CAN2_L** (camera) | green |
| 25 | optional resistor loopback | | 26 | GND | black |

- **PT1–PT4** are pass-throughs: the camera's other wires, joined end to end through the box.
- **Buses:** CAN0 is the car side and arrives at the panda as bus 0 (`can0`). CAN1 is usually the
  radar: bus 1. CAN2 is the camera side: bus 2.
- **The relay:** the box's relay joins CAN0 and CAN2 unless the panda opens it. PandaCapture never
  opens it, so buses 0 and 2 show the same traffic and the camera keeps working.
- **Termination:** a jumper from pin 3 to pin 5 adds 120 Ω to bus 0. A jumper from pin 23 to pin
  25 takes bus 2 from 120 Ω to 60 Ω. Leave both out when tapping a car's bus, which already has
  its terminators.
- **Other drawings:** some third-party drawings number the buses 1–3. Their "1H/1L" is CAN0 on
  pins 4/6, and "3H/3L" is CAN2 on pins 22/24.

### Tapping a bus through the 26-pin connector

To record a bus the harness doesn't reach, such as a powertrain bus, wire it into a spare 26-pin
plug. comma sells a pre-crimped development harness, which saves buying Molex's crimp tool.

| Wire to | 26-pin |
|---|---|
| CAN-H / CAN-L of the bus to record | 4 / 6 (CAN0, bus 0) |
| A second bus, if wanted | 8 / 10 (CAN1, bus 1) |
| +12 V | 12 and 14 |
| Ground | 1 and 26 |
| Ignition (optional; PandaCapture doesn't need it) | 2 |

Then plug it into the harness box, and the box into the panda with the OBD-C cable.

### Example: a third-party 26-pin pigtail

Colours mean nothing on third-party cables. One supplied with a oneclone mini blackpanda used red
for ground. Go by cavity number: the harness box fixes each cavity's function, whatever the wire
colour. This cable read:

| Cavity | Wire | Function |
|---|---|---|
| 1, 26 | long red, yellow | ground |
| 12, 14 | purple, orange | +12 V |
| 4 / 6 | blue / grey | CAN0 high / low: bus 0, the one to tap |
| 22 / 24 | long black / green | CAN2 high / low: bus 2, joined to bus 0 by the relay |
| 9 ↔ 19, 11 ↔ 17 | short brown ↔ short red, short black ↔ short white | pass-throughs, unused |

Read the cavity numbers off the housing, or pair the wires up with a continuity meter through
the harness box (see Option 3's notes). Confirm ground and +12 V before applying power.

**Check the termination before tapping a car's bus.** With the cable in the box and everything
unpowered, measure CAN0 high to low. About 120 Ω means the box terminates the bus, which suits a
bench ECU but adds a third terminator to a car's bus.

### comma power (RJ45)

comma power plugs into the harness with an RJ45 jack. It brings the OBD-II port's CAN in as
**CAN3**: record it as bus 1 with `--obd`.

| RJ45 pin | Signal |
|---|---|
| 1, 5 | GND |
| 2 / 4 | CAN3_L / CAN3_H (OBD-II) |
| 3 / 6 | CAN0_L / CAN0_H |
| 7, 8 | VIN |

## Black Panda

The Black Panda's OBD-C port has the same pinout, the same three buses plus the multiplexed one,
and goes to the same harness box and 26-pin harness. It doesn't support CAN FD.
