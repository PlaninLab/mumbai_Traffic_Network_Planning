"""
define_scenario.py — Phase 4: network-modification primitives for interventions.

Each function returns a *copy* of the graph with one intervention applied, so the
base network is never mutated. Interventions map to the plan's scenario types:

  - widen_link      : Scenario A — add lanes to a link (raises capacity)
  - add_link        : Scenario B — insert a new connector edge
  - remove_link     : Scenario C — close a link (Braess / closure test)
  - set_incident    : Scenario D — place N stopped vehicles on a link (§1.5),
                      reducing its effective capacity via the incident model

All capacity edits keep capacity_pcu_hr (nominal) and capacity_eff_pcu_hr
(incident-adjusted) consistent so the assignment always reads the effective value.
"""

from __future__ import annotations

import networkx as nx

from src.network import incident
from src.network.enrich_attributes import ENCROACHMENT_FACTOR, ROAD_DEFAULTS, _base_class


def _recompute_capacity(d: dict) -> None:
    """Recompute nominal + effective capacity for an edge from its lanes/road class."""
    base = _base_class(d.get("highway"))
    cap_lane = ROAD_DEFAULTS.get(base, ROAD_DEFAULTS["secondary"])["cap_pcu_lane"]
    lanes = int(d.get("lanes", 1))
    nominal = lanes * cap_lane * ENCROACHMENT_FACTOR
    d["capacity_pcu_hr"] = round(nominal, 1)
    mu = incident.incident_capacity_factor(lanes, int(d.get("n_stopped", 0)))
    d["capacity_eff_pcu_hr"] = round(nominal * mu, 1)


def widen_link(G, u, v, k=0, add_lanes: int = 1):
    """Scenario A: add lanes to a link (both the edge and its reverse if present)."""
    H = G.copy()
    for a, b in [(u, v), (v, u)]:
        if H.has_edge(a, b):
            for key in list(H.get_edge_data(a, b)):
                d = H[a][b][key]
                d["lanes"] = int(d.get("lanes", 1)) + add_lanes
                _recompute_capacity(d)
    return H


def add_link(G, u, v, lanes: int = 2, highway: str = "primary",
             speed_kph: float = 55.0, length_m: float | None = None):
    """Scenario B: add a new bidirectional link between existing nodes u and v."""
    H = G.copy()
    if length_m is None:
        # Straight-line distance from node coords (metres, planar approx).
        import math
        x1, y1 = H.nodes[u]["x"], H.nodes[u]["y"]
        x2, y2 = H.nodes[v]["x"], H.nodes[v]["y"]
        length_m = math.hypot((x2 - x1) * 111000 * math.cos(math.radians((y1 + y2) / 2)),
                              (y2 - y1) * 111000)
    for a, b in [(u, v), (v, u)]:
        d = {
            "highway": highway, "lanes": lanes, "length": length_m,
            "free_flow_speed_kph": speed_kph,
            "free_flow_travel_time_s": length_m / (speed_kph / 3.6),
            "n_stopped": 0, "name": "NEW_LINK",
        }
        _recompute_capacity(d)
        H.add_edge(a, b, **d)
    return H


def remove_link(G, u, v):
    """Scenario C: remove a link (both directions) — closure / Braess test."""
    H = G.copy()
    for a, b in [(u, v), (v, u)]:
        if H.has_edge(a, b):
            for key in list(H.get_edge_data(a, b)):
                H.remove_edge(a, b, key)
    return H


def set_incident(G, u, v, n_stopped: int, k=None):
    """Scenario D: place N stopped vehicles on a link (§1.5), reducing its capacity.

    Applies to both directions if the reverse edge exists (an incident blocks one
    carriageway; pass a directed edge and set both=False to restrict — kept simple here).
    """
    H = G.copy()
    edges = []
    if k is not None and H.has_edge(u, v, k):
        edges = [(u, v, k)]
    else:
        for a, b in [(u, v)]:
            if H.has_edge(a, b):
                edges += [(a, b, key) for key in H.get_edge_data(a, b)]
    for a, b, key in edges:
        d = H[a][b][key]
        d["n_stopped"] = int(n_stopped)
        _recompute_capacity(d)
    return H
