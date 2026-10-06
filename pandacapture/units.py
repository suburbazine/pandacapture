"""US or metric: how measurements are shown. Values are kept, logged and calculated in the units they come in
(the map's, the run files'); only what people read is converted, here.

US shows mph, °F, psi, lb, ft, hp, lb-ft; metric km/h, °C, bar, kg, m, kW, Nm. Text written once and read later
(coaching and check details in run files, event summaries) carries its measurements as tokens,
`{kind:value:decimals}` (kinds: kmh, psi, c for °C, m, kg, hp, lbft), shown with render() in whatever units are
set when it's read. The same conversions and tokens as PandaCapture Android's Units, so either reads the other's.
The pages do the same in web/units.js.
"""

import re

US, METRIC = "us", "metric"
SYSTEMS = (US, METRIC)

KMH_PER_MPH = 1.609344
KPA_PER_PSI = 6.894757
M_PER_FT = 0.3048
KG_PER_LB = 0.45359237
NM_PER_LBFT = 1.3558179
KW_PER_HP = 0.7456999

# unit -> (shown unit, factor, offset, extra decimals); units with no counterpart (%, rpm, V, g/s, λ, °) stay
_TO = {
    US: {"km/h": ("mph", 1 / KMH_PER_MPH, 0.0, 0), "°C": ("°F", 1.8, 32.0, 0), "kPa": ("psi", 1 / KPA_PER_PSI, 0.0, 1),
         "bar": ("psi", 100 / KPA_PER_PSI, 0.0, 0), "Nm": ("lb-ft", 1 / NM_PER_LBFT, 0.0, 0),
         "kg": ("lb", 1 / KG_PER_LB, 0.0, 0), "m": ("ft", 1 / M_PER_FT, 0.0, 0), "km": ("mi", 1 / KMH_PER_MPH, 0.0, 0),
         "L/h": ("gal/h", 0.2641720524, 0.0, 1), "kW": ("hp", 1 / KW_PER_HP, 0.0, 0)},
    METRIC: {"mph": ("km/h", KMH_PER_MPH, 0.0, 0), "°F": ("°C", 1 / 1.8, -32 / 1.8, 0),
             "psi": ("bar", KPA_PER_PSI / 100, 0.0, 1), "kPa": ("bar", 0.01, 0.0, 2), "lb-ft": ("Nm", NM_PER_LBFT, 0.0, 0),
             "lb": ("kg", KG_PER_LB, 0.0, 0), "ft": ("m", M_PER_FT, 0.0, 1), "mi": ("km", KMH_PER_MPH, 0.0, 0),
             "hp": ("kW", KW_PER_HP, 0.0, 0)},
}


def system_of(name) -> str:
    """"metric" or "us" (the default) from a setting's text."""
    return METRIC if str(name or "").strip().lower() == METRIC else US


def conversion(unit: str, system=US):
    """(shown unit, factor, offset, extra decimals) for a value in `unit`."""
    return _TO[system_of(system)].get(unit, (unit, 1.0, 0.0, 0))


def value(v: float, unit: str, system=US) -> float:
    _, f, o, _ = conversion(unit, system)
    return v * f + o


def show(v: float, unit: str, decimals: int, system=US) -> str:
    """A value in `unit` shown in the system's unit, e.g. (96.6, "km/h", 0) -> "60 mph"."""
    shown, f, o, extra = conversion(unit, system)
    return f"{v * f + o:,.{max(decimals + extra, 0)}f} {shown}".rstrip()


def token(kind: str, v: float, decimals: int = 0) -> str:
    return f"{{{kind}:{v:.4f}:{decimals}}}"


def kmh(v, decimals=0):
    return token("kmh", v, decimals)


def psi(v, decimals=1):
    return token("psi", v, decimals)


def celsius(v):
    return token("c", v, 0)


TOKEN = re.compile(r"\{(kmh|psi|c|m|kg|hp|lbft):(-?[0-9.]+):([0-9])\}")
_UNIT_OF = {"kmh": "km/h", "psi": "psi", "c": "°C", "m": "m", "kg": "kg", "hp": "hp", "lbft": "lb-ft"}


def render(text: str, system=US) -> str:
    """Text with tokens, its measurements shown in the system's units. Text without tokens is as it was."""
    return TOKEN.sub(lambda m: show(float(m.group(2)), _UNIT_OF[m.group(1)], int(m.group(3)), system), text)
