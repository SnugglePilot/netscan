/* netscan dashboard. Plain JS + uPlot (vendored). All labels go in via textContent. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const RANGES = { "1h": 3600, "6h": 6 * 3600, "24h": 86400, "3d": 3 * 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
  const state = { preset: "24h", from: null, to: null, net: "", history: [], networks: [], netIndex: {} };
  const charts = {};
  let data = {};
  let refreshTimer = null;

  // ------------------------------------------------------------ helpers
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const colors = () => ({
    s: [1, 2, 3, 4, 5, 6, 7, 8].map((i) => css("--s" + i)),
    ink: css("--ink"), ink2: css("--ink-2"), muted: css("--muted"), grid: css("--grid"), axis: css("--axis"),
    surface: css("--surface"), good: css("--good"), warning: css("--warning"), serious: css("--serious"),
    critical: css("--critical"), nodata: css("--nodata"),
    seq: [0, 1, 2, 3, 4, 5, 6].map((i) => css("--seq-" + i)),
  });
  // Fixed entity colors, the same in every chart: internet = slot 1, router = slot 2, ISP first hop = slot 3.
  const ENT = { inet: 0, gw: 1, isp: 2 };

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    for (const kid of kids) if (kid !== null && kid !== undefined) e.append(kid);
    return e;
  }
  const fmt = (v, d = 1, unit = "") => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d) + unit);
  const fmtPct = (v) => {
    if (v === null || v === undefined) return "–";
    if (v === 0) return "0%";
    if (v < 0.1) return v.toFixed(3) + "%";
    if (v < 10) return v.toFixed(2) + "%";
    return v.toFixed(1) + "%";
  };
  function fmtDur(s) {
    if (s === null || s === undefined) return "–";
    s = Math.round(s);
    if (s < 90) return s + "s";
    const m = Math.round(s / 60);
    if (m < 90) return m + " min";
    const h = Math.floor(m / 60);
    if (h < 48) return h + "h " + (m % 60) + "m";
    return Math.floor(h / 24) + "d " + (h % 24) + "h";
  }
  const pad = (n) => String(n).padStart(2, "0");
  function fmtTime(ts, withDate = true) {
    const d = new Date(ts * 1000);
    const t = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
    if (!withDate) return t;
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + t;
  }
  function toLocalInput(ts) {
    const d = new Date(ts * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  const netLabel = (key) => (state.netIndex[key] ? state.netIndex[key].label : key || "–");
  const netColor = (key, c) => {
    const n = state.netIndex[key];
    return n ? c.s[n.idx % 8] : c.muted;
  };
  async function api(path, params) {
    const u = new URL(path, location.href);
    for (const [k, v] of Object.entries(params || {})) if (v !== "" && v !== null && v !== undefined) u.searchParams.set(k, v);
    const r = await fetch(u);
    if (!r.ok) throw new Error(path + " " + r.status);
    return r.json();
  }

  // ------------------------------------------------------------ range state
  function currentRange() {
    if (state.preset) {
      const to = Date.now() / 1000;
      if (state.preset === "all") return { from: state.allFrom || to - 30 * 86400, to };
      return { from: to - RANGES[state.preset], to };
    }
    return { from: state.from, to: state.to };
  }
  function setRange(from, to, push = true) {
    if (push) state.history.push({ preset: state.preset, from: state.from, to: state.to });
    state.preset = null;
    state.from = Math.floor(from);
    state.to = Math.ceil(to);
    saveHash();
    load();
  }
  function setPreset(p, push = true) {
    if (push) state.history.push({ preset: state.preset, from: state.from, to: state.to });
    state.preset = p;
    saveHash();
    load();
  }
  function zoomOut() {
    const prev = state.history.pop();
    if (prev) {
      Object.assign(state, prev);
      saveHash();
      load();
      return;
    }
    const { from, to } = currentRange();
    const span = to - from;
    const now = Date.now() / 1000;
    setRange(from - span / 2, Math.min(now, to + span / 2), false);
  }
  function saveHash() {
    const p = new URLSearchParams();
    if (state.preset) p.set("r", state.preset);
    else { p.set("from", state.from); p.set("to", state.to); }
    if (state.net) p.set("net", state.net);
    history.replaceState(null, "", "#" + p.toString());
  }
  function readHash() {
    const p = new URLSearchParams(location.hash.slice(1));
    if (p.get("from") && p.get("to")) {
      state.preset = null; state.from = +p.get("from"); state.to = +p.get("to");
    } else if (p.get("r") && (RANGES[p.get("r")] || p.get("r") === "all")) {
      state.preset = p.get("r");
    }
    state.net = p.get("net") || "";
  }

  // ------------------------------------------------------------ uPlot plumbing
  const SYNC = uPlot.sync("ns");

  function baseOpts(el, c, height, extra) {
    const { from, to } = currentRange();
    return Object.assign({
      width: Math.max(280, el.clientWidth),
      height,
      cursor: {
        sync: { key: SYNC.key, setSeries: false },
        drag: { x: true, y: false, setScale: false },
        points: { size: 7 },
      },
      select: { show: true },
      scales: { x: { time: true, range: () => [from, to] } },
      legend: { live: true },
      axes: [
        { stroke: c.muted, grid: { stroke: c.grid, width: 1 }, ticks: { stroke: c.grid, width: 1, size: 4 }, font: "11px system-ui" },
        { stroke: c.muted, grid: { stroke: c.grid, width: 1 }, ticks: { show: false }, font: "11px system-ui", size: 48 },
      ],
      hooks: {
        setSelect: [(u) => {
          if (u.select.width < 4) return;
          const a = u.posToVal(u.select.left, "x");
          const b = u.posToVal(u.select.left + u.select.width, "x");
          u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
          if (b - a >= 60) setRange(a, b);
        }],
        ready: [(u) => u.over.addEventListener("dblclick", (e) => { e.preventDefault(); zoomOut(); })],
      },
    }, extra || {});
  }

  function line(label, color, unit, digits = 1, more) {
    return Object.assign({
      label, stroke: color, width: 2, spanGaps: false, points: { show: false },
      value: (u, v) => (v === null || v === undefined ? "–" : v.toFixed(digits) + unit),
    }, more || {});
  }

  function mount(id, opts, d) {
    const host = $(id);
    if (charts[id]) { charts[id].destroy(); delete charts[id]; }
    host.textContent = "";
    if (!d[0] || !d[0].length) {
      host.append(el("div", { class: "empty", text: "No data in this range." }));
      return null;
    }
    const u = new uPlot(opts, d, host);
    SYNC.sub(u);
    charts[id] = u;
    return u;
  }

  // shades windows where something was wrong behind a chart's lines
  function shadeHook(c) {
    return (u) => {
      const s = data.series && data.series.series;
      if (!s) return;
      const { ctx, bbox } = u;
      const b = data.series.bucket;
      ctx.save();
      for (let i = 0; i < s.t.length; i++) {
        const off = s.offline[i], nonet = s.nonet[i];
        if (!(off > 0 || nonet > 0)) continue;
        const x0 = u.valToPos(s.t[i], "x", true), x1 = u.valToPos(s.t[i] + b, "x", true);
        ctx.fillStyle = off > 0 ? c.critical : c.muted;
        ctx.globalAlpha = 0.10 + 0.15 * Math.max(off || 0, nonet || 0);
        ctx.fillRect(x0, bbox.top, Math.max(1, x1 - x0), bbox.height);
      }
      ctx.restore();
    };
  }

  // ------------------------------------------------------------ charts
  function drawCharts() {
    const c = colors();
    const S = data.series.series;
    const t = S.t;
    const shade = { drawClear: [shadeHook(c)] };
    const withShade = (o) => { o.hooks = Object.assign({}, o.hooks, shade); return o; };

    // timeline strip
    {
      const rows = ["Status", "Network", "Lid", "Power", "Speed test"];
      const rowH = 18, top = 4;
      const opts = baseOpts($("c-timeline"), c, rows.length * rowH + top + 76, {
        legend: { show: false },
        scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: [0, 1] } },
        axes: [
          { stroke: c.muted, grid: { show: false }, ticks: { stroke: c.grid, width: 1, size: 4 }, font: "11px system-ui" },
          { show: false },
        ],
        series: [{}, { label: "-", stroke: "transparent", points: { show: false } }],
      });
      opts.axes[1] = { show: true, size: 70, stroke: c.ink2, grid: { show: false }, ticks: { show: false }, font: "11px system-ui",
        splits: () => [], values: () => [] };
      opts.hooks.draw = [(u) => drawTimeline(u, c, rows, rowH, top)];
      mount("c-timeline", opts, [t, t.map(() => null)]);
    }

    // packet loss
    mount("c-loss", withShade(baseOpts($("c-loss"), c, 200, {
      series: [{}, line("Internet", c.s[ENT.inet], "%", 2), line("Router", c.s[ENT.gw], "%", 2), line("ISP first hop", c.s[ENT.isp], "%", 2)],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(1, mx * 1.1)] } },
    })), [t, S.inet_loss, S.gw_loss, S.isp_loss]);

    // blackout seconds, stacked bars by location
    {
      const lan = S.dead_lan_s, acc = S.dead_access_s, up = S.dead_upstream_s;
      const c1 = lan.map((v) => (v === null ? null : v));
      const c2 = lan.map((v, i) => (v === null ? null : v + (acc[i] || 0)));
      const c3 = lan.map((v, i) => (v === null ? null : v + (acc[i] || 0) + (up[i] || 0)));
      const raw = [null, up, acc, lan];
      const bars = uPlot.paths.bars({ size: [0.9, 40, 2], align: 1 });
      const bar = (label, color, idx) => ({
        label, stroke: color, fill: color, width: 0, paths: bars, points: { show: false },
        value: (u, v, si, i) => (i === null || raw[idx][i] === null || raw[idx][i] === undefined ? "–" : raw[idx][i] + " s"),
      });
      mount("c-dead", baseOpts($("c-dead"), c, 160, {
        series: [{}, bar("Upstream (ISP hop fine)", c.s[ENT.inet], 1), bar("ISP access (router fine)", c.s[ENT.isp], 2), bar("LAN / Wi-Fi", c.s[ENT.gw], 3)],
        scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(5, mx * 1.1)] } },
      }), [t, c3, c2, c1]);
    }

    // latency
    mount("c-rtt", withShade(baseOpts($("c-rtt"), c, 220, {
      series: [{}, line("Internet", c.s[ENT.inet], " ms"), line("Router", c.s[ENT.gw], " ms"), line("ISP first hop", c.s[ENT.isp], " ms")],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(10, mx * 1.1)] } },
    })), [t, S.inet_rtt, S.gw_rtt, S.isp_rtt]);

    mount("c-peak", withShade(baseOpts($("c-peak"), c, 180, {
      series: [{}, line("Internet", c.s[ENT.inet], " ms", 0), line("Router", c.s[ENT.gw], " ms", 0)],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(10, mx * 1.1)] } },
    })), [t, S.inet_max, S.gw_max]);

    mount("c-jitter", withShade(baseOpts($("c-jitter"), c, 180, {
      series: [{}, line("Internet", c.s[ENT.inet], " ms"), line("Router", c.s[ENT.gw], " ms"), line("ISP first hop", c.s[ENT.isp], " ms")],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(5, mx * 1.1)] } },
    })), [t, S.inet_jitter, S.gw_jitter, S.isp_jitter]);

    // per-hop loss
    {
      const P = data.path.series;
      const keys = Object.keys(P).filter((k) => k !== "t" && k !== "inet")
        .sort((a, b) => (a === "gw" ? -1 : b === "gw" ? 1 : (+a.slice(3)) - (+b.slice(3))));
      const hopColor = (k, i) => (k === "gw" ? c.s[ENT.gw] : i === 1 ? c.s[ENT.isp] : c.s[[3, 4, 5, 6, 7][(i - 2) % 5]]);
      const series = [{}];
      const arrs = [P.t];
      keys.forEach((k, i) => {
        const h = (data.path.hops || []).find((x) => x.key === k);
        const name = k === "gw" ? "Router" : "Hop " + k.slice(3) + (h && h.ips[0] ? " " + h.ips[0] : "");
        // rate-limited hops start hidden (click the legend to show), so their fake loss doesn't swamp the scale
        series.push(h && h.limited
          ? line(name + " (rate-limited)", hopColor(k, i), "%", 2, { show: false, dash: [4, 4], width: 1.5 })
          : line(name, hopColor(k, i), "%", 2));
        arrs.push(P[k]);
      });
      series.push(line("Internet", c.s[ENT.inet], "%", 2, { width: 2.5 }));
      arrs.push(P.inet);
      mount("c-hops", baseOpts($("c-hops"), c, 220, {
        series,
        scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(1, Math.min(100, mx * 1.1))] } },
      }), arrs);
      $("path-dest").textContent = (data.latest && data.latest.config.path_dest) || "1.1.1.1";
    }

    // speed tests
    {
      const st = (data.speedtests || []).filter((x) => x.ok);
      const pts = (label, color) => ({
        label, stroke: color, width: 2, spanGaps: true, points: { show: true, size: 8, fill: color, stroke: c.surface, width: 2 },
        value: (u, v) => (v === null || v === undefined ? "–" : v.toFixed(0) + " Mbps"),
      });
      mount("c-speed", baseOpts($("c-speed"), c, 200, {
        series: [{}, pts("Download", c.s[0]), pts("Upload", c.s[1])],
        scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(10, mx * 1.1)] } },
      }), [st.map((x) => x.ts), st.map((x) => x.dl_mbps), st.map((x) => x.ul_mbps)]);
      const all = data.speedtests || [];
      const failed = all.length - st.length;
      const lat = data.latest || {};
      const nxt = lat.collector && lat.collector.next_speedtest;
      const parts = [all.length + " test" + (all.length === 1 ? "" : "s") + " in range"];
      if (failed) parts.push(failed + " failed");
      if (st.length) {
        const rpm = st.map((x) => x.rpm).filter((x) => x).sort((a, b) => a - b);
        if (rpm.length) parts.push("median responsiveness " + Math.round(rpm[Math.floor(rpm.length / 2)]) + " RPM (round trips per minute while loaded; under 300 means heavy bufferbloat)");
      }
      if (lat.collector && lat.collector.speedtest_running) parts.push("a test is running now");
      else if (nxt) parts.push("next scheduled " + fmtTime(nxt));
      else if (lat.config && lat.config.speedtest_interval_min === 0) parts.push("scheduled tests off");
      $("speed-note").textContent = parts.join(" · ");
    }

    // DNS / HTTPS
    mount("c-web", withShade(baseOpts($("c-web"), c, 180, {
      series: [{}, line("DNS lookup", c.s[0], " ms"), line("TCP connect", c.s[1], " ms"), line("HTTPS total", c.s[2], " ms")],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(10, mx * 1.1)] } },
    })), [t, S.dns_ms, S.http_connect_ms, S.http_total_ms]);

    // Wi-Fi
    mount("c-wifi", baseOpts($("c-wifi"), c, 180, {
      series: [{}, line("Signal (RSSI)", c.s[0], " dBm", 0), line("Noise", c.s[1], " dBm", 0)],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [Math.min(-100, mn - 5), Math.max(-30, mx + 5)] } },
    }), [t, S.rssi, S.noise]);
    mount("c-rate", baseOpts($("c-rate"), c, 180, {
      series: [{}, line("Link rate", c.s[0], " Mbps", 0)],
      scales: { x: { time: true, range: () => [currentRange().from, currentRange().to] }, y: { range: (u, mn, mx) => [0, Math.max(100, mx * 1.1)] } },
    }), [t, S.tx_rate]);
  }

  function drawTimeline(u, c, rows, rowH, top) {
    const S = data.series.series;
    const b = data.series.bucket;
    const { ctx, bbox } = u;
    const dpr = devicePixelRatio || 1;
    const y = (r) => bbox.top + (top + r * rowH) * dpr;
    const h = (rowH - 4) * dpr;
    ctx.save();
    ctx.font = 11 * dpr + "px system-ui";
    ctx.textBaseline = "middle";
    rows.forEach((name, r) => {
      ctx.fillStyle = c.ink2;
      ctx.textAlign = "right";
      ctx.fillText(name, bbox.left - 8 * dpr, y(r) + h / 2);
      ctx.fillStyle = c.nodata;
      ctx.fillRect(bbox.left, y(r), bbox.width, h);
    });
    let lastNet = null, labelEnd = -1;
    for (let i = 0; i < S.t.length; i++) {
      if (S.n[i] === null) continue;
      const x0 = u.valToPos(S.t[i], "x", true);
      const x1 = u.valToPos(S.t[i] + b, "x", true);
      const w = Math.max(1, x1 - x0 + 0.5);
      // status
      let col = c.good, a = 0.55;
      if (S.offline[i] > 0) { col = c.critical; a = 0.6 + 0.4 * S.offline[i]; }
      else if (S.nonet[i] > 0) { col = c.muted; a = 0.8; }
      else if (S.degraded[i] > 0) { col = c.warning; a = 0.4 + 0.6 * S.degraded[i]; }
      ctx.globalAlpha = a; ctx.fillStyle = col; ctx.fillRect(x0, y(0), w, h);
      // network
      ctx.globalAlpha = 0.75; ctx.fillStyle = netColor(S.net_key[i], c); ctx.fillRect(x0, y(1), w, h);
      if (S.net_key[i] !== lastNet && x0 > labelEnd) {
        const txt = netLabel(S.net_key[i]);
        ctx.globalAlpha = 1; ctx.fillStyle = c.ink; ctx.textAlign = "left";
        ctx.fillText(txt, x0 + 3 * dpr, y(1) + h / 2);
        labelEnd = x0 + ctx.measureText(txt).width + 12 * dpr;
      }
      lastNet = S.net_key[i];
      // lid
      if (S.lid_closed[i] !== null) {
        ctx.globalAlpha = S.lid_closed[i] > 0 ? 0.35 + 0.65 * S.lid_closed[i] : 0.25;
        ctx.fillStyle = S.lid_closed[i] > 0 ? c.ink : c.good; ctx.fillRect(x0, y(2), w, h);
      }
      // power
      if (S.on_ac[i] !== null) {
        const batt = 1 - S.on_ac[i];
        ctx.globalAlpha = batt > 0 ? 0.35 + 0.65 * batt : 0.25;
        ctx.fillStyle = batt > 0 ? c.s[3] : c.good; ctx.fillRect(x0, y(3), w, h);
      }
      if (S.speedtest[i]) { ctx.globalAlpha = 0.9; ctx.fillStyle = c.s[6]; ctx.fillRect(x0, y(4), Math.max(w, 2 * dpr), h); }
    }
    ctx.restore();
  }

  // custom readout for the timeline (its uPlot legend is hidden)
  function timelineTip() {
    const host = $("c-timeline");
    host.addEventListener("mousemove", (e) => {
      const u = charts["c-timeline"];
      if (!u || !data.series) return;
      const idx = u.cursor.idx;
      const S = data.series.series;
      if (idx === null || idx === undefined || S.n[idx] === null) return hideTip();
      const parts = [
        ["", fmtTime(S.t[idx])],
        ["Status", S.offline[idx] > 0 ? "offline " + Math.round(S.offline[idx] * 100) + "% of the time" : S.nonet[idx] > 0 ? "no network" : S.degraded[idx] > 0 ? "degraded " + Math.round(S.degraded[idx] * 100) + "% of the time" : "ok"],
        ["Network", netLabel(S.net_key[idx])],
        ["Lid", S.lid_closed[idx] === null ? "–" : S.lid_closed[idx] > 0.5 ? "closed" : S.lid_closed[idx] > 0 ? "partly closed" : "open"],
        ["Power", S.on_ac[idx] === null ? "–" : S.on_ac[idx] >= 0.5 ? "AC power" : "battery"],
      ];
      if (S.speedtest[idx]) parts.push(["Speed test", "running (latency is inflated on purpose)"]);
      showTip(e, parts);
    });
    host.addEventListener("mouseleave", hideTip);
  }

  // ------------------------------------------------------------ tooltip
  function showTip(e, rows) {
    const tip = $("tip");
    tip.textContent = "";
    rows.forEach(([k, v], i) => {
      const line = el("div");
      if (i === 0 && !k) line.append(el("span", { class: "muted", text: v }));
      else { line.append(el("b", { text: v }), " ", el("span", { class: "muted", text: k })); }
      tip.append(line);
    });
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  const hideTip = () => { $("tip").hidden = true; };

  // ------------------------------------------------------------ verdict + path
  function lossClass(l) {
    if (l === null || l === undefined) return "";
    if (l < 0.3) return "good";
    if (l < 1) return "warning";
    if (l < 3) return "serious";
    return "critical";
  }

  function drawPath() {
    const P = data.path;
    const v = P.verdict || {};
    const vt = $("verdict-text");
    vt.textContent = "";
    vt.append(el("strong", { text: v.level === "good" ? "No meaningful loss. " : v.level === "none" ? "" : v.level === "bad" ? "Packet loss found. " : "Some packet loss. " }), v.text || "");
    const { from, to } = currentRange();
    $("verdict-range").textContent = fmtTime(from) + " to " + fmtTime(to) + (state.net ? " · " + netLabel(state.net) : "");

    const ol = $("path");
    ol.textContent = "";
    const hops = P.hops || [];
    const inet = P.internet || {};
    // "limited" (set by the server) = rate-limited: the hop's loss doesn't carry on to everything after it
    const chain = hops.map((h) => ({ ...h })).concat([{ ...inet, label: "Internet", role: "internet", ips: (data.latest && data.latest.config.internet_targets) || [] }]);
    ol.append(el("li", {}, el("div", { class: "node" },
      el("div", { class: "role", text: "start" }), el("div", { class: "name", text: "This Mac" }),
      el("div", { class: "ip", text: (data.latest && data.latest.sample && data.latest.sample.local_ip) || "" }))));
    for (const h of chain) {
      const role = { router: "your router", isp: "ISP side", hop: "transit", internet: "destinations" }[h.role] || h.role;
      const node = el("div", { class: "node " + (h.limited ? "limited" : lossClass(h.loss)) });
      node.append(el("div", { class: "role", text: role }));
      node.append(el("div", { class: "name", text: h.role === "isp" ? "Hop " + h.hop : h.label }));
      const ipText = (h.ips || []).join(", ");
      if (ipText) node.append(el("div", { class: "ip", text: ipText }));
      const nm = (h.names || []).filter(Boolean)[0];
      if (nm) node.append(el("div", { class: "ip", text: nm }));
      if (h.limited) {
        node.append(el("div", { class: "val" }, "rate-limited"));
        node.append(el("div", { class: "meta", text: fmtPct(h.loss) + " of probes unanswered" }));
      } else {
        node.append(el("div", { class: "val" }, fmtPct(h.loss), el("small", { text: " loss" })));
      }
      node.append(el("div", { class: "meta", text: "median " + fmt(h.rtt_p50, 1, " ms") + " · p95 " + fmt(h.rtt_p95, 0, " ms") }));
      if (h.burst) node.append(el("div", { class: "meta", text: "longest run lost: " + h.burst + " s" }));
      if (h.limited) node.append(el("div", { class: "flag", text: "the hops after it don't lose these packets, so this is not real loss" }));
      ol.append(el("li", {}, node));
    }
  }

  // ------------------------------------------------------------ tiles
  function drawTiles() {
    const all = data.summary[state.net || "*"] || data.summary["*"] || {};
    const host = $("tiles");
    host.textContent = "";
    const tile = (k, v, d, word) => {
      const t = el("div", { class: "tile" }, el("div", { class: "k", text: k }), el("div", { class: "v", text: v }));
      if (word) t.append(el("div", { class: "status-word " + word[0], text: word[1] }));
      if (d) t.append(el("div", { class: "d", text: d }));
      host.append(t);
    };
    tile("Internet uptime", all.uptime_pct === null || all.uptime_pct === undefined ? "–" : fmt(all.uptime_pct, all.uptime_pct >= 99.9 ? 3 : 2, "%"),
      fmtDur(all.downtime_s) + " down in " + (all.outages || 0) + " outage" + (all.outages === 1 ? "" : "s") + (all.longest_outage_s ? ", longest " + fmtDur(all.longest_outage_s) : ""));
    tile("Internet packet loss", fmtPct(all.inet_loss), "router " + fmtPct(all.gw_loss) + " · ISP first hop " + fmtPct(all.isp_loss),
      all.inet_loss >= 1 ? ["bad", "▲ high"] : all.inet_loss !== null && all.inet_loss < 0.3 ? ["good", "✓ healthy"] : null);
    tile("Blackout seconds", String(all.dead_s ?? "–"),
      "LAN " + (all.dead_lan_s || 0) + " · ISP access " + (all.dead_access_s || 0) + " · upstream " + (all.dead_upstream_s || 0) + " · " + (all.drop_windows || 0) + " short drops");
    tile("Internet latency", fmt(all.inet_rtt_p50, 1, " ms"), "median · p95 " + fmt(all.inet_rtt_p95, 0, " ms") + " · router " + fmt(all.gw_rtt_p50, 1, " ms"));
    tile("Jitter", fmt(all.inet_jitter_p50, 1, " ms"), "median, internet");
    tile("Download", fmt(all.dl_p50, 0, " Mbps"), "median of " + (all.speedtests || 0) + " tests · worst " + fmt(all.dl_min, 0) + " · up " + fmt(all.ul_p50, 0, " Mbps"));
    tile("DNS", fmt(all.dns_p50, 1, " ms"), "median · " + fmtPct(all.dns_fail_pct) + " of windows failed");
    tile("Wi-Fi signal", fmt(all.rssi_p50, 0, " dBm"), "median · lid closed " + fmt(all.lid_closed_pct, 0, "%") + " · battery " + fmt(all.battery_pct, 0, "%"));
    tile("Time recorded", fmtDur((all.hours || 0) * 3600), (all.samples || 0) + " windows");
  }

  // ------------------------------------------------------------ heatmap
  const HEAT_UNITS = { inet_loss: "%", isp_loss: "%", gw_loss: "%", dead_s: " s", inet_rtt: " ms", bad_pct: "%", offline_min: " min", dl_mbps: " Mbps", ul_mbps: " Mbps", dns_ms: " ms", rssi: " dBm" };
  // metrics where a bigger number is better: invert the ramp so dark always means "worse"
  const HIGHER_BETTER = { dl_mbps: 1, ul_mbps: 1, rssi: 1 };

  async function drawHeatmap() {
    const metric = $("heat-metric").value;
    const { from, to } = currentRange();
    const hm = await api("/api/heatmap", { from, to, net: state.net, metric });
    const host = $("heatmap");
    host.textContent = "";
    const c = colors();
    const days = [];
    const start = new Date(from * 1000); start.setHours(0, 0, 0, 0);
    for (let d = new Date(start); d.getTime() / 1000 <= to; d.setDate(d.getDate() + 1)) {
      days.push(d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()));
      if (days.length > 120) break;
    }
    const cells = {};
    const vals = [];
    for (const [d, h, v, n] of hm.cells) { cells[d + "|" + h] = [v, n]; if (v !== null) vals.push(v); }
    host.style.gridTemplateColumns = "40px repeat(" + days.length + ", minmax(10px, 36px))";
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (!vals.length) { lo = 0; hi = 1; }
    if (!HIGHER_BETTER[metric]) lo = Math.min(0, lo);
    if (hi === lo) hi = lo + 1;
    const unit = HEAT_UNITS[metric] || "";
    const step = (v) => {
      let f = (v - lo) / (hi - lo);
      if (HIGHER_BETTER[metric]) f = 1 - f;
      return Math.max(0, Math.min(6, Math.round(f * 6)));
    };
    for (let h = 0; h < 24; h++) {
      host.append(el("div", { class: "rowlab", text: h % 3 === 0 ? pad(h) + ":00" : "" }));
      for (const d of days) {
        const cv = cells[d + "|" + h];
        const cell = el("div", { class: "cell", tabindex: "-1" });
        if (cv && cv[0] !== null) cell.style.background = c.seq[step(cv[0])];
        const rows = [["", d + " " + pad(h) + ":00–" + pad(h + 1) + ":00"]];
        if (cv && cv[0] !== null) rows.push([$("heat-metric").selectedOptions[0].textContent, (+cv[0]).toFixed(metric === "dead_s" ? 0 : 2) + unit], ["windows / tests", String(cv[1])]);
        else rows.push(["", "no data"]);
        cell.addEventListener("mousemove", (e) => showTip(e, rows));
        cell.addEventListener("mouseleave", hideTip);
        cell.addEventListener("click", () => {
          const t0 = new Date(d + "T" + pad(h) + ":00:00").getTime() / 1000;
          setRange(t0, t0 + 3600);
        });
        host.append(cell);
      }
    }
    host.append(el("div"));
    days.forEach((d, i) => {
      const show = days.length <= 14 || i % Math.ceil(days.length / 14) === 0;
      host.append(el("div", { class: "collab", text: show ? d.slice(5) : "" }));
    });
    const lg = $("heat-legend");
    lg.textContent = "";
    lg.append(el("span", { text: (HIGHER_BETTER[metric] ? hi : lo).toFixed(1) + unit }));
    for (let i = 0; i <= 6; i++) lg.append(el("span", { class: "sw", style: "background:" + c.seq[i] }));
    lg.append(el("span", { text: (HIGHER_BETTER[metric] ? lo : hi).toFixed(1) + unit }));
    lg.append(el("span", { class: "sw", style: "background:" + c.nodata + ";margin-left:12px" }), el("span", { text: "no data (asleep or not running)" }));
    lg.append(el("span", { text: " · darker = worse · click a cell to zoom to that hour" }));
  }

  // ------------------------------------------------------------ tables
  function table(id, head, rows, numCols) {
    const t = $(id);
    t.textContent = "";
    const tr = el("tr");
    head.forEach((h, i) => tr.append(el("th", { class: numCols.includes(i) ? "num" : null, text: h })));
    t.append(el("thead", {}, tr));
    const tb = el("tbody");
    for (const r of rows) {
      const row = el("tr");
      r.forEach((v, i) => {
        const td = el("td", { class: numCols.includes(i) ? "num" : null });
        if (v instanceof Node) td.append(v); else td.textContent = v === null || v === undefined ? "–" : v;
        row.append(td);
      });
      tb.append(row);
    }
    if (!rows.length) tb.append(el("tr", {}, el("td", { colspan: head.length, class: "muted", text: "Nothing in this range." })));
    t.append(tb);
  }

  function drawNetworks() {
    const c = colors();
    const rows = state.networks.map((n) => {
      const s = data.summary[n.net_key] || {};
      const name = el("span", {}, el("span", { class: "swatch", style: "background:" + netColor(n.net_key, c) }),
        el("button", { class: "rename", text: n.label, title: "Rename", onclick: () => rename(n) }));
      const ident = [n.ssid ? "SSID " + n.ssid : null, n.gateway ? "router " + n.gateway : null, n.router_mac ? n.router_mac : null].filter(Boolean).join(" · ");
      return [name, ident, n.public_ip, fmtDur((s.hours || 0) * 3600), s.uptime_pct === undefined || s.uptime_pct === null ? "–" : fmt(s.uptime_pct, 2, "%"),
        fmtPct(s.inet_loss), fmtPct(s.isp_loss), fmtPct(s.gw_loss), s.dead_s ?? "–", fmt(s.inet_rtt_p50, 1), fmt(s.inet_rtt_p95, 0),
        fmt(s.dl_p50, 0), fmt(s.ul_p50, 0), fmt(s.rssi_p50, 0), s.outages ?? "–"];
    });
    table("networks", ["Network", "Identified by", "Public IP", "Time in range", "Uptime", "Internet loss", "ISP hop loss", "Router loss",
      "Blackout s", "Latency p50", "p95", "Down Mbps", "Up Mbps", "Signal dBm", "Outages"], rows, [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]);
    const noSsid = state.networks.some((n) => n.iface_type === "wifi" && !n.ssid);
    $("ssid-note").textContent = noSsid
      ? "macOS hides Wi-Fi names from background tools, so networks are told apart by their router's hardware address. Click a name to give it a label. To have the Wi-Fi name recorded automatically, run once in Terminal: sudo ipconfig setverbose 1"
      : "";
  }

  async function rename(n) {
    const label = prompt("Name for this network (empty resets it):", n.custom_label ? n.label : "");
    if (label === null) return;
    await fetch("/api/label", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ net_key: n.net_key, label }) });
    load();
  }

  const WHERE = { lan: "LAN / Wi-Fi", access: "ISP access", upstream: "upstream" };

  function drawOutages() {
    const rows = [];
    for (const o of data.outages) {
      rows.push({ ts: o.start, cells: [
        el("span", { class: "tag " + (o.kind === "offline" ? "critical" : ""), text: o.kind === "offline" ? "outage" : "no network" }),
        fmtTime(o.start), fmtDur(o.duration_s), netLabel(o.net_key),
        o.where ? WHERE[o.where] + " (" + o.dead_lan_s + "/" + o.dead_access_s + "/" + o.dead_upstream_s + " s)" : o.kind === "no-network" ? "Mac not connected" : "–",
        o.lid_closed ? "closed" : "open",
        zoomBtn(o.start, o.end)] });
    }
    for (const d of data.drops) {
      const where = { lan: d.dead_lan_s, access: d.dead_access_s, upstream: d.dead_upstream_s };
      const w = Object.keys(where).reduce((a, b) => (where[a] >= where[b] ? a : b));
      rows.push({ ts: d.ts - (d.window_s || 30), cells: [
        el("span", { class: "tag warning", text: "drop" }),
        fmtTime(d.ts - (d.window_s || 30)), d.dead_s + " s" + (d.inet_burst ? " (longest " + d.inet_burst + " s)" : ""), netLabel(d.net_key),
        WHERE[w] + " (" + d.dead_lan_s + "/" + d.dead_access_s + "/" + d.dead_upstream_s + " s)", "–",
        zoomBtn(d.ts - (d.window_s || 30) - 300, d.ts + 300)] });
    }
    rows.sort((a, b) => b.ts - a.ts);
    table("outages", ["Kind", "Started", "Length", "Network", "Where it failed (LAN/ISP/upstream s)", "Lid", ""], rows.slice(0, 300).map((r) => r.cells), [2]);
  }
  function zoomBtn(a, b) {
    return el("button", { text: "Zoom", onclick: () => { setRange(a - 120, b + 120); scrollTo({ top: 0, behavior: "smooth" }); } });
  }

  function drawEvents() {
    const rows = data.events.slice().reverse().slice(0, 300).map((e) => {
      let detail = e.detail || "";
      if (e.kind === "gap") {
        try {
          const d = JSON.parse(e.detail);
          detail = "not recording for " + fmtDur((e.end_ts - e.ts)) + (d.sleep ? " · slept: " + d.sleep : "") + (d.wake ? " · woke: " + d.wake : "") + (d.dark_wakes ? " · " + d.dark_wakes + " dark wakes" : "");
        } catch (err) { /* keep raw */ }
      } else if (e.kind === "path") {
        try {
          const d = JSON.parse(e.detail);
          detail = d.hops.map((h) => h[0] + ": " + (h[1] || "*") + (h[2] ? " (" + h[2] + ")" : "")).join(" → ") + " → " + d.dest;
        } catch (err) { /* keep raw */ }
      } else if (e.kind === "network") {
        detail = detail.split(" -> ").map(netLabel).join(" → ");
      }
      const label = { gap: "asleep / off", "public-ip": "public IP changed", path: "route", start: "collector start" }[e.kind] || e.kind;
      return [fmtTime(e.ts), el("span", { class: "tag", text: label }), detail];
    });
    table("events", ["Time", "Event", "Detail"], rows, []);
  }

  // ------------------------------------------------------------ live header
  function drawLive() {
    const L = data.latest || {};
    const s = L.sample;
    const pill = $("live-status");
    if (!s) { pill.textContent = "waiting for first sample"; pill.className = "pill"; return; }
    const age = L.now - s.ts;
    const stale = age > (L.config.interval_s || 30) * 3;
    pill.className = "pill " + (stale ? "" : s.status);
    pill.textContent = stale ? "not recording" : { ok: "Online", degraded: "Degraded", offline: "Offline", "no-network": "No network" }[s.status] || s.status;
    $("live-net").textContent = (L.label || "") + (s.inet_rtt !== null ? " · " + s.inet_rtt.toFixed(1) + " ms" : "") +
      (s.inet_loss !== null ? " · " + fmtPct(s.inet_loss) + " loss" : "") + (s.rssi ? " · " + s.rssi + " dBm" : "");
    $("live-age").textContent = "updated " + fmtDur(age) + " ago";
  }

  // ------------------------------------------------------------ load
  let loading = 0;
  async function load() {
    const my = ++loading;
    $("main").classList.add("loading");
    syncControls();
    const { from, to } = currentRange();
    const q = { from, to, net: state.net };
    try {
      const [series, path, summary, speedtests, events, outages, drops, networks, latest] = await Promise.all([
        api("/api/series", { ...q, points: Math.min(1500, Math.round($("main").clientWidth)) }),
        api("/api/path", { ...q, points: Math.min(800, Math.round($("main").clientWidth / 2)) }),
        api("/api/summary", q), api("/api/speedtests", q), api("/api/events", q), api("/api/outages", q),
        api("/api/drops", q), api("/api/networks"), api("/api/latest"),
      ]);
      if (my !== loading) return;
      data = { series, path, summary, speedtests, events, outages, drops, latest };
      state.networks = networks;
      state.netIndex = {};
      networks.forEach((n, i) => { state.netIndex[n.net_key] = { ...n, idx: i }; });
      if (networks.length) state.allFrom = Math.min(...networks.map((n) => n.first));
      fillNetSelect();
      drawLive();
      drawPath();
      drawTiles();
      drawCharts();
      drawNetworks();
      drawOutages();
      drawEvents();
      await drawHeatmap();
    } catch (err) {
      console.error(err);
      $("live-status").textContent = "dashboard can't reach netscan";
      $("live-status").className = "pill offline";
    } finally {
      if (my === loading) $("main").classList.remove("loading");
    }
    clearTimeout(refreshTimer);
    if (state.preset) refreshTimer = setTimeout(load, 30000);
    else refreshTimer = setTimeout(refreshLive, 30000);
  }
  async function refreshLive() {
    try { data.latest = await api("/api/latest"); drawLive(); } catch (e) { /* ignore */ }
    refreshTimer = setTimeout(refreshLive, 30000);
  }

  function fillNetSelect() {
    const sel = $("net");
    const keep = state.net;
    sel.textContent = "";
    sel.append(el("option", { value: "", text: "All networks" }));
    for (const n of state.networks) sel.append(el("option", { value: n.net_key, text: n.label }));
    sel.value = keep;
  }

  function syncControls() {
    document.querySelectorAll("#presets button").forEach((b) => b.classList.toggle("on", b.dataset.r === state.preset));
    const { from, to } = currentRange();
    $("from").value = toLocalInput(from);
    $("to").value = toLocalInput(to);
    const u = new URL("/api/export.csv", location.href);
    u.searchParams.set("from", Math.floor(from));
    u.searchParams.set("to", Math.ceil(to));
    if (state.net) u.searchParams.set("net", state.net);
    $("export").href = u.toString();
  }

  // ------------------------------------------------------------ wire up
  document.querySelectorAll("#presets button").forEach((b) => b.addEventListener("click", () => setPreset(b.dataset.r)));
  $("zoomout").addEventListener("click", zoomOut);
  const onDates = () => {
    const a = new Date($("from").value).getTime() / 1000, b = new Date($("to").value).getTime() / 1000;
    if (a && b && b > a) setRange(a, b);
  };
  $("from").addEventListener("change", onDates);
  $("to").addEventListener("change", onDates);
  $("net").addEventListener("change", () => { state.net = $("net").value; saveHash(); load(); });
  $("heat-metric").addEventListener("change", drawHeatmap);
  $("speedtest").addEventListener("click", async () => {
    const b = $("speedtest");
    b.disabled = true;
    b.textContent = "Speed test running…";
    try { await fetch("/api/speedtest", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); } catch (e) { /* ignore */ }
    setTimeout(() => { b.disabled = false; b.textContent = "Run speed test"; load(); }, 45000);
  });
  let rz;
  addEventListener("resize", () => {
    clearTimeout(rz);
    rz = setTimeout(() => { for (const [id, u] of Object.entries(charts)) u.setSize({ width: Math.max(280, $(id).clientWidth), height: u.height }); }, 150);
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => data.series && (drawCharts(), drawHeatmap(), drawNetworks()));
  timelineTip();
  readHash();
  load();
})();
