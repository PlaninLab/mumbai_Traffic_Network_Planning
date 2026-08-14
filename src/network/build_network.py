"""
build_network.py — Extract the pilot-corridor road network from OpenStreetMap.

Phase 0 / Phase 1 (Layer 1: Network Model).

Pilot corridor: Western Express Highway, Dahisar -> Bandra (~25 km), with a ~2 km
buffer, restricted to the "drive" network. Downloads via osmnx (Overpass API),
saves a GraphML file (full topology + attributes) and a GeoPackage (for GIS /
geopandas inspection).

Usage:
    python src/network/build_network.py
    python src/network/build_network.py --place "Mumbai, India"   # wider extract

Design notes (see project plan sections 3.1, 5-Phase 1):
- We use lane COUNT, never road width. OSM tags `lanes=*` directly.
- Capacity/speed enrichment happens in a later step (enrich_attributes.py);
  this module is only responsible for producing a clean drivable graph.
- Every run is deterministic given the same bounding box + OSM snapshot.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import osmnx as ox

# --- Repo paths (this file lives at src/network/build_network.py) ---
REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_OSM_DIR = REPO_ROOT / "data" / "raw" / "osm"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

# --- Pilot corridor bounding box: WEH Dahisar -> Bandra, ~2 km buffer ---
# (lat/lon). Dahisar is the northern end (~19.25 N), Bandra the southern (~19.05 N).
# Longitude band ~72.82-72.87 E covers the WEH alignment plus arterial connections.
CORRIDOR_BBOX = {
    "north": 19.270,   # just north of Dahisar check naka
    "south": 19.045,   # around Bandra / WEH southern approach
    "east": 72.885,    # eastern buffer (towards Aarey / Western suburbs interior)
    "west": 72.820,    # western buffer (towards the coast side of WEH)
}

# osmnx "drive" keeps motorway/trunk/primary/secondary/tertiary + residential,
# dropping footpaths and service roads — matches the Phase 1 cleaning intent.
NETWORK_TYPE = "drive"


def build_corridor_graph(bbox: dict = CORRIDOR_BBOX, network_type: str = NETWORK_TYPE):
    """Download and lightly clean the corridor drive network as a NetworkX graph."""
    print(f"[build_network] Downloading OSM '{network_type}' network for bbox:")
    for k, v in bbox.items():
        print(f"    {k:>5} = {v}")

    # osmnx >=2.0 takes bbox as a (west, south, east, north) tuple.
    bbox_tuple = (bbox["west"], bbox["south"], bbox["east"], bbox["north"])
    graph = ox.graph_from_bbox(
        bbox_tuple,
        network_type=network_type,
        simplify=True,      # merge interstitial nodes (Phase 1: simplify topology)
        retain_all=False,   # drop disconnected fragments
        truncate_by_edge=True,
    )

    n_nodes = graph.number_of_nodes()
    n_edges = graph.number_of_edges()
    print(f"[build_network] Raw graph: {n_nodes} nodes, {n_edges} edges")

    # Add edge speeds + travel times from OSM maxspeed / imputed by road class.
    # This gives a first-cut free-flow travel time; enrich_attributes.py refines it.
    graph = ox.add_edge_speeds(graph)          # -> 'speed_kph'
    graph = ox.add_edge_travel_times(graph)    # -> 'travel_time' (seconds)

    return graph


def save_graph(graph, tag: str = "corridor") -> None:
    """Persist the graph as GraphML (topology) and GeoPackage (GIS layers)."""
    RAW_OSM_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    graphml_path = RAW_OSM_DIR / f"{tag}.graphml"
    gpkg_path = PROCESSED_DIR / f"network_{tag}.gpkg"

    ox.save_graphml(graph, graphml_path)
    print(f"[build_network] Saved GraphML -> {graphml_path}")

    # GeoPackage with separate nodes + edges layers, for geopandas/QGIS inspection.
    ox.save_graph_geopackage(graph, filepath=gpkg_path, directed=True)
    print(f"[build_network] Saved GeoPackage -> {gpkg_path}")


def summarize(graph) -> None:
    """Print a quick sanity summary (road-class mix, total length)."""
    import collections

    highway_counts: dict = collections.Counter()
    total_length_m = 0.0
    for _u, _v, data in graph.edges(data=True):
        hwy = data.get("highway", "unknown")
        if isinstance(hwy, list):
            hwy = hwy[0]
        highway_counts[hwy] += 1
        total_length_m += float(data.get("length", 0.0) or 0.0)

    print("\n[build_network] --- Summary ---")
    print(f"  Nodes: {graph.number_of_nodes()}")
    print(f"  Edges: {graph.number_of_edges()}")
    print(f"  Total edge length: {total_length_m / 1000:.1f} km")
    print("  Road-class mix (by edge count):")
    for hwy, count in highway_counts.most_common():
        print(f"    {hwy:>15}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the pilot-corridor road network from OSM.")
    parser.add_argument(
        "--place",
        default=None,
        help="Optional OSM place name (e.g. 'Mumbai, India'). Overrides the corridor bbox.",
    )
    parser.add_argument("--tag", default="corridor", help="Output filename tag.")
    args = parser.parse_args()

    if args.place:
        print(f"[build_network] Downloading by place name: {args.place!r}")
        graph = ox.graph_from_place(args.place, network_type=NETWORK_TYPE, simplify=True)
        graph = ox.add_edge_speeds(graph)
        graph = ox.add_edge_travel_times(graph)
    else:
        graph = build_corridor_graph()

    summarize(graph)
    save_graph(graph, tag=args.tag)
    print("\n[build_network] Done.")


if __name__ == "__main__":
    main()
