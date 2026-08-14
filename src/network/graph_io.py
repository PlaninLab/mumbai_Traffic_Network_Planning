"""
graph_io.py — load the enriched network with numeric attributes cast correctly.

osmnx.save_graphml serializes every attribute to a string; on reload, numeric
fields come back as text. This loader casts the attributes the demand and
assignment code relies on, so shortest-path weights and BPR math work directly.
"""

from __future__ import annotations

from pathlib import Path

import osmnx as ox

REPO_ROOT = Path(__file__).resolve().parents[2]
ENRICHED = REPO_ROOT / "data" / "processed" / "network_corridor_enriched.graphml"

FLOAT_ATTRS = (
    "length", "free_flow_speed_kph", "free_flow_travel_time_s",
    "capacity_pcu_hr", "capacity_eff_pcu_hr",
)
INT_ATTRS = ("lanes", "n_stopped")


def load_enriched_graph(path: Path = ENRICHED):
    """Load the enriched GraphML and cast numeric edge attributes back to numbers."""
    G = ox.load_graphml(path)
    for _u, _v, _k, d in G.edges(keys=True, data=True):
        for a in FLOAT_ATTRS:
            if a in d and d[a] is not None:
                try:
                    d[a] = float(d[a])
                except (ValueError, TypeError):
                    d[a] = 0.0
        for a in INT_ATTRS:
            if a in d and d[a] is not None:
                try:
                    d[a] = int(float(d[a]))
                except (ValueError, TypeError):
                    d[a] = 0
    return G
