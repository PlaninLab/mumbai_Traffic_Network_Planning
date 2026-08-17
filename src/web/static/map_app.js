/* map_app.js — Mumbai Traffic Observatory (WEH corridor).
 *
 * Pure deck.gl, no basemap tiles: the modeled road network IS the basemap, so
 * the page renders with zero internet access (meeting-room safe).
 *
 * Data source: window.__MAP_DATA__ (standalone file) or /api/map/* (server).
 * Every view separates MEASURED (probe speeds) from MODELED (equilibrium
 * volumes, queue lengths) — the two words appear on every layer and tile.
 */
/* global deck */
(function () {
  "use strict";

  // ---- palette (dataviz reference instance, dark mode) ----------------------
  const COL = {
    good: [12, 163, 12],
    warning: [250, 178, 25],
    serious: [236, 131, 90],
    critical: [208, 59, 59],
    blue: [57, 135, 229],
    ink: [255, 255, 255],
    muted: [137, 135, 129],
    baseCls: [
      [64, 64, 58],   // motorway
      [54, 54, 49],   // trunk
      [54, 54, 49],   // primary
      [44, 44, 40],   // secondary
      [40, 40, 37],   // tertiary
      [33, 33, 31],   // other
    ],
    nodata: [58, 58, 54],
  };
  const HEX = { good: "#0ca30c", warning: "#fab219", serious: "#ec835a",
                critical: "#d03b3b", blue: "#3987e5" };

  function ttiColor(t) {
    if (t == null || !isFinite(t)) return COL.nodata;
    if (t < 1.15) return COL.good;
    if (t < 1.5) return COL.warning;
    if (t < 2.0) return COL.serious;
    return COL.critical;
  }
  function ttiHex(t) {
    if (t == null || !isFinite(t)) return "#3a3a36";
    if (t < 1.15) return HEX.good;
    if (t < 1.5) return HEX.warning;
    if (t < 2.0) return HEX.serious;
    return HEX.critical;
  }
  function vcColor(v) {
    if (v == null) return COL.nodata;
    if (v < 0.7) return COL.good;
    if (v < 0.9) return COL.warning;
    if (v < 1.1) return COL.serious;
    return COL.critical;
  }
  function vcHex(v) {
    if (v == null) return "#3a3a36";
    if (v < 0.7) return HEX.good;
    if (v < 0.9) return HEX.warning;
    if (v < 1.1) return HEX.serious;
    return HEX.critical;
  }
  function vcWord(v) {
    if (v == null) return "no data";
    if (v < 0.7) return "free";
    if (v < 0.9) return "busy";
    if (v < 1.1) return "at capacity";
    return "over capacity";
  }

  const fmt = (n) => (n == null ? "—" : Math.round(n).toLocaleString("en-IN"));
  // OSM leaves many junctions unnamed; show the locality instead of a node id.
  const jname = (n) => /^junction \d+$/.test(n.name)
    ? "Unnamed junction, " + derived.locality(n.lat) : n.name;
  const fmt1 = (n) => (n == null ? "—" : (Math.round(n * 10) / 10).toLocaleString("en-IN"));
  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // ---- state -----------------------------------------------------------------
  const S = {
    data: null,
    tab: "overview",
    show: { film: true, probes: true, model: true, columns: true, queues: true,
            here: false, od: false, points: false },
    is3D: true,
    cinema: false,
    binIdx: -1,
    playing: false,
    selected: null,
    animT: 0,
    viewState: {
      longitude: 72.861, latitude: 19.149, zoom: 11.65,
      pitch: 50, bearing: -20, minZoom: 9.3, maxZoom: 17,
    },
  };

  let deckgl = null;
  let derived = null;   // precomputed geometry (chain segments, junction list…)
  let lastTick = null;
  let playTimer = null;

  // ---- data loading ----------------------------------------------------------
  async function loadAll() {
    if (window.__MAP_DATA__) return window.__MAP_DATA__;
    const names = ["network", "intersections", "frames", "od", "here", "summary"];
    const out = {};
    await Promise.all(names.map(async (n) => {
      const r = await fetch("/api/map/" + n);
      if (!r.ok) throw new Error("payload " + n + " missing (" + r.status + ")");
      out[n] = await r.json();
    }));
    return out;
  }

  function segLenM(a, b) {
    const my = 110574, mx = 111320 * Math.cos((a[1] * Math.PI) / 180);
    return Math.hypot((b[0] - a[0]) * mx, (b[1] - a[1]) * my);
  }

  function derive(d) {
    // WEH sample chain, ordered by idx.
    const pts = (d.frames.points || []).slice().sort((a, b) => a.idx - b.idx);
    const chainSegs = [];
    for (let i = 0; i + 1 < pts.length; i++) {
      chainSegs.push({
        idx: pts[i].idx,
        path: [[pts[i].lon, pts[i].lat], [pts[i + 1].lon, pts[i + 1].lat]],
        len: segLenM([pts[i].lon, pts[i].lat], [pts[i + 1].lon, pts[i + 1].lat]),
        free: pts[i].free_kph || 40,
      });
    }
    // Junction list for the ranked table: real junctions only.
    const junctions = d.intersections.nodes
      .filter((n) => n.street_count >= 3 && (n.volume_pcu_h > 0 || n.queue_total_m > 0));
    const ranked = junctions.slice()
      .sort((a, b) => (b.queue_total_m - a.queue_total_m) || (b.volume_pcu_h - a.volume_pcu_h))
      .slice(0, 15);
    // Zone positions by name.
    const zonePos = {};
    for (const z of d.od.zones) zonePos[z.name] = [z.lon, z.lat];
    // Locality lookup by latitude band.
    const zonesByLat = d.od.zones.slice().sort((a, b) => b.lat - a.lat);
    const locality = (lat) => {
      let best = zonesByLat[0], bd = Infinity;
      for (const z of zonesByLat) {
        const dd = Math.abs(z.lat - lat);
        if (dd < bd) { bd = dd; best = z; }
      }
      return best ? best.name : "";
    };
    // Queue bands, flattened (with a human junction name for the tooltip).
    const bands = [];
    for (const n of d.intersections.nodes) {
      for (const a of n.approaches || []) {
        if (a.band && a.band.length >= 2) {
          const disp = /^junction \d+$/.test(n.name)
            ? "Unnamed junction, " + locality(n.lat) : n.name;
          bands.push({ ...a, junction: disp, jid: n.id });
        }
      }
    }
    const maxFlow = Math.max(1, ...d.network.links.map((l) => l.flow));
    const maxVol = Math.max(1, ...junctions.map((n) => n.volume_pcu_h));
    const maxArc = Math.max(1, ...d.od.arcs.map((a) => a.v));
    return { pts, chainSegs, junctions, ranked, bands, zonePos, locality,
             maxFlow, maxVol, maxArc };
  }

  function currentBin() {
    const bins = S.data.frames.bins || [];
    if (!bins.length || S.binIdx < 0 || S.binIdx >= bins.length) return null;
    return bins[S.binIdx];
  }

  // Probe trips along the WEH chain, timed by the current bin's measured speeds.
  function buildTrips() {
    const bin = currentBin();
    const segs = derived.chainSegs;
    if (!segs.length) return { rows: [], loopT: 1 };
    const path = [segs[0].path[0]];
    const ts = [0];
    let t = 0;
    for (const s of segs) {
      const kph = bin && bin.kph[s.idx] ? bin.kph[s.idx] : s.free;
      t += s.len / (Math.max(5, kph) / 3.6);
      path.push(s.path[1]);
      ts.push(t);
    }
    const loopT = t;
    const N = 12;
    const rows = [];
    for (let k = 0; k < N; k++) {
      const off = (k * loopT) / N;
      rows.push({ path, ts: ts.map((x) => x + off) });
      rows.push({ path, ts: ts.map((x) => x + off + loopT) });
    }
    return { rows, loopT };
  }

  // ---- deck layers ------------------------------------------------------------
  function makeLayers() {
    const d = S.data;
    const layers = [];
    const bin = currentBin();
    const pulse = 0.6 + 0.3 * Math.sin((S.animT * 2 * Math.PI) / 1.6);

    // Base fabric: the network itself is the basemap.
    layers.push(new deck.PathLayer({
      id: "base",
      data: d.network.links,
      getPath: (l) => l.p,
      getColor: (l) => COL.baseCls[l.cls],
      getWidth: (l) => [16, 12, 10, 6, 4, 2.4][l.cls],
      widthMinPixels: 0.6,
      capRounded: true, jointRounded: true,
      pickable: false,
    }));

    // Modeled volume + V/C on every link that carries flow.
    if (S.show.model) {
      layers.push(new deck.PathLayer({
        id: "model-links",
        data: d.network.links.filter((l) => l.flow > 0),
        getPath: (l) => l.p,
        getColor: (l) => vcColor(l.vc),
        getWidth: (l) => 3 + 20 * (l.flow / derived.maxFlow),
        widthMinPixels: 1,
        opacity: 0.85,
        capRounded: true, jointRounded: true,
        pickable: true,
      }));
    }

    // Measured HERE link speeds (true road shapes).
    if (S.show.here) {
      layers.push(new deck.PathLayer({
        id: "here-links",
        data: d.here.links,
        getPath: (l) => l.p,
        getColor: (l) => ttiColor(l.tti),
        getWidth: 9,
        widthMinPixels: 2,
        capRounded: true, jointRounded: true,
        pickable: true,
      }));
    }

    // Measured film: WEH chain colored by the selected time-of-day bin.
    if (S.show.film && derived.chainSegs.length) {
      layers.push(new deck.PathLayer({
        id: "film",
        data: derived.chainSegs,
        getPath: (s) => s.path,
        getColor: (s) => ttiColor(bin ? bin.tti[s.idx] : null),
        getWidth: 26,
        widthMinPixels: 2.5,
        opacity: bin ? 0.95 : 0.35,
        capRounded: true, jointRounded: true,
        pickable: true,
        updateTriggers: { getColor: S.binIdx },
      }));
    }

    // Probe animation: light streaks that crawl where traffic crawls.
    if (S.show.probes && derived.chainSegs.length && deck.TripsLayer) {
      const t = buildTrips();
      layers.push(new deck.TripsLayer({
        id: "probes",
        data: t.rows,
        getPath: (r) => r.path,
        getTimestamps: (r) => r.ts,
        getColor: [200, 224, 255],
        getWidth: 7,
        widthMinPixels: 2,
        capRounded: true, jointRounded: true,
        trailLength: t.loopT / 10,
        currentTime: t.loopT + (S.animT * 18) % t.loopT,
        updateTriggers: { getTimestamps: S.binIdx },
      }));
    }

    // Modeled standing queues, drawn to physical scale.
    if (S.show.queues && derived.bands.length) {
      layers.push(new deck.PathLayer({
        id: "queues",
        data: derived.bands,
        getPath: (b) => b.band,
        getColor: COL.critical,
        getWidth: (b) => 6 + 3 * (b.lanes || 1),
        widthMinPixels: 2,
        opacity: pulse,
        capRounded: true, jointRounded: true,
        pickable: true,
      }));
    }

    // Modeled intersection volumes as 3D columns.
    if (S.show.columns) {
      layers.push(new deck.ColumnLayer({
        id: "columns",
        data: derived.junctions,
        getPosition: (n) => [n.lon, n.lat],
        getElevation: (n) => n.volume_pcu_h,
        getFillColor: (n) => vcColor(n.vc_max),
        elevationScale: S.is3D ? 0.11 : 0.001,
        radius: 38,
        extruded: true,
        opacity: 0.92,
        pickable: true,
        updateTriggers: { elevationScale: S.is3D },
      }));
    }

    // Selected junction ring.
    const sel = S.selected != null
      ? derived.junctions.find((n) => n.id === S.selected) : null;
    if (sel) {
      layers.push(new deck.ScatterplotLayer({
        id: "selected",
        data: [sel],
        getPosition: (n) => [n.lon, n.lat],
        getRadius: 130,
        stroked: true, filled: false,
        getLineColor: COL.blue,
        lineWidthMinPixels: 2.5,
      }));
    }

    // Modeled OD desire lines.
    if (S.show.od && d.od.arcs.length) {
      layers.push(new deck.ArcLayer({
        id: "od-arcs",
        data: d.od.arcs,
        getSourcePosition: (a) => derived.zonePos[a.o],
        getTargetPosition: (a) => derived.zonePos[a.d],
        getSourceColor: [57, 135, 229, 70],
        getTargetColor: [57, 135, 229, 235],
        getWidth: (a) => 1 + 7 * (a.v / derived.maxArc),
        getHeight: 0.4,
        pickable: true,
      }));
    }

    // Measured sample points.
    if (S.show.points && derived.pts.length) {
      layers.push(new deck.ScatterplotLayer({
        id: "sample-points",
        data: derived.pts,
        getPosition: (p) => [p.lon, p.lat],
        getRadius: 34,
        getFillColor: [57, 135, 229, 210],
        pickable: true,
      }));
    }

    // Locality labels — orientation without any basemap.
    layers.push(new deck.TextLayer({
      id: "labels",
      data: d.od.zones,
      getPosition: (z) => [z.lon - 0.024, z.lat],
      getText: (z) => z.name.toUpperCase(),
      getSize: 11,
      getColor: [137, 135, 129, 220],
      getTextAnchor: "end",
      getAlignmentBaseline: "center",
      fontFamily: "system-ui, sans-serif",
      fontWeight: 600,
      billboard: true,
    }));

    return layers;
  }

  function tooltip(info) {
    const o = info.object;
    if (!o) return null;
    const id = info.layer && info.layer.id;
    const bin = currentBin();
    if (id === "columns" || id === "selected") {
      return { html:
        "<b>" + esc(o.name) + "</b><br>" +
        "<span class='tt-k'>arriving volume</span> <b>" + fmt(o.volume_pcu_h) + "</b> PCU/h · modeled<br>" +
        "<span class='tt-k'>worst approach V/C</span> <b>" + fmt1(o.vc_max) + "</b> (" + vcWord(o.vc_max) + ")<br>" +
        "<span class='tt-k'>standing queue</span> <b>" + fmt(o.queue_total_m) + "</b> m" };
    }
    if (id === "queues") {
      return { html:
        "<b>Queue · " + esc(o.junction) + "</b><br>" +
        "<span class='tt-k'>approach</span> " + esc(o.name || "unnamed road") + "<br>" +
        "<span class='tt-k'>length</span> <b>" + fmt(o.queue_len_m) + " m</b> (" + fmt(o.queued_veh) + " veh)<br>" +
        "<span class='tt-k'>arrival vs capacity</span> " + fmt(o.flow_pcu_h) + " / " + fmt(o.capacity_pcu_h) + " PCU/h<br>" +
        "<span class='tt-k'>delay</span> " + fmt(o.delay_s) + " s per vehicle · modeled upper bound" };
    }
    if (id === "model-links") {
      return { html:
        "<b>" + esc(o.name || "unnamed road") + "</b><br>" +
        "<span class='tt-k'>modeled flow</span> <b>" + fmt(o.flow) + "</b> / " + fmt(o.cap) + " PCU/h<br>" +
        "<span class='tt-k'>V/C</span> <b>" + fmt1(o.vc) + "</b> (" + vcWord(o.vc) + ") · " + (o.lanes || 1) + " lanes" };
    }
    if (id === "here-links") {
      return { html:
        "<b>" + esc(o.desc || "road segment") + "</b><br>" +
        "<span class='tt-k'>measured</span> <b>" + fmt1(o.kph) + "</b> km/h (free-flow " + fmt1(o.free_kph) + ")<br>" +
        "<span class='tt-k'>TTI</span> <b>" + fmt1(o.tti) + "</b> · HERE probe data" };
    }
    if (id === "film") {
      const t = bin ? bin.tti[o.idx] : null;
      const k = bin ? bin.kph[o.idx] : null;
      return { html:
        "<b>WEH · sample point " + o.idx + "</b><br>" +
        (bin
          ? "<span class='tt-k'>at " + bin.label + " IST</span> <b>" + fmt1(k) + "</b> km/h · TTI <b>" + fmt1(t) + "</b>"
          : "<span class='tt-k'>no sample in this time slot yet</span>") };
    }
    if (id === "od-arcs") {
      return { html: "<b>" + esc(o.o) + " → " + esc(o.d) + "</b><br>" +
        "<span class='tt-k'>modeled demand</span> <b>" + fmt(o.v) + "</b> PCU/h" };
    }
    if (id === "sample-points") {
      return { html: "<b>Sample point " + o.idx + "</b><br>" +
        "<span class='tt-k'>free-flow</span> " + fmt1(o.free_kph) + " km/h" };
    }
    return null;
  }

  // ---- camera ----------------------------------------------------------------
  function setView(vs) {
    S.viewState = { ...S.viewState, ...vs };
    deckgl.setProps({ viewState: S.viewState });
  }
  function flyTo(lon, lat, zoom) {
    const t = deck.FlyToInterpolator
      ? { transitionDuration: 1100, transitionInterpolator: new deck.FlyToInterpolator() }
      : {};
    setView({ longitude: lon, latitude: lat, zoom: zoom || 13.6, ...t });
  }

  // ---- DOM shell -------------------------------------------------------------
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function buildShell() {
    const shell = document.getElementById("shell");
    shell.innerHTML =
      '<div class="hdr">' +
      '  <div class="mark"></div>' +
      '  <div><h1>Mumbai Traffic Observatory</h1></div>' +
      '  <div class="sub">Western Express Highway · Dahisar → Bandra · 24 km</div>' +
      '  <div class="spacer"></div>' +
      (window.__MAP_DATA__ ? "" : '  <a href="/">Dashboard</a>') +
      '  <button class="btn" id="btn3d"></button>' +
      '  <button class="btn" id="btncine">Cinematic</button>' +
      "</div>" +
      '<div class="main">' +
      '  <div class="panel">' +
      '    <div class="tabs">' +
      '      <button data-tab="overview">Overview</button>' +
      '      <button data-tab="junctions">Junctions</button>' +
      '      <button data-tab="day">Day</button>' +
      '      <button data-tab="data">Data</button>' +
      "    </div>" +
      '    <div class="tabbody" id="tabbody"></div>' +
      "  </div>" +
      '  <div class="maparea">' +
      '    <div id="deck"></div>' +
      '    <div class="float layers" id="layerbox"></div>' +
      '    <div class="float legend" id="legend"></div>' +
      '    <div class="timebar" id="timebar"></div>' +
      "  </div>" +
      "</div>";

    shell.querySelectorAll(".tabs button").forEach((b) => {
      b.onclick = () => { S.tab = b.dataset.tab; renderPanel(); };
    });
    document.getElementById("btn3d").onclick = () => {
      S.is3D = !S.is3D;
      setView({ pitch: S.is3D ? 50 : 0, transitionDuration: 600 });
      renderChrome(); renderLayers();
    };
    document.getElementById("btncine").onclick = () => {
      S.cinema = !S.cinema; renderChrome();
    };
  }

  function renderChrome() {
    const b3 = document.getElementById("btn3d");
    b3.textContent = S.is3D ? "2D view" : "3D view";
    const bc = document.getElementById("btncine");
    bc.classList.toggle("on", S.cinema);
  }

  // ---- layer toggles + legend -------------------------------------------------
  const LAYER_DEFS = [
    ["film", "Speed film (WEH)", "measured"],
    ["probes", "Probe animation", "measured"],
    ["here", "Road speeds (HERE)", "measured"],
    ["points", "Sample points", "measured"],
    ["model", "Link volume · V/C", "modeled"],
    ["columns", "Intersection volumes", "modeled"],
    ["queues", "Queue lengths", "modeled"],
    ["od", "OD flows", "modeled"],
  ];

  function renderLayerBox() {
    const box = document.getElementById("layerbox");
    box.innerHTML = "<h2>Layers</h2>" + LAYER_DEFS.map(([k, label, tag]) =>
      '<label><input type="checkbox" data-k="' + k + '"' +
      (S.show[k] ? " checked" : "") + "> " + label +
      ' <span class="badge ' + tag + ' tag">' + (tag === "measured" ? "M" : "MOD") + "</span></label>"
    ).join("");
    box.querySelectorAll("input").forEach((i) => {
      i.onchange = () => { S.show[i.dataset.k] = i.checked; renderLayers(); };
    });
  }

  function renderLegend() {
    const lg = document.getElementById("legend");
    const row = (color, txt) =>
      '<div class="row"><span class="sw" style="background:' + color + '"></span>' + txt + "</div>";
    lg.innerHTML =
      "<h2>Congestion state</h2>" +
      row(HEX.good, "Free · TTI &lt; 1.15") +
      row(HEX.warning, "Slow · 1.15–1.5") +
      row(HEX.serious, "Congested · 1.5–2.0") +
      row(HEX.critical, "Jammed · ≥ 2.0") +
      '<div class="row" style="margin-top:6px"><span class="sw" style="background:#3a3a36"></span>No sample yet</div>' +
      '<div class="row" style="margin-top:8px;color:var(--muted)">Column height = arriving PCU/h</div>' +
      '<div class="row" style="color:var(--muted)">Red band = queue, to scale</div>';
  }

  // ---- time bar ---------------------------------------------------------------
  function renderTimebar() {
    const tb = document.getElementById("timebar");
    const bins = S.data.frames.bins || [];
    if (!bins.length) {
      tb.innerHTML =
        '<button class="play" disabled>▶</button>' +
        '<div class="clock">—:—<small>IST</small></div>' +
        '<div class="scrub"><input type="range" min="0" max="1" value="0" disabled></div>' +
        '<div class="meta">awaiting first collection run</div>';
      return;
    }
    tb.innerHTML =
      '<button class="play" id="btnplay">' + (S.playing ? "⏸" : "▶") + "</button>" +
      '<div class="clock" id="clock"></div>' +
      '<div class="scrub">' +
      '  <input type="range" id="scrub" min="-1" max="' + (bins.length - 1) + '" step="1" value="' + S.binIdx + '">' +
      '  <div class="ticks">' + bins.map((b, i) =>
        '<div class="tick" style="left:' + (bins.length > 1 ? (i / (bins.length - 1)) * 100 : 50) + '%"></div>'
      ).join("") + "</div>" +
      "</div>" +
      '<div class="meta" id="binmeta"></div>';
    document.getElementById("btnplay").onclick = togglePlay;
    const scrub = document.getElementById("scrub");
    scrub.oninput = () => { setBin(parseInt(scrub.value, 10)); };
    updateClock();
  }

  function updateClock() {
    const clock = document.getElementById("clock");
    const meta = document.getElementById("binmeta");
    if (!clock) return;
    const bin = currentBin();
    clock.innerHTML = (bin ? bin.label : "—:—") + "<small>IST</small>";
    if (meta) {
      meta.textContent = bin
        ? bin.n_obs + " obs · " + bin.n_days + " day" + (bin.n_days > 1 ? "s" : "")
        : "all-day view";
    }
  }

  function setBin(i) {
    S.binIdx = i;
    const scrub = document.getElementById("scrub");
    if (scrub) scrub.value = String(i);
    updateClock();
    renderLayers();
    drawTimespaceCursor();
  }

  function togglePlay() {
    S.playing = !S.playing;
    const b = document.getElementById("btnplay");
    if (b) b.textContent = S.playing ? "⏸" : "▶";
    if (playTimer) { clearInterval(playTimer); playTimer = null; }
    if (S.playing) {
      const bins = S.data.frames.bins || [];
      if (!bins.length) { S.playing = false; return; }
      playTimer = setInterval(() => {
        setBin((S.binIdx + 1) % bins.length);
      }, 1700);
    }
  }

  // ---- side panel tabs --------------------------------------------------------
  function renderPanel() {
    document.querySelectorAll(".tabs button").forEach((b) =>
      b.classList.toggle("on", b.dataset.tab === S.tab));
    const body = document.getElementById("tabbody");
    body.innerHTML = "";
    if (S.tab === "overview") renderOverview(body);
    else if (S.tab === "junctions") renderJunctions(body);
    else if (S.tab === "day") renderDay(body);
    else renderData(body);
  }

  function tile(v, k, wide) {
    return '<div class="tile' + (wide ? " wide" : "") + '"><div class="v">' + v +
           '</div><div class="k">' + k + "</div></div>";
  }

  function renderOverview(body) {
    const sm = S.data.summary;
    const m = sm.model, ms = sm.measured, cost = sm.cost;
    const g = (S.data.od.google || [])[0];

    let corridorTile;
    if (g) {
      const kph = g.distance_km / (g.duration_min / 60);
      corridorTile = tile(
        fmt(g.duration_min) + " <small>min</small>",
        "door-to-door, Dahisar → Bandra (" + fmt1(g.distance_km) + " km at <b>" +
        fmt(kph) + " km/h</b>) <span class='badge measured'>MEASURED</span>", true);
    } else {
      corridorTile = '<div class="tile wide empty"><b>Corridor travel time</b> appears ' +
        "here after the first Google OD reading.</div>";
    }

    body.innerHTML =
      '<div class="sec"><h2>The corridor right now</h2><div class="tiles">' +
      corridorTile +
      tile(fmt1(m.total_queue_km) + " <small>km</small>",
        "total standing queue across <b>" + fmt(m.n_queued_junctions) +
        "</b> junctions in the peak hour <span class='badge modeled'>MODELED</span>") +
      tile(fmt(m.delay_pcu_h) + " <small>PCU-h</small>",
        "lost to congestion every peak hour <span class='badge modeled'>MODELED</span>") +
      tile("₹" + fmt1(cost.annual_inr_crore) + " <small>crore</small>",
        "estimated congestion cost per year <span class='badge modeled'>EST.</span>") +
      tile(fmt(m.n_junctions),
        "junctions monitored — volume + queue computed for each <span class='badge modeled'>MODELED</span>") +
      "</div></div>" +
      '<div class="sec"><h2>How to read this map</h2><div class="note">' +
      "<b>Colored spine</b> — measured probe speed on the WEH, by time of day (play it below the map).<br><br>" +
      "<b>Columns</b> — vehicles arriving at each intersection per hour, from one calibrated equilibrium run.<br><br>" +
      "<b>Red bands</b> — the standing queue on each over-capacity approach, drawn to physical scale on the road it occupies." +
      "</div></div>" +
      '<div class="sec"><h2>Cost basis</h2><div class="note">' +
      fmt(cost.daily_person_h) + " person-hours/day = delay × " +
      cost.assumptions.persons_per_pcu + " persons/PCU × " +
      cost.assumptions.peak_equivalent_hours_per_day + " peak-equivalent hours. " +
      "Valued at ₹" + cost.assumptions.value_of_time_inr_per_person_h +
      "/person-hour × " + cost.assumptions.days_per_year + " days. " +
      "<b>A planning estimate, not a measurement.</b></div></div>";
  }

  function renderJunctions(body) {
    const rows = derived.ranked.map((n, i) => {
      const sel = n.id === S.selected ? " on" : "";
      return '<div class="jrow' + sel + '" data-id="' + n.id + '">' +
        '<span class="rank">' + (i + 1) + "</span>" +
        '<span class="dot" style="background:' + vcHex(n.vc_max) + '"></span>' +
        '<span class="nm"><span class="t">' + esc(jname(n)) + "</span>" +
        '<span class="s">' + esc(derived.locality(n.lat)) + " · " +
        fmt(n.volume_pcu_h) + " PCU/h</span></span>" +
        '<span class="q"><span class="t">' + fmt(n.queue_total_m) + " m</span>" +
        '<span class="s">queue</span></span></div>';
    }).join("");

    body.innerHTML =
      '<div class="sec"><h2>Worst junctions — peak hour ' +
      '<span class="badge modeled">MODELED</span></h2>' + rows + "</div>" +
      '<div id="jdetail"></div>' +
      '<div class="sec note">Ranked by standing queue, then by arriving volume. ' +
      "Click a junction to fly to it. Queue lengths are the no-rerouting upper bound.</div>";

    body.querySelectorAll(".jrow").forEach((r) => {
      r.onclick = () => selectJunction(parseInt(r.dataset.id, 10));
    });
    if (S.selected != null) renderJunctionDetail();
  }

  function selectJunction(id) {
    S.selected = id;
    const n = derived.junctions.find((x) => x.id === id);
    if (n) flyTo(n.lon, n.lat, 14.2);
    if (S.tab === "junctions") renderPanel();
    renderLayers();
  }

  function renderJunctionDetail() {
    const host = document.getElementById("jdetail");
    const n = derived.junctions.find((x) => x.id === S.selected);
    if (!host || !n) return;
    const rows = (n.approaches || []).map((a) =>
      "<tr><td>" + esc(a.name || "unnamed") + "</td>" +
      "<td>" + a.lanes + "</td>" +
      "<td>" + fmt(a.flow_pcu_h) + "/" + fmt(a.capacity_pcu_h) + "</td>" +
      '<td style="color:' + vcHex(a.vc) + ';font-weight:700">' + fmt1(a.vc) + "</td>" +
      "<td>" + (a.head ? fmt(a.queue_len_m) + " m" : "joins downstream jam") +
      "</td></tr>").join("");
    host.innerHTML =
      '<div class="detail"><h3>' + esc(jname(n)) + "</h3>" +
      '<div class="sub">' + esc(derived.locality(n.lat)) + " · arriving volume " +
      fmt(n.volume_pcu_h) + " PCU/h · " + fmt(n.queued_veh_total) + " vehicles queued</div>" +
      (rows
        ? "<table><tr><th>approach</th><th>lanes</th><th>PCU/h / cap</th><th>V/C</th><th>queue</th></tr>" +
          rows + "</table>"
        : '<div class="note">No approach is over capacity at this junction.</div>') +
      "</div>";
  }

  // ---- Day tab: time-space diagram -------------------------------------------
  function renderDay(body) {
    const fr = S.data.frames;
    if (!fr.bins || !fr.bins.length) {
      body.innerHTML =
        '<div class="sec"><h2>A day on the WEH</h2>' +
        '<div class="empty"><b>No speed readings yet.</b> The weekday collector fills ' +
        "this view automatically: every 10 minutes in the peak, every 15 minutes " +
        "otherwise. Each new day of collection adds a column to the diagram that " +
        "will appear here — position along the corridor (vertical) against time of " +
        "day (horizontal).</div></div>";
      return;
    }
    const nCollected = fr.bins.length;
    body.innerHTML =
      '<div class="sec"><h2>A day on the WEH <span class="badge measured">MEASURED</span></h2>' +
      '<div class="chart"><canvas id="tscanvas" height="300"></canvas>' +
      '<div class="cap">Time of day (IST) → across; position along the corridor, ' +
      "Dahisar (top) → Bandra (bottom). Each cell is the mean over all collected " +
      "days. Dark cells: no sample in that slot yet (" + nCollected +
      " of 38 half-hour slots collected).</div></div></div>" +
      '<div class="sec"><h2>Mean corridor TTI by time of day</h2>' +
      '<div class="chart"><canvas id="sparkcanvas" height="90"></canvas>' +
      '<div class="cap">TTI 1.0 = free flow. The peak-hour “shoulders” appear ' +
      "as collection covers 08:00–11:00 and 17:30–20:30.</div></div></div>" +
      '<div class="sec note">Use ▶ under the map to replay the day on the corridor itself.</div>';
    drawTimespace();
    drawSpark();
  }

  const TS = { x0: 34, t0: 5 * 60, t1: 24 * 60 };   // canvas frame + day window

  function drawTimespace() {
    const cv = document.getElementById("tscanvas");
    if (!cv) return;
    const fr = S.data.frames;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 320, H = 300;
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext("2d");
    ctx.scale(dpr, dpr);
    const x0 = TS.x0, y0 = 6, x1 = W - 6, y1 = H - 22;
    const idxs = fr.points.map((p) => p.idx).sort((a, b) => a - b);
    const nIdx = idxs.length;
    const binW = (x1 - x0) / ((TS.t1 - TS.t0) / fr.bin_minutes);
    const cellH = (y1 - y0) / nIdx;

    ctx.fillStyle = "#232322";
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    for (const b of fr.bins) {
      const bx = x0 + ((b.t - TS.t0) / fr.bin_minutes) * binW;
      if (bx < x0 - 1) continue;
      idxs.forEach((idx, r) => {
        ctx.fillStyle = ttiHex(b.tti[idx]);
        ctx.fillRect(bx + 0.5, y0 + r * cellH, Math.max(1, binW - 1), Math.max(1, cellH - 0.35));
      });
    }
    // axes
    ctx.fillStyle = "#898781";
    ctx.font = "10px system-ui";
    ctx.textAlign = "center";
    for (let h = 6; h <= 23; h += 3) {
      const bx = x0 + ((h * 60 - TS.t0) / fr.bin_minutes) * binW;
      ctx.fillText(String(h).padStart(2, "0"), bx, H - 8);
    }
    ctx.save();
    ctx.textAlign = "left";
    ctx.fillText("Dahisar", 0, y0 + 10);
    ctx.fillText("Bandra", 0, y1 - 2);
    ctx.restore();
    drawTimespaceCursor();
  }

  function drawTimespaceCursor() {
    const cv = document.getElementById("tscanvas");
    if (!cv) return;
    // Redraw base then cursor line for the active bin.
    const bin = currentBin();
    if (!bin) return;
    const fr = S.data.frames;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 320, H = 300;
    const ctx = cv.getContext("2d");
    // cheap approach: draw a thin cursor without full redraw accumulation
    // (full redraw first to clear previous cursor)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const x0 = TS.x0, x1 = W - 6;
    const binW = (x1 - x0) / ((TS.t1 - TS.t0) / fr.bin_minutes);
    // redraw everything except cursor:
    drawTimespaceBase(ctx, W, H, fr);
    const bx = x0 + ((bin.t - TS.t0) / fr.bin_minutes) * binW + binW / 2;
    ctx.strokeStyle = "#3987e5";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(bx, 4);
    ctx.lineTo(bx, H - 20);
    ctx.stroke();
  }

  function drawTimespaceBase(ctx, W, H, fr) {
    const x0 = TS.x0, y0 = 6, x1 = W - 6, y1 = H - 22;
    const idxs = fr.points.map((p) => p.idx).sort((a, b) => a - b);
    const nIdx = idxs.length;
    const binW = (x1 - x0) / ((TS.t1 - TS.t0) / fr.bin_minutes);
    const cellH = (y1 - y0) / nIdx;
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = "#232322";
    ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
    for (const b of fr.bins) {
      const bx = x0 + ((b.t - TS.t0) / fr.bin_minutes) * binW;
      idxs.forEach((idx, r) => {
        ctx.fillStyle = ttiHex(b.tti[idx]);
        ctx.fillRect(bx + 0.5, y0 + r * cellH, Math.max(1, binW - 1), Math.max(1, cellH - 0.35));
      });
    }
    ctx.fillStyle = "#898781";
    ctx.font = "10px system-ui";
    ctx.textAlign = "center";
    for (let h = 6; h <= 23; h += 3) {
      const bx = x0 + ((h * 60 - TS.t0) / fr.bin_minutes) * binW;
      ctx.fillText(String(h).padStart(2, "0"), bx, H - 8);
    }
    ctx.textAlign = "left";
    ctx.fillText("Dahisar", 0, y0 + 10);
    ctx.fillText("Bandra", 0, y1 - 2);
  }

  function drawSpark() {
    const cv = document.getElementById("sparkcanvas");
    if (!cv) return;
    const fr = S.data.frames;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 320, H = 90;
    cv.width = W * dpr; cv.height = H * dpr;
    const ctx = cv.getContext("2d");
    ctx.scale(dpr, dpr);
    const x0 = 26, y0 = 8, x1 = W - 6, y1 = H - 18;
    const tMax = Math.max(2.0, ...fr.bins.map((b) => b.mean_tti));
    const X = (t) => x0 + ((t - TS.t0) / (TS.t1 - TS.t0)) * (x1 - x0);
    const Y = (v) => y1 - ((v - 1.0) / (tMax - 1.0)) * (y1 - y0);
    // gridline at TTI 1 and 1.5
    ctx.strokeStyle = "#2c2c2a";
    ctx.lineWidth = 1;
    [1.0, 1.5, 2.0].forEach((v) => {
      if (v > tMax) return;
      ctx.beginPath(); ctx.moveTo(x0, Y(v)); ctx.lineTo(x1, Y(v)); ctx.stroke();
      ctx.fillStyle = "#898781"; ctx.font = "9px system-ui"; ctx.textAlign = "left";
      ctx.fillText(v.toFixed(1), 2, Y(v) + 3);
    });
    // dots + connecting line over collected bins
    ctx.strokeStyle = "#3987e5";
    ctx.lineWidth = 2;
    ctx.beginPath();
    fr.bins.forEach((b, i) => {
      const x = X(b.t + fr.bin_minutes / 2), y = Y(b.mean_tti);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    if (fr.bins.length > 1) ctx.stroke();
    fr.bins.forEach((b) => {
      const x = X(b.t + fr.bin_minutes / 2), y = Y(b.mean_tti);
      ctx.fillStyle = "#3987e5";
      ctx.beginPath(); ctx.arc(x, y, 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "#1a1a19"; ctx.lineWidth = 2; ctx.stroke();
    });
    ctx.fillStyle = "#898781"; ctx.font = "10px system-ui"; ctx.textAlign = "center";
    for (let h = 6; h <= 23; h += 3) ctx.fillText(String(h).padStart(2, "0"), X(h * 60), H - 5);
  }

  // ---- Data tab ---------------------------------------------------------------
  function renderData(body) {
    const ms = S.data.summary.measured;
    const asm = S.data.summary.cost.assumptions;
    const kv = (k, v) => '<div class="row"><span class="k">' + k +
      '</span><span class="v">' + v + "</span></div>";
    body.innerHTML =
      '<div class="sec"><h2>What has been collected</h2><div class="kv">' +
      kv("Speed readings (runs)", fmt(ms.n_runs)) +
      kv("Sample points on the WEH", fmt(ms.n_points)) +
      kv("Collection days", fmt(ms.n_days)) +
      kv("Half-hour slots covered", fmt(ms.n_bins) + " / 38") +
      kv("Span", ms.span_ist ? esc(ms.span_ist[0] + " → " + ms.span_ist[1]) : "—") +
      kv("HERE road-link samples", fmt(ms.here_links)) +
      kv("Google OD readings", fmt(ms.google_od)) +
      "</div></div>" +
      '<div class="sec"><h2>Collection plan</h2><div class="note">' +
      "The weekday collector samples the corridor <b>every 10 minutes in the " +
      "peak</b> (08:00–11:00, 17:30–20:30 IST) and every 15 minutes otherwise. " +
      "Each run lands in the SQLite store and this page reads it live — " +
      "no manual step. As days accumulate, the film, the day diagram, and the " +
      "calibration all sharpen automatically.</div></div>" +
      '<div class="sec"><h2>Providers</h2><div class="note">' +
      "<b>TomTom / HERE</b> — live segment speeds (measured layers).<br>" +
      "<b>Google Routes</b> — door-to-door OD travel times (measured).<br>" +
      "<b>OpenStreetMap</b> — the road network model itself.</div></div>" +
      '<div class="sec"><h2>Model assumptions</h2><div class="note">' +
      "Demand: gravity model scaled to 18,000 PCU/h peak, assigned by user " +
      "equilibrium (Frank-Wolfe, BPR congestion curve).<br><br>" +
      "Queues: deterministic input-output model per over-capacity approach; " +
      "jam density 130 veh/km/lane; no rerouting — an upper bound.<br><br>" +
      "Cost: ₹" + asm.value_of_time_inr_per_person_h + "/person-h · " +
      asm.persons_per_pcu + " persons/PCU · " + asm.peak_equivalent_hours_per_day +
      " peak-equivalent h/day · " + asm.days_per_year + " days/yr.</div></div>";
  }

  // ---- animation loop ---------------------------------------------------------
  function renderLayers() {
    deckgl.setProps({ layers: makeLayers() });
  }

  function tick(now) {
    if (lastTick == null) lastTick = now;
    const dt = Math.min(0.1, (now - lastTick) / 1000);
    lastTick = now;
    S.animT += dt;
    if (S.cinema) {
      S.viewState = { ...S.viewState, bearing: S.viewState.bearing + dt * 1.6 };
      deckgl.setProps({ viewState: S.viewState });
    }
    if (S.show.probes || S.show.queues || S.cinema) renderLayers();
    requestAnimationFrame(tick);
  }

  // ---- boot -------------------------------------------------------------------
  async function boot() {
    // Deep link: /map#junctions, /map#day, /map#data open on that tab.
    const hash = (location.hash || "").slice(1);
    if (["overview", "junctions", "day", "data"].indexOf(hash) >= 0) S.tab = hash;
    buildShell();
    const body = document.getElementById("tabbody");
    body.innerHTML = '<div class="empty">Loading corridor model…</div>';
    try {
      S.data = await loadAll();
    } catch (e) {
      body.innerHTML = '<div class="empty"><b>Could not load map data.</b> ' +
        "Run <code>python -m src.viz.map_export</code> first.<br>" + esc(e.message) + "</div>";
      return;
    }
    derived = derive(S.data);
    // Start at the worst collected bin, so the first view is the strongest one.
    const bins = S.data.frames.bins || [];
    if (bins.length) {
      let worst = 0;
      bins.forEach((b, i) => { if (b.mean_tti > bins[worst].mean_tti) worst = i; });
      S.binIdx = worst;
    }

    deckgl = new deck.DeckGL({
      container: "deck",
      views: new deck.MapView({ repeat: false }),
      viewState: S.viewState,
      onViewStateChange: ({ viewState }) => {
        S.viewState = viewState;
        deckgl.setProps({ viewState });
      },
      controller: true,
      layers: [],
      getTooltip: tooltip,
    });

    renderChrome();
    renderLayerBox();
    renderLegend();
    renderTimebar();
    renderPanel();
    renderLayers();
    requestAnimationFrame(tick);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
