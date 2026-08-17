"""
build_area.py — download a MAJOR-ROAD network for a wider area (e.g. BMC/MMRDA).

The corridor builder (build_network.py) pulls the full 'drive' network for a small
box. To scale to Greater Mumbai we only want the assignment-relevant major roads
(motorway/trunk/primary/secondary + links), which is a fraction of the data and
keeps the Overpass download tractable. osmnx builds correct routable topology
(nodes split at intersections), which coverage.json geometry lacks.

Bounds default to the BMC (Greater Mumbai) extent from the junction inventory.

Usage:
    python -m src.network.build_area --scope bmc
    python -m src.network.build_area --scope mmrda --tag mmrda
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import osmnx as ox

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_OSM_DIR = REPO_ROOT / "data" / "raw" / "osm"
COVERAGE_JSON = REPO_ROOT / "data" / "processed" / "map" / "coverage.json"

# Major-road classes only (server-side filter — keeps the download small).
MAJOR_FILTER = ('["highway"~"motorway|trunk|primary|secondary|'
                'motorway_link|trunk_link|primary_link|secondary_link"]')

# Fallback bounds if coverage.json is unavailable (BMC extent).
BMC_BOUNDS = {"west": 72.773229, "south": 18.89216, "east": 72.981748, "north": 19.269477}


def _bounds(scope: str) -> dict:
    if COVERAGE_JSON.exists():
        cov = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
        b = cov.get("scopes", {}).get(scope, {}).get("bounds")
        if b:
            return b
    return BMC_BOUNDS


def build_area_graph(scope: str = "bmc", timeout: int = 600):
    bounds = _bounds(scope)
    print(f"[build_area] Major-road network for scope={scope}: {bounds}")
    ox.settings.timeout = timeout      # Overpass here is slow; give it room.
    ox.settings.overpass_rate_limit = True

    bbox_tuple = (bounds["west"], bounds["south"], bounds["east"], bounds["north"])
    graph = ox.graph_from_bbox(
        bbox_tuple,
        custom_filter=MAJOR_FILTER,
        simplify=True,
        retain_all=False,          # keep the largest connected component
        truncate_by_edge=True,
    )
    print(f"[build_area] Raw: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges")
    graph = ox.add_edge_speeds(graph)
    graph = ox.add_edge_travel_times(graph)
    return graph


def main() -> None:
    ap = argparse.ArgumentParser(description="Download a major-road network for a wider area.")
    ap.add_argument("--scope", default="bmc", choices=["bmc", "mmrda"])
    ap.add_argument("--tag", default=None, help="Output filename tag (default = scope).")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    tag = args.tag or args.scope
    G = build_area_graph(scope=args.scope, timeout=args.timeout)

    import collections
    cls = collections.Counter()
    km = 0.0
    for _u, _v, d in G.edges(data=True):
        h = d.get("highway"); h = h[0] if isinstance(h, list) else h
        cls[h] += 1
        km += float(d.get("length", 0) or 0) / 1000
    print(f"[build_area] {tag}: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
          f"{km:.0f} km")
    print(f"[build_area] class mix: {dict(cls)}")

    RAW_OSM_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_OSM_DIR / f"{tag}.graphml"
    ox.save_graphml(G, out)
    print(f"[build_area] Saved -> {out}")


if __name__ == "__main__":
    main()
