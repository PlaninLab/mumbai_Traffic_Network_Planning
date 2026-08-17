"""
bmc_sim_map.py — self-contained BMC junction map with a simulation timeline.

Reads data/processed/bmc/bmc_frames.json (from `bmc_scale.py --frames`) and writes
docs/bmc_sim.html: a single offline file (deck.gl inlined) that draws all 867 BMC
junctions colored by modelled V/C, with a slider/▶ that ramps demand off-peak →
over-peak so you can watch congestion build across Greater Mumbai and spot bugs
(junctions lighting up in the wrong place or out of order) at a glance.

Usage:
    python -m src.scenarios.bmc_scale --frames    # build the frames first
    python -m src.viz.bmc_sim_map
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMES = REPO_ROOT / "data" / "processed" / "bmc" / "bmc_frames.json"
DECK = REPO_ROOT / "src" / "web" / "static" / "vendor" / "deck.min.js"
OUT = REPO_ROOT / "docs" / "bmc_sim.html"

CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html,body { margin:0; height:100%; background:#0d0d0d; color:#eee;
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  #deck { position:absolute; inset:0; }
  .hdr { position:absolute; top:0; left:0; right:0; padding:10px 16px; z-index:5;
    background:linear-gradient(#0d0d0dcc,#0d0d0d00); display:flex; align-items:baseline; gap:12px; }
  .hdr h1 { font-size:15px; margin:0; font-weight:650; }
  .hdr .sub { color:#8a8a84; font-size:12px; }
  .legend { position:absolute; top:52px; right:14px; z-index:5; background:#161616e8;
    border:1px solid #2a2a28; border-radius:10px; padding:10px 12px; font-size:12px; min-width:150px; }
  .legend h2 { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:#8a8a84; margin:0 0 6px; }
  .legend .row { display:flex; align-items:center; gap:7px; margin:3px 0; }
  .sw { width:12px; height:12px; border-radius:3px; display:inline-block; }
  .bar { position:absolute; left:14px; right:14px; bottom:14px; z-index:6; background:#161616f0;
    border:1px solid #2a2a28; border-radius:12px; padding:12px 16px; display:flex; align-items:center; gap:14px; }
  .bar button { background:#2a2a28; color:#eee; border:0; width:38px; height:38px; border-radius:9px;
    font-size:16px; cursor:pointer; }
  .bar button:hover { background:#3a3a37; }
  .bar .lab { min-width:210px; }
  .bar .lab .t { font-weight:650; }
  .bar .lab .s { color:#8a8a84; font-size:12px; }
  .bar input[type=range] { flex:1; accent-color:#3987e5; }
  .bar .stat { text-align:right; font-variant-numeric:tabular-nums; min-width:120px; }
  .bar .stat b { font-size:16px; }
  .bar .stat span { color:#8a8a84; font-size:11px; display:block; }
  .tt { background:#161616f5!important; color:#eee!important; font:12px system-ui!important;
    border:1px solid #2a2a28!important; border-radius:8px!important; padding:8px 10px!important; }
"""

APP = r"""
(function(){
  "use strict";
  const D = window.__BMC__;
  const F = D.frames, J = D.junctions;
  const maxVol = Math.max(1, ...J.flatMap(j => j.vol));
  const S = { f: F.length - 1, playing: false, timer: null };

  const HEX = { good:"#0ca30c", warn:"#fab219", ser:"#ec835a", crit:"#d03b3b" };
  function vcCol(v){ if(v==null)return[80,80,74];
    if(v<0.7)return[12,163,12]; if(v<0.9)return[250,178,25];
    if(v<1.1)return[236,131,90]; return[208,59,59]; }
  function vcWord(v){ if(v<0.7)return"free"; if(v<0.9)return"busy";
    if(v<1.1)return"at capacity"; return"over capacity"; }
  const fmt = n => (n==null?"—":Math.round(n).toLocaleString("en-IN"));

  const deckgl = new deck.DeckGL({
    container:"deck",
    views:new deck.MapView({repeat:false}),
    initialViewState:{ longitude:72.877, latitude:19.075, zoom:10.6, pitch:0, bearing:0,
      minZoom:8, maxZoom:16 },
    controller:true,
    getTooltip: info => {
      const o=info.object; if(!o||info.layer.id!=="jn")return null;
      const i=S.f;
      return { className:"tt", html:
        "<b>"+(o.name||"Junction")+"</b><br>"+
        "V/C <b style='color:"+({0:HEX.good,1:HEX.warn,2:HEX.ser,3:HEX.crit})[o.vc[i]<0.7?0:o.vc[i]<0.9?1:o.vc[i]<1.1?2:3]+"'>"+
        (o.vc[i]).toFixed(2)+"</b> ("+vcWord(o.vc[i])+")<br>"+
        "arriving <b>"+fmt(o.vol[i])+"</b> PCU/h · queue <b>"+(o.q[i]).toFixed(2)+"</b> km · modeled" };
    },
  });

  function layers(){
    const i=S.f;
    return [
      new deck.PathLayer({ id:"ctx", data:D.context_links, getPath:p=>p,
        getColor:[54,54,49], getWidth:2, widthMinPixels:0.5, opacity:0.8,
        capRounded:true, jointRounded:true, pickable:false }),
      new deck.ScatterplotLayer({ id:"jn", data:J,
        getPosition:j=>[j.lon,j.lat],
        getRadius:j=>90+520*(j.vol[i]/maxVol),
        radiusMinPixels:2.5, radiusMaxPixels:22,
        getFillColor:j=>[...vcCol(j.vc[i]),225],
        stroked:true, getLineColor:[13,13,13,180], lineWidthMinPixels:0.4,
        pickable:true, autoHighlight:true, highlightColor:[255,255,255,60],
        updateTriggers:{ getFillColor:i, getRadius:i } }),
    ];
  }
  function render(){ deckgl.setProps({layers:layers()}); paintBar(); }

  function paintBar(){
    const fr=F[S.f];
    document.getElementById("lab").innerHTML=
      "<div class='t'>"+fr.label+"</div><div class='s'>frame "+(S.f+1)+" / "+F.length+"</div>";
    document.getElementById("scrub").value=String(S.f);
    document.getElementById("stat").innerHTML=
      "<b>"+fmt(fr.tstt_pcu_h)+"</b><span>TSTT PCU·h</span>";
    document.getElementById("stat2").innerHTML=
      "<b>"+fr.max_vc.toFixed(2)+"</b><span>max V/C · "+fr.over_cap+" links over cap</span>";
    document.getElementById("play").textContent=S.playing?"⏸":"▶";
  }
  function setF(i){ S.f=Math.max(0,Math.min(F.length-1,i)); render(); }
  function toggle(){
    S.playing=!S.playing;
    if(S.timer){clearInterval(S.timer);S.timer=null;}
    if(S.playing) S.timer=setInterval(()=>setF((S.f+1)%F.length),1400);
    paintBar();
  }

  document.getElementById("play").onclick=toggle;
  document.getElementById("scrub").max=String(F.length-1);
  document.getElementById("scrub").oninput=e=>setF(parseInt(e.target.value,10));
  render();
})();
"""


def _inline(text: str) -> str:
    return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)


def build(out: Path = OUT) -> Path:
    if not FRAMES.exists():
        raise FileNotFoundError(
            "bmc_frames.json missing — run: python -m src.scenarios.bmc_scale --frames")
    data = FRAMES.read_text(encoding="utf-8")
    deck = _inline(DECK.read_text(encoding="utf-8"))
    html = (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Greater Mumbai — Peak build-up simulation</title>"
        f"<style>{CSS}</style></head><body>"
        "<div id='deck'></div>"
        "<div class='hdr'><h1>Greater Mumbai — BMC junctions</h1>"
        "<span class='sub'>867 junctions · modelled V/C · peak build-up simulation</span></div>"
        "<div class='legend'><h2>V/C · congestion</h2>"
        "<div class='row'><span class='sw' style='background:#0ca30c'></span>free &lt; 0.7</div>"
        "<div class='row'><span class='sw' style='background:#fab219'></span>busy 0.7–0.9</div>"
        "<div class='row'><span class='sw' style='background:#ec835a'></span>at capacity 0.9–1.1</div>"
        "<div class='row'><span class='sw' style='background:#d03b3b'></span>over capacity ≥ 1.1</div>"
        "<div class='row' style='margin-top:6px;color:#8a8a84'>dot size = arriving PCU/h</div></div>"
        "<div class='bar'><button id='play'>▶</button>"
        "<div class='lab' id='lab'></div>"
        "<input type='range' id='scrub' min='0' step='1' value='0'>"
        "<div class='stat' id='stat'></div><div class='stat' id='stat2'></div></div>"
        f"<script>{deck}</script>"
        f"<script>window.__BMC__={data};</script>"
        f"<script>{APP}</script>"
        "</body></html>"
    )
    out.write_text(html, encoding="utf-8")
    print(f"[bmc-sim] wrote {out}  ({out.stat().st_size/1024/1024:.1f} MB)")
    return out


if __name__ == "__main__":
    build()
