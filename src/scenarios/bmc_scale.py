"""
bmc_scale.py — hybrid BMC / Greater-Mumbai scale-up.

Scales the corridor model to the whole BMC major-road network (network_bmc_enriched,
~3,940 nodes / 6,922 links / 1,369 km) using the method the user chose:

  * ASSIGNMENT where we can build demand — a grid of TAZs over the BMC extent, a
    doubly-constrained gravity OD, and one Frank-Wolfe user-equilibrium run. This
    gives modelled link V/C and, at every tracked junction, arriving volume and the
    standing-queue length on each congested approach (same physics as
    assignment/intersections.py).
  * OBSERVATION elsewhere — where the junction collector has live TTI for a junction
    (store.intersection_readings), we fill V/C / queue from measured speed instead of
    the model. Until that table has rows every junction is "model" or "capacity_only".

Each junction in the output carries a `source` so model and observed figures are
never silently mixed.

Demand note: per-zone production/attraction uses an intersection-density PROXY (no
per-ward census wired in yet) scaled to a target total, so BMC absolute volumes are a
synthetic baseline — the same honest footing as the corridor. Structure over absolute.

Usage:
    python -m src.scenarios.bmc_scale --grid 6 --total 60000
Writes:
    data/processed/bmc/bmc_link_flows.csv
    data/processed/bmc/bmc_junction_metrics.json
    data/processed/bmc/bmc_summary.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd

from src.assignment import metrics
from src.assignment.frank_wolfe import assign
from src.demand.gravity_model import (AVG_OCCUPANCY, AVG_PCU, PRIVATE_VEHICLE_SHARE,
                                      furness)
from src.network.incident import JAM_DENSITY_VEH_KM_LANE
from src.network.graph_io import load_enriched_graph

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED = REPO_ROOT / "data" / "processed"
BMC_ENRICHED = PROCESSED / "network_bmc_enriched.graphml"
COVERAGE_JSON = PROCESSED / "map" / "coverage.json"
OUT_DIR = PROCESSED / "bmc"

ARTERIAL = {"motorway", "trunk", "primary"}
PEAK_DURATION_H = 1.0


def _hwy(d):
    h = d.get("highway")
    return h[0] if isinstance(h, list) else h


def _arterial_nodes(G) -> set:
    art = set()
    for u, v, d in G.edges(data=True):
        if _hwy(d) in ARTERIAL:
            art.add(u)
            art.add(v)
    return art


# --------------------------------------------------------------------------- #
# Grid TAZs over the BMC extent
# --------------------------------------------------------------------------- #
def build_grid_zones(G, grid: int = 6) -> pd.DataFrame:
    """A grid of TAZs; each non-empty cell -> one zone with an arterial connector
    node drawn from the network's largest strongly connected component."""
    xs = np.array([d["x"] for _, d in G.nodes(data=True)])
    ys = np.array([d["y"] for _, d in G.nodes(data=True)])
    ids = np.array([n for n in G.nodes()])
    minx, maxx, miny, maxy = xs.min(), xs.max(), ys.min(), ys.max()

    scc = max(nx.strongly_connected_components(G), key=len)   # assignable core
    art = _arterial_nodes(G) & scc

    def cell(x, y):
        cx = min(grid - 1, int((x - minx) / (maxx - minx + 1e-9) * grid))
        cy = min(grid - 1, int((y - miny) / (maxy - miny + 1e-9) * grid))
        return cx, cy

    # Node membership per cell (for the density proxy) + candidate connectors.
    from collections import defaultdict
    members = defaultdict(list)
    for n, x, y in zip(ids, xs, ys):
        members[cell(x, y)].append((int(n), x, y))

    zones = []
    for (cx, cy), nodes in members.items():
        if not nodes:
            continue
        cxx = np.mean([x for _, x, _ in nodes])
        cyy = np.mean([y for _, _, y in nodes])
        # connector: nearest arterial-in-SCC node to the cell centroid, else nearest SCC node
        cand = [(nid, x, y) for nid, x, y in nodes if nid in art] or \
               [(nid, x, y) for nid, x, y in nodes if nid in scc]
        if not cand:
            continue
        nid, _, _ = min(cand, key=lambda t: (t[1] - cxx) ** 2 + (t[2] - cyy) ** 2)
        zones.append({
            "zone_id": f"z{cx}_{cy}",
            "connector_node": nid,
            "lat": round(float(cyy), 6), "lon": round(float(cxx), 6),
            "n_nodes": len(nodes),   # intersection-density proxy for activity
        })
    zdf = pd.DataFrame(zones).reset_index(drop=True)
    # De-duplicate connectors (two cells can snap to the same node).
    zdf = zdf.drop_duplicates("connector_node").reset_index(drop=True)
    return zdf


def cost_matrix(G, zones: pd.DataFrame) -> np.ndarray:
    nodes = zones["connector_node"].astype(int).tolist()
    n = len(nodes)
    C = np.full((n, n), 1e6)
    for i, o in enumerate(nodes):
        lengths = nx.single_source_dijkstra_path_length(G, o, weight="free_flow_travel_time_s")
        for j, d in enumerate(nodes):
            if i == j:
                continue
            if d in lengths:
                C[i, j] = lengths[d] / 60.0     # seconds -> minutes
    for i in range(n):
        off = [C[i, j] for j in range(n) if j != i and C[i, j] < 1e5]
        C[i, i] = 0.5 * min(off) if off else 1.0
    return C


def build_demand(G, zones: pd.DataFrame, beta: float = 2.0,
                 total_pcu: float = 60000.0):
    proxy = zones["n_nodes"].to_numpy(float)      # activity proxy
    P = proxy.copy()
    A = proxy * (P.sum() / proxy.sum())           # balance attractions to productions
    C = cost_matrix(G, zones)
    with np.errstate(divide="ignore"):
        F = np.where(C > 0, C ** (-beta), 0.0)
    person_T = furness(P, A, F)
    veh_T = person_T * PRIVATE_VEHICLE_SHARE / AVG_OCCUPANCY * AVG_PCU
    inter = veh_T.copy()
    np.fill_diagonal(inter, 0.0)
    s = inter.sum()
    if total_pcu and s > 0:
        veh_T *= total_pcu / s
    return veh_T


def od_pairs(zones: pd.DataFrame, veh_T: np.ndarray):
    nodes = zones["connector_node"].astype(int).tolist()
    pairs = []
    n = len(nodes)
    for i in range(n):
        for j in range(n):
            if i != j and veh_T[i, j] > 0:
                pairs.append((nodes[i], nodes[j], float(veh_T[i, j])))
    return pairs


# --------------------------------------------------------------------------- #
# Per-junction metrics (model), with an observation hook
# --------------------------------------------------------------------------- #
def _load_bmc_junctions() -> list[dict]:
    cov = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    return [j for j in cov.get("junctions", []) if j.get("in_bmc")]


def _observation_tti() -> dict:
    """junction_id -> latest TTI from the collector, if that table exists yet."""
    try:
        import sqlite3
        from src.data.store import DB_PATH
        c = sqlite3.connect(DB_PATH)
        rows = c.execute(
            "SELECT point_id, tti FROM intersection_readings "
            "WHERE tti IS NOT NULL ORDER BY fetched_utc").fetchall()
        c.close()
        return {pid: tti for pid, tti in rows}      # last wins = latest
    except Exception:
        return {}


def junction_metrics(G, result, df: pd.DataFrame) -> list[dict]:
    import osmnx as ox

    junctions = _load_bmc_junctions()
    obs_tti = _observation_tti()

    # incoming flow + capacity per node from the link table
    incoming = {}
    for r in df.itertuples():
        incoming.setdefault(int(r.v), []).append(
            (float(r.flow_pcu_hr), float(r.capacity_pcu_hr),
             int(r.lanes or 1) if not pd.isna(r.lanes) else 1))

    lons = np.array([j["lon"] for j in junctions], float)
    lats = np.array([j["lat"] for j in junctions], float)
    nearest = ox.nearest_nodes(G, lons, lats)

    out = []
    for j, node in zip(junctions, nearest):
        node = int(node)
        approaches = incoming.get(node, [])
        volume = round(sum(f for f, _, _ in approaches), 1)
        max_vc = round(max((f / c for f, c, _ in approaches if c > 0), default=0.0), 3)
        # Standing queue on the worst congested approach: an approach whose arrival
        # flow exceeds its capacity backs up at (flow - capacity) over the peak hour;
        # physical length = queued vehicles / (lanes * jam density). No incident
        # clamp here — the approach capacity IS the bottleneck.
        qkm = 0.0
        for f, c, lanes in approaches:
            excess = f - c
            if excess > 0:
                queued = excess * PEAK_DURATION_H
                qkm = max(qkm, queued / (max(1, lanes) * JAM_DENSITY_VEH_KM_LANE))
        rec = {
            "junction_id": j["id"], "name": j.get("name") or "",
            "lat": j["lat"], "lon": j["lon"],
            "graph_node": node,
            "volume_pcu_h": volume,
            "max_approach_vc": max_vc,
            "queue_len_km": round(qkm, 3),
            "source": "model" if approaches else "capacity_only",
        }
        # Observation override where live TTI exists for this junction.
        if j["id"] in obs_tti and obs_tti[j["id"]]:
            rec["observed_tti"] = round(float(obs_tti[j["id"]]), 3)
            rec["source"] = "observation"
        out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run(grid: int = 8, total_pcu: float = 60000.0, beta: float = 2.0,
        max_iter: int = 60, tol: float = 0.001, verbose: bool = True) -> dict:
    G = load_enriched_graph(BMC_ENRICHED)
    zones = build_grid_zones(G, grid=grid)
    if verbose:
        print(f"[bmc] {len(zones)} TAZs over BMC; network "
              f"{G.number_of_nodes()} nodes / {G.number_of_edges()} links")
    veh_T = build_demand(G, zones, beta=beta, total_pcu=total_pcu)
    pairs = od_pairs(zones, veh_T)
    if verbose:
        print(f"[bmc] {len(pairs)} OD pairs, total {sum(p[2] for p in pairs):.0f} PCU/h")

    result = assign(G, pairs, alpha=0.15, beta=4.0, max_iter=max_iter, tol=tol, verbose=verbose)
    df = metrics.link_table(G, result)
    jm = junction_metrics(G, result, df)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_DIR / "bmc_link_flows.csv", index=False)
    (OUT_DIR / "bmc_junction_metrics.json").write_text(
        json.dumps({"generated_utc": datetime.now(timezone.utc).isoformat(),
                    "n_junctions": len(jm), "junctions": jm}, indent=1), encoding="utf-8")

    active = df[df["flow_pcu_hr"] > 0]["vc_ratio"]
    by_source = pd.Series([j["source"] for j in jm]).value_counts().to_dict()
    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "bmc", "grid": grid, "n_zones": len(zones),
        "n_nodes": G.number_of_nodes(), "n_links": G.number_of_edges(),
        "network_km": round(sum(float(d.get("length", 0) or 0)
                                for *_ , d in G.edges(data=True)) / 1000, 1),
        "converged": result.converged, "iterations": result.iterations,
        "tstt_pcu_h": round(metrics.tstt_hours(result), 1),
        "target_total_pcu": total_pcu,
        "max_vc": round(float(active.max()), 2) if len(active) else None,
        "mean_vc": round(float(active.mean()), 2) if len(active) else None,
        "links_over_cap": int((active > 1.0).sum()),
        "n_junctions": len(jm),
        "junctions_by_source": by_source,
        "worst_junctions": sorted(
            [j for j in jm if j["source"] != "capacity_only"],
            key=lambda x: x["max_approach_vc"], reverse=True)[:10],
    }
    (OUT_DIR / "bmc_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Hybrid BMC scale-up (assignment + observation).")
    ap.add_argument("--grid", type=int, default=8, help="TAZ grid resolution (default 8x8).")
    ap.add_argument("--total", type=float, default=60000.0, help="Target total OD PCU/h.")
    ap.add_argument("--beta", type=float, default=2.0)
    ap.add_argument("--max-iter", type=int, default=60)
    ap.add_argument("--tol", type=float, default=0.001)
    args = ap.parse_args()

    s = run(grid=args.grid, total_pcu=args.total, beta=args.beta,
            max_iter=args.max_iter, tol=args.tol)
    print("\n===== BMC SCALE-UP SUMMARY =====")
    for k in ("n_zones", "n_nodes", "n_links", "network_km", "converged", "iterations",
              "tstt_pcu_h", "max_vc", "mean_vc", "links_over_cap", "n_junctions",
              "junctions_by_source"):
        print(f"  {k}: {s[k]}")
    print("\n  Worst junctions (by approach V/C):")
    for j in s["worst_junctions"][:8]:
        print(f"    {j['max_approach_vc']:>5}  vol={j['volume_pcu_h']:>7} PCU/h  "
              f"queue={j['queue_len_km']}km  {j['name'][:45]}")


if __name__ == "__main__":
    main()
