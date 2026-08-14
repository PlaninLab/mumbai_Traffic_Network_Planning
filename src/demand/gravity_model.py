"""
gravity_model.py — Phase 2: doubly-constrained gravity trip distribution.

    T_ij = A_i^p * B_j^a * P_i * A_j * f(c_ij),   f(c) = c^(-beta)

with balancing factors A_i^p, B_j^a iterated (Furness / IPF) so that
    sum_j T_ij = P_i   and   sum_i T_ij = A_j.

Cost c_ij is the free-flow travel time (minutes) between TAZ connector nodes,
computed by shortest path on the enriched network (plan §Phase 2 allows network
shortest-path OR TomTom Matrix Routing; we use the network to stay free/offline).

Outputs a person-trip OD matrix, then converts to a vehicle-PCU OD matrix via
mode split and PCU factor, ready for assignment.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

from src.demand.generation import production_attraction
from src.network.graph_io import load_enriched_graph
from src.network.zones import build_zones

REPO_ROOT = Path(__file__).resolve().parents[2]
ENRICHED = REPO_ROOT / "data" / "processed" / "network_corridor_enriched.graphml"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

# Mode split: share of person-trips made by private road vehicle (plan §Phase 2, ~30% Mumbai).
PRIVATE_VEHICLE_SHARE = 0.30
# Average vehicle occupancy (persons per vehicle) and PCU factor (mixed traffic).
AVG_OCCUPANCY = 1.4
AVG_PCU = 1.0   # person-vehicles already ~ car-equivalent at baseline; refine with composition later

DEFAULT_BETA = 2.0

# Target total peak-hour interzonal loading (PCU/h). The synthetic generation gives
# only a relative demand shape; we scale the final matrix to this total so the busiest
# corridor links reach realistic congestion (V/C ~ 0.8-1.2). WEH peak carries ~10-12k
# veh/h/direction; a corridor total of ~45k PCU/h across all OD pairs loads it plausibly.
# This is an explicit calibration knob (docs/calibration_log.md).
TARGET_TOTAL_PCU = 18000.0


def cost_matrix(zones: pd.DataFrame, G=None) -> np.ndarray:
    """TAZ-to-TAZ free-flow travel-time matrix (minutes) via network shortest path."""
    if G is None:
        G = load_enriched_graph()
    nodes = zones["connector_node"].astype(np.int64).tolist()
    n = len(nodes)
    C = np.zeros((n, n))
    for i, o in enumerate(nodes):
        lengths = nx.single_source_dijkstra_path_length(G, o, weight="free_flow_travel_time_s")
        for j, d in enumerate(nodes):
            if i == j:
                C[i, j] = 0.0
            else:
                # Fallback large cost if unreachable.
                C[i, j] = lengths.get(d, 1e6) / 60.0  # seconds -> minutes
    # Intrazonal cost: half the nearest interzonal (avoids div-by-zero, keeps some intra demand).
    for i in range(n):
        offdiag = [C[i, j] for j in range(n) if j != i and C[i, j] < 1e5]
        C[i, i] = 0.5 * min(offdiag) if offdiag else 1.0
    return C


def furness(P: np.ndarray, A: np.ndarray, deterrence: np.ndarray,
            max_iter: int = 50, tol: float = 1e-4) -> np.ndarray:
    """Doubly-constrained balancing (IPF). Returns the trip matrix T.

    T_ij = a_i * b_j * P_i * A_j * F_ij, with balancing factors
        a_i = 1 / sum_j (b_j A_j F_ij)   -> row sum T_i. = P_i
        b_j = 1 / sum_i (a_i P_i F_ij)   -> col sum T_.j = A_j
    (Note: a_i, b_j do NOT re-include P_i/A_j — those are already explicit in T.)
    """
    n = len(P)
    a = np.ones(n)  # origin balancing factors
    b = np.ones(n)  # dest balancing factors
    for _ in range(max_iter):
        a_new = 1.0 / np.maximum(deterrence @ (b * A), 1e-12)
        b_new = 1.0 / np.maximum(deterrence.T @ (a_new * P), 1e-12)
        if np.max(np.abs(a_new - a)) < tol and np.max(np.abs(b_new - b)) < tol:
            a, b = a_new, b_new
            break
        a, b = a_new, b_new
    T = (a[:, None] * P[:, None]) * deterrence * (b[None, :] * A[None, :])
    return T


def build_od(beta: float = DEFAULT_BETA, G=None, target_total_pcu: float = TARGET_TOTAL_PCU,
             production_scale=1.0, attraction_scale=1.0, processing_rate=None):
    """Full pipeline: zones -> P/A -> cost -> gravity -> vehicle-PCU OD.

    The gravity output gives the relative OD *shape*; the interzonal (off-diagonal)
    total is then scaled to `target_total_pcu` so assignment congestion is realistic.
    Robustness params (production_scale / attraction_scale / processing_rate) reshape
    the ingoing/outgoing rates per zone — see generation.production_attraction.
    Returns (zones_df, person_T, vehicle_pcu_T, cost_C).
    """
    zones = build_zones()
    pa = production_attraction(production_scale=production_scale,
                               attraction_scale=attraction_scale,
                               processing_rate=processing_rate)
    zones = zones.merge(pa[["P", "A"]], left_on="zone_id", right_index=True)

    P = zones["P"].to_numpy(float)
    A = zones["A"].to_numpy(float)
    C = cost_matrix(zones, G=G)

    # Deterrence f(c) = c^(-beta); guard the zero diagonal.
    with np.errstate(divide="ignore"):
        F = np.where(C > 0, C ** (-beta), 0.0)
    np.fill_diagonal(F, np.where(np.diag(C) > 0, np.diag(C) ** (-beta), 0.0))

    person_T = furness(P, A, F)
    # person-trips -> private-vehicle trips -> PCU (relative shape).
    veh_pcu_T = person_T * PRIVATE_VEHICLE_SHARE / AVG_OCCUPANCY * AVG_PCU

    # Scale interzonal loading to the target total (intrazonal trips don't load the network).
    if target_total_pcu:
        interzonal = veh_pcu_T.copy()
        np.fill_diagonal(interzonal, 0.0)
        s = interzonal.sum()
        if s > 0:
            veh_pcu_T = veh_pcu_T * (target_total_pcu / s)
    return zones, person_T, veh_pcu_T, C


def od_to_pairs(zones: pd.DataFrame, veh_pcu_T: np.ndarray):
    """Convert an OD matrix to (origin_node, dest_node, demand) tuples for assignment."""
    nodes = zones["connector_node"].astype(np.int64).tolist()
    pairs = []
    n = len(nodes)
    for i in range(n):
        for j in range(n):
            if i != j and veh_pcu_T[i, j] > 0:
                pairs.append((nodes[i], nodes[j], float(veh_pcu_T[i, j])))
    return pairs


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Build the gravity-model OD matrix.")
    parser.add_argument("--beta", type=float, default=DEFAULT_BETA)
    args = parser.parse_args()

    zones, person_T, veh_T, C = build_od(beta=args.beta)
    names = zones["name"].tolist()

    print(f"Gravity model (beta={args.beta}) — vehicle-PCU OD matrix (PCU/peak-hr):\n")
    od_df = pd.DataFrame(veh_T.round(0).astype(int), index=names, columns=names)
    pd.set_option("display.width", 160, "display.max_columns", 20)
    print(od_df)
    print(f"\nTotal person-trips: {person_T.sum():.0f}")
    print(f"Total vehicle-PCU trips (peak-hr): {veh_T.sum():.0f}")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "od_matrix.csv"
    od_df.to_csv(out)
    print(f"\nSaved OD matrix -> {out}")


if __name__ == "__main__":
    main()
