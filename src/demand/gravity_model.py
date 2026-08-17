"""
gravity_model.py — Phase 2: doubly-constrained gravity trip distribution.

    T_ij = A_i^p * B_j^a * P_i * A_j * f(c_ij),   f(c) = c^(-beta)

with balancing factors A_i^p, B_j^a iterated (Furness / IPF) so that
    sum_j T_ij = P_i   and   sum_i T_ij = A_j.

Cost c_ij is the free-flow travel time (minutes) between TAZ connector nodes,
computed by shortest path on the enriched network (plan §Phase 2 allows network
shortest-path OR TomTom Matrix Routing; we use the network to stay free/offline).
Unless explicitly overridden, beta is calibrated so the modelled interzonal
private-vehicle trip length matches the 2017 MMRDA CTS car/two-wheeler target.

Outputs a person-trip OD matrix, then converts to a vehicle-PCU OD matrix via
mode split and PCU factor, ready for assignment.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

from src.demand.generation import DEFAULT_CTS_CONTROL_YEAR, production_attraction
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

# MMRDA CTS base-year 2017 morning-peak mode shares (Table 6-1) and observed
# mean trip lengths (Figure 2-9).  The present model is a single private-road-
# vehicle class, so its calibration target is the car/TW share-weighted mean.
CTS_AM_CAR_SHARE = 0.069
CTS_AM_TWO_WHEELER_SHARE = 0.111
CTS_CAR_TRIP_LENGTH_KM = 16.5
CTS_TWO_WHEELER_TRIP_LENGTH_KM = 13.1
CTS_PRIVATE_TRIP_LENGTH_KM = (
    CTS_AM_CAR_SHARE * CTS_CAR_TRIP_LENGTH_KM
    + CTS_AM_TWO_WHEELER_SHARE * CTS_TWO_WHEELER_TRIP_LENGTH_KM
) / (CTS_AM_CAR_SHARE + CTS_AM_TWO_WHEELER_SHARE)

# None means calibrate against CTS_PRIVATE_TRIP_LENGTH_KM for the current
# network/cost matrix.  Supplying a number preserves an explicit sensitivity run.
DEFAULT_BETA: float | None = None
BETA_CALIBRATION_BOUNDS = (0.1, 8.0)

# Target total peak-hour interzonal loading (PCU/h). The synthetic generation gives
# only a relative demand shape; we scale the final matrix to this total so the busiest
# corridor links reach realistic congestion. WEH peak carries ~10-12k
# veh/h/direction; a corridor total of 18k PCU/h across all OD pairs loads it plausibly.
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
                if d not in lengths:
                    raise ValueError(
                        f"TAZ connector is unreachable: {zones.iloc[i]['name']} -> "
                        f"{zones.iloc[j]['name']}"
                    )
                C[i, j] = lengths[d] / 60.0  # seconds -> minutes
    # Intrazonal cost: half the nearest interzonal (avoids div-by-zero, keeps some intra demand).
    for i in range(n):
        offdiag = [C[i, j] for j in range(n) if j != i and C[i, j] < 1e5]
        C[i, i] = 0.5 * min(offdiag) if offdiag else 1.0
    return C


def distance_matrix(zones: pd.DataFrame, G=None) -> np.ndarray:
    """TAZ-to-TAZ network distance matrix in kilometres.

    This is a separate skim from travel-time cost because CTS calibration targets
    are reported as average trip lengths.  Intrazonal distance follows the same
    half-nearest-interzonal approximation used for travel time.
    """
    if G is None:
        G = load_enriched_graph()
    nodes = zones["connector_node"].astype(np.int64).tolist()
    n = len(nodes)
    D = np.zeros((n, n))
    for i, o in enumerate(nodes):
        lengths = nx.single_source_dijkstra_path_length(G, o, weight="length")
        for j, d in enumerate(nodes):
            if i == j:
                continue
            if d not in lengths:
                raise ValueError(
                    f"TAZ connector is unreachable: {zones.iloc[i]['name']} -> "
                    f"{zones.iloc[j]['name']}"
                )
            D[i, j] = lengths[d] / 1000.0
    for i in range(n):
        D[i, i] = 0.5 * min(D[i, j] for j in range(n) if j != i)
    return D


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


def _gravity_matrix(P: np.ndarray, A: np.ndarray, C: np.ndarray,
                    beta: float) -> np.ndarray:
    """Build a balanced person-trip matrix for one deterrence exponent."""
    if np.any(~np.isfinite(C)) or np.any(C <= 0):
        raise ValueError("Gravity cost matrix must contain finite, positive values")
    F = np.power(C, -beta)
    return furness(P, A, F, max_iter=500, tol=1e-8)


def mean_interzonal_trip_length(T: np.ndarray, D: np.ndarray) -> float:
    """Demand-weighted network distance for trips represented by assignment."""
    interzonal = np.array(T, dtype=float, copy=True)
    np.fill_diagonal(interzonal, 0.0)
    demand = float(interzonal.sum())
    if demand <= 0:
        raise ValueError("Cannot measure trip length without interzonal demand")
    return float(np.sum(interzonal * D) / demand)


def calibrate_gravity_beta(P: np.ndarray, A: np.ndarray, C: np.ndarray,
                           D: np.ndarray,
                           target_km: float = CTS_PRIVATE_TRIP_LENGTH_KM,
                           bounds: tuple[float, float] = BETA_CALIBRATION_BOUNDS,
                           grid_points: int = 80) -> tuple[float, float]:
    """Fit beta to a target mean interzonal trip length.

    Doubly constrained models are not guaranteed to have a globally monotonic
    mean-distance response because row/column totals remain fixed.  We therefore
    scan the physical beta range, bracket every target crossing, refine each by
    bisection, and choose the solution nearest the legacy beta=2 starting point.
    If the target is outside the attainable range, the closest bounded solution
    is returned with a warning instead of inventing an unphysical negative beta.
    """
    lo, hi = bounds
    if not (0 < lo < hi) or grid_points < 2:
        raise ValueError("beta calibration requires 0 < lower < upper and >=2 grid points")

    def error(beta: float) -> float:
        T = _gravity_matrix(P, A, C, beta)
        return mean_interzonal_trip_length(T, D) - target_km

    grid = np.linspace(lo, hi, grid_points + 1)
    errors = np.array([error(float(beta)) for beta in grid])
    roots: list[float] = []
    for left, right, e_left, e_right in zip(grid[:-1], grid[1:], errors[:-1], errors[1:]):
        if e_left == 0:
            roots.append(float(left))
            continue
        if e_left * e_right > 0:
            continue
        a, b = float(left), float(right)
        fa = float(e_left)
        for _ in range(50):
            mid = 0.5 * (a + b)
            fm = error(mid)
            if abs(fm) < 1e-6:
                a = b = mid
                break
            if fa * fm <= 0:
                b = mid
            else:
                a, fa = mid, fm
        roots.append(0.5 * (a + b))

    if roots:
        beta = min(roots, key=lambda value: abs(value - 2.0))
    else:
        best = int(np.argmin(np.abs(errors)))
        beta = float(grid[best])
        warnings.warn(
            f"CTS trip-length target {target_km:.2f} km is outside the modelled "
            f"range; using closest bounded beta={beta:.3f}",
            RuntimeWarning,
            stacklevel=2,
        )
    achieved = target_km + error(beta)
    return float(beta), float(achieved)


def build_od(beta: float | None = DEFAULT_BETA, G=None,
             target_total_pcu: float = TARGET_TOTAL_PCU,
             production_scale=1.0, attraction_scale=1.0, processing_rate=None,
             cost_source: str = "network", departure_time: str | None = None,
             control_year: int = DEFAULT_CTS_CONTROL_YEAR):
    """Full pipeline: zones -> P/A -> cost -> gravity -> vehicle-PCU OD.

    The gravity output gives the relative OD *shape*; the interzonal (off-diagonal)
    total is then scaled to `target_total_pcu` so assignment congestion is realistic.
    Robustness params (production_scale / attraction_scale / processing_rate) reshape
    the ingoing/outgoing rates per zone — see generation.production_attraction.

    cost_source: 'network' (free-flow shortest path, default) or a real traffic-aware
    provider — 'google' / 'tomtom' — which pulls live TAZ×TAZ travel times
    (src/demand/od_costs.py). Real costs need the provider's API key configured.
    control_year selects the CTSU Western Suburbs population/employment totals.
    beta=None calibrates the deterrence exponent to the CTS private-vehicle
    interzonal trip-length target; a numeric beta is an explicit override.
    Returns (zones_df, person_T, vehicle_pcu_T, cost_C).
    """
    if G is None:
        G = load_enriched_graph()
    zones = build_zones(G=G)
    pa = production_attraction(production_scale=production_scale,
                               attraction_scale=attraction_scale,
                               processing_rate=processing_rate,
                               control_year=control_year)
    zones = zones.merge(pa[["P", "A"]], left_on="zone_id", right_index=True)

    P = zones["P"].to_numpy(float)
    A = zones["A"].to_numpy(float)
    if cost_source == "network":
        C = cost_matrix(zones, G=G)
    else:
        from src.demand.od_costs import cost_matrix_from_provider
        C = cost_matrix_from_provider(zones, G=G, source=cost_source,
                                      departure_time=departure_time)

    D = distance_matrix(zones, G=G)
    if beta is None:
        beta, achieved_km = calibrate_gravity_beta(P, A, C, D)
        person_T = _gravity_matrix(P, A, C, beta)
    else:
        person_T = _gravity_matrix(P, A, C, beta)
        achieved_km = mean_interzonal_trip_length(person_T, D)

    zones.attrs.update({
        "cts_control_year": control_year,
        "gravity_beta": float(beta),
        "trip_length_target_km": CTS_PRIVATE_TRIP_LENGTH_KM,
        "trip_length_achieved_km": achieved_km,
    })
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
    parser.add_argument("--beta", type=float, default=DEFAULT_BETA,
                        help="Gravity exponent; omit to calibrate to CTS trip length.")
    parser.add_argument("--control-year", type=int,
                        choices=[2017, 2021, 2026, 2031, 2041],
                        default=DEFAULT_CTS_CONTROL_YEAR)
    args = parser.parse_args()

    zones, person_T, veh_T, C = build_od(beta=args.beta, control_year=args.control_year)
    names = zones["name"].tolist()
    used_beta = zones.attrs["gravity_beta"]

    print(f"Gravity model (beta={used_beta:.4f}) — vehicle-PCU OD matrix (PCU/peak-hr):\n")
    od_df = pd.DataFrame(veh_T.round(0).astype(int), index=names, columns=names)
    pd.set_option("display.width", 160, "display.max_columns", 20)
    print(od_df)
    print(f"\nTotal person-trips: {person_T.sum():.0f}")
    interzonal_total = float(veh_T.sum() - np.trace(veh_T))
    print(f"Interzonal vehicle-PCU demand (assigned): {interzonal_total:.0f} PCU/peak-hr")
    print(f"Vehicle-PCU demand including intrazonal: {veh_T.sum():.0f} PCU/peak-hr")
    print(f"CTS control year: {zones.attrs['cts_control_year']}")
    print("Mean interzonal trip length: "
          f"{zones.attrs['trip_length_achieved_km']:.2f} km "
          f"(CTS target {zones.attrs['trip_length_target_km']:.2f} km)")

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "od_matrix.csv"
    od_df.to_csv(out)
    print(f"\nSaved OD matrix -> {out}")


if __name__ == "__main__":
    main()
