"""
od_costs.py — build the gravity-model cost matrix from REAL traffic travel times.

The baseline gravity model uses free-flow network shortest-path times as the
zone-to-zone cost matrix (gravity_model.cost_matrix). That is synthetic. This
module replaces it with live traffic-aware travel times between the zone
connector nodes, sourced from a real provider:

    google  -> Google Routes Route Matrix   (best India urban travel-time model)
    tomtom  -> TomTom Matrix Routing         (already-integrated fallback)

Both return an n_zones × n_zones matrix in MINUTES, with the intrazonal (diagonal)
cost filled the same way as the network baseline (half the nearest interzonal).

These calls hit a paid/quota'd API once per matrix (n² elements) and are cached by
the underlying client, so re-running is free.
"""

from __future__ import annotations

import numpy as np

from src.network.graph_io import load_enriched_graph


def zone_points(zones, G) -> list[str]:
    """'lat,lon' string for each zone's connector node (from the graph)."""
    pts = []
    for node in zones["connector_node"].astype(np.int64):
        nd = G.nodes[int(node)]
        pts.append(f"{nd['y']:.6f},{nd['x']:.6f}")   # y=lat, x=lon
    return pts


def _fill_intrazonal(C: np.ndarray) -> np.ndarray:
    n = C.shape[0]
    for i in range(n):
        off = [C[i, j] for j in range(n) if j != i and np.isfinite(C[i, j]) and C[i, j] < 1e5]
        C[i, i] = 0.5 * min(off) if off else 1.0
    return C


def cost_matrix_from_provider(zones, G=None, source: str = "google",
                              departure_time: str | None = None) -> np.ndarray:
    """Traffic-aware TAZ×TAZ travel-time matrix (minutes) from a real provider."""
    if G is None:
        G = load_enriched_graph()
    pts = zone_points(zones, G)

    if source == "google":
        from src.data import google_client
        M = google_client.matrix_minutes(pts, pts, departure_time=departure_time)
        C = np.array(M, dtype=float)
    elif source == "tomtom":
        from src.data import tomtom_client as tt
        body = tt.matrix(pts, pts)
        C = _parse_tomtom_matrix(body, len(pts))
    else:
        raise ValueError(f"Unknown cost source '{source}' (use 'google' or 'tomtom').")

    return _fill_intrazonal(C)


def _parse_tomtom_matrix(body: dict, n: int) -> np.ndarray:
    """TomTom Matrix Routing v2 -> minutes matrix. Response shape:
    {'data': [{'originIndex', 'destinationIndex', 'routeSummary': {'travelTimeInSeconds'}}]}."""
    C = np.full((n, n), np.inf)
    for e in body.get("data", []):
        i, j = e.get("originIndex"), e.get("destinationIndex")
        summ = e.get("routeSummary") or {}
        secs = summ.get("travelTimeInSeconds")
        if i is not None and j is not None and secs is not None:
            C[i, j] = secs / 60.0
    return C
