"""
robustness.py — re-run the all-cases simulation under different calibrations.

Answers the sensitivity question (plan §6.3): do the intervention conclusions hold
when we change the BPR calibration and the regional in/out flow rates? If the RANK
ORDER of scenarios (by ΔTSTT) is stable across configurations, the model is
producing structurally useful results even though absolute numbers are approximate.

Configurations swept:
  - default      : BPR alpha=0.15, beta=4 (US defaults), demand 18k PCU/h
  - calibrated   : BPR fitted to the TomTom evening snapshot (alpha=0.183, beta=1.0)
  - low_flow     : lower ingoing/outgoing rate (demand 12k PCU/h)
  - high_flow    : higher ingoing/outgoing rate (demand 24k PCU/h)
  - capped_proc  : regional processing-rate ceiling (zones capped at 60k person-trips/hr)

For each config the full case set (base + A/B/C + incident N=1..3) is simulated;
we tabulate ΔTSTT% per scenario per config and plot them side by side.

Usage:
    python -m src.scenarios.robustness
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.scenarios.evaluate import run_all_cases

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
DOCS = REPO_ROOT / "docs"

# BPR params fitted by src.demand.calibration against the evening TomTom snapshot.
CALIBRATED_ALPHA, CALIBRATED_BETA = 0.183, 1.0

CONFIGS = [
    {"name": "default",     "alpha": 0.15,  "bpr_beta": 4.0, "total_pcu": 18000},
    {"name": "calibrated",  "alpha": CALIBRATED_ALPHA, "bpr_beta": CALIBRATED_BETA, "total_pcu": 18000},
    {"name": "low_flow",    "alpha": 0.15,  "bpr_beta": 4.0, "total_pcu": 12000},
    {"name": "high_flow",   "alpha": 0.15,  "bpr_beta": 4.0, "total_pcu": 24000},
    {"name": "capped_proc", "alpha": 0.15,  "bpr_beta": 4.0, "total_pcu": 18000, "processing_rate": 60000},
]


def run_sweep(configs=CONFIGS):
    """Run every config; return a tidy DataFrame of case x config -> metrics."""
    frames = []
    for cfg in configs:
        name = cfg["name"]
        print(f"\n===== CONFIG: {name} =====")
        summary, _cases, _tgt = run_all_cases(
            alpha=cfg.get("alpha", 0.15), bpr_beta=cfg.get("bpr_beta", 4.0),
            total_pcu=cfg.get("total_pcu", 18000),
            processing_rate=cfg.get("processing_rate"),
            verbose=False,
        )
        summary["config"] = name
        frames.append(summary[["config", "case", "dTSTT_pct", "dCorridor_pct",
                                "max_vc", "TSTT_pcu_h"]])
        print(summary[["case", "dTSTT_pct", "dCorridor_pct", "max_vc"]].to_string(index=False))
    return pd.concat(frames, ignore_index=True)


def plot_robustness(tidy: pd.DataFrame, out_path: Path = DOCS / "robustness_sweep.png") -> Path:
    """Grouped bars: ΔTSTT% per scenario across configs (base excluded)."""
    scen = tidy[tidy["case"] != "base"].copy()
    cases = list(dict.fromkeys(scen["case"]))
    configs = list(dict.fromkeys(scen["config"]))

    fig, ax = plt.subplots(figsize=(13, 6))
    width = 0.8 / len(configs)
    x = np.arange(len(cases))
    for i, cfg in enumerate(configs):
        vals = [scen[(scen["case"] == c) & (scen["config"] == cfg)]["dTSTT_pct"].values
                for c in cases]
        vals = [v[0] if len(v) else 0 for v in vals]
        ax.bar(x + i * width, vals, width, label=cfg)
    ax.set_xticks(x + width * (len(configs) - 1) / 2)
    ax.set_xticklabels(cases, rotation=30, ha="right")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("ΔTSTT vs base (%)")
    ax.set_title("Robustness: intervention impact across calibrations & flow regimes")
    ax.legend(title="config", ncol=len(configs))
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out_path


def rank_stability(tidy: pd.DataFrame) -> pd.DataFrame:
    """Rank scenarios by ΔTSTT within each config; show rank per config to check stability."""
    scen = tidy[tidy["case"] != "base"].copy()
    scen["rank"] = scen.groupby("config")["dTSTT_pct"].rank(method="min")
    pivot = scen.pivot(index="case", columns="config", values="rank")
    return pivot


def main() -> None:
    tidy = run_sweep()
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tidy.to_csv(PROCESSED_DIR / "robustness_sweep.csv", index=False)

    print("\n\n===== ΔTSTT% pivot (case x config) =====")
    pivot = tidy[tidy["case"] != "base"].pivot(index="case", columns="config", values="dTSTT_pct")
    print(pivot.to_string())

    print("\n===== Rank stability (1 = most negative ΔTSTT = best) =====")
    print(rank_stability(tidy).to_string())

    out = plot_robustness(tidy)
    print(f"\nSaved robustness chart -> {out}")
    print(f"Saved robustness data  -> {PROCESSED_DIR / 'robustness_sweep.csv'}")


if __name__ == "__main__":
    main()
