"""
run_assignment.py — Phase 3 driver: build demand, run UE, report metrics.

Ties Phase 2 (demand) and Phase 3 (assignment) together for the base case:
    load network -> gravity OD -> Frank-Wolfe UE -> TSTT / V/C / bottlenecks.

Usage:
    python -m src.assignment.run_assignment --beta 2.0 --total 25000
"""

from __future__ import annotations

import argparse

from src.network.graph_io import load_enriched_graph
from src.demand.gravity_model import build_od, od_to_pairs, TARGET_TOTAL_PCU
from src.assignment.frank_wolfe import assign
from src.assignment import metrics


def run_base(beta: float = 2.0, total_pcu: float = TARGET_TOTAL_PCU,
             max_iter: int = 80, tol: float = 0.01, verbose: bool = True):
    """Return (G, zones, result, link_df) for the base case."""
    G = load_enriched_graph()
    zones, _person_T, veh_T, _C = build_od(beta=beta, G=G, target_total_pcu=total_pcu)
    pairs = od_to_pairs(zones, veh_T)
    total = sum(p[2] for p in pairs)
    if verbose:
        print(f"[run] {len(pairs)} OD pairs, total interzonal demand {total:.0f} PCU/h")
    result = assign(G, pairs, max_iter=max_iter, tol=tol, verbose=verbose)
    df = metrics.link_table(G, result)
    return G, zones, result, df


def main() -> None:
    parser = argparse.ArgumentParser(description="Run base-case UE assignment.")
    parser.add_argument("--beta", type=float, default=2.0)
    parser.add_argument("--total", type=float, default=TARGET_TOTAL_PCU)
    args = parser.parse_args()

    G, zones, res, df = run_base(beta=args.beta, total_pcu=args.total)
    print(f"\nConverged: {res.converged} in {res.iterations} iters "
          f"(final gap {res.gaps[-1]:.4f})")
    print(f"TSTT = {metrics.tstt_hours(res):.1f} PCU-hours")
    vc = df[df["flow_pcu_hr"] > 0]["vc_ratio"]
    print(f"V/C: max {vc.max():.2f}, mean {vc.mean():.2f}, "
          f"links V/C>0.9: {(vc>0.9).sum()}, V/C>1.0: {(vc>1.0).sum()}")
    print("\nTop 10 bottlenecks:")
    print(metrics.top_bottlenecks(df, 10).to_string(index=False))


if __name__ == "__main__":
    main()
