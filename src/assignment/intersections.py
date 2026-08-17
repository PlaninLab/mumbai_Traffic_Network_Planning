"""
intersections.py — per-INTERSECTION volume and queue metrics from the base case.

The project's target numbers are, for every intersection in the corridor:

  - VOLUME:  how many PCU/h arrive at the node = sum of the equilibrium flows on
             its incoming links (one base-case Frank-Wolfe run; no scenarios).
  - QUEUE:   how far the standing queue on each congested approach reaches.

Queue model (deterministic input-output, same physics as network/incident.py):
an approach whose arrival flow exceeds its capacity is a standing bottleneck
during the peak hour. Vehicles accumulate at (flow - capacity) for the peak
duration, and the physical backup is

    queue_len = queued_vehicles / (lanes * jam_density).

This is the "if drivers hold their route" upper bound — the honest planning
figure for "how long does the queue at this junction get".

The Frank-Wolfe run is cached on disk (link_flows.csv + assignment_meta.json),
so the web exporter never re-solves unless asked to.

CLI:
    python -m src.assignment.intersections            # use cache if present
    python -m src.assignment.intersections --rebuild  # force a fresh UE run
Writes:
    data/processed/link_flows.csv
    data/processed/assignment_meta.json
    data/processed/intersection_metrics.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.network.graph_io import load_enriched_graph
from src.network.incident import JAM_DENSITY_VEH_KM_LANE

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED = REPO_ROOT / "data" / "processed"
LINK_FLOWS_CSV = PROCESSED / "link_flows.csv"
ASSIGNMENT_META = PROCESSED / "assignment_meta.json"
INTERSECTIONS_JSON = PROCESSED / "intersection_metrics.json"

PEAK_DURATION_H = 1.0        # the modeled peak hour
QUEUE_VC_THRESHOLD = 1.0     # an approach queues when flow exceeds capacity

# Corridor endpoints for the driver-experienced travel time (Dahisar check naka
# and the Bandra end of the WEH).
CORRIDOR_ORIGIN = (19.2502, 72.8568)
CORRIDOR_DEST = (19.0550, 72.8400)


def _first(value):
    """OSM attributes can be lists; take the first entry."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _nearest_node(G, lat: float, lon: float):
    best, best_d2 = None, float("inf")
    for n, d in G.nodes(data=True):
        d2 = (d["y"] - lat) ** 2 + (d["x"] - lon) ** 2
        if d2 < best_d2:
            best, best_d2 = n, d2
    return best


def solve_base_case(verbose: bool = True) -> tuple[pd.DataFrame, dict]:
    """Run one base-case UE assignment and return (link table, meta)."""
    # Imported here so `--info` style reads never pull the heavy solver chain.
    from src.assignment import metrics
    from src.assignment.run_assignment import run_base

    G, zones, result, df = run_base(verbose=verbose)

    freeflow_tstt_h = sum(result.flow[e] * result.t0[e] for e in result.flow) / 3600.0
    o = _nearest_node(G, *CORRIDOR_ORIGIN)
    d = _nearest_node(G, *CORRIDOR_DEST)
    corridor_eq_min = metrics.corridor_travel_time(G, result, o, d)
    # Free-flow corridor time: same path search on t0.
    import networkx as nx
    for (u, v, k), t in result.t0.items():
        G[u][v][k]["_ff_time"] = t
    try:
        corridor_ff_min = nx.shortest_path_length(G, o, d, weight="_ff_time") / 60.0
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        corridor_ff_min = float("inf")

    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "converged": bool(result.converged),
        "iterations": int(result.iterations),
        "final_gap": float(result.gaps[-1]) if result.gaps else None,
        "tstt_pcu_h": round(result.tstt / 3600.0, 1),
        "freeflow_tstt_pcu_h": round(freeflow_tstt_h, 1),
        "delay_pcu_h": round(result.tstt / 3600.0 - freeflow_tstt_h, 1),
        "corridor_eq_min": round(corridor_eq_min, 1),
        "corridor_ff_min": round(corridor_ff_min, 1),
        "corridor_origin_node": int(o),
        "corridor_dest_node": int(d),
        "n_links": int(len(df)),
        "peak_duration_h": PEAK_DURATION_H,
        "jam_density_veh_km_lane": JAM_DENSITY_VEH_KM_LANE,
    }
    return df, meta


def load_or_solve(rebuild: bool = False, verbose: bool = True) -> tuple[pd.DataFrame, dict]:
    """Return the cached link-flow table, or solve and cache it."""
    if not rebuild and LINK_FLOWS_CSV.exists() and ASSIGNMENT_META.exists():
        df = pd.read_csv(LINK_FLOWS_CSV)
        with ASSIGNMENT_META.open(encoding="utf-8") as f:
            meta = json.load(f)
        return df, meta
    df, meta = solve_base_case(verbose=verbose)
    df.to_csv(LINK_FLOWS_CSV, index=False)
    with ASSIGNMENT_META.open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    return df, meta


def _junction_name(G, node) -> str:
    """Name a junction from the street names that meet there: "A × B"."""
    names = Counter()
    for _u, _v, d in list(G.in_edges(node, data=True)) + list(G.out_edges(node, data=True)):
        name = _first(d.get("name"))
        if name:
            names[str(name)] += 1
    distinct = [n for n, _c in names.most_common()]
    if len(distinct) >= 2:
        return f"{distinct[0]} × {distinct[1]}"
    if len(distinct) == 1:
        return distinct[0]
    return f"junction {node}"


def approach_queue(flow: float, capacity: float, lanes: int,
                   peak_duration_h: float = PEAK_DURATION_H) -> dict:
    """Standing-bottleneck queue on one approach during the peak hour."""
    excess = max(0.0, flow - capacity)
    queued_veh = excess * peak_duration_h
    lanes = max(1, int(lanes or 1))
    queue_len_km = queued_veh / (lanes * JAM_DENSITY_VEH_KM_LANE)
    return {
        "queued_veh": round(queued_veh, 1),
        "queue_len_m": round(queue_len_km * 1000.0, 0),
        "growth_pcu_h": round(excess, 1),
    }


def _norm_name(name) -> str:
    return str(name).strip().lower() if isinstance(name, str) else ""


def node_metrics(G, links: pd.DataFrame) -> list[dict]:
    """Per-intersection volume + queue metrics from the link-flow table.

    Includes every node where >= 3 street legs meet (a real junction), plus any
    node that has a queuing approach regardless of degree.

    Queue attribution: on a chain of consecutive over-capacity links of the SAME
    road, every link reports the same excess — but physically that is ONE jam,
    owned by its most downstream link (the bottleneck head). An approach is a
    head when the same road does not continue over capacity out of the node; only
    head approaches count toward a junction's queue totals (the continuation
    links are marked head=false and excluded).
    """
    by_target: dict[int, list[dict]] = {}
    by_source: dict[int, list[dict]] = {}
    for row in links.itertuples(index=False):
        r = row._asdict()
        by_target.setdefault(int(row.v), []).append(r)
        by_source.setdefault(int(row.u), []).append(r)

    def _queues(r) -> bool:
        return bool(r["capacity_pcu_hr"]) and pd.notna(r["vc_ratio"]) \
            and r["vc_ratio"] > QUEUE_VC_THRESHOLD

    def _is_head(r) -> bool:
        """True unless the same road continues over capacity downstream."""
        nm = _norm_name(r["name"])
        for out in by_source.get(int(r["v"]), []):
            if _queues(out) and _norm_name(out["name"]) == nm:
                return False
        return True

    out = []
    for node, data in G.nodes(data=True):
        incoming = by_target.get(int(node), [])
        volume = sum(r["flow_pcu_hr"] for r in incoming)
        street_count = int(data.get("street_count") or 0)

        approaches = []
        for r in incoming:
            if _queues(r):
                q = approach_queue(r["flow_pcu_hr"], r["capacity_pcu_hr"], r["lanes"])
                approaches.append({
                    "u": int(r["u"]), "v": int(r["v"]), "key": int(r["key"]),
                    "name": r["name"] if isinstance(r["name"], str) else "",
                    "lanes": int(r["lanes"] or 1),
                    "flow_pcu_h": round(r["flow_pcu_hr"], 0),
                    "capacity_pcu_h": round(r["capacity_pcu_hr"], 0),
                    "vc": round(r["vc_ratio"], 2),
                    "delay_s": round(max(0.0, r["time_s"] - r["t0_s"]), 1),
                    "head": _is_head(r),
                    **q,
                })
        has_queue = any(a["head"] for a in approaches)
        if street_count < 3 and not has_queue:
            continue
        if volume <= 0 and not has_queue:
            continue

        vcs = [r["vc_ratio"] for r in incoming
               if r["capacity_pcu_hr"] and pd.notna(r["vc_ratio"])]
        delays = [max(0.0, r["time_s"] - r["t0_s"]) for r in incoming]
        heads = [a for a in approaches if a["head"]]
        out.append({
            "id": int(node),
            "lat": round(float(data["y"]), 6),
            "lon": round(float(data["x"]), 6),
            "name": _junction_name(G, node),
            "street_count": street_count,
            "volume_pcu_h": round(volume, 0),
            "vc_max": round(max(vcs), 2) if vcs else 0.0,
            "delay_s_max": round(max(delays), 1) if delays else 0.0,
            "queue_total_m": round(sum(a["queue_len_m"] for a in heads), 0),
            "queued_veh_total": round(sum(a["queued_veh"] for a in heads), 0),
            "approaches": approaches,
        })
    out.sort(key=lambda r: (-r["queue_total_m"], -r["volume_pcu_h"]))
    return out


def build(rebuild: bool = False, verbose: bool = True) -> dict:
    """Compute and persist intersection metrics. Returns the written payload."""
    links, meta = load_or_solve(rebuild=rebuild, verbose=verbose)
    G = load_enriched_graph()
    nodes = node_metrics(G, links)
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "assignment": meta,
        "queue_model": {
            "peak_duration_h": PEAK_DURATION_H,
            "jam_density_veh_km_lane": JAM_DENSITY_VEH_KM_LANE,
            "note": "Standing deterministic queue per congested approach; "
                    "upper bound (no rerouting).",
        },
        "n_intersections": len(nodes),
        "nodes": nodes,
    }
    with INTERSECTIONS_JSON.open("w", encoding="utf-8") as f:
        json.dump(payload, f)
    return payload


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-intersection volume + queue metrics.")
    ap.add_argument("--rebuild", action="store_true",
                    help="Force a fresh Frank-Wolfe run instead of the disk cache.")
    args = ap.parse_args()

    payload = build(rebuild=args.rebuild)
    meta = payload["assignment"]
    nodes = payload["nodes"]
    print(f"[intersections] {len(nodes)} junctions  "
          f"(TSTT {meta['tstt_pcu_h']} PCU-h, corridor {meta['corridor_eq_min']} min)")
    queued = [n for n in nodes if n["queue_total_m"] > 0]
    print(f"[intersections] {len(queued)} junctions with a standing peak queue")
    for n in queued[:10]:
        print(f"  {n['name'][:52]:<52} vol {n['volume_pcu_h']:>6.0f} PCU/h  "
              f"queue {n['queue_total_m']:>6.0f} m")
    print(f"[intersections] wrote {INTERSECTIONS_JSON.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
