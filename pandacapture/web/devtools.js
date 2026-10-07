// PCDev: ready-made hooks for tools built into the Developer Tools page. Everything a tool needs to reach the
// running dashboard — its live values, the address map, the JSON API, the units setting — plus small helpers to
// mount UI in the page's own style. Load after units.js. A tool grabs these off window.PCDev; the Developer Tools
// page lists each one with a copyable snippet.
(() => {
  "use strict";
  const U = window.PCUnits;

  // ---- the JSON API ----
  const get = (path) => fetch(path).then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; });
  const post = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
    .then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; });

  // ---- the live stream ----
  // onState: the latest value of every signal, ~10 times a second (the dashboard's own feed). cb gets the parsed
  // /events snapshot: { ...status, values: { key: { v, age, stale, level|on, text? } } }. Returns a stop().
  function onState(cb) {
    const es = new EventSource("/events");
    es.addEventListener("snapshot", (e) => { try { cb(JSON.parse(e.data)); } catch (err) {} });
    return () => es.close();
  }
  // onSamples: every decoded sample as it arrives (high-resolution). cb gets { key, v, t } (t = seconds since the
  // reader started). The feed also drops a "lost" count when a slow client falls behind. Returns a stop().
  function onSamples(cb, onLost) {
    const es = new EventSource("/events?mode=high");
    es.addEventListener("samples", (e) => {
      try {
        const d = JSON.parse(e.data) || {};
        (d.s || []).forEach(([key, v, t]) => cb({ key, v, t }));
        if (d.lost && onLost) onLost(d.lost);
      } catch (err) {}
    });
    return () => es.close();
  }
  // onSignal: just one signal's value, from the live snapshot feed. cb(v, entry). Returns a stop().
  function onSignal(key, cb) {
    return onState((s) => { const e = s.values && s.values[key]; if (e) cb(e.v, e); });
  }
  // map: the address map (signals and their decoding). onMap also re-fetches whenever the map is switched.
  const map = () => get("/map");
  function onMap(cb) {
    let version = null;
    map().then(cb).catch(() => {});
    return onState((s) => { if (s.map_version !== version) { version = s.map_version; map().then(cb).catch(() => {}); } });
  }

  // ---- convenience wrappers for the side-effect routes ----
  const record = (on = true) => post("/record", { on });
  const marker = () => post("/marker", {});

  // ---- mounting UI in the page's own style ----
  const HOST = () => document.getElementById("toolHost");
  // panel: a titled <section> added to the Developer Tools build area, so a tool looks like the rest of the app.
  // Returns the section element; put your controls in it.
  function panel(title, hint) {
    const sec = document.createElement("section");
    sec.innerHTML = `<h2>${esc(title)}</h2>` + (hint ? `<p class="hint">${esc(hint)}</p>` : "");
    (HOST() || document.querySelector("main")).appendChild(sec);
    return sec;
  }
  // spark: a quick sparkline of recent numbers into a canvas (makes one if given a plain element). Handy for a
  // live readout without pulling in the Runs page's full chart engine.
  function spark(el, values, opts = {}) {
    const cv = el.tagName === "CANVAS" ? el : el.appendChild(document.createElement("canvas"));
    const w = cv.width = (opts.width || el.clientWidth || 240) * devicePixelRatio;
    const h = cv.height = (opts.height || 48) * devicePixelRatio;
    cv.style.width = (w / devicePixelRatio) + "px"; cv.style.height = (h / devicePixelRatio) + "px";
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    const xs = values.filter((v) => typeof v === "number" && !Number.isNaN(v));
    if (xs.length < 2) return cv;
    const lo = opts.min != null ? opts.min : Math.min(...xs), hi = opts.max != null ? opts.max : Math.max(...xs);
    const span = hi - lo || 1, pad = 2 * devicePixelRatio;
    ctx.beginPath();
    xs.forEach((v, i) => {
      const x = pad + i / (xs.length - 1) * (w - 2 * pad);
      const y = h - pad - (v - lo) / span * (h - 2 * pad);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.strokeStyle = opts.color || getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#4fb3ff";
    ctx.lineWidth = 2 * devicePixelRatio; ctx.lineJoin = "round"; ctx.lineCap = "round";
    ctx.stroke();
    return cv;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  window.PCDev = { get, post, onState, onSamples, onSignal, map, onMap, record, marker, panel, spark, esc, units: U };
})();
