"""
enrich_attributes.py — Phase 1: attribute the corridor road graph for assignment.

Takes the raw OSM corridor graph (from build_network.py), trims it to the major
road classes that carry corridor traffic, and computes the attributes the traffic
assignment needs:

  - lanes                     (OSM `lanes` tag, imputed by road class where missing)
  - free_flow_speed_kph       (OSM `maxspeed`, imputed by road class per IRC)
  - free_flow_travel_time_s   (length / free-flow speed)
  - capacity_pcu_hr           (lanes x per-lane capacity x encroachment factor)
  - n_stopped                 (stopped-vehicle count, default 0 — see incident.py)
  - capacity_eff_pcu_hr       (capacity_pcu_hr x mu_incident; == capacity when N=0)

Capacity is expressed in PCU/hour (Passenger Car Units) — the demand model converts
vehicle trips to PCU to stay consistent (project plan §3.1).

Encroachment factor accounts for Mumbai's effective-width loss from on-street
parking, hawkers, bus stops, and debris (plan §3.1): effective capacity is applied
as a multiplier rather than by trying to measure "true" width.

Usage:
    python -m src.network.enrich_attributes
    python -m src.network.enrich_attributes --keep-secondary
"""

from __future__ import annotations

import argparse
from pathlib import Path

import networkx as nx
import osmnx as ox

from src.network import incident

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_GRAPHML = REPO_ROOT / "data" / "raw" / "osm" / "corridor.graphml"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

# Road classes retained for the assignment network.
MAJOR_CLASSES = {
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
}

# --- IRC-informed defaults, keyed by base road class (link variants inherit) ---
# free-flow speed (km/h), lanes per direction if untagged, per-lane capacity (PCU/h).
ROAD_DEFAULTS = {
    "motorway":  {"speed_kph": 80, "lanes": 3, "cap_pcu_lane": 2000},
    "trunk":     {"speed_kph": 60, "lanes": 3, "cap_pcu_lane": 1900},
    "primary":   {"speed_kph": 55, "lanes": 2, "cap_pcu_lane": 1800},
    "secondary": {"speed_kph": 45, "lanes": 2, "cap_pcu_lane": 1500},
    "tertiary":  {"speed_kph": 35, "lanes": 1, "cap_pcu_lane": 1200},
}
DEFAULT_CLASS = "secondary"

# Mumbai effective-width / encroachment capacity multiplier (plan §3.1).
ENCROACHMENT_FACTOR = 0.85


def _base_class(highway) -> str:
    """Normalize an OSM highway tag (str or list, possibly a _link) to a base class."""
    if isinstance(highway, list):
        highway = highway[0]
    return highway.replace("_link", "") if isinstance(highway, str) else DEFAULT_CLASS


def _first_num(value, default: float) -> float:
    """Parse an OSM numeric tag that may be a list or a string like '60 mph'."""
    if value is None:
        return default
    if isinstance(value, list):
        value = value[0]
    try:
        return float(str(value).split()[0])
    except (ValueError, IndexError):
        return default


def filter_major(graph, keep_secondary: bool = True):
    """Return the subgraph of major road classes (largest connected component)."""
    classes = set(MAJOR_CLASSES)
    if not keep_secondary:
        classes -= {"secondary", "secondary_link"}

    keep_edges = []
    for u, v, k, d in graph.edges(keys=True, data=True):
        hwy = d.get("highway")
        base = _base_class(hwy)
        variant = hwy[0] if isinstance(hwy, list) else hwy
        if variant in classes or base in {c.replace("_link", "") for c in classes}:
            keep_edges.append((u, v, k))

    sub = graph.edge_subgraph(keep_edges).copy()
    # Keep the largest weakly-connected component (drop stray fragments).
    largest = max(nx.weakly_connected_components(sub), key=len)
    return sub.subgraph(largest).copy()


def enrich(graph) -> None:
    """Populate assignment attributes on every edge, in place."""
    for _u, _v, _k, d in graph.edges(keys=True, data=True):
        base = _base_class(d.get("highway"))
        defaults = ROAD_DEFAULTS.get(base, ROAD_DEFAULTS[DEFAULT_CLASS])

        # --- lanes: OSM tag, else class default. Track provenance. ---
        osm_lanes = d.get("lanes")
        if osm_lanes is not None:
            lanes = int(_first_num(osm_lanes, defaults["lanes"]))
            lanes = max(1, lanes)
            d["lanes_source"] = "osm"
        else:
            lanes = defaults["lanes"]
            d["lanes_source"] = "imputed"
        d["lanes"] = lanes

        # --- free-flow speed: OSM maxspeed, else class default. ---
        speed = _first_num(d.get("maxspeed"), defaults["speed_kph"])
        d["free_flow_speed_kph"] = speed
        d["speed_source"] = "osm" if d.get("maxspeed") is not None else "imputed"

        # --- free-flow travel time (seconds): length[m] / speed[m/s]. ---
        length_m = float(d.get("length", 0.0) or 0.0)
        d["free_flow_travel_time_s"] = length_m / (speed / 3.6) if speed else 0.0

        # --- capacity (PCU/h): lanes x per-lane x encroachment. ---
        cap = lanes * defaults["cap_pcu_lane"] * ENCROACHMENT_FACTOR
        d["capacity_pcu_hr"] = round(cap, 1)

        # --- stopped-vehicle incident hook (default: none). ---
        d["n_stopped"] = 0
        mu = incident.incident_capacity_factor(lanes, d["n_stopped"])
        d["capacity_eff_pcu_hr"] = round(cap * mu, 1)


def summarize(graph) -> None:
    import collections

    cls = collections.Counter()
    lane_src = collections.Counter()
    total_km = 0.0
    for _u, _v, d in graph.edges(data=True):
        cls[_base_class(d.get("highway"))] += 1
        lane_src[d.get("lanes_source", "?")] += 1
        total_km += float(d.get("length", 0) or 0) / 1000
    print("\n[enrich] --- Enriched network summary ---")
    print(f"  Nodes: {graph.number_of_nodes()}   Edges: {graph.number_of_edges()}")
    print(f"  Total length: {total_km:.1f} km")
    print(f"  Road-class mix: {dict(cls)}")
    print(f"  Lane data source: {dict(lane_src)} "
          f"({100*lane_src['osm']/max(1,sum(lane_src.values())):.0f}% from OSM)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich the corridor network for assignment.")
    parser.add_argument("--keep-secondary", action="store_true", default=True,
                        help="Keep secondary roads (default True).")
    parser.add_argument("--drop-secondary", dest="keep_secondary", action="store_false",
                        help="Drop secondary roads for a leaner network.")
    parser.add_argument("--tag", default="corridor", help="Output filename tag.")
    args = parser.parse_args()

    print(f"[enrich] Loading raw graph: {RAW_GRAPHML}")
    G = ox.load_graphml(RAW_GRAPHML)
    print(f"[enrich] Raw: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    Gm = filter_major(G, keep_secondary=args.keep_secondary)
    print(f"[enrich] Major subgraph: {Gm.number_of_nodes()} nodes, {Gm.number_of_edges()} edges")

    enrich(Gm)

    # Phase 0: override capacity from measured road widths where available
    # (no-op until data/measurements/road_widths.csv has rows). See road_width.py.
    try:
        from src.network import road_width
        applied = road_width.apply_if_available(Gm)
        if applied.get("applied"):
            print(f"[enrich] Measured widths applied to {applied['applied']} link(s) "
                  f"-> capacity from effective width, not guessed lanes")
    except Exception as e:  # noqa: BLE001 — measured-width override must never break enrich
        print(f"[enrich] measured-width override skipped: {e}")

    summarize(Gm)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    graphml_out = PROCESSED_DIR / f"network_{args.tag}_enriched.graphml"
    gpkg_out = PROCESSED_DIR / f"network_{args.tag}_enriched.gpkg"
    ox.save_graphml(Gm, graphml_out)
    ox.save_graph_geopackage(Gm, filepath=gpkg_out, directed=True)
    print(f"\n[enrich] Saved -> {graphml_out}")
    print(f"[enrich] Saved -> {gpkg_out}")


if __name__ == "__main__":
    main()
