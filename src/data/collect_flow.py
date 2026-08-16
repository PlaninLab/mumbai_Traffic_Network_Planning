"""
collect_flow.py — Sample TomTom Flow Segment data along the WEH corridor.

Samples ~N points evenly along the Western Express Highway spine (Dahisar -> Bandra)
extracted from the OSM graph, queries TomTom Flow Segment Data for each (cached),
and writes a per-run summary CSV.

Each row: point lat/lon, currentSpeed, freeFlowSpeed, TTI (=freeFlow/current speed),
confidence, and a UTC + local timestamp. Because every raw response is cached by
tomtom_client, re-running only pays for genuinely new (point, time) combinations.

Run this at different times of day to capture the congestion cycle:
    python -m src.data.collect_flow --n 50 --label evening
    python -m src.data.collect_flow --n 50 --label am_peak   # 8-10 AM
    python -m src.data.collect_flow --n 50 --label pm_peak   # 6-8 PM

Outputs: data/raw/tomtom/collected/flow_<label>_<timestamp>.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import osmnx as ox
from shapely.geometry import LineString

from src.data import tomtom_client as tt
from src.data import segments as seg
from src.data import store

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPHML = REPO_ROOT / "data" / "raw" / "osm" / "corridor.graphml"
OUT_DIR = REPO_ROOT / "data" / "raw" / "tomtom" / "collected"

# Corridor endpoints (lat, lon).
DAHISAR = (19.250, 72.856)   # northern end
BANDRA = (19.055, 72.840)    # southern end


def weh_spine_points(n_points: int) -> list[tuple[float, float]]:
    """Return n_points (lat, lon) evenly spaced along the WEH spine in the graph.

    The fastest path Dahisar->Bandra follows the WEH (it is the fastest road),
    so weighting by travel_time keeps sample points on the highway itself.
    """
    G = ox.load_graphml(GRAPHML)
    orig = ox.nearest_nodes(G, X=DAHISAR[1], Y=DAHISAR[0])
    dest = ox.nearest_nodes(G, X=BANDRA[1], Y=BANDRA[0])
    route = nx.shortest_path(G, orig, dest, weight="travel_time")

    # Build a single LineString from the route edge geometries (fallback: node coords).
    coords: list[tuple[float, float]] = []  # (lon, lat)
    for u, v in zip(route[:-1], route[1:]):
        data = min(G.get_edge_data(u, v).values(), key=lambda d: d.get("length", 1e9))
        geom = data.get("geometry")
        if geom is not None:
            pts = list(geom.coords)
        else:
            pts = [(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])]
        if coords and pts and coords[-1] == pts[0]:
            pts = pts[1:]
        coords.extend(pts)

    line = LineString(coords)
    # Sample evenly by normalized distance along the line.
    samples = []
    for i in range(n_points):
        frac = i / (n_points - 1) if n_points > 1 else 0.0
        pt = line.interpolate(frac, normalized=True)
        samples.append((pt.y, pt.x))  # (lat, lon)
    return samples


def collect(n_points: int, label: str, segment: str | None = None) -> Path:
    points = weh_spine_points(n_points)
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.astimezone().strftime("%Y%m%d_%H%M")
    # If not explicitly tagged, record the segment this reading actually falls into.
    segment = segment or seg.classify(now_utc)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"flow_{label}_{ts}.csv"

    rows = []
    print(f"[collect_flow] Sampling {n_points} points along WEH ({label}) ...")
    for i, (lat, lon) in enumerate(points):
        point = f"{lat:.5f},{lon:.5f}"
        try:
            d = tt.flow_segment(point)
        except Exception as e:  # noqa: BLE001 — log and continue the sweep
            print(f"  [{i:>2}] {point}  ERROR: {e}")
            continue
        cur, free = d.get("currentSpeed"), d.get("freeFlowSpeed")
        tti = (free / cur) if cur else None  # travel-time index: >1 means slower than free-flow
        rows.append({
            "idx": i,
            "lat": lat,
            "lon": lon,
            "currentSpeed_kph": cur,
            "freeFlowSpeed_kph": free,
            "tti": round(tti, 3) if tti else None,
            "confidence": d.get("confidence"),
            "roadClosure": d.get("roadClosure"),
            "label": label,
            "segment": segment,
            "fetched_utc": now_utc.isoformat(),
        })
        flag = "" if not tti or tti < 1.2 else ("  <-- congested" if tti < 2 else "  <-- SEVERE")
        print(f"  [{i:>2}] {point}  cur={cur} free={free} TTI={tti and round(tti,2)}{flag}")

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Also persist to the tabular SQLite store (run_id matches the CSV stem).
    run_id = f"{label}_{ts}"
    inserted = store.insert_readings(rows, run_id)

    # Quick corridor summary.
    ttis = [r["tti"] for r in rows if r["tti"]]
    if ttis:
        print(f"\n[collect_flow] {len(rows)} points collected -> {out_path}")
        print(f"  Stored {inserted} rows in {store.DB_PATH.name} (run_id={run_id})")
        print(f"  Mean TTI: {sum(ttis)/len(ttis):.2f}   Max TTI: {max(ttis):.2f}")
        congested = [r for r in rows if r["tti"] and r["tti"] >= 1.5]
        print(f"  Congested points (TTI>=1.5): {len(congested)}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect TomTom Flow Segment data along WEH.")
    parser.add_argument("--n", type=int, default=50, help="Number of sample points (default 50).")
    parser.add_argument("--label", default="run", help="Session label (e.g. am_peak, pm_peak, evening).")
    parser.add_argument("--segment", choices=["peak", "avg"], default=None,
                        help="Tag this reading as a weekday planning segment. Guards against "
                             "collecting outside the segment's weekday window (override with --force).")
    parser.add_argument("--force", action="store_true",
                        help="Collect even if the current time is outside the --segment window.")
    args = parser.parse_args()

    label = args.label
    if args.segment:
        label = args.label if args.label != "run" else args.segment
        now = seg.ist_now()
        actual = seg.classify(now)
        if actual != args.segment and not args.force:
            print(f"[collect_flow] Refusing to tag '{args.segment}': it is {now:%a %H:%M} IST, "
                  f"which is the '{actual}' window, not '{args.segment}'.")
            print(f"  Expected window: {seg.SEGMENTS[args.segment]['windows_ist']}")
            print("  Re-run inside the window, or pass --force to record anyway.")
            sys.exit(1)
        if actual != args.segment:
            print(f"[collect_flow] --force: tagging as '{args.segment}' despite "
                  f"being in the '{actual}' window.")

    collect(args.n, label, segment=args.segment)


if __name__ == "__main__":
    main()
