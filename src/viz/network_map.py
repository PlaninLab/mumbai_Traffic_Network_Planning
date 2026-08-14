"""
network_map.py — corridor visualizations (Phase 5, early cut).

Currently provides a congestion-snapshot plot: the enriched network in grey with
the collected TomTom flow-sample points overlaid, colored by Travel-Time Index
(TTI = freeFlowSpeed / currentSpeed; higher = more congested).

Usage:
    python -m src.viz.network_map --csv data/raw/tomtom/collected/flow_evening_XXXX.csv
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import osmnx as ox
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
ENRICHED = REPO_ROOT / "data" / "processed" / "network_corridor_enriched.graphml"
COLLECTED_DIR = REPO_ROOT / "data" / "raw" / "tomtom" / "collected"
DOCS = REPO_ROOT / "docs"


def _tti_color(tti: float) -> str:
    if tti is None or tti < 1.2:
        return "#2a9d8f"   # green — free-flowing
    if tti < 1.5:
        return "#e9c46a"   # yellow — building
    if tti < 2.0:
        return "#f4a261"   # orange — congested
    return "#e63946"       # red — severe


def latest_csv() -> Path:
    files = sorted(glob.glob(str(COLLECTED_DIR / "flow_*.csv")))
    if not files:
        raise FileNotFoundError("No collected flow CSVs — run src.data.collect_flow first.")
    return Path(files[-1])


def plot_snapshot(csv_path: Path, out_path: Path) -> Path:
    G = ox.load_graphml(ENRICHED)
    df = pd.read_csv(csv_path)

    fig, ax = ox.plot_graph(
        G, node_size=0, edge_color="#cfd2d6", edge_linewidth=0.4,
        bgcolor="white", show=False, close=False, figsize=(7, 13),
    )
    colors = [_tti_color(t) for t in df["tti"]]
    ax.scatter(df["lon"], df["lat"], c=colors, s=42, edgecolors="black",
               linewidths=0.4, zorder=5)

    label = df["label"].iloc[0] if "label" in df and len(df) else "snapshot"
    mean_tti = df["tti"].mean()
    ax.set_title(f"WEH corridor — TomTom flow snapshot ({label})\n"
                 f"mean TTI = {mean_tti:.2f}   (green<1.2  yellow<1.5  orange<2.0  red≥2.0)",
                 color="black", fontsize=10)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _vc_color(vc: float) -> str:
    if vc is None:
        return "#cfd2d6"
    if vc < 0.7:
        return "#2a9d8f"   # green
    if vc < 0.9:
        return "#e9c46a"   # yellow
    if vc < 1.0:
        return "#f4a261"   # orange
    return "#e63946"       # red — over capacity


def plot_vc_map(G, link_df, out_path: Path, title: str = "V/C") -> Path:
    """Plot the network colored by post-assignment V/C ratio (loaded links only)."""
    import matplotlib.pyplot as plt

    # Map (u,v,key) -> vc for quick edge lookup.
    vc_by_edge = {(int(r.u), int(r.v), int(r.key)): r.vc_ratio
                  for r in link_df.itertuples(index=False)}
    ec = []
    for u, v, k in G.edges(keys=True):
        vc = vc_by_edge.get((u, v, k))
        # Only color links that actually carry flow; others faint grey.
        ec.append(_vc_color(vc) if (vc is not None and vc > 0.01) else "#e8e8e8")

    fig, ax = ox.plot_graph(
        G, node_size=0, edge_color=ec, edge_linewidth=0.8,
        bgcolor="white", show=False, close=False, figsize=(7, 13),
    )
    ax.set_title(f"{title}\n(green<0.7  yellow<0.9  orange<1.0  red≥1.0 over capacity)",
                 color="black", fontsize=10)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot a TomTom congestion snapshot over the corridor.")
    parser.add_argument("--csv", default=None, help="Flow CSV (default: latest in collected/).")
    parser.add_argument("--out", default=str(DOCS / "corridor_congestion_snapshot.png"))
    args = parser.parse_args()

    csv_path = Path(args.csv) if args.csv else latest_csv()
    out = plot_snapshot(csv_path, Path(args.out))
    print(f"[network_map] Rendered {csv_path.name} -> {out}")


if __name__ == "__main__":
    main()
