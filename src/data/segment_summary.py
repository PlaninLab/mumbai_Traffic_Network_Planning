"""
segment_summary.py — pool collected TomTom readings into the two weekday segments.

Reads every data/raw/tomtom/collected/flow_*.csv, tags each observation with its
segment (peak / avg / offpeak, via src.data.segments), then produces:

  data/processed/segment_<seg>_points.csv   per-sample-point averages (the
        "congested circuits" table: mean TTI + speeds per point along the WEH,
        which also feeds peak-hour OD-matrix calibration)
  data/processed/segment_overview.json      headline stats per segment +
        peak-vs-average comparison (max time-savings potential)

Each WEH sample point has a stable `idx` (0 = Dahisar end ... N = Bandra end), so
averaging by idx gives a per-circuit congestion profile independent of which day a
reading was taken.

Usage:
    python -m src.data.segment_summary
"""

from __future__ import annotations

import glob
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.data import segments as seg
from src.data import store

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTED_DIR = REPO_ROOT / "data" / "raw" / "tomtom" / "collected"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

CONGESTED_TTI = 1.5   # a point at/above this is "congested"


def _load_all() -> pd.DataFrame:
    """Load all readings — from the SQLite store if populated, else pooled CSVs."""
    if store.has_data():
        obs = store.load_readings_df().rename(columns={
            "current_speed_kph": "currentSpeed_kph",
            "free_speed_kph": "freeFlowSpeed_kph",
            "run_id": "source_file",   # so n_readings = distinct runs
        })
    else:
        files = sorted(glob.glob(str(COLLECTED_DIR / "flow_*.csv")))
        if not files:
            raise FileNotFoundError(
                "No readings — run src.data.collect_flow (or collect_segment) first.")
        frames = []
        for f in files:
            df = pd.read_csv(f)
            df["source_file"] = Path(f).name
            frames.append(df)
        obs = pd.concat(frames, ignore_index=True)

    obs = obs[obs["tti"].notna() & (obs["tti"] > 0)].copy()
    # Ensure a segment tag: keep an existing one, else classify from fetched_utc.
    if "segment" in obs.columns and obs["segment"].notna().any():
        obs["segment"] = obs["segment"].fillna(
            obs["fetched_utc"].map(seg.classify_utc_iso))
    else:
        obs["segment"] = obs["fetched_utc"].map(seg.classify_utc_iso)
    return obs


def _per_point(obs_seg: pd.DataFrame) -> pd.DataFrame:
    """Average each WEH sample point (by idx) across all readings in the segment."""
    g = obs_seg.groupby("idx")
    pts = g.agg(
        lat=("lat", "mean"),
        lon=("lon", "mean"),
        mean_tti=("tti", "mean"),
        max_tti=("tti", "max"),
        mean_current_kph=("currentSpeed_kph", "mean"),
        mean_free_kph=("freeFlowSpeed_kph", "mean"),
        n_obs=("tti", "size"),
    ).reset_index()
    return pts.sort_values("idx").round(3)


def _segment_stats(obs_seg: pd.DataFrame, pts: pd.DataFrame) -> dict:
    ttis = obs_seg["tti"]
    mean_tti = float(ttis.mean())
    # Delay share of travel time = fraction of the current trip time that is delay.
    # (t/t0 = TTI, so delay fraction = 1 - 1/TTI). This is the "max time saving"
    # achievable if this segment were restored to free-flow.
    max_saving_vs_freeflow_pct = round((1.0 - 1.0 / mean_tti) * 100, 1) if mean_tti else 0.0
    return {
        "n_readings": int(obs_seg["source_file"].nunique()),
        "n_points": int(len(pts)),
        "n_obs": int(len(obs_seg)),
        "mean_tti": round(mean_tti, 3),
        "median_tti": round(float(ttis.median()), 3),
        "max_tti": round(float(ttis.max()), 3),
        "mean_current_kph": round(float(obs_seg["currentSpeed_kph"].mean()), 1),
        "mean_free_kph": round(float(obs_seg["freeFlowSpeed_kph"].mean()), 1),
        "delay_penalty_pct": round((mean_tti - 1.0) * 100, 1),
        "max_saving_vs_freeflow_pct": max_saving_vs_freeflow_pct,
        "congested_points": int((pts["mean_tti"] >= CONGESTED_TTI).sum()),
        # Top congested circuits (worst mean-TTI points) for the map/table.
        "worst_circuits": [
            {"idx": int(r.idx), "lat": round(float(r.lat), 5), "lon": round(float(r.lon), 5),
             "mean_tti": round(float(r.mean_tti), 2),
             "mean_current_kph": round(float(r.mean_current_kph), 1)}
            for r in pts.sort_values("mean_tti", ascending=False).head(5).itertuples()
        ],
    }


def build_summary() -> dict:
    obs = _load_all()
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "congested_tti_threshold": CONGESTED_TTI,
        "segments": {},
    }
    seg_stats = {}
    for s in ("peak", "avg", "offpeak"):
        obs_s = obs[obs["segment"] == s]
        meta = seg.SEGMENTS[s]
        if obs_s.empty:
            out["segments"][s] = {**meta, "available": False,
                                  "note": "No readings collected in this segment yet."}
            continue
        pts = _per_point(obs_s)
        pts.to_csv(PROCESSED_DIR / f"segment_{s}_points.csv", index=False)
        stats = _segment_stats(obs_s, pts)
        seg_stats[s] = stats
        out["segments"][s] = {**meta, "available": True, **stats}

    # Peak-vs-average comparison: how much of peak delay is peak-specific, i.e. the
    # time saving achievable by smoothing peak down to the average-delayed state.
    if "peak" in seg_stats and "avg" in seg_stats:
        pk, av = seg_stats["peak"]["mean_tti"], seg_stats["avg"]["mean_tti"]
        out["peak_vs_avg"] = {
            "peak_mean_tti": pk,
            "avg_mean_tti": av,
            "extra_peak_delay_pct": round((pk - av) * 100, 1),
            # If peak trips moved at the average-delay speed instead of peak speed:
            "time_saving_peak_to_avg_pct": round((1.0 - av / pk) * 100, 1) if pk else 0.0,
        }
    else:
        out["peak_vs_avg"] = {
            "note": "Need at least one weekday PEAK and one weekday AVG reading to compare."
        }

    with (PROCESSED_DIR / "segment_overview.json").open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    return out


def _fmt(v):
    return "—" if v is None else v


def main() -> None:
    out = build_summary()
    print("===== WEEKDAY SEGMENT SUMMARY =====")
    for s in ("peak", "avg", "offpeak"):
        d = out["segments"][s]
        print(f"\n[{s}] {d['name']}  ({d['windows_ist']})")
        if not d.get("available"):
            print(f"  {d.get('note')}")
            continue
        print(f"  readings={d['n_readings']} points={d['n_points']} obs={d['n_obs']}")
        print(f"  mean TTI={d['mean_tti']}  max TTI={d['max_tti']}  "
              f"congested points={d['congested_points']}")
        print(f"  mean speed={d['mean_current_kph']} kph (free {d['mean_free_kph']})  "
              f"delay penalty={d['delay_penalty_pct']}%")
        print(f"  max saving vs free-flow={d['max_saving_vs_freeflow_pct']}%")
        if d["worst_circuits"]:
            worst = ", ".join(f"idx{c['idx']}(TTI{c['mean_tti']})" for c in d["worst_circuits"])
            print(f"  worst circuits: {worst}")

    pva = out["peak_vs_avg"]
    print("\n[peak vs average]")
    if "note" in pva:
        print(f"  {pva['note']}")
    else:
        print(f"  peak TTI={pva['peak_mean_tti']}  avg TTI={pva['avg_mean_tti']}")
        print(f"  extra peak delay={pva['extra_peak_delay_pct']}%  "
              f"time saving peak->avg={pva['time_saving_peak_to_avg_pct']}%")
    print(f"\nSaved -> {PROCESSED_DIR / 'segment_overview.json'}")


if __name__ == "__main__":
    main()
