"""
dashboard.py — Phase 5: summary comparison chart across all simulated cases.

Reads data/processed/scenario_comparison.csv and renders a TSTT / ΔTSTT bar chart
so every case is directly comparable at a glance (plan §Phase 5 "simulate all cases").
"""

from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPARISON_CSV = REPO_ROOT / "data" / "processed" / "scenario_comparison.csv"
DOCS = REPO_ROOT / "docs"


def plot_comparison(csv_path: Path = COMPARISON_CSV,
                    out_path: Path = DOCS / "scenario_comparison.png") -> Path:
    df = pd.read_csv(csv_path)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 6))

    # --- TSTT by case ---
    colors = ["#264653" if c == "base" else
              ("#e63946" if v > 0 else "#2a9d8f")
              for c, v in zip(df["case"], df["dTSTT_vs_base"])]
    ax1.bar(df["case"], df["TSTT_pcu_h"], color=colors)
    ax1.axhline(df.loc[df["case"] == "base", "TSTT_pcu_h"].iloc[0],
                color="#264653", ls="--", lw=1, label="base TSTT")
    ax1.set_ylabel("TSTT (PCU·hours)")
    ax1.set_title("Total System Travel Time by case")
    ax1.tick_params(axis="x", rotation=45)
    ax1.legend()

    # --- ΔTSTT % vs base ---
    dfx = df[df["case"] != "base"]
    dcolors = ["#e63946" if v > 0 else "#2a9d8f" for v in dfx["dTSTT_pct"]]
    ax2.barh(dfx["case"], dfx["dTSTT_pct"], color=dcolors)
    ax2.axvline(0, color="black", lw=0.8)
    ax2.set_xlabel("ΔTSTT vs base (%)   — green = improvement, red = worse")
    ax2.set_title("Intervention impact (fixed demand)")
    ax2.invert_yaxis()

    fig.suptitle("WEH corridor — all-cases simulation (assumes fixed demand)", fontsize=13)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out_path


if __name__ == "__main__":
    out = plot_comparison()
    print(f"[dashboard] Rendered scenario comparison -> {out}")
