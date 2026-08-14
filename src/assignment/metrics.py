"""
metrics.py — post-assignment metrics: TSTT, V/C ratios, bottleneck ranking.

Consumes an AssignmentResult (from frank_wolfe.assign) and the network graph to
produce planning-relevant outputs (plan §Phase 3):
  - TSTT (total system travel time, in PCU-hours)
  - per-link V/C ratio (flow / capacity) -> congestion level
  - top-N bottleneck links by V/C
  - internal consistency checks (§6.1)
"""

from __future__ import annotations

import networkx as nx
import pandas as pd


def link_table(G, result) -> pd.DataFrame:
    """Per-link results as a DataFrame, sorted by V/C descending."""
    rows = []
    for (u, v, k), flow in result.flow.items():
        d = G[u][v][k]
        cap = result.capacity[(u, v, k)]
        hwy = d.get("highway")
        hwy = hwy[0] if isinstance(hwy, list) else hwy
        rows.append({
            "u": u, "v": v, "key": k,
            "name": (d.get("name") or [""])[0] if isinstance(d.get("name"), list) else d.get("name", ""),
            "highway": hwy,
            "lanes": d.get("lanes"),
            "length_m": round(float(d.get("length", 0) or 0), 1),
            "flow_pcu_hr": round(flow, 1),
            "capacity_pcu_hr": round(cap, 1),
            "vc_ratio": round(flow / cap, 3) if cap > 0 else float("inf"),
            "t0_s": round(result.t0[(u, v, k)], 1),
            "time_s": round(result.time[(u, v, k)], 1),
            "n_stopped": d.get("n_stopped", 0),
        })
    df = pd.DataFrame(rows)
    return df.sort_values("vc_ratio", ascending=False).reset_index(drop=True)


def tstt_hours(result) -> float:
    """Total System Travel Time in PCU-hours."""
    return result.tstt / 3600.0


def top_bottlenecks(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Top-n congested links by V/C (flow > 0)."""
    active = df[df["flow_pcu_hr"] > 0]
    return active.head(n)[["name", "highway", "lanes", "flow_pcu_hr",
                           "capacity_pcu_hr", "vc_ratio", "n_stopped"]]


def corridor_travel_time(G, result, origin, dest) -> float:
    """Equilibrium shortest-path travel time (minutes) between two nodes.

    Uses the converged congested link times as edge weights. For a through-corridor
    OD (e.g. Dahisar -> Bandra) this is the driver-experienced travel time, which —
    unlike network TSTT — behaves intuitively under an incident (it rises), because
    it measures the cost of actually traversing the corridor rather than the
    system-wide sum that UE rerouting can rebalance.
    """
    # Write congested times onto the graph, then Dijkstra.
    for (u, v, k), t in result.time.items():
        G[u][v][k]["_eq_time"] = t
    try:
        secs = nx.shortest_path_length(G, origin, dest, weight="_eq_time")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return float("inf")
    return secs / 60.0


def link_delay(G, result, u, v, k=None) -> dict:
    """Travel-time increase (s) on a specific link vs its free-flow time.

    Always >= 0 for a congested/incident link — the direct bottleneck signal.
    """
    keys = [k] if k is not None else list(G.get_edge_data(u, v).keys())
    out = {}
    for key in keys:
        e = (u, v, key)
        if e in result.time:
            out[e] = round(result.time[e] - result.t0[e], 1)
    return out


def consistency_checks(G, result, od_total: float) -> dict:
    """Internal consistency checks (plan §6.1). Returns a dict of pass/fail + values."""
    flows = list(result.flow.values())
    times = result.time
    t0 = result.t0
    checks = {
        "all_flows_nonnegative": all(f >= -1e-6 for f in flows),
        "bpr_time_ge_freeflow": all(times[e] >= t0[e] - 1e-6 for e in times),
        "converged": result.converged,
        "final_gap": result.gaps[-1] if result.gaps else None,
        "n_links_over_capacity": int(sum(1 for e in result.flow
                                         if result.capacity[e] > 0
                                         and result.flow[e] / result.capacity[e] > 1.0)),
    }
    return checks
