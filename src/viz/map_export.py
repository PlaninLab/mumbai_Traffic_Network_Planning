"""
map_export.py — build every JSON payload for the interactive corridor map.

READ-ONLY over the collected data: this module never calls a provider API and
never writes to the readings DB. It folds together

    data/processed/network_corridor_enriched.graphml   the road model
    data/processed/link_flows.csv (+ meta)             one base-case UE run
    data/processed/intersection_metrics.json           volume + queue per junction
    data/processed/traffic.db  (or raw CSVs)           measured speed readings
    data/raw/here/flow/*.json                          measured HERE link shapes
    data/raw/google/route_matrix/*.json                measured OD travel times
    data/processed/od_matrix.csv                       modeled demand (for arcs)

into data/processed/map/*.json, which /map serves. The regional ``coverage``
payload is different from the corridor model: it is an OSM inventory of major
roads and junction locations, enriched only with provider readings that really
exist in SQLite. No traffic value is imputed for an uncollected junction.

Every payload degrades to an
empty-but-valid structure when its source is missing, so the page renders on
day one and simply fills in as collection proceeds.

CLI:
    python -m src.viz.map_export                 # payloads only
    python -m src.viz.map_export --standalone    # + docs/corridor_map.html (single file)
    python -m src.viz.map_export --rebuild       # force a fresh UE solve first
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.assignment.intersections import (INTERSECTIONS_JSON, build,
                                          load_or_solve)
from src.network.graph_io import load_enriched_graph
from src.network.zones import CORRIDOR_NORTH, EAST, WEST, ZONE_BANDS

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED = REPO_ROOT / "data" / "processed"
MAP_DIR = PROCESSED / "map"
DOCS = REPO_ROOT / "docs"
WEB = REPO_ROOT / "src" / "web"

IST = timezone(timedelta(hours=5, minutes=30))
ZONE_LON = 0.5 * (WEST + EAST)

HIGHWAY_CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary", "other"]

# Cost-of-delay planning assumptions — surfaced verbatim in the UI so the
# estimate is never mistaken for a measurement.
COST_ASSUMPTIONS = {
    "persons_per_pcu": 1.5,
    "value_of_time_inr_per_person_h": 140,
    "peak_equivalent_hours_per_day": 3,
    "days_per_year": 300,
}


# --- geometry helpers ----------------------------------------------------------

def _m_per_deg(lat: float) -> tuple[float, float]:
    """Meters per degree of latitude / longitude at this latitude."""
    return 110574.0, 111320.0 * math.cos(math.radians(lat))


def _seg_len_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    my, mx = _m_per_deg(0.5 * (a[1] + b[1]))
    return math.hypot((b[0] - a[0]) * mx, (b[1] - a[1]) * my)


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _edge_path(G, u, v, d) -> list[list[float]]:
    """Edge polyline as [lon, lat] pairs, oriented u -> v."""
    geom = d.get("geometry")
    if geom is not None:
        pts = [[round(x, 5), round(y, 5)] for x, y in geom.coords]
    else:
        pts = [[round(G.nodes[u]["x"], 5), round(G.nodes[u]["y"], 5)],
               [round(G.nodes[v]["x"], 5), round(G.nodes[v]["y"], 5)]]
    # GraphML geometry is not guaranteed oriented u->v: flip if the first
    # vertex sits nearer to v than to u.
    ux, uy = G.nodes[u]["x"], G.nodes[u]["y"]
    vx, vy = G.nodes[v]["x"], G.nodes[v]["y"]
    p0 = pts[0]
    if ((p0[0] - ux) ** 2 + (p0[1] - uy) ** 2) > ((p0[0] - vx) ** 2 + (p0[1] - vy) ** 2):
        pts = pts[::-1]
    return pts


def _truncate_from_end(path: list[list[float]], keep_m: float) -> tuple[list[list[float]], float]:
    """Keep `keep_m` meters of a polyline measured from its END (the junction).

    Returns (sub-path ordered upstream->junction, meters actually kept).
    """
    if keep_m <= 0 or len(path) < 2:
        return [], 0.0
    out = [path[-1]]
    kept = 0.0
    for i in range(len(path) - 1, 0, -1):
        a, b = path[i], path[i - 1]          # walking backwards
        seg = _seg_len_m(a, b)
        if seg <= 0:
            continue
        if kept + seg >= keep_m:
            frac = (keep_m - kept) / seg
            out.append([round(a[0] + (b[0] - a[0]) * frac, 6),
                        round(a[1] + (b[1] - a[1]) * frac, 6)])
            kept = keep_m
            break
        out.append(b)
        kept += seg
    return out[::-1], kept


# --- payload builders ----------------------------------------------------------

def build_network(G, links: pd.DataFrame) -> dict:
    """All model links with geometry, class and assigned flow / V/C."""
    flow_by_edge = {(int(r.u), int(r.v), int(r.key)): (float(r.flow_pcu_hr),
                                                       float(r.vc_ratio) if pd.notna(r.vc_ratio) else 0.0)
                    for r in links.itertuples(index=False)}
    out = []
    for u, v, k, d in G.edges(keys=True, data=True):
        hwy = _first(d.get("highway")) or "other"
        base = str(hwy).replace("_link", "")
        cls = HIGHWAY_CLASSES.index(base) if base in HIGHWAY_CLASSES else 5
        flow, vc = flow_by_edge.get((int(u), int(v), int(k)), (0.0, 0.0))
        out.append({
            "p": _edge_path(G, u, v, d),
            "cls": cls,
            "name": str(_first(d.get("name")) or ""),
            "lanes": int(d.get("lanes") or 1),
            "flow": round(flow, 0),
            "vc": round(vc, 2),
            "cap": round(float(d.get("capacity_eff_pcu_hr") or 0), 0),
        })
    return {"classes": HIGHWAY_CLASSES, "links": out}


def build_intersections(G, links: pd.DataFrame) -> dict:
    """intersection_metrics.json + a drawn queue band per congested approach."""
    with INTERSECTIONS_JSON.open(encoding="utf-8") as f:
        metrics = json.load(f)

    # Fast lookups for the upstream walk.
    edge_data = {}
    incoming: dict[int, list[tuple]] = {}
    for r in links.itertuples(index=False):
        e = (int(r.u), int(r.v), int(r.key))
        edge_data[e] = float(r.flow_pcu_hr)
        incoming.setdefault(int(r.v), []).append(e)

    def queue_band(u: int, v: int, k: int, length_m: float) -> list[list[float]]:
        """Polyline of `length_m` meters ending at node v, walking upstream
        through the highest-flow predecessor when one link is not enough."""
        band: list[list[float]] = []
        need = length_m
        edge = (u, v, k)
        visited = {v}
        for _hop in range(40):
            eu, ev, ek = edge
            try:
                d = G[eu][ev][ek]
            except KeyError:
                break
            path = _edge_path(G, eu, ev, d)
            sub, kept = _truncate_from_end(path, need)
            if sub:
                band = sub[:-1] + band if band else sub
            need -= kept
            if need <= 1.0 or eu in visited:
                break
            visited.add(eu)
            preds = [e for e in incoming.get(eu, []) if e[0] not in visited]
            if not preds:
                break
            edge = max(preds, key=lambda e: edge_data.get(e, 0.0))
        return band

    nodes = []
    for n in metrics["nodes"]:
        approaches = []
        for a in n["approaches"]:
            # Only the bottleneck HEAD draws a band; the band itself walks
            # upstream over the whole saturated chain, so one jam = one band.
            band = (queue_band(a["u"], a["v"], a["key"], a["queue_len_m"])
                    if a.get("head") else [])
            approaches.append({**a, "band": band})
        nodes.append({**n, "approaches": approaches})
    return {**metrics, "nodes": nodes}


def _readings_frame() -> pd.DataFrame:
    """All collected speed readings: the DB first, raw CSVs as fallback."""
    from src.data import store
    df = store.load_readings_df()
    if not df.empty:
        df = df.rename(columns={})
        return df[["fetched_utc", "idx", "lat", "lon",
                   "current_speed_kph", "free_speed_kph", "tti"]].copy()

    frames = []
    for f in sorted(glob.glob(str(REPO_ROOT / "data/raw/tomtom/collected/flow_*.csv"))):
        c = pd.read_csv(f)
        c = c.rename(columns={"currentSpeed_kph": "current_speed_kph",
                              "freeFlowSpeed_kph": "free_speed_kph"})
        frames.append(c[["fetched_utc", "idx", "lat", "lon",
                         "current_speed_kph", "free_speed_kph", "tti"]])
    if not frames:
        return pd.DataFrame(columns=["fetched_utc", "idx", "lat", "lon",
                                     "current_speed_kph", "free_speed_kph", "tti"])
    return pd.concat(frames, ignore_index=True)


def build_frames(bin_minutes: int = 30) -> dict:
    """Measured speeds folded into IST time-of-day bins for the film + heatmap."""
    df = _readings_frame()
    empty = {"bin_minutes": bin_minutes, "points": [], "bins": [],
             "n_runs": 0, "n_days": 0, "span_ist": None}
    if df.empty:
        return empty

    ts = pd.to_datetime(df["fetched_utc"], errors="coerce", utc=True)
    df = df[ts.notna()].copy()
    ist = ts.dt.tz_convert("Asia/Kolkata")
    df["day"] = ist.dt.strftime("%Y-%m-%d")
    df["tod_min"] = (ist.dt.hour * 60 + ist.dt.minute) // bin_minutes * bin_minutes

    points = (df.groupby("idx")
                .agg(lat=("lat", "first"), lon=("lon", "first"),
                     free_kph=("free_speed_kph", "mean"))
                .round({"lat": 5, "lon": 5, "free_kph": 1})
                .reset_index())

    bins = []
    for tod, g in df.groupby("tod_min"):
        pt = (g.groupby("idx").agg(tti=("tti", "mean"),
                                   kph=("current_speed_kph", "mean")).round(2))
        bins.append({
            "t": int(tod),
            "label": f"{tod // 60:02d}:{tod % 60:02d}",
            "n_days": int(g["day"].nunique()),
            "n_obs": int(len(g)),
            "mean_tti": round(float(g["tti"].mean()), 3),
            "tti": {int(i): float(r["tti"]) for i, r in pt.iterrows()},
            "kph": {int(i): float(r["kph"]) for i, r in pt.iterrows()},
        })
    bins.sort(key=lambda b: b["t"])
    return {
        "bin_minutes": bin_minutes,
        "points": points.to_dict(orient="records"),
        "bins": bins,
        "n_runs": int(pd.to_datetime(df["fetched_utc"]).nunique()),
        "n_days": int(df["day"].nunique()),
        "span_ist": [df["day"].min(), df["day"].max()],
    }


def build_od() -> dict:
    """Modeled OD desire lines + measured Google corridor travel times."""
    zones = []
    north = CORRIDOR_NORTH
    for name, south in ZONE_BANDS:
        zones.append({"name": name, "lat": round(0.5 * (north + south), 5),
                      "lon": round(ZONE_LON, 5)})
        north = south

    arcs = []
    od_csv = PROCESSED / "od_matrix.csv"
    if od_csv.exists():
        od = pd.read_csv(od_csv, index_col=0)
        pos = {z["name"]: z for z in zones}
        for o_name, row in od.iterrows():
            for d_name, v in row.items():
                if o_name == d_name or o_name not in pos or d_name not in pos:
                    continue
                v = float(v)
                if v >= 20.0:
                    arcs.append({"o": o_name, "d": d_name, "v": round(v, 0)})
        arcs.sort(key=lambda a: -a["v"])
        arcs = arcs[:80]

    google = []
    for f in sorted(glob.glob(str(REPO_ROOT / "data/raw/google/route_matrix/*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            origins = d["identity"]["origins"]
            dests = d["identity"]["destinations"]
            for el in d.get("response", []):
                if el.get("condition") != "ROUTE_EXISTS":
                    continue
                o = [float(x) for x in origins[el.get("originIndex", 0)].split(",")]
                de = [float(x) for x in dests[el.get("destinationIndex", 0)].split(",")]
                dur = el.get("duration", "0s")
                secs = float(re.sub(r"[^0-9.]", "", str(dur)) or 0)
                google.append({
                    "o": [o[1], o[0]], "d": [de[1], de[0]],
                    "duration_min": round(secs / 60.0, 1),
                    "distance_km": round(el.get("distanceMeters", 0) / 1000.0, 1),
                    "fetched_utc": d.get("fetched_at_utc"),
                })
        except (KeyError, ValueError, IndexError, json.JSONDecodeError):
            continue
    return {"zones": zones, "arcs": arcs, "google": google}


def build_here() -> dict:
    """Measured HERE link shapes with speeds (HERE reports m/s; we ship km/h).

    Repeated fetches of the same street keep only the LATEST reading, so the
    layer shows one current color per road, not stacked history.
    """
    by_key: dict[tuple, dict] = {}
    for f in sorted(glob.glob(str(REPO_ROOT / "data/raw/here/flow/*.json"))):
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
            fetched = d.get("fetched_at_utc")
            for res in d.get("response", {}).get("results", []):
                flow = res.get("currentFlow") or {}
                speed = flow.get("speed")
                free = flow.get("freeFlow")
                if not speed or not free:
                    continue
                pts = []
                for link in res.get("location", {}).get("shape", {}).get("links", []):
                    for p in link.get("points", []):
                        q = [round(p["lng"], 5), round(p["lat"], 5)]
                        if not pts or pts[-1] != q:
                            pts.append(q)
                if len(pts) < 2:
                    continue
                desc = res.get("location", {}).get("description", "")
                key = (desc, tuple(pts[0]), tuple(pts[-1]))
                prev = by_key.get(key)
                if prev and str(prev["fetched_utc"]) >= str(fetched):
                    continue
                by_key[key] = {
                    "p": pts,
                    "desc": desc,
                    "kph": round(speed * 3.6, 1),
                    "free_kph": round(free * 3.6, 1),
                    "tti": round(free / speed, 2) if speed > 0 else None,
                    "jam": flow.get("jamFactor"),
                    "fetched_utc": fetched,
                }
        except (KeyError, ValueError, json.JSONDecodeError):
            continue
    return {"links": list(by_key.values())}


def build_coverage() -> dict:
    """Return the nested BMC/MMRDA OSM inventory plus real latest readings.

    ``src.network.coverage`` owns the geometry snapshot and writes it to the
    coverage payload path. This exporter only joins successful provider
    observations from the separate ``intersection_readings`` table. Missing
    geometry or missing readings stay visibly empty; traffic is never inferred.
    """
    p = MAP_DIR / "coverage.json"
    if p.exists():
        with p.open(encoding="utf-8") as f:
            coverage = json.load(f)
    else:
        coverage = {
            "generated_utc": None,
            "source": "OpenStreetMap",
            "note": "Run: python -m src.network.coverage --download",
            "scopes": {
                "bmc": {"label": "BMC", "junction_count": 0,
                        "collected_count": 0},
                "mmrda": {"label": "MMRDA", "junction_count": 0,
                          "collected_count": 0},
            },
            "links": [],
            "junctions": [],
        }

    # Imported lazily so a map export can still render its empty-state payload
    # in a minimal environment where only the static geometry file is present.
    from src.data import store

    latest = store.load_latest_intersection_readings()
    latest_by_id = {}
    if not latest.empty:
        for row in latest.to_dict(orient="records"):
            def json_value(key: str):
                value = row.get(key)
                if value is None or pd.isna(value):
                    return None
                return value.item() if hasattr(value, "item") else value

            point_id = str(row.get("point_id", ""))
            latest_by_id[point_id] = {
                "run_id": json_value("run_id"),
                "fetched_utc": json_value("fetched_utc"),
                "provider": json_value("provider"),
                "current_speed_kph": json_value("current_speed_kph"),
                "free_speed_kph": json_value("free_speed_kph"),
                "tti": json_value("tti"),
                "confidence": json_value("confidence"),
                "road_closure": (bool(row["road_closure"])
                                 if pd.notna(row.get("road_closure")) else None),
            }

    junctions = []
    for junction in coverage.get("junctions", []):
        observation = latest_by_id.get(str(junction.get("id", "")))
        if observation:
            junctions.append({**junction, "status": "collected",
                              "latest": observation})
        else:
            clean = {k: v for k, v in junction.items() if k != "latest"}
            junctions.append({**clean, "status": "awaiting_collection"})
    coverage["junctions"] = junctions

    bmc_total = sum(bool(j.get("in_bmc")) for j in junctions)
    bmc_collected = sum(bool(j.get("in_bmc")) and j.get("status") == "collected"
                        for j in junctions)
    mmrda_collected = sum(j.get("status") == "collected" for j in junctions)
    scopes = coverage.setdefault("scopes", {})
    scopes.setdefault("bmc", {}).update({"junction_count": bmc_total,
                                          "collected_count": bmc_collected})
    scopes.setdefault("mmrda", {}).update({"junction_count": len(junctions),
                                            "collected_count": mmrda_collected})
    return coverage


def build_summary(meta: dict, intersections: dict, frames: dict,
                  od: dict, here: dict) -> dict:
    nodes = intersections["nodes"]
    junctions = [n for n in nodes if n["street_count"] >= 3]
    queued = [n for n in nodes if n["queue_total_m"] > 0]
    total_queue_km = sum(n["queue_total_m"] for n in queued) / 1000.0

    delay_pcu_h = meta["delay_pcu_h"]
    a = COST_ASSUMPTIONS
    daily_person_h = delay_pcu_h * a["persons_per_pcu"] * a["peak_equivalent_hours_per_day"]
    annual_inr = daily_person_h * a["value_of_time_inr_per_person_h"] * a["days_per_year"]

    google_min = None
    if od["google"]:
        google_min = od["google"][-1]["duration_min"]

    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model": {
            **{k: meta[k] for k in ("tstt_pcu_h", "freeflow_tstt_pcu_h", "delay_pcu_h",
                                    "corridor_eq_min", "corridor_ff_min", "converged",
                                    "final_gap", "n_links")},
            "n_junctions": len(junctions),
            "n_queued_junctions": len(queued),
            "total_queue_km": round(total_queue_km, 1),
            "longest_queue_m": max((n["queue_total_m"] for n in queued), default=0),
        },
        "measured": {
            "n_runs": frames["n_runs"],
            "n_days": frames["n_days"],
            "n_points": len(frames["points"]),
            "n_bins": len(frames["bins"]),
            "span_ist": frames["span_ist"],
            "worst_bin_tti": max((b["mean_tti"] for b in frames["bins"]), default=None),
            "here_links": len(here["links"]),
            "google_od": len(od["google"]),
            "google_corridor_min": google_min,
        },
        "cost": {
            "assumptions": a,
            "daily_person_h": round(daily_person_h, 0),
            "annual_inr": round(annual_inr, 0),
            "annual_inr_crore": round(annual_inr / 1e7, 1),
            "note": "Planning estimate from the modeled peak hour; not a measurement.",
        },
    }


# --- orchestration -------------------------------------------------------------

PAYLOADS = ("network", "intersections", "frames", "od", "here", "coverage",
            "summary")


def export(rebuild: bool = False, verbose: bool = True) -> dict[str, Path]:
    """Build every payload; returns {name: path}."""
    if rebuild or not INTERSECTIONS_JSON.exists():
        build(rebuild=rebuild, verbose=verbose)
    links, meta = load_or_solve(rebuild=False, verbose=verbose)
    G = load_enriched_graph()

    MAP_DIR.mkdir(parents=True, exist_ok=True)
    payloads = {
        "network": build_network(G, links),
        "intersections": build_intersections(G, links),
        "frames": build_frames(),
        "od": build_od(),
        "here": build_here(),
        "coverage": build_coverage(),
    }
    payloads["summary"] = build_summary(meta, payloads["intersections"],
                                        payloads["frames"], payloads["od"],
                                        payloads["here"])
    written = {}
    for name, data in payloads.items():
        p = MAP_DIR / f"{name}.json"
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        written[name] = p
        if verbose:
            print(f"[map] {name:>13}.json  {p.stat().st_size / 1024:8.0f} kB")
    return written


def export_standalone(out_path: Path | None = None, verbose: bool = True) -> Path:
    """One self-contained HTML file (data + libraries inlined) for offline use."""
    out_path = out_path or (DOCS / "corridor_map.html")
    data = {}
    for name in PAYLOADS:
        p = MAP_DIR / f"{name}.json"
        with p.open(encoding="utf-8") as f:
            data[name] = json.load(f)

    # Inline-script safety: a literal "</script" inside the inlined JS would
    # close the <script> tag early and truncate the bundle (the deck.gl UMD
    # contains one, inside a string). Escape ONLY that sequence — a blanket
    # "</" escape would corrupt regexes like /</g in the app code.
    def _inline(text: str) -> str:
        return re.sub(r"</(script)", r"<\\/\1", text, flags=re.IGNORECASE)

    css = (WEB / "static" / "map.css").read_text(encoding="utf-8")
    js = _inline((WEB / "static" / "map_app.js").read_text(encoding="utf-8"))
    deck = _inline((WEB / "static" / "vendor" / "deck.min.js").read_text(encoding="utf-8"))

    html = f"""<!DOCTYPE html>
<html lang="en" style="color-scheme: dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0d0d0d">
<title>Greater Mumbai — Mumbai Traffic Observatory</title>
<style>{css}</style>
</head><body>
<div id="shell"></div>
<script>{deck}</script>
<script>window.__MAP_DATA__ = {_inline(json.dumps(data, separators=(",", ":")))};</script>
<script>{js}</script>
</body></html>"""
    out_path.write_text(html, encoding="utf-8")
    if verbose:
        print(f"[map] standalone -> {out_path.relative_to(REPO_ROOT)} "
              f"({out_path.stat().st_size / 1024 / 1024:.1f} MB)")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Export corridor-map payloads.")
    ap.add_argument("--rebuild", action="store_true",
                    help="Force a fresh Frank-Wolfe solve before exporting.")
    ap.add_argument("--standalone", action="store_true",
                    help="Also write docs/corridor_map.html (single offline file).")
    args = ap.parse_args()

    export(rebuild=args.rebuild)
    if args.standalone:
        export_standalone()


if __name__ == "__main__":
    main()
