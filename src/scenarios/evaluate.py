"""
evaluate.py — Phase 4: simulate ALL cases and compare (plan §Phase 4).

Runs the full case set on the SAME fixed OD demand and collects one comparison
table + per-case outputs:

  - Base case (no intervention)
  - Scenario A: widen the worst bottleneck link (+1 lane)
  - Scenario B: add a bypass connector link
  - Scenario C: close a link (Braess / closure test)
  - Scenario D: stopped-vehicle incident SWEEP (N = 1, 2, 3) on the worst link

Each case: re-run UE assignment, record TSTT, ΔTSTT vs base, worst V/C, and the
count of over-capacity links. Results -> data/processed/scenario_comparison.csv,
and a congestion (V/C) map per case -> docs/scenarios/.

Usage:
    python -m src.scenarios.evaluate --beta 2.0 --total 18000
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.assignment.frank_wolfe import assign
from src.assignment import metrics
from src.demand.gravity_model import build_od, od_to_pairs, TARGET_TOTAL_PCU
from src.network.graph_io import load_enriched_graph
from src.scenarios import define_scenario as scn

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
SCEN_DOCS = REPO_ROOT / "docs" / "scenarios"


def _worst_arterial_link(G, df: pd.DataFrame):
    """Pick the highest-V/C link on an arterial class with real flow (target for A/D)."""
    arterials = {"motorway", "trunk", "primary", "secondary"}
    cand = df[(df["flow_pcu_hr"] > 0) & (df["highway"].isin(arterials))]
    top = cand.iloc[0]
    return int(top["u"]), int(top["v"]), int(top["key"]), top


def _summarize(label, description, G, result, df, base_tstt):
    tstt = metrics.tstt_hours(result)
    vc = df[df["flow_pcu_hr"] > 0]["vc_ratio"]
    checks = metrics.consistency_checks(G, result, 0)
    return {
        "case": label,
        "description": description,
        "TSTT_pcu_h": round(tstt, 1),
        "dTSTT_vs_base": round(tstt - base_tstt, 1) if base_tstt is not None else 0.0,
        "dTSTT_pct": round(100 * (tstt - base_tstt) / base_tstt, 2) if base_tstt else 0.0,
        "max_vc": round(vc.max(), 2),
        "mean_vc": round(vc.mean(), 2),
        "links_over_cap": int((vc > 1.0).sum()),
        "converged": result.converged,
        "gap": round(result.gaps[-1], 4) if result.gaps else None,
    }


def run_all_cases(beta: float = 2.0, total_pcu: float = TARGET_TOTAL_PCU,
                  incident_sweep=(1, 2, 3), verbose: bool = True):
    """Simulate every case on fixed demand. Returns (summary_df, cases dict)."""
    G0 = load_enriched_graph()
    zones, _pT, veh_T, _C = build_od(beta=beta, G=G0, target_total_pcu=total_pcu)
    pairs = od_to_pairs(zones, veh_T)  # fixed demand across all scenarios

    def _run(H):
        # Tight tolerance so scenario ΔTSTT is well below the smallest effect we report.
        r = assign(H, pairs, max_iter=250, tol=0.001, verbose=False)
        return r, metrics.link_table(H, r)

    summaries = []
    cases = {}

    # --- Base ---
    if verbose:
        print("[evaluate] Base case ...")
    res0, df0 = _run(G0)
    base_tstt = metrics.tstt_hours(res0)
    summaries.append(_summarize("base", "Current network + demand", G0, res0, df0, None))
    cases["base"] = (G0, res0, df0)

    # Target link for A/D = worst arterial bottleneck in the base case.
    tu, tv, tk, top = _worst_arterial_link(G0, df0)
    tgt = f"{top['name'] or top['highway']} ({tu}->{tv})"
    if verbose:
        print(f"[evaluate] Worst bottleneck: {tgt}  V/C={top['vc_ratio']} lanes={top['lanes']}")

    # --- Scenario A: widen worst bottleneck (+1 lane) ---
    if verbose:
        print("[evaluate] Scenario A: widen worst bottleneck ...")
    Ga = scn.widen_link(G0, tu, tv, tk, add_lanes=1)
    resa, dfa = _run(Ga)
    summaries.append(_summarize("A_widen", f"Widen {tgt} +1 lane", Ga, resa, dfa, base_tstt))
    cases["A_widen"] = (Ga, resa, dfa)

    # --- Scenario B: add a bypass link between the two zones around the bottleneck ---
    if verbose:
        print("[evaluate] Scenario B: add bypass connector ...")
    # Connect the connector nodes of the two most-loaded adjacent zones (north & south ends).
    n_north = int(zones.iloc[0]["connector_node"])
    n_south = int(zones.iloc[-1]["connector_node"])
    Gb = scn.add_link(G0, n_north, n_south, lanes=2, highway="primary", speed_kph=60)
    resb, dfb = _run(Gb)
    summaries.append(_summarize("B_addlink", "Add Dahisar-Bandra bypass link", Gb, resb, dfb, base_tstt))
    cases["B_addlink"] = (Gb, resb, dfb)

    # --- Scenario C: close the worst bottleneck link ---
    if verbose:
        print("[evaluate] Scenario C: close a link ...")
    Gc = scn.remove_link(G0, tu, tv)
    resc, dfc = _run(Gc)
    summaries.append(_summarize("C_close", f"Close {tgt}", Gc, resc, dfc, base_tstt))
    cases["C_close"] = (Gc, resc, dfc)

    # --- Scenario D: stopped-vehicle incident sweep on the worst bottleneck ---
    for n in incident_sweep:
        label = f"D_incident_N{n}"
        if verbose:
            print(f"[evaluate] Scenario D: incident N={n} stopped vehicles ...")
        Gd = scn.set_incident(G0, tu, tv, n_stopped=n, k=tk)
        resd, dfd = _run(Gd)
        summaries.append(_summarize(label, f"{n} stopped vehicle(s) on {tgt}", Gd, resd, dfd, base_tstt))
        cases[label] = (Gd, resd, dfd)

    summary_df = pd.DataFrame(summaries)
    return summary_df, cases, (tu, tv, tk)


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate all scenario cases and compare.")
    parser.add_argument("--beta", type=float, default=2.0)
    parser.add_argument("--total", type=float, default=TARGET_TOTAL_PCU)
    parser.add_argument("--no-maps", action="store_true", help="Skip rendering V/C maps.")
    args = parser.parse_args()

    summary_df, cases, target = run_all_cases(beta=args.beta, total_pcu=args.total)

    pd.set_option("display.width", 200, "display.max_columns", 20)
    print("\n===== ALL-CASES COMPARISON =====")
    print(summary_df.to_string(index=False))

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    out = PROCESSED_DIR / "scenario_comparison.csv"
    summary_df.to_csv(out, index=False)
    print(f"\nSaved comparison -> {out}")

    if not args.no_maps:
        from src.viz.network_map import plot_vc_map
        SCEN_DOCS.mkdir(parents=True, exist_ok=True)
        for label, (G, res, df) in cases.items():
            p = SCEN_DOCS / f"vc_{label}.png"
            plot_vc_map(G, df, p, title=f"V/C — {label}")
            print(f"  rendered {p}")


if __name__ == "__main__":
    main()
