// PCDev: ready-made hooks for tools built onto the Developer Tools page. What a tool needs to reach the running
// dashboard (its live values, the address map, the JSON API, the units setting), plus small helpers to mount UI
// in the page's own style. Load after units.js. The Developer Tools page lists each hook with a snippet.
//
// Live hooks share one connection per feed: browsers allow only 6 connections to the dashboard across all its
// windows, and each live feed holds one open, so a page that opened one per subscriber would stall every other
// request (the gauges' included). A feed opens with its first subscriber and closes after its last one stops.
(() => {
  "use strict";
  const U = window.PCUnits;

  // ---- the JSON API ----
  const answer = async (r) => {
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };
  const get = (path) => fetch(path).then(answer);
  const post = (path, body) => fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) }).then(answer);

  // ---- the live feeds, one connection each, shared by every subscriber ----
  function feed(url, event) {
    const subs = new Set();
    let source = null;
    return (cb) => {
      subs.add(cb);
      if (!source) {
        source = new EventSource(url);
        source.addEventListener(event, (e) => {
          let data;
          try { data = JSON.parse(e.data); } catch (err) { return; }
          for (const f of [...subs]) {
            try { f(data); } catch (err) { console.error("PCDev subscriber failed:", err); }
          }
        });
      }
      return () => {
        subs.delete(cb);
        if (!subs.size && source) { source.close(); source = null; }
      };
    };
  }
  const stateFeed = feed("/events", "snapshot");
  const sampleFeed = feed("/events?mode=high", "samples");

  // onState: the latest value of every signal, about 10 times a second (the gauges' own feed). cb gets
  // { ...status, values: { key: { v, age, stale, level or on, text? } } }. Returns a stop().
  const onState = (cb) => stateFeed(cb);

  // onSignal: one signal's value from that feed, each time it comes. cb(v, entry). Returns a stop().
  const onSignal = (key, cb) => stateFeed((s) => { const e = s.values && s.values[key]; if (e) cb(e.v, e); });

  // onSamples: every decoded sample as it arrives (high resolution), as { key, v, t } with t the frame's Unix
  // time in seconds. A heavy feed: subscribe only while a tool needs it. onLost(n) hears of samples dropped when
  // the page fell behind. Returns a stop().
  const onSamples = (cb, onLost) => sampleFeed((d) => {
    for (const [key, v, t] of d.s || []) cb({ key, v, t });
    if (d.lost && onLost) onLost(d.lost);
  });

  // map: the address map in use (its signals and how each is decoded). onMap: cb(map) now, and again whenever
  // the map is switched. Returns a stop().
  const map = () => get("/map");
  function onMap(cb) {
    let version;
    return stateFeed((s) => {
      if (s.map_version === version) return;
      version = s.map_version;
      map().then(cb).catch((err) => console.error("PCDev.onMap:", err));
    });
  }

  // ---- the recording ----
  const record = (on = true) => post("/record", { on });
  const marker = () => post("/marker", {});

  // ---- UI in the page's own style ----
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // panel: a titled <section> added to the Developer Tools build area (#toolHost), so a tool looks like the rest
  // of the app. Returns the section; put the tool's controls in it.
  function panel(title, hint) {
    const sec = document.createElement("section");
    sec.innerHTML = `<h2>${esc(title)}</h2>` + (hint ? `<p class="hint">${esc(hint)}</p>` : "");
    (document.getElementById("toolHost") || document.querySelector("main") || document.body).appendChild(sec);
    return sec;
  }

  // spark: a sparkline of recent numbers in el (a canvas, or an element it keeps one canvas in), for a live
  // readout without the Runs page's full chart engine. Call it again with new values to redraw. opts: min, max,
  // height (px), color.
  function spark(el, values, opts = {}) {
    const cv = el.tagName === "CANVAS" ? el
      : el.querySelector(":scope > canvas.pc-spark") || el.appendChild(Object.assign(document.createElement("canvas"), { className: "pc-spark" }));
    const dpr = window.devicePixelRatio || 1;
    const cssW = opts.width || (el.tagName === "CANVAS" ? cv.clientWidth : el.clientWidth) || 240, cssH = opts.height || 48;
    cv.style.display = "block";
    cv.style.width = cssW + "px";
    cv.style.height = cssH + "px";
    cv.width = Math.round(cssW * dpr);
    cv.height = Math.round(cssH * dpr);
    const ctx = cv.getContext("2d");
    ctx.clearRect(0, 0, cv.width, cv.height);
    const xs = values.filter((v) => typeof v === "number" && Number.isFinite(v));
    if (xs.length < 2) return cv;
    const lo = opts.min ?? Math.min(...xs), hi = opts.max ?? Math.max(...xs);
    const span = hi - lo || 1, pad = 2 * dpr, w = cv.width, h = cv.height;
    ctx.beginPath();
    xs.forEach((v, i) => {
      const x = pad + i / (xs.length - 1) * (w - 2 * pad);
      const y = h - pad - (Math.min(Math.max(v, lo), hi) - lo) / span * (h - 2 * pad);
      if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    });
    ctx.strokeStyle = opts.color || getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#4fb3ff";
    ctx.lineWidth = 2 * dpr;
    ctx.lineJoin = ctx.lineCap = "round";
    ctx.stroke();
    return cv;
  }

  window.PCDev = { get, post, onState, onSignal, onSamples, map, onMap, record, marker, panel, spark, esc, units: U };
})();
