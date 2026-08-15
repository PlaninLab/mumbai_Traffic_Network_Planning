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
from src.network import incident as inc
from src.network.graph_io import load_enriched_graph
from src.scenarios import define_scenario as scn

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
SCEN_DOCS = REPO_ROOT / "docs" / "scenarios"


def _worst_arterial_link(G, df: pd.DataFrame):
    """Pick the highest-V/C link on an arterial class with real flow (target for A/C)."""
    arterials = {"motorway", "trunk", "primary", "secondary"}
    cand = df[(df["flow_pcu_hr"] > 0) & (df["highway"].isin(arterials))]
    top = cand.iloc[0]
    return int(top["u"]), int(top["v"]), int(top["key"]), top


def _weh_incident_stretch(df: pd.DataFrame, k: int = 6):
    """Top-k highest-flow WEH mainline links — a realistic incident zone (Scenario D).

    An incident on a single 96 m segment is trivially bypassed under UE; a
    contiguous high-flow stretch represents a real stopped-vehicle blockage and
    produces an unambiguous corridor delay.
    """
    weh = df[(df["flow_pcu_hr"] > 0) & (df["highway"].isin({"motorway", "trunk"}))]
    weh = weh.sort_values("flow_pcu_hr", ascending=False).head(k)
    return [(int(r.u), int(r.v), int(r.key)) for r in weh.itertuples()]


def _incident_queue(stretch, base_df, incident_df):
    """Deterministic queue behind the incident stretch.

    Arrival = the PRE-incident equilibrium flow that wanted each link (base case);
    service rate = the incident-reduced capacity; clearance = nominal capacity.
    Aggregates across the stretch: worst single-link queue length + stretch totals.
    """
    base = {(int(r.u), int(r.v), int(r.key)): r for r in base_df.itertuples()}
    inci = {(int(r.u), int(r.v), int(r.key)): r for r in incident_df.itertuples()}
    max_len = 0.0
    tot_veh = 0.0
    tot_delay = 0.0
    worst_clear = 0.0
    persists = False   # any link stays saturated at nominal cap => queue never clears
    for e in stretch:
        b, i = base.get(e), inci.get(e)
        if b is None or i is None:
            continue
        q = inc.deterministic_queue(
            arrival_flow=float(b.flow_pcu_hr),
            capacity_incident=float(i.capacity_pcu_hr),
            lanes=int(b.lanes or 1),
            capacity_nominal=float(b.capacity_pcu_hr),
        )
        max_len = max(max_len, q["queue_len_km"])
        tot_veh += q["queued_veh"]
        delay, clear = q["total_delay_veh_h"], q["clear_time_min"]
        if delay == float("inf") or clear == float("inf"):
            persists = persists or q["overloaded"]
        else:
            tot_delay += delay
            worst_clear = max(worst_clear, clear)
    return {
        "queue_len_km": round(max_len, 3),
        "queued_veh": round(tot_veh, 0),
        # inf where the corridor is already saturated at nominal capacity: the
        # incident queue persists until peak demand subsides (a real signal that
        # this stretch has no spare capacity to recover into).
        "queue_delay_veh_h": float("inf") if persists else round(tot_delay, 1),
        "queue_clear_min": float("inf") if persists else round(worst_clear, 1),
    }


def _summarize(label, description, G, result, df, base_tstt, corridor_od=None,
               base_corr=None):
    tstt = metrics.tstt_hours(result)
    vc = df[df["flow_pcu_hr"] > 0]["vc_ratio"]
    corr = (metrics.corridor_travel_time(G, result, *corridor_od)
            if corridor_od else float("nan"))
    row = {
        "case": label,
        "description": description,
        "TSTT_pcu_h": round(tstt, 1),
        "dTSTT_pct": round(100 * (tstt - base_tstt) / base_tstt, 2) if base_tstt else 0.0,
        "corridor_tt_min": round(corr, 1),
        "dCorridor_pct": round(100 * (corr - base_corr) / base_corr, 2)
                         if base_corr else 0.0,
        "max_vc": round(vc.max(), 2),
        "mean_vc": round(vc.mean(), 2),
        "links_over_cap": int((vc > 1.0).sum()),
        # Deterministic incident-queue metrics (0 for non-incident cases; filled
        # in for Scenario D). See src/network/incident.deterministic_queue.
        "queue_len_km": 0.0,
        "queued_veh": 0.0,
        "queue_clear_min": 0.0,
        "queue_delay_veh_h": 0.0,
        "converged": result.converged,
        "gap": round(result.gaps[-1], 4) if result.gaps else None,
    }
    return row, corr


def run_all_cases(beta: float = 2.0, total_pcu: float = TARGET_TOTAL_PCU,
                  alpha: float = 0.15, bpr_beta: float = 4.0,
                  production_scale=1.0, attraction_scale=1.0, processing_rate=None,
                  incident_sweep=(1, 2, 3), verbose: bool = True):
    """Simulate every case on fixed demand. Returns (summary_df, cases dict).

    BPR (alpha/bpr_beta) and demand robustness params (production_scale,
    attraction_scale, processing_rate) let the whole sweep be re-run under
    different calibrations / flow regimes.
    """
    G0 = load_enriched_graph()
    zones, _pT, veh_T, _C = build_od(
        beta=beta, G=G0, target_total_pcu=total_pcu,
        production_scale=production_scale, attraction_scale=attraction_scale,
        processing_rate=processing_rate)
    pairs = od_to_pairs(zones, veh_T)  # fixed demand across all scenarios

    def _run(H):
        # Tight tolerance so scenario ΔTSTT is well below the smallest effect we report.
        r = assign(H, pairs, alpha=alpha, beta=bpr_beta, max_iter=250, tol=0.001, verbose=False)
        return r, metrics.link_table(H, r)

    summaries = []
    cases = {}

    # Corridor OD for the driver-experienced through-time metric (Dahisar -> Bandra).
    corridor_od = (int(zones.iloc[0]["connector_node"]), int(zones.iloc[-1]["connector_node"]))

    # --- Base ---
    if verbose:
        print("[evaluate] Base case ...")
    res0, df0 = _run(G0)
    base_tstt = metrics.tstt_hours(res0)
    row0, base_corr = _summarize("base", "Current network + demand", G0, res0, df0,
                                 None, corridor_od, None)
    summaries.append(row0)
    cases["base"] = (G0, res0, df0)

    def _add(label, desc, H, res, df, extra=None):
        row, _ = _summarize(label, desc, H, res, df, base_tstt, corridor_od, base_corr)
        if extra:
            row.update(extra)
        summaries.append(row)
        cases[label] = (H, res, df)

    # Target link for A/C = worst arterial bottleneck in the base case.
    tu, tv, tk, top = _worst_arterial_link(G0, df0)
    tgt = f"{top['name'] or top['highway']} ({tu}->{tv})"
    if verbose:
        print(f"[evaluate] Worst bottleneck: {tgt}  V/C={top['vc_ratio']} lanes={top['lanes']}")

    # --- Scenario A: widen worst bottleneck (+1 lane) ---
    Ga = scn.widen_link(G0, tu, tv, tk, add_lanes=1)
    _add("A_widen", f"Widen {tgt} +1 lane", Ga, *_run(Ga))

    # --- Scenario B: add a bypass link end-to-end ---
    Gb = scn.add_link(G0, corridor_od[0], corridor_od[1], lanes=2, highway="primary", speed_kph=60)
    _add("B_addlink", "Add Dahisar-Bandra bypass link", Gb, *_run(Gb))

    # --- Scenario C: close the worst bottleneck link ---
    Gc = scn.remove_link(G0, tu, tv)
    _add("C_close", f"Close {tgt}", Gc, *_run(Gc))

    # --- Scenario D: stopped-vehicle incident SWEEP on a WEH mainline stretch ---
    stretch = _weh_incident_stretch(df0, k=6)
    if verbose:
        print(f"[evaluate] Incident stretch: {len(stretch)} WEH links")
    for n in incident_sweep:
        Gd = G0
        for (iu, iv, ik) in stretch:
            Gd = scn.set_incident(Gd, iu, iv, n_stopped=n, k=ik)
        res_d, df_d = _run(Gd)
        q = _incident_queue(stretch, df0, df_d)
        _add(f"D_incident_N{n}", f"{n} stopped veh on WEH stretch ({len(stretch)} links)",
             Gd, res_d, df_d, extra=q)

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
