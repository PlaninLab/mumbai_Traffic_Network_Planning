"""
report.py — generate the self-contained HTML stakeholder report (Layer 5).

Embeds the corridor maps and result charts as base64 data URIs so the page is fully
self-contained (no external files), suitable for publishing as an Artifact or opening
directly. Plain-language narrative for non-technical decision-makers.

Usage:
    python -m src.viz.report
Output: docs/report.html
"""

from __future__ import annotations

import base64
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
OUT = DOCS / "report.html"

IMAGES = {
    "NETWORK": DOCS / "corridor_network_raw.png",
    "SNAPSHOT": DOCS / "corridor_congestion_snapshot.png",
    "BASE": DOCS / "scenarios" / "vc_base.png",
    "COMPARE": DOCS / "scenario_comparison.png",
    "MONTAGE": DOCS / "scenario_maps_montage.png",
    "ROBUST": DOCS / "robustness_sweep.png",
}


def _data_uri(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


HTML = """<title>WEH Corridor Decision Report</title>
<style>
  :root {
    --paper:#f5f7f8; --surface:#ffffff; --ink:#18212e; --ink-soft:#5a6675;
    --line:#e0e5ea; --accent:#0e6b83; --accent-soft:#e6f1f3;
    --good:#2a9d8f; --warn:#e0972b; --bad:#d1495b;
    --shadow:0 1px 2px rgba(20,30,45,.05),0 8px 28px rgba(20,30,45,.06);
  }
  :root:not([data-theme="light"]) { }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --paper:#0e131a; --surface:#161e28; --ink:#e9eef3; --ink-soft:#98a5b3;
      --line:#25303d; --accent:#43b3cc; --accent-soft:#123039;
      --good:#3bb9a9; --warn:#e0a341; --bad:#e0687a;
      --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"] {
    --paper:#0e131a; --surface:#161e28; --ink:#e9eef3; --ink-soft:#98a5b3;
    --line:#25303d; --accent:#43b3cc; --accent-soft:#123039;
    --good:#3bb9a9; --warn:#e0a341; --bad:#e0687a;
    --shadow:0 1px 2px rgba(0,0,0,.3),0 10px 30px rgba(0,0,0,.35);
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--paper); color:var(--ink);
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.65; -webkit-font-smoothing:antialiased;
  }
  .wrap { max-width:62rem; margin:0 auto; padding:clamp(1.2rem,4vw,3.5rem); }
  .display { font-family:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif; }
  .eyebrow {
    font-size:.72rem; letter-spacing:.16em; text-transform:uppercase;
    color:var(--accent); font-weight:700; margin:0 0 .8rem;
  }
  h1 { font-size:clamp(2rem,5vw,3.1rem); line-height:1.08; margin:0 0 .6rem;
       text-wrap:balance; letter-spacing:-.01em; }
  h2 { font-size:clamp(1.4rem,3vw,1.9rem); margin:3.2rem 0 .3rem; text-wrap:balance;
       letter-spacing:-.01em; }
  h3 { font-size:1.12rem; margin:1.8rem 0 .3rem; }
  .lede { font-size:1.2rem; color:var(--ink-soft); max-width:40ch; text-wrap:balance; }
  p { margin:.7rem 0; }
  .col { max-width:66ch; }
  a { color:var(--accent); }
  .sec-num {
    display:inline-block; font-family:"Iowan Old Style",Georgia,serif; font-weight:700;
    color:var(--accent); font-size:.95rem; margin-right:.5rem;
    border:1px solid var(--line); border-radius:50%; width:1.9rem; height:1.9rem;
    line-height:1.8rem; text-align:center; vertical-align:.15em;
  }
  figure { margin:1.6rem 0; }
  figure img {
    width:100%; height:auto; display:block; border-radius:12px;
    border:1px solid var(--line); background:var(--surface); box-shadow:var(--shadow);
  }
  figcaption { font-size:.86rem; color:var(--ink-soft); margin-top:.6rem;
               padding-left:.2rem; }
  .verdict {
    background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--accent);
    border-radius:12px; padding:1.3rem 1.5rem; margin:2rem 0; box-shadow:var(--shadow);
  }
  .verdict h3 { margin:0 0 .5rem; font-family:"Iowan Old Style",Georgia,serif; }
  .pipeline {
    display:flex; flex-direction:column; gap:.4rem; margin:1.4rem 0;
    font-family:ui-monospace,"Cascadia Code",Consolas,monospace; font-size:.83rem;
  }
  .layer { display:grid; grid-template-columns:auto 1fr; gap:.9rem; align-items:center;
    background:var(--surface); border:1px solid var(--line); border-radius:10px;
    padding:.7rem .9rem; }
  .layer .n { font-weight:700; color:var(--accent); }
  .layer b { color:var(--ink); }
  table { width:100%; border-collapse:collapse; margin:1.2rem 0; font-size:.95rem;
    font-variant-numeric:tabular-nums; }
  th,td { text-align:left; padding:.65rem .7rem; border-bottom:1px solid var(--line); }
  th { font-size:.74rem; letter-spacing:.06em; text-transform:uppercase; color:var(--ink-soft); }
  td .tag { font-weight:700; }
  .up { color:var(--bad); } .down { color:var(--good); } .mid { color:var(--warn); }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
    gap:1rem; margin:1.4rem 0; }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:12px;
    padding:1.1rem 1.2rem; box-shadow:var(--shadow); }
  .card .big { font-family:"Iowan Old Style",Georgia,serif; font-size:1.9rem;
    font-weight:700; line-height:1; }
  .card .lbl { font-size:.82rem; color:var(--ink-soft); margin-top:.4rem; }
  .swatch { display:inline-block; width:.8em; height:.8em; border-radius:2px;
    margin-right:.35em; vertical-align:.02em; }
  .callout { background:var(--accent-soft); border:1px solid var(--line);
    border-radius:12px; padding:1.1rem 1.3rem; margin:1.6rem 0; }
  ul.clean { padding-left:1.1rem; } ul.clean li { margin:.35rem 0; }
  .foot { margin-top:3.5rem; padding-top:1.4rem; border-top:1px solid var(--line);
    color:var(--ink-soft); font-size:.86rem; }
  hr { border:0; border-top:1px solid var(--line); margin:2.5rem 0; }
</style>

<div class="wrap">

  <p class="eyebrow">Mumbai Traffic Network Planning &middot; Baseline Report</p>
  <h1 class="display">Should we build it? Testing road fixes on the Western Express Highway.</h1>
  <p class="lede">A digital model of the Dahisar&ndash;Bandra corridor that predicts how traffic
  responds to infrastructure changes &mdash; before construction.</p>

  <div class="verdict">
    <h3>The bottom line</h3>
    <p style="margin:0">On the same traffic demand, the model tested four interventions. Only
    <b>widening</b> the worst link helped. A new <b>bypass backfired</b> (+6% total travel time
    &mdash; the Braess paradox). <b>Closing</b> the highway link was worst (+19%). A <b>stalled
    vehicle</b> steadily worsened delay as more piled up. These conclusions held under every
    uncertainty setting we tried &mdash; so the <i>advice is trustworthy even though the exact
    numbers still need peak-hour calibration.</i></p>
  </div>

  <h2><span class="sec-num">1</span><span class="display">The problem</span></h2>
  <div class="col">
    <p>Mumbai's roads are saturated, and every fix is expensive. But roads don't behave simply:
    <b>you can't tell drivers which route to take.</b> Build a new road and everyone piles onto
    it; widen one spot and the jam moves down the street. Occasionally a brand-new road makes
    traffic <i>worse</i> &mdash; a real, proven effect called the <b>Braess paradox</b>.</p>
    <p>So "will this project help?" can't be answered by intuition. This tool answers it by
    <b>simulation</b>: build a digital twin of the corridor, pour in realistic demand, let
    simulated drivers each pick their fastest route (as real drivers do), and measure the
    congestion. Then change one thing and re-simulate to see the difference.</p>
  </div>

  <h2><span class="sec-num">2</span><span class="display">How it works</span></h2>
  <div class="col">
    <p>Five layers, each feeding the next &mdash; from raw map data up to the maps and charts
    in this report.</p>
  </div>
  <div class="pipeline">
    <div class="layer"><span class="n">5</span><span><b>Reporting</b> &mdash; maps, comparison charts, this report</span></div>
    <div class="layer"><span class="n">4</span><span><b>Scenarios</b> &mdash; change the network, re-simulate, compare</span></div>
    <div class="layer"><span class="n">3</span><span><b>Assignment</b> &mdash; drivers pick fastest routes until nobody can do better</span></div>
    <div class="layer"><span class="n">2</span><span><b>Demand</b> &mdash; how many trips run between each area in the peak hour</span></div>
    <div class="layer"><span class="n">1</span><span><b>Network</b> &mdash; the road map: lanes, speed, capacity per segment</span></div>
    <div class="layer"><span class="n">0</span><span><b>Data</b> &mdash; OpenStreetMap (roads) + TomTom (live speeds)</span></div>
  </div>

  <figure>
    <img src="%%NETWORK%%" alt="Road network of the WEH corridor">
    <figcaption>The corridor's real road map from OpenStreetMap. The red spine is the Western
    Express Highway; orange/yellow are main arterials; grey is the local street fabric.</figcaption>
  </figure>

  <h3>Real congestion, measured live</h3>
  <div class="col">
    <p>We sample real vehicle speeds along the highway from the <b>TomTom traffic API</b> and
    measure how much slower than free-flow each point is. This grounds the model in reality.</p>
  </div>
  <figure>
    <img src="%%SNAPSHOT%%" alt="Live congestion snapshot along the WEH">
    <figcaption><span class="swatch" style="background:#2a9d8f"></span>free-flowing
    <span class="swatch" style="background:#e9c46a;margin-left:1rem"></span>slowing
    <span class="swatch" style="background:#f4a261;margin-left:1rem"></span>congested &mdash;
    a real snapshot. The Dahisar&ndash;Goregaon stretch and Bandra approach slow down even
    off-peak.</figcaption>
  </figure>

  <h2><span class="sec-num">3</span><span class="display">The model, in three ideas</span></h2>
  <div class="cards">
    <div class="card"><div class="big">1</div><div class="lbl"><b>Drivers are selfish.</b>
      Each picks the route fastest for them; the model shuffles routes until no one can do
      better &mdash; a stable equilibrium.</div></div>
    <div class="card"><div class="big">2</div><div class="lbl"><b>Roads slow as they fill.</b>
      Near-empty roads run free; over capacity they crawl. A standard engineering curve
      captures this.</div></div>
    <div class="card"><div class="big">3</div><div class="lbl"><b>A stalled vehicle steals
      extra road.</b> Traffic swerves around it, losing far more capacity than the car's
      size &mdash; our custom addition.</div></div>
  </div>
  <div class="col">
    <p>The headline measure is <b>Total System Travel Time</b> &mdash; the total person-hours the
    corridor spends driving. Lower is better; every scenario is judged by how it moves this number.</p>
  </div>

  <h2><span class="sec-num">4</span><span class="display">Results</span></h2>
  <h3>The model finds the real bottleneck on its own</h3>
  <div class="col">
    <p>Run on today's network, the <b>highway itself lights up red</b> (over capacity) &mdash;
    exactly the bottleneck every commuter knows. Reproducing reality <i>without being told to</i>
    is the key validation.</p>
  </div>
  <figure>
    <img src="%%BASE%%" alt="Base-case congestion map, WEH in red">
    <figcaption>Base case. <span class="swatch" style="background:#2a9d8f"></span>under capacity
    &rarr; <span class="swatch" style="background:#e63946;margin-left:.6rem"></span>over capacity.
    The WEH spine is saturated end to end.</figcaption>
  </figure>

  <h3>Every case, simulated and compared</h3>
  <table>
    <thead><tr><th>Intervention</th><th>Total travel time</th><th>What it means</th></tr></thead>
    <tbody>
      <tr><td><b>A &middot; Widen worst link</b></td><td class="down tag">&#9660; improves</td>
        <td>Helps, but the jam partly relocates</td></tr>
      <tr><td><b>B &middot; Add a bypass</b></td><td class="up tag">&#9650; +6% worse</td>
        <td>Braess paradox &mdash; the new road backfires</td></tr>
      <tr><td><b>C &middot; Close the link</b></td><td class="up tag">&#9650; +19% worse</td>
        <td>Losing the bottleneck link is very costly</td></tr>
      <tr><td><b>D &middot; Stalled vehicles (1&rarr;3)</b></td><td class="up tag">&#9650; +1% &rarr; +8%</td>
        <td>Each breakdown compounds the delay</td></tr>
    </tbody>
  </table>
  <figure>
    <img src="%%COMPARE%%" alt="Bar charts comparing all scenarios">
    <figcaption>Left: total travel time per case (green improves, red worsens). Right: the same
    as a percentage change from today.</figcaption>
  </figure>
  <figure>
    <img src="%%MONTAGE%%" alt="Side-by-side congestion maps for each scenario">
    <figcaption>The corridor under each intervention. Note how closing the link (third panel)
    pushes red and orange congestion out onto the parallel arterials.</figcaption>
  </figure>

  <h3>Does the advice survive uncertainty?</h3>
  <div class="col">
    <p>Our demand and speed inputs are approximate, so we re-ran <b>every case under five different
    settings</b> (higher/lower traffic, different congestion assumptions, capacity caps). The
    <b>directional conclusions never flip</b> &mdash; only the exact percentages move.</p>
  </div>
  <figure>
    <img src="%%ROBUST%%" alt="Robustness of each intervention across five settings">
    <figcaption>Each intervention's impact across five settings. Widening is always the only
    improvement; stalled vehicles always hurt; closure is always ~20% worse; the bypass always
    backfires. <b>The planning advice is stable.</b></figcaption>
  </figure>

  <h2><span class="sec-num">5</span><span class="display">What we assume &mdash; and the one real gap</span></h2>
  <div class="col">
    <p>This is a <b>baseline</b>: it proves the machinery works and gives stable directional
    advice. It is not yet planning-grade, and we are explicit about why:</p>
    <ul class="clean">
      <li><b>Demand is synthetic</b> &mdash; a plausible estimate, not a travel survey. Absolute
      volumes are approximate.</li>
      <li><b>Fixed demand</b> before/after a project &mdash; ignores that new roads attract new
      trips (to be added later).</li>
      <li><b>Instant rerouting</b> &mdash; right for planning questions, but understates the
      short-term chaos of a live incident.</li>
      <li><b>Some lane counts guessed</b> &mdash; only 16% are tagged in the open map data.</li>
    </ul>
  </div>
  <div class="callout">
    <p style="margin:0"><b>The one thing that would most improve accuracy:</b> a single
    working-day peak-hour speed reading (Mon&ndash;Fri, 8&ndash;10&nbsp;AM or 6&ndash;8&nbsp;PM).
    Our three snapshots so far are all holiday/off-peak and too "flat" to fully calibrate the
    congestion curve. One good peak reading unlocks real calibration.</p>
  </div>

  <h2><span class="sec-num">6</span><span class="display">Possible next steps</span></h2>
  <div class="col">
    <ul class="clean">
      <li><b>Collect one working-day peak reading</b> &rarr; unlock real calibration (highest value, least effort).</li>
      <li><b>Calibrate demand to real volumes</b> using the TomTom "origin&ndash;destination from travel-times" method &mdash; the biggest accuracy jump.</li>
      <li><b>Add induced demand</b> so project benefits aren't overstated.</li>
      <li><b>Verify lane counts</b> on the highway from satellite imagery.</li>
      <li><b>Real census wards</b> for the demand zones.</li>
      <li><b>Microsimulation</b> for true incident dynamics and signal timing, once the base model is trusted.</li>
    </ul>
  </div>

  <div class="foot">
    <p>Baseline status: modelling pipeline complete end to end (network &rarr; demand &rarr;
    equilibrium &rarr; scenarios &rarr; reporting). Results are structurally sound; absolute
    numbers await peak-hour calibration. Pilot corridor: Western Express Highway, Dahisar&ndash;Bandra.
    Data: OpenStreetMap &amp; TomTom. Method: static User-Equilibrium traffic assignment.</p>
  </div>

</div>
"""


def build(out: Path = OUT) -> Path:
    html = HTML
    for key, path in IMAGES.items():
        if not path.exists():
            raise FileNotFoundError(f"Missing figure: {path} — run the viz steps first.")
        html = html.replace(f"%%{key}%%", _data_uri(path))
    out.write_text(html, encoding="utf-8")
    size_mb = out.stat().st_size / 1e6
    print(f"[report] Wrote {out}  ({size_mb:.1f} MB, self-contained)")
    return out


if __name__ == "__main__":
    build()
