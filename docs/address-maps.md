# Address maps

An address map tells the dashboard which CAN IDs and bits of a vehicle carry which values, and
how to show each one: as a gauge, a number or a status light. Maps are JSON files.

- **Built-in maps:** they live in `pandacapture/maps/`, and `pandacapture maps` lists them.
- **Your own map:** pass any file with `pandacapture dashboard --map my-car.json`.

```json
{
  "name": "Kia Stinger 3.3T (P-CAN)",
  "vehicle": "2018-2020 Kia Stinger 3.3T",
  "bitrate": 500,
  "notes": "Shown at the bottom of the dashboard.",
  "signals": [
    {"key": "rpm", "label": "Engine speed", "group": "Engine", "id": "0x316", "byte": 2, "bits": 16,
     "scale": 0.25, "unit": "rpm", "display": "gauge", "min": 0, "max": 7000,
     "warn_above": 6200, "alert_above": 6500, "source": "verified"},
    {"key": "mil", "label": "Check engine", "group": "Lamps", "id": "0x545", "byte": 0, "bit": 1,
     "bits": 1, "display": "light", "color": "amber", "source": "observed"}
  ]
}
```

## Map fields

| Field | Meaning |
|---|---|
| `name` | Shown as the dashboard's title |
| `vehicle`, `notes` | Shown under the title and at the bottom |
| `bitrate` | kbit/s. The dashboard sets every bus to it, unless you pass `--bitrate` |
| `signals` | The list below, shown in this order, grouped by `group` |

## Signal fields

| Field | Default | Meaning |
|---|---|---|
| `key` | required | Short unique name, e.g. `rpm` |
| `label` | the key | What the dashboard shows |
| `short` | the label | A shorter name for the tiles, when the label would be cut off (a phone tile fits about 20 characters). Tapping or hovering a tile still shows the full label |
| `group` | `Signals` | Section heading; signals with the same group sit together |
| `id` | required | CAN ID, as `"0x316"` or a number |
| `bus` | any | Only frames from this panda bus (0, 1 or 2) |
| `byte` | required | Byte the field starts in (0 = first data byte) |
| `bit` | 0 | Bit within the field's start where it begins (0 = least significant) |
| `bits` | 8 | Field length in bits, 1–64 |
| `order` | `little` | `little` (Intel: low byte first) or `big` (Motorola: high byte first, from `byte`) |
| `signed` | false | Two's complement |
| `scale`, `offset` | 1, 0 | Value = raw × scale + offset |
| `raw_max` | | Raw values above this mean "no data", e.g. `254` when 255 is a sensor fault |
| `raw_invalid` | | Raw values that mean "no data", e.g. `[128]` when 0x80 is "no target" |
| `labels` | | Text for values, e.g. `{"0": "P", "14": "R"}`. Unlisted values show as numbers |
| `expr` | | A derived signal: computed from signals defined above it, instead of decoded (see below) |
| `unit` | | Shown after the value |
| `decimals` | from the scale | Digits after the point |
| `display` | `number` | `gauge`, `number` or `light` |
| `min`, `max` | | Gauge range (required for gauges); also the range of high-resolution traces |
| `warn_above`, `alert_above`, `warn_below`, `alert_below` | | Gauges and numbers turn amber or red past these; gauges mark the zones |
| `on_above`, `on_below` | | Lights: on past this value. Without either, a light is on when the value isn't 0 |
| `color` | `red` | Light colour: `red`, `amber`, `green` or `blue` |
| `source` | `unconfirmed` | Where the definition came from: `verified` (checked on the car), `dbc`, `observed` or `unconfirmed`. Shown in each tile's description (tap or hover the tile) |
| `note` | | Shown when you hover over the tile |

### Bit layout examples

- **16-bit little-endian from byte 2** (RPM on 0x316): `"byte": 2, "bits": 16`. That's bytes 2–3,
  with byte 2 the low byte.
- **One bit:** bit 1 of byte 0 (the MIL on 0x545): `"byte": 0, "bit": 1, "bits": 1`.
- **12-bit big-endian field in bytes 3–4, low 4 bits unused:** `"byte": 3, "bit": 4, "bits": 12,
  "order": "big"`.
- **A derived reading:** the same bits can appear in several signals. 0x492 byte 3 is boost in
  kPa absolute (× 2.11). As psi above atmospheric it's `"scale": 0.30603, "offset": -14.696`.

## Derived signals

A signal with `expr` is computed from other signals instead of decoded from a frame, so it has no
`id`, `byte` or other frame fields. It's recomputed whenever one of its inputs updates. In high
resolution it streams at its inputs' rate.

```json
{"key": "torque_nm", "label": "Net torque", "expr": "(tqi_acor - tqfr) * 5", "unit": "Nm"},
{"key": "rpm_p2p", "label": "RPM swing (5 s)", "expr": "p2p(rpm, 5)", "unit": "rpm"},
{"key": "spark_at_min", "label": "Spark on its minimum", "expr": "tqi_ems16 <= tqi_min", "display": "light"}
```

| You can use | Meaning |
|---|---|
| numbers, keys of signals defined above | `rpm`, `0.25` |
| `+ - * /`, brackets, unary minus | |
| `< <= > >= == !=` | 1 when true, 0 when false: handy for lights |
| `abs(x)`, `min(a, b, ...)`, `max(a, b, ...)` | |
| `p2p(key, seconds)`, `lo(key, seconds)`, `hi(key, seconds)`, `avg(key, seconds)` | peak-to-peak, lowest, highest and mean of a signal over the last 1-60 s. The key can be a derived signal above, e.g. `avg(intake_b1_err, 3)` |

Nothing else is accepted. Expressions are checked when the map loads, and evaluated without running
any code from the file. A derived signal has no value until all its inputs have one.

## Finding signals

- **Against another tool's log:** record a capture while another logger (a tuner's datalog, say) records the same
  drive, then run `pandacapture match capture.log other-log.csv`. It ranks every field against
  every column of the other log.
- **Against the ECU's own answers:** if anything was polling the ECU over OBD during the capture,
  `pandacapture match capture.log --obd` uses those answers as the reference instead.
- **Other tools:** PandaCapture Android's signal finder, SavvyCAN, or a DBC from
  [opendbc](https://github.com/commaai/opendbc).

Mark what you've checked against a real gauge or the ECU's own answers as `verified`.

## Rates

The dashboard streams at two rates, switched on the page:
- **Normal:** each signal's latest value, 10 times a second.
- **High resolution:** every decoded sample, with its receive time, as soon as it arrives. A
  100 Hz signal shows all 100 updates a second, with a 10-second trace on each tile.
