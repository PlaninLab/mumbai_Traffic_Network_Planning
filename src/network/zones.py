"""
zones.py — Phase 1/2: define Traffic Analysis Zones (TAZs) for the corridor.

The WEH corridor is roughly linear north->south, so the baseline TAZs are
latitude bands spanning the corridor, one per major suburb from Dahisar (north)
to Bandra (south). Each zone gets a polygon, a centroid, and the nearest network
node (the connector where zone demand loads onto the network).

**Placeholder note:** these are locality-band zones, NOT census ward boundaries.
Task 0.8 (ward-level census population) will replace these bands with true ward
polygons. The band scheme is a defensible first cut for a linear corridor and lets
the demand model (Phase 2) proceed now. Documented in docs/assumptions.md.

Usage:
    python -m src.network.zones
Outputs: data/processed/zones.gpkg  (+ prints the zone table)
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
from shapely.geometry import box

from src.network.graph_io import load_enriched_graph

REPO_ROOT = Path(__file__).resolve().parents[2]
ENRICHED_GRAPHML = REPO_ROOT / "data" / "processed" / "network_corridor_enriched.graphml"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

# Zone demand loads onto the arterial network, not small side streets: connectors
# snap to the nearest node touching one of these classes (the WEH spine + main arterials).
ARTERIAL_CLASSES = {"motorway", "trunk", "primary"}

# Corridor bbox longitudes (from build_network.CORRIDOR_BBOX).
WEST, EAST = 72.820, 72.885

# Suburbs along the WEH, north -> south, with the southern latitude boundary of
# each band. The northern edge of the corridor is 19.270 (Dahisar check naka).
# (name, south_lat)  — each zone spans [south_lat, previous north].
CORRIDOR_NORTH = 19.270
ZONE_BANDS = [
    ("Dahisar",    19.240),
    ("Borivali",   19.220),
    ("Kandivali",  19.200),
    ("Malad",      19.180),
    ("Goregaon",   19.155),
    ("Jogeshwari", 19.135),
    ("Andheri",    19.110),
    ("Vile Parle", 19.095),
    ("Santacruz",  19.080),
    ("Khar",       19.065),
    ("Bandra",     19.045),
]


def _arterial_nodes(G, eligible_nodes=None):
    """Node IDs and coordinates for eligible nodes touching an arterial edge."""
    eligible = set(G.nodes) if eligible_nodes is None else set(eligible_nodes)
    art = set()
    for u, v, d in G.edges(data=True):
        hwy = d.get("highway")
        hwy = hwy[0] if isinstance(hwy, list) else hwy
        base = hwy.replace("_link", "") if isinstance(hwy, str) else ""
        if base in ARTERIAL_CLASSES:
            if u in eligible:
                art.add(u)
            if v in eligible:
                art.add(v)
    if not art:
        raise ValueError("No eligible arterial nodes found for TAZ connectors")
    ids = list(art)
    xs = np.array([G.nodes[n]["x"] for n in ids])
    ys = np.array([G.nodes[n]["y"] for n in ids])
    return ids, xs, ys


def _nearest_arterial(centroid, ids, xs, ys):
    """Nearest arterial node to a centroid (planar approx — fine at corridor scale)."""
    d2 = (xs - centroid.x) ** 2 + (ys - centroid.y) ** 2
    return ids[int(np.argmin(d2))]


def build_zones(G=None) -> gpd.GeoDataFrame:
    """Construct the TAZ GeoDataFrame with polygons, centroids, and connector nodes."""
    if G is None:
        G = load_enriched_graph()

    # Every OD connector must be mutually reachable on the directed network.
    # Restricting candidates to the largest SCC prevents a one-way ramp or sink
    # node from silently dropping demand during assignment.
    main_scc = max(nx.strongly_connected_components(G), key=len)
    art_ids, art_xs, art_ys = _arterial_nodes(G, eligible_nodes=main_scc)

    records = []
    north = CORRIDOR_NORTH
    for i, (name, south) in enumerate(ZONE_BANDS):
        poly = box(WEST, south, EAST, north)  # (minx, miny, maxx, maxy)
        centroid = poly.centroid
        connector_node = _nearest_arterial(centroid, art_ids, art_xs, art_ys)
        records.append({
            "zone_id": i,
            "name": name,
            "south_lat": south,
            "north_lat": north,
            "centroid_lon": round(centroid.x, 5),
            "centroid_lat": round(centroid.y, 5),
            "connector_node": connector_node,
            "geometry": poly,
        })
        north = south  # next band starts where this one ended

    gdf = gpd.GeoDataFrame(records, crs="EPSG:4326")
    return gdf


def main() -> None:
    gdf = build_zones()
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "zones.gpkg"
    gdf.to_file(out, driver="GPKG")

    print(f"[zones] {len(gdf)} TAZs defined along the WEH corridor (Dahisar -> Bandra):\n")
    print(f"{'id':>2}  {'name':<11} {'lat band':<20} {'connector_node':>14}")
    for _, r in gdf.iterrows():
        band = f"{r['south_lat']:.3f}-{r['north_lat']:.3f}"
        print(f"{r['zone_id']:>2}  {r['name']:<11} {band:<20} {r['connector_node']:>14}")
    print(f"\n[zones] Saved -> {out}")
    print("[zones] NOTE: locality-band placeholder zones — replace with census wards (task 0.8).")


if __name__ == "__main__":
    main()
