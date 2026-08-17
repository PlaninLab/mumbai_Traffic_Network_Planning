"""
road_width.py — Phase 0: measured road width -> capacity.

Today the network's capacity comes from GUESSED lane counts (~84% imputed) times a
per-lane rate times a flat 0.85 "encroachment" fudge factor (enrich_attributes.py).
This module replaces that, per link, with MEASURED geometry:

    capacity_nominal  = (total_width_m     / lane_width) * cap_pcu_per_lane   # physical max
    capacity (used)   = (effective_width_m / lane_width) * cap_pcu_per_lane   # usable now
    encroachment_factor = effective_width_m / total_width_m                   # measured, not 0.85

`total_width_m` is the paved carriageway width; `effective_width_m` is the usable
width after permanent obstruction (parking rows, encroachment, narrowing). For
Mumbai's weak-lane-discipline mixed traffic, capacity really is a function of
usable WIDTH rather than a whole number of lanes (IRC:106), so this is both more
accurate and better-posed than counting lanes.

Phase 0 workflow (free, no imagery purchase):
  1. `python -m src.network.road_width --worklist`
        -> writes data/measurements/road_widths_template.csv, the priority
           junctions to measure (WEH + high-class BMC first).
  2. In Google Earth Pro, use the ruler on each junction's carriageway; fill
     `measured_total_width_m` (edge-to-edge paved) and, where you can see it,
     `measured_effective_width_m` (minus parked rows / encroachment). Save the
     filled file as data/measurements/road_widths.csv.
  3. `python -m src.network.road_width --apply`  (or just re-run enrich_attributes)
        -> overrides capacity on the nearest network link to each measured junction.

Measurements automatically flow into the enriched graph: enrich_attributes calls
`apply_if_available()`, a no-op until road_widths.csv has rows.
"""

from __future__ import annotations

import argparse
import json
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

import pandas as pd

from src.network.enrich_attributes import (DEFAULT_CLASS, ENCROACHMENT_FACTOR,
                                           ROAD_DEFAULTS, _base_class)

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_JSON = REPO_ROOT / "data" / "processed" / "map" / "coverage.json"
MEAS_DIR = REPO_ROOT / "data" / "measurements"
TEMPLATE_CSV = MEAS_DIR / "road_widths_template.csv"
MEASUREMENTS_CSV = MEAS_DIR / "road_widths.csv"

STD_LANE_WIDTH_M = 3.5   # IRC urban lane width (matches incident.LANE_WIDTH_M)
# A junction measurement only overrides a link if a network edge is within this
# distance — so measurements outside the current corridor graph are skipped
# (recorded as misses) rather than mis-assigned to a far edge.
MAX_MATCH_M = 250.0


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371008.8
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))

COLUMNS = [
    "target_kind", "target_id", "name", "roads", "classes", "scope", "lat", "lon",
    "priority", "measured_total_width_m", "measured_effective_width_m",
    "lanes_observed", "obstruction_notes", "measured_by", "measured_date",
]


# --------------------------------------------------------------------------- #
# Width -> capacity
# --------------------------------------------------------------------------- #
def capacity_from_width(total_width_m: float, effective_width_m: float | None = None,
                        road_class: str = "primary",
                        lane_width_m: float = STD_LANE_WIDTH_M,
                        cap_pcu_lane: float | None = None) -> dict:
    """Capacity (PCU/h) from measured carriageway width.

    total_width_m      edge-to-edge paved width.
    effective_width_m  usable width after permanent obstruction; if omitted, falls
                       back to total * ENCROACHMENT_FACTOR (0.85) so a total-only
                       measurement is still an improvement over guessed lanes.
    """
    base = _base_class(road_class)
    if cap_pcu_lane is None:
        cap_pcu_lane = ROAD_DEFAULTS.get(base, ROAD_DEFAULTS[DEFAULT_CLASS])["cap_pcu_lane"]

    total = float(total_width_m)
    if effective_width_m is None or float(effective_width_m) <= 0:
        eff = total * ENCROACHMENT_FACTOR
        eff_source = "fallback_0.85"
    else:
        eff = min(float(effective_width_m), total)   # usable can't exceed paved
        eff_source = "measured"

    nominal = (total / lane_width_m) * cap_pcu_lane
    operational = (eff / lane_width_m) * cap_pcu_lane
    return {
        "total_width_m": round(total, 2),
        "effective_width_m": round(eff, 2),
        "effective_width_source": eff_source,
        "encroachment_factor": round(eff / total, 3) if total else None,
        "capacity_nominal_pcu_hr": round(nominal, 1),
        "capacity_pcu_hr": round(operational, 1),
        "cap_pcu_lane": cap_pcu_lane,
    }


# --------------------------------------------------------------------------- #
# Worklist (what to measure)
# --------------------------------------------------------------------------- #
def _priority(j: dict) -> float:
    classes = [c.lower() for c in (j.get("classes") or [])]
    roads = " ".join(j.get("roads") or []).lower()
    if "motorway" in classes or "trunk" in classes:
        score = 0.0
    elif "primary" in classes:
        score = 1.0
    elif "secondary" in classes:
        score = 2.0
    else:
        score = 3.0
    if j.get("in_bmc"):
        score -= 0.5
    if "western express" in roads or "eastern express" in roads:
        score -= 5.0        # the corridors we model come first
    return score


def build_worklist(limit: int = 80, scope: str = "bmc") -> Path:
    """Write the priority measurement template from the junction inventory."""
    if not COVERAGE_JSON.exists():
        raise FileNotFoundError(
            f"{COVERAGE_JSON} not found — build the junction inventory first "
            "(python -m src.viz.map_export / coverage).")
    cov = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    junctions = cov.get("junctions", [])
    if scope in ("bmc", "mmrda"):
        junctions = [j for j in junctions if scope in (j.get("scopes") or [])]

    junctions = sorted(junctions, key=_priority)[:limit]
    rows = []
    for j in junctions:
        rows.append({
            "target_kind": "junction",
            "target_id": j.get("id"),
            "name": j.get("name") or "",
            "roads": "; ".join(j.get("roads") or []),
            "classes": "; ".join(j.get("classes") or []),
            "scope": "bmc" if j.get("in_bmc") else "mmrda",
            "lat": round(float(j["lat"]), 6),
            "lon": round(float(j["lon"]), 6),
            "priority": round(_priority(j), 2),
            "measured_total_width_m": "",
            "measured_effective_width_m": "",
            "lanes_observed": "",
            "obstruction_notes": "",
            "measured_by": "",
            "measured_date": "",
        })
    MEAS_DIR.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=COLUMNS).to_csv(TEMPLATE_CSV, index=False)
    # Seed an empty measurements file (header only) so apply_if_available has a schema.
    if not MEASUREMENTS_CSV.exists():
        pd.DataFrame(columns=COLUMNS).to_csv(MEASUREMENTS_CSV, index=False)
    return TEMPLATE_CSV


# --------------------------------------------------------------------------- #
# Load + apply
# --------------------------------------------------------------------------- #
def load_measurements(path: Path = MEASUREMENTS_CSV) -> pd.DataFrame:
    """Rows with a usable measured total width. Empty DataFrame if none."""
    if not Path(path).exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_csv(path)
    if "measured_total_width_m" not in df.columns:
        return pd.DataFrame(columns=COLUMNS)
    df["measured_total_width_m"] = pd.to_numeric(df["measured_total_width_m"], errors="coerce")
    df["measured_effective_width_m"] = pd.to_numeric(
        df.get("measured_effective_width_m"), errors="coerce")
    return df[df["measured_total_width_m"] > 0].reset_index(drop=True)


def _set_edge_capacity(G, u, v, k, meas: dict) -> None:
    from src.network import incident
    d = G[u][v][k]
    d["measured_total_width_m"] = meas["total_width_m"]
    d["measured_effective_width_m"] = meas["effective_width_m"]
    d["width_source"] = "measured"
    d["encroachment_factor"] = meas["encroachment_factor"]
    d["capacity_nominal_pcu_hr"] = meas["capacity_nominal_pcu_hr"]
    d["capacity_pcu_hr"] = meas["capacity_pcu_hr"]
    mu = incident.incident_capacity_factor(int(d.get("lanes", 1) or 1),
                                           int(d.get("n_stopped", 0) or 0))
    d["capacity_eff_pcu_hr"] = round(meas["capacity_pcu_hr"] * mu, 1)


def apply_measurements(G, measurements: pd.DataFrame | None = None) -> dict:
    """Override capacity on the network from measured widths. For junction rows the
    nearest edge (both directions) is used; for edge rows the u-v-key is used."""
    import osmnx as ox

    if measurements is None:
        measurements = load_measurements()
    if measurements.empty:
        return {"applied": 0, "reason": "no measured rows"}

    applied, misses = 0, 0
    junction_rows = measurements[measurements["target_kind"] != "edge"]
    edge_rows = measurements[measurements["target_kind"] == "edge"]

    # Junctions -> nearest edge (batched).
    if not junction_rows.empty:
        try:
            lons = junction_rows["lon"].to_numpy(float)
            lats = junction_rows["lat"].to_numpy(float)
            nearest = ox.nearest_edges(G, lons, lats)
        except Exception as e:  # noqa: BLE001 — e.g. scikit-learn missing
            return {"applied": 0, "reason": f"nearest_edges failed: {e}"}
        for (_, row), edge in zip(junction_rows.iterrows(), nearest):
            u, v, k = int(edge[0]), int(edge[1]), int(edge[2])
            # Guard: skip if the nearest edge is too far (junction not in this graph).
            dist = min(_haversine_m(row["lat"], row["lon"], G.nodes[n]["y"], G.nodes[n]["x"])
                       for n in (u, v))
            if dist > MAX_MATCH_M:
                misses += 1
                continue
            cls = _base_class(G[u][v][k].get("highway"))
            meas = capacity_from_width(row["measured_total_width_m"],
                                       row.get("measured_effective_width_m"), road_class=cls)
            _set_edge_capacity(G, u, v, k, meas)
            # Apply to the opposite direction too, if the network stores it separately.
            if G.has_edge(v, u):
                for rk in G[v][u]:
                    _set_edge_capacity(G, v, u, rk, meas)
            applied += 1

    # Explicit edges "u-v-key".
    for _, row in edge_rows.iterrows():
        try:
            parts = str(row["target_id"]).split("-")
            u, v, k = int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
        except (ValueError, IndexError):
            misses += 1
            continue
        if not G.has_edge(u, v, k):
            misses += 1
            continue
        cls = _base_class(G[u][v][k].get("highway"))
        meas = capacity_from_width(row["measured_total_width_m"],
                                   row.get("measured_effective_width_m"), road_class=cls)
        _set_edge_capacity(G, u, v, k, meas)
        applied += 1

    return {"applied": applied, "misses": misses, "measured_rows": len(measurements)}


def apply_if_available(G) -> dict:
    """No-op hook for enrich_attributes: apply measurements if any exist."""
    df = load_measurements()
    if df.empty:
        return {"applied": 0, "reason": "no measurements"}
    return apply_measurements(G, df)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 0: measured road width -> capacity.")
    ap.add_argument("--worklist", action="store_true", help="Write the measurement template.")
    ap.add_argument("--limit", type=int, default=80, help="Worklist size (default 80).")
    ap.add_argument("--scope", default="bmc", choices=["bmc", "mmrda", "all"],
                    help="Junction scope for the worklist (default bmc).")
    ap.add_argument("--status", action="store_true", help="Show how many measurements are filled in.")
    ap.add_argument("--apply", action="store_true", help="Apply measurements to the enriched graph.")
    ap.add_argument("--save", action="store_true", help="With --apply, save a measured graphml.")
    ap.add_argument("--demo", action="store_true", help="Show width->capacity for sample widths.")
    args = ap.parse_args()

    if args.demo:
        print("width -> capacity (primary road, cap 1800 PCU/lane, lane 3.5 m):")
        for tw, ew in [(11.0, None), (11.0, 9.0), (7.0, 5.5), (14.0, 10.5)]:
            c = capacity_from_width(tw, ew, road_class="primary")
            print(f"  total={tw:>5} m  eff={c['effective_width_m']:>5} m "
                  f"({c['effective_width_source']:>12}) -> "
                  f"nominal {c['capacity_nominal_pcu_hr']:>6}  usable {c['capacity_pcu_hr']:>6} PCU/h "
                  f"(encroach {c['encroachment_factor']})")
        return

    if args.worklist:
        scope = args.scope if args.scope != "all" else "mmrda"
        p = build_worklist(limit=args.limit, scope=scope)
        print(f"[road_width] Worklist ({args.limit} priority junctions, scope={scope}) -> {p}")
        print(f"[road_width] Measure widths in Google Earth Pro, fill the blank columns,")
        print(f"[road_width] and save as {MEASUREMENTS_CSV}. Then: python -m src.network.road_width --apply")
        return

    if args.status:
        df = load_measurements()
        print(f"[road_width] {len(df)} measured rows in {MEASUREMENTS_CSV}")
        if not df.empty:
            print(df[["target_id", "name", "measured_total_width_m",
                      "measured_effective_width_m"]].to_string(index=False))
        return

    if args.apply:
        import osmnx as ox
        from src.network.graph_io import load_enriched_graph
        G = load_enriched_graph()
        summary = apply_measurements(G)
        print(f"[road_width] {summary}")
        if args.save and summary.get("applied"):
            out = REPO_ROOT / "data" / "processed" / "network_corridor_measured.graphml"
            ox.save_graphml(G, out)
            print(f"[road_width] Saved measured graph -> {out}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
