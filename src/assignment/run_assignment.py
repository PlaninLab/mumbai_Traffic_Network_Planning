"""
run_assignment.py — Phase 3 driver: build demand, run UE, report metrics.

Ties Phase 2 (demand) and Phase 3 (assignment) together for the base case:
    load network -> gravity OD -> Frank-Wolfe UE -> TSTT / V/C / bottlenecks.

Usage:
    python -m src.assignment.run_assignment --total 25000
    python -m src.assignment.run_assignment --beta 2.0  # explicit sensitivity override
"""

from __future__ import annotations

import argparse

from src.network.graph_io import load_enriched_graph
from src.demand.generation import DEFAULT_CTS_CONTROL_YEAR
from src.demand.gravity_model import DEFAULT_BETA, build_od, od_to_pairs, TARGET_TOTAL_PCU
from src.assignment.frank_wolfe import assign
from src.assignment import metrics


def run_base(beta: float | None = DEFAULT_BETA, total_pcu: float = TARGET_TOTAL_PCU,
             alpha: float = 0.15, bpr_beta: float = 4.0,
             production_scale=1.0, attraction_scale=1.0, processing_rate=None,
             cost_source: str = "network", departure_time: str | None = None,
             control_year: int = DEFAULT_CTS_CONTROL_YEAR,
             max_iter: int = 80, tol: float = 0.01, verbose: bool = True):
    """Return (G, zones, result, link_df) for the base case.

    cost_source: 'network' (free-flow shortest path) or a real traffic-aware provider
    ('google' / 'tomtom') for the gravity OD cost matrix — needs that provider's key.
    """
    G = load_enriched_graph()
    zones, _person_T, veh_T, _C = build_od(
        beta=beta, G=G, target_total_pcu=total_pcu,
        production_scale=production_scale, attraction_scale=attraction_scale,
        processing_rate=processing_rate,
        cost_source=cost_source, departure_time=departure_time,
        control_year=control_year)
    pairs = od_to_pairs(zones, veh_T)
    total = sum(p[2] for p in pairs)
    if verbose:
        print(f"[run] {len(pairs)} OD pairs, total interzonal demand {total:.0f} PCU/h")
    result = assign(G, pairs, alpha=alpha, beta=bpr_beta,
                    max_iter=max_iter, tol=tol, verbose=verbose)
    df = metrics.link_table(G, result)
    return G, zones, result, df


def main() -> None:
    parser = argparse.ArgumentParser(description="Run base-case UE assignment.")
    parser.add_argument("--beta", type=float, default=DEFAULT_BETA,
                        help="Gravity exponent; omit to calibrate to CTS trip length.")
    parser.add_argument("--total", type=float, default=TARGET_TOTAL_PCU)
    parser.add_argument("--control-year", type=int,
                        choices=[2017, 2021, 2026, 2031, 2041],
                        default=DEFAULT_CTS_CONTROL_YEAR)
    parser.add_argument("--cost-source", choices=["network", "google", "tomtom"],
                        default="network",
                        help="Gravity OD cost matrix source (default network free-flow; "
                             "'google'/'tomtom' use real traffic-aware travel times).")
    args = parser.parse_args()

    G, zones, res, df = run_base(beta=args.beta, total_pcu=args.total,
                                 cost_source=args.cost_source,
                                 control_year=args.control_year)
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
