"""
calibration.py — calibrate BPR alpha/beta against observed TomTom TTI.

The BPR function predicts a link's travel-time index:
    TTI_model = t/t0 = 1 + alpha * (v/C)^beta

TomTom Flow Segment Data gives an OBSERVED TTI per sampled point:
    TTI_obs = freeFlowSpeed / currentSpeed   (== congested/free-flow travel time)

Procedure:
  1. Run the base UE assignment -> modelled v/C per link.
  2. Match each observed TomTom point to its nearest network link -> (v/C, TTI_obs).
  3. Least-squares fit alpha, beta to TTI_obs = 1 + alpha (v/C)^beta.
  4. (Optional) re-run assignment with the fitted params and refit once (the
     equilibrium flows depend on alpha/beta, so one refinement pass reduces bias).

Caveats: the current collected data is an evening/off-peak snapshot (mean TTI ~1.3),
so this is a PRELIMINARY calibration — recollect at AM/PM peak for a stronger signal.
The demand scale (TARGET_TOTAL_PCU) also affects v/C and should be jointly calibrated;
here we hold demand fixed and fit only the BPR shape.

Usage:
    python -m src.demand.calibration
    python -m src.demand.calibration --csv data/raw/tomtom/collected/flow_evening_XXXX.csv
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import numpy as np
import osmnx as ox
import pandas as pd
from scipy.optimize import curve_fit

from src.assignment.run_assignment import run_base
from src.assignment import metrics

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTED_DIR = REPO_ROOT / "data" / "raw" / "tomtom" / "collected"


def _latest_csv() -> Path:
    files = sorted(glob.glob(str(COLLECTED_DIR / "flow_*.csv")))
    if not files:
        raise FileNotFoundError("No collected flow CSVs — run src.data.collect_flow first.")
    return Path(files[-1])


def _all_csvs() -> list[Path]:
    files = sorted(glob.glob(str(COLLECTED_DIR / "flow_*.csv")))
    if not files:
        raise FileNotFoundError("No collected flow CSVs — run src.data.collect_flow first.")
    return [Path(f) for f in files]


def match_observations(G, link_df: pd.DataFrame, flow_csvs) -> pd.DataFrame:
    """Attach each observed TomTom point (pooled over one or more CSVs) to its
    nearest link's modelled v/C."""
    if isinstance(flow_csvs, (str, Path)):
        flow_csvs = [flow_csvs]
    frames = [pd.read_csv(c) for c in flow_csvs]
    obs = pd.concat(frames, ignore_index=True)
    obs = obs[(obs["tti"].notna()) & (obs["tti"] > 0)]

    # Nearest edges for all observed points. osmnx 2.x returns an array of (u,v,k) rows.
    nearest = ox.nearest_edges(G, obs["lon"].to_numpy(), obs["lat"].to_numpy())

    vc_by_edge = {(int(r.u), int(r.v), int(r.key)): r.vc_ratio
                  for r in link_df.itertuples(index=False)}
    rows = []
    for edge, tti in zip(nearest, obs["tti"].to_numpy()):
        u, v, k = int(edge[0]), int(edge[1]), int(edge[2])
        vc = vc_by_edge.get((u, v, k))
        if vc is not None and vc > 0:
            rows.append({"u": u, "v": v, "key": k, "vc": vc, "tti_obs": float(tti)})
    return pd.DataFrame(rows)


def _bpr_tti(vc, alpha, beta):
    return 1.0 + alpha * np.power(vc, beta)


def fit_bpr(matched: pd.DataFrame):
    """Least-squares fit of (alpha, beta). Returns (alpha, beta, rmse)."""
    x = matched["vc"].to_numpy(float)
    y = matched["tti_obs"].to_numpy(float)
    # Bounds keep parameters physical: alpha in [0.01, 2], beta in [1, 8].
    popt, _ = curve_fit(_bpr_tti, x, y, p0=[0.15, 4.0],
                        bounds=([0.01, 1.0], [2.0, 8.0]), maxfev=10000)
    alpha, beta = popt
    rmse = float(np.sqrt(np.mean((_bpr_tti(x, alpha, beta) - y) ** 2)))
    return float(alpha), float(beta), rmse


def calibrate(flow_csv=None, total_pcu: float = 18000.0, passes: int = 2):
    """Run the calibration loop, pooling ALL collected snapshots by default.

    flow_csv: a single path, a list of paths, or None (pool every collected CSV).
    """
    if flow_csv is None:
        flow_csvs = _all_csvs()
    elif isinstance(flow_csv, (str, Path)):
        flow_csvs = [Path(flow_csv)]
    else:
        flow_csvs = [Path(c) for c in flow_csv]
    alpha, beta = 0.15, 4.0
    history = []

    for p in range(passes):
        G, zones, res, df = run_base(beta=2.0, total_pcu=total_pcu,
                                     max_iter=250, tol=0.001, verbose=False)
        # Re-run assignment with the current BPR params (pass 0 uses defaults).
        if p > 0:
            from src.assignment.frank_wolfe import assign
            from src.demand.gravity_model import build_od, od_to_pairs
            _z, _pt, vehT, _c = build_od(beta=2.0, G=G, target_total_pcu=total_pcu)
            pairs = od_to_pairs(zones, vehT)
            res = assign(G, pairs, alpha=alpha, beta=beta, max_iter=250, tol=0.001, verbose=False)
            df = metrics.link_table(G, res)

        matched = match_observations(G, df, flow_csvs)
        if len(matched) < 4:
            raise RuntimeError(f"Only {len(matched)} matched observations — insufficient to fit.")
        alpha, beta, rmse = fit_bpr(matched)
        history.append({"pass": p, "alpha": round(alpha, 4), "beta": round(beta, 3),
                        "rmse": round(rmse, 4), "n_matched": len(matched)})

    return {
        "flow_csv": ", ".join(c.name for c in flow_csvs),
        "alpha": round(alpha, 4),
        "beta": round(beta, 3),
        "rmse": round(rmse, 4),
        "n_matched": len(matched),
        "history": history,
        "matched": matched,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate BPR alpha/beta to TomTom TTI.")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--total", type=float, default=18000.0)
    args = parser.parse_args()

    csv = Path(args.csv) if args.csv else None
    r = calibrate(csv, total_pcu=args.total)
    print(f"Calibration against {r['flow_csv']}  ({r['n_matched']} matched points)\n")
    print("Default BPR:    alpha=0.15, beta=4.0")
    print(f"Calibrated BPR: alpha={r['alpha']}, beta={r['beta']}   (RMSE {r['rmse']} on TTI)")
    print("\nPass history:")
    for h in r["history"]:
        print(f"  pass {h['pass']}: alpha={h['alpha']} beta={h['beta']} "
              f"rmse={h['rmse']} n={h['n_matched']}")
    obs = r["matched"]
    print(f"\nObserved TTI range: {obs['tti_obs'].min():.2f}-{obs['tti_obs'].max():.2f}, "
          f"modelled v/C range: {obs['vc'].min():.2f}-{obs['vc'].max():.2f}")


if __name__ == "__main__":
    main()
