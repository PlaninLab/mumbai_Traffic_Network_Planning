"""
observed_queue.py — measured jam length straight from the live traffic readings.

The scenario evaluator reports a *modelled* incident queue (assignment flow vs.
reduced capacity). This module instead measures the queue that is ACTUALLY on the
road right now, from the collected TomTom/HERE speeds — the physical length of the
"orange + red" (congested + severe) part of the corridor.

Method (per reading = one full sweep of the WEH sample points):
  1. Walk the sample points in order (idx 0 = Dahisar … N = Bandra).
  2. For each GAP between consecutive points, take the mean of the two endpoints'
     TTI and the geodesic (haversine) gap length in km.
  3. A gap is "congested" (orange) if its mean TTI >= 1.5, "severe" (red) if >= 2.0.
  4. Sum congested/severe gap lengths, and find the LONGEST CONTIGUOUS run of
     congested gaps — that contiguous run is the physical queue/jam length.

Outputs per reading:
  congested_km   total orange+red length along the corridor
  severe_km      total red (TTI>=2) length
  longest_jam_km longest single contiguous congested stretch  <- the "queue length"
  n_jams         number of distinct congested stretches
  corridor_km    total sampled corridor length (for context)

Aggregated per segment (peak / avg / offpeak): mean & max across readings.

CLI:
    python -m src.data.observed_queue
"""

from __future__ import annotations

import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import numpy as np
import pandas as pd

from src.data import store

REPO_ROOT = Path(__file__).resolve().parents[2]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

CONGESTED_TTI = 1.5   # orange
SEVERE_TTI = 2.0      # red


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0088
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


def reading_queue(pts: pd.DataFrame, congested: float = CONGESTED_TTI,
                  severe: float = SEVERE_TTI) -> dict:
    """Measure jam length for ONE reading. pts columns: idx, lat, lon, tti."""
    pts = pts.sort_values("idx")
    lat = pts["lat"].to_numpy(float)
    lon = pts["lon"].to_numpy(float)
    tti = pts["tti"].to_numpy(float)
    n = len(pts)
    if n < 2:
        return {"corridor_km": 0.0, "congested_km": 0.0, "severe_km": 0.0,
                "longest_jam_km": 0.0, "n_jams": 0}

    gap_len = np.empty(n - 1)
    gap_tti = np.empty(n - 1)
    for i in range(n - 1):
        gap_len[i] = haversine_km(lat[i], lon[i], lat[i + 1], lon[i + 1])
        gap_tti[i] = 0.5 * (tti[i] + tti[i + 1])   # midpoint congestion of the gap

    cong = gap_tti >= congested
    sev = gap_tti >= severe

    # Longest contiguous congested run (the physical queue) + jam count.
    longest = cur = 0.0
    n_jams = 0
    in_jam = False
    for i in range(n - 1):
        if cong[i]:
            cur += gap_len[i]
            if not in_jam:
                n_jams += 1
                in_jam = True
            longest = max(longest, cur)
        else:
            cur = 0.0
            in_jam = False

    return {
        "corridor_km": round(float(gap_len.sum()), 2),
        "congested_km": round(float(gap_len[cong].sum()), 2),
        "severe_km": round(float(gap_len[sev].sum()), 2),
        "longest_jam_km": round(float(longest), 2),
        "n_jams": int(n_jams),
    }


def by_segment(obs: pd.DataFrame) -> dict:
    """Per-segment observed-queue aggregates. `obs` has the reading id in either
    'run_id' or 'source_file', plus idx, lat, lon, tti, segment."""
    rid = "run_id" if "run_id" in obs.columns else "source_file"
    obs = obs[obs["tti"].notna() & (obs["tti"] > 0)]
    out = {}
    for seg_name, g in obs.groupby("segment"):
        per = [reading_queue(rr) for _, rr in g.groupby(rid) if len(rr) >= 2]
        if not per:
            out[seg_name] = {"available": False}
            continue
        df = pd.DataFrame(per)
        # The reading with the single longest jam (the worst observed queue).
        worst = df.loc[df["longest_jam_km"].idxmax()]
        out[seg_name] = {
            "available": True,
            "n_readings": int(len(df)),
            "mean_jam_km": round(float(df["longest_jam_km"].mean()), 2),
            "max_jam_km": round(float(df["longest_jam_km"].max()), 2),
            "mean_congested_km": round(float(df["congested_km"].mean()), 2),
            "mean_severe_km": round(float(df["severe_km"].mean()), 2),
            "corridor_km": round(float(df["corridor_km"].mean()), 2),
            "worst_reading": {
                "longest_jam_km": round(float(worst["longest_jam_km"]), 2),
                "congested_km": round(float(worst["congested_km"]), 2),
                "severe_km": round(float(worst["severe_km"]), 2),
            },
        }
    return out


def _load_obs() -> pd.DataFrame:
    df = store.load_readings_df()
    if df.empty:
        raise FileNotFoundError("No readings in the store — collect data first.")
    return df


def main() -> None:
    obs = _load_obs()
    res = by_segment(obs)
    print("===== OBSERVED QUEUE / JAM LENGTH (from live speeds) =====")
    print(f"(congested = TTI >= {CONGESTED_TTI}, severe = TTI >= {SEVERE_TTI})\n")
    for s in ("peak", "avg", "offpeak"):
        d = res.get(s, {"available": False})
        name = {"peak": "Peak", "avg": "Average", "offpeak": "Off-peak"}[s]
        if not d.get("available"):
            print(f"[{s:7s}] {name}: no readings yet")
            continue
        print(f"[{s:7s}] {name}: {d['n_readings']} readings over ~{d['corridor_km']} km corridor")
        print(f"          longest jam  mean {d['mean_jam_km']} km / max {d['max_jam_km']} km")
        print(f"          congested (orange+red) mean {d['mean_congested_km']} km, "
              f"severe (red) mean {d['mean_severe_km']} km")
    (PROCESSED_DIR / "observed_queue.json").write_text(
        json.dumps(res, indent=2), encoding="utf-8")
    print(f"\nSaved -> {PROCESSED_DIR / 'observed_queue.json'}")


if __name__ == "__main__":
    main()
