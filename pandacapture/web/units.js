// US or metric: how the pages show measurements. One setting for every page (this browser's "pc-units"); values
// stay in the units they come in (the map's, the run files'), and only what's shown is converted. The same
// conversions and {kind:value:decimals} tokens as units.py and PandaCapture Android's Units.
(() => {
  "use strict";
  const KEY = "pc-units";
  const KMH_PER_MPH = 1.609344, KPA_PER_PSI = 6.894757, M_PER_FT = 0.3048, KG_PER_LB = 0.45359237,
    NM_PER_LBFT = 1.3558179, KW_PER_HP = 0.7456999;
  // unit -> [shown unit, factor, offset, extra decimals]; units with no counterpart (%, rpm, V, g/s, λ, °) stay
  const TO = {
    us: { "km/h": ["mph", 1 / KMH_PER_MPH, 0, 0], "°C": ["°F", 1.8, 32, 0], kPa: ["psi", 1 / KPA_PER_PSI, 0, 1],
      bar: ["psi", 100 / KPA_PER_PSI, 0, 0], Nm: ["lb-ft", 1 / NM_PER_LBFT, 0, 0], kg: ["lb", 1 / KG_PER_LB, 0, 0],
      m: ["ft", 1 / M_PER_FT, 0, 0], km: ["mi", 1 / KMH_PER_MPH, 0, 0], "L/h": ["gal/h", 0.2641720524, 0, 1],
      kW: ["hp", 1 / KW_PER_HP, 0, 0] },
    metric: { mph: ["km/h", KMH_PER_MPH, 0, 0], "°F": ["°C", 1 / 1.8, -32 / 1.8, 0], psi: ["bar", KPA_PER_PSI / 100, 0, 1],
      kPa: ["bar", 0.01, 0, 2], "lb-ft": ["Nm", NM_PER_LBFT, 0, 0], lb: ["kg", KG_PER_LB, 0, 0],
      ft: ["m", M_PER_FT, 0, 1], mi: ["km", KMH_PER_MPH, 0, 0], hp: ["kW", KW_PER_HP, 0, 0] },
  };
  const read = () => { try { return localStorage.getItem(KEY) === "metric" ? "metric" : "us"; } catch (e) { return "us"; } };
  let system = read();
  const listeners = [];
  const tell = () => listeners.forEach((f) => f(system));

  const conversion = (unit, sys = system) => {
    const c = TO[sys][unit];
    return c ? { unit: c[0], f: c[1], o: c[2], extra: c[3] } : { unit: unit || "", f: 1, o: 0, extra: 0 };
  };
  const num = (v, d) => (v == null || Number.isNaN(v)) ? "–" :
    Number(v).toLocaleString("en-US", { minimumFractionDigits: Math.max(0, d), maximumFractionDigits: Math.max(0, d) });
  const value = (v, unit, sys = system) => { const c = conversion(unit, sys); return v * c.f + c.o; };
  /** A value in `unit` shown in the system's unit, e.g. (96.6, "km/h", 0) → "60 mph". */
  const show = (v, unit, d, sys = system) => { const c = conversion(unit, sys); return `${num(v * c.f + c.o, d + c.extra)} ${c.unit}`.trim(); };

  const TOKEN = /\{(kmh|psi|c|m|kg|hp|lbft):(-?[0-9.]+):([0-9])\}/g;
  const UNIT_OF = { kmh: "km/h", psi: "psi", c: "°C", m: "m", kg: "kg", hp: "hp", lbft: "lb-ft" };
  /** Text with tokens, its measurements shown in the system's units. Text without tokens is as it was. */
  const render = (text, sys = system) => String(text ?? "").replace(TOKEN, (_, k, v, d) => show(Number(v), UNIT_OF[k], Number(d), sys));

  // ---- runs: 0-60 mph and 0-100 km/h are different runs, not one in two units ----
  const HEADLINE_ORDER = {
    us: ["0-60 mph", "1/4 mile", "1/8 mile", "0-100 mph", "60-130 mph", "40-100 mph", "60-0 mph", "0-30 mph", "60 ft"],
    metric: ["0-100 km/h", "1/4 mile", "1/8 mile", "0-200 km/h", "100-200 km/h", "100-0 km/h", "60 ft"],
  };
  const shownIn = (name, sys = system) => name.includes("mph") ? sys === "us" : name.includes("km/h") ? sys === "metric" : true;
  /** The results to show (all of them, if none is in the system's units). */
  const shown = (metrics, sys = system) => { const s = metrics.filter((m) => shownIn(m.name, sys)); return s.length ? s : metrics; };
  const headline = (metrics, sys = system) => {
    const order = HEADLINE_ORDER[sys], ms = shown(metrics, sys), std = ms.filter((m) => m.standard);
    const rank = (m) => { const i = order.indexOf(m.name); return i < 0 ? order.length : i; };
    return std.length ? std.reduce((a, b) => rank(b) < rank(a) ? b : a) : ms[0];
  };

  /** A capture's speed range tag: "0–64 mph" (or km/h), "Stationary", or "No speed". */
  const speedLabel = (sp, sys = system) => !sp.heard ? "No speed" : sp.max < 2 ? "Stationary" :
    `${num(value(sp.min, "km/h", sys), 0)}–${num(value(sp.max, "km/h", sys), 0)} ${conversion("km/h", sys).unit}`;

  /** A button that switches the setting, labeled with the units in use. */
  const button = (el) => {
    const label = () => {
      el.textContent = system === "metric" ? "Metric" : "US";
      el.title = `Units: ${system === "metric" ? "metric (km/h, °C, bar, kg, m, kW)" : "US (mph, °F, psi, lb, ft, hp)"}. Click to switch.`;
      el.setAttribute("aria-label", el.title);
    };
    label();
    el.addEventListener("click", () => set(system === "metric" ? "us" : "metric"));
    listeners.push(label);
  };

  const set = (s) => {
    system = s === "metric" ? "metric" : "us";
    try { localStorage.setItem(KEY, system); } catch (e) {}
    tell();
  };
  // Another window of the app switched them
  addEventListener("storage", (e) => { if (e.key === KEY) { system = read(); tell(); } });

  window.PCUnits = {
    get system() { return system; }, set, onChange: (f) => listeners.push(f), button,
    conversion, value, show, num, render, shownIn, shown, headline, speedLabel, KMH_PER_MPH, M_PER_FT, KG_PER_LB,
  };
})();
