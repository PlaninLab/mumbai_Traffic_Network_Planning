"""
collect_flow.py — Sample TomTom Flow Segment data along the WEH corridor.

Samples ~N points evenly along the Western Express Highway spine (Dahisar -> Bandra)
extracted from the OSM graph, queries TomTom Flow Segment Data for each (cached),
and writes a per-run summary CSV.

Each row: point lat/lon, currentSpeed, freeFlowSpeed, TTI (=freeFlow/current speed),
confidence, and a UTC + local timestamp. Because every raw response is cached by
tomtom_client, re-running only pays for genuinely new (point, time) combinations.

Run this at different times of day to capture the congestion cycle:
    python -m src.data.collect_flow --n 50 --label evening
    python -m src.data.collect_flow --n 50 --label am_peak   # 8-10 AM
    python -m src.data.collect_flow --n 50 --label pm_peak   # 6-8 PM

Outputs: data/raw/tomtom/collected/flow_<label>_<timestamp>.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx
import osmnx as ox
from shapely.geometry import LineString

from src.data import tomtom_client as tt
from src.data import here_client
from src.data import budget, incidents
from src.data import segments as seg
from src.data import store

REPO_ROOT = Path(__file__).resolve().parents[2]
GRAPHML = REPO_ROOT / "data" / "raw" / "osm" / "corridor.graphml"
OUT_DIR = REPO_ROOT / "data" / "raw" / "tomtom" / "collected"

# Corridor endpoints (lat, lon).
DAHISAR = (19.250, 72.856)   # northern end
BANDRA = (19.055, 72.840)    # southern end


def weh_spine_points(n_points: int) -> list[tuple[float, float]]:
    """Return n_points (lat, lon) evenly spaced along the WEH spine in the graph.

    The fastest path Dahisar->Bandra follows the WEH (it is the fastest road),
    so weighting by travel_time keeps sample points on the highway itself.
    """
    G = ox.load_graphml(GRAPHML)
    orig = ox.nearest_nodes(G, X=DAHISAR[1], Y=DAHISAR[0])
    dest = ox.nearest_nodes(G, X=BANDRA[1], Y=BANDRA[0])
    route = nx.shortest_path(G, orig, dest, weight="travel_time")

    # Build a single LineString from the route edge geometries (fallback: node coords).
    coords: list[tuple[float, float]] = []  # (lon, lat)
    for u, v in zip(route[:-1], route[1:]):
        data = min(G.get_edge_data(u, v).values(), key=lambda d: d.get("length", 1e9))
        geom = data.get("geometry")
        if geom is not None:
            pts = list(geom.coords)
        else:
            pts = [(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])]
        if coords and pts and coords[-1] == pts[0]:
            pts = pts[1:]
        coords.extend(pts)

    line = LineString(coords)
    # Sample evenly by normalized distance along the line.
    samples = []
    for i in range(n_points):
        frac = i / (n_points - 1) if n_points > 1 else 0.0
        pt = line.interpolate(frac, normalized=True)
        samples.append((pt.y, pt.x))  # (lat, lon)
    return samples


def _flow_reading(provider: str, lat: float, lon: float):
    """Return (current_kph, free_kph, confidence, road_closure) from the chosen
    provider, normalised so the rest of the pipeline is provider-agnostic.

    use_cache=False is REQUIRED here. The provider cache identity is the sample
    POINT only (lat/lon[/radius]) with no time component, so a cached read would
    replay the first response for that point at every later reading — a full day
    of collection would then record one frozen speed per point. The raw response
    is still archived to data/raw/<provider>/, and every reading is kept in the
    per-run CSV and the SQLite store, so nothing is lost by skipping the read.
    """
    if provider == "here":
        r = here_client.flow_point(lat, lon, use_cache=False)
        return r["current_kph"], r["free_kph"], r.get("confidence"), r.get("road_closure")
    # default: TomTom Flow Segment
    d = tt.flow_segment(f"{lat:.5f},{lon:.5f}", use_cache=False)
    return d.get("currentSpeed"), d.get("freeFlowSpeed"), d.get("confidence"), d.get("roadClosure")


def collect(n_points: int, label: str, segment: str | None = None,
            provider: str = "tomtom", max_calls_month: int | None = None,
            request_pause: float = 0.0, latch_after: int | None = None) -> Path:
    # The hard stop wins over everything: no reservation, no graph load, no calls.
    # (Named `latch`, not `lat` — `lat` is the latitude in the sweep loop below.)
    latch = incidents.latch_state(provider)
    if latch["latched"]:
        raise incidents.ProviderError(
            f"{provider} collection is STOPPED since {latch['latched_utc'][:19]} UTC — "
            f"{latch['latch_reason']}. Resume it before collecting again.",
            kind="latched")

    # Reserve the whole sweep against the monthly cap BEFORE doing any work —
    # before the graph load, before the first request. A sweep that dies partway
    # keeps its reservation, which is what bounds a restart loop (see budget.py).
    limit = budget.resolve_limit(provider, max_calls_month)
    reserved = budget.reserve(provider, n_points, limit)   # raises BudgetExhausted
    if limit:
        print(f"[collect_flow] Budget: {reserved:,}/{limit:,} {provider} calls used "
              f"this month ({limit - reserved:,} left).")

    points = weh_spine_points(n_points)
    now_utc = datetime.now(timezone.utc)
    ts = now_utc.astimezone().strftime("%Y%m%d_%H%M")
    # If not explicitly tagged, record the segment this reading actually falls into.
    segment = segment or seg.classify(now_utc)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"flow_{label}_{ts}.csv"

    rows = []
    issued = 0          # requests actually sent — what we are willing to pay for
    provider_fails = 0  # consecutive provider-level failures
    print(f"[collect_flow] Sampling {n_points} points along WEH ({label}, provider={provider}) ...")
    for i, (lat, lon) in enumerate(points):
        point = f"{lat:.5f},{lon:.5f}"
        try:
            cur, free, conf, rc = _flow_reading(provider, lat, lon)
            issued += 1
            provider_fails = 0
        except incidents.ProviderError as e:
            # The provider itself is refusing or unreachable. Hammering the
            # remaining points cannot help and spends the cap, so stop the sweep.
            issued += 0 if e.kind == "network" else 1
            provider_fails += 1
            print(f"  [{i:>2}] {point}  {e.kind.upper()}: {e}")
            if e.kind in incidents.ABORT_IMMEDIATELY or \
                    provider_fails >= incidents.ABORT_AFTER_CONSECUTIVE:
                budget.refund(provider, max(0, n_points - issued))
                state = incidents.record(provider, e, requests_issued=issued,
                                         failed_calls=provider_fails,
                                         latch_after=latch_after)
                tail = (f"; STOPPED — {state['latch_reason']}" if state["latched"]
                        else f"; holding {state['hold_minutes']} min "
                             f"(failure #{state['consecutive']})")
                raise incidents.ProviderError(
                    f"{e} — aborted after {issued} of {n_points} requests{tail}",
                    kind=e.kind, status=e.status, evidence=e.evidence) from e
            continue
        except Exception as e:  # noqa: BLE001 — one bad point, not a bad provider
            issued += 1
            print(f"  [{i:>2}] {point}  ERROR: {e}")
            continue
        if request_pause and i + 1 < n_points:
            time.sleep(request_pause)
        tti = (free / cur) if cur else None  # travel-time index: >1 means slower than free-flow
        rows.append({
            "idx": i,
            "lat": lat,
            "lon": lon,
            "currentSpeed_kph": cur,
            "freeFlowSpeed_kph": free,
            "tti": round(tti, 3) if tti else None,
            "confidence": conf,
            "roadClosure": rc,
            "provider": provider,
            "label": label,
            "segment": segment,
            "fetched_utc": now_utc.isoformat(),
        })
        flag = "" if not tti or tti < 1.2 else ("  <-- congested" if tti < 2 else "  <-- SEVERE")
        print(f"  [{i:>2}] {point}  cur={cur} free={free} TTI={tti and round(tti,2)}{flag}")

    if not rows:
        # Every point failed without tripping the provider-abort rule. Give back
        # what was never sent and say so plainly, rather than raising IndexError
        # off rows[0] and leaving the real cause unreported.
        budget.refund(provider, max(0, n_points - issued))
        err = incidents.ProviderError(
            f"{provider}: {n_points} points attempted, none returned usable data.",
            kind="other")
        incidents.record(provider, err, requests_issued=issued,
                         failed_calls=n_points, latch_after=latch_after)
        raise err

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Also persist to the tabular SQLite store (run_id matches the CSV stem).
    run_id = f"{label}_{ts}"
    inserted = store.insert_readings(rows, run_id)
    # A sweep that produced rows means the provider is healthy — clear any hold.
    incidents.record_success(provider)
    if issued < n_points:
        budget.refund(provider, n_points - issued)

    # Quick corridor summary.
    ttis = [r["tti"] for r in rows if r["tti"]]
    if ttis:
        print(f"\n[collect_flow] {len(rows)} points collected -> {out_path}")
        print(f"  Stored {inserted} rows in {store.DB_PATH.name} (run_id={run_id})")
        print(f"  Mean TTI: {sum(ttis)/len(ttis):.2f}   Max TTI: {max(ttis):.2f}")
        congested = [r for r in rows if r["tti"] and r["tti"] >= 1.5]
        print(f"  Congested points (TTI>=1.5): {len(congested)}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect TomTom Flow Segment data along WEH.")
    parser.add_argument("--n", type=int, default=50, help="Number of sample points (default 50).")
    parser.add_argument("--label", default="run", help="Session label (e.g. am_peak, pm_peak, evening).")
    parser.add_argument("--segment", choices=["peak", "avg"], default=None,
                        help="Tag this reading as a weekday planning segment. Guards against "
                             "collecting outside the segment's weekday window (override with --force).")
    parser.add_argument("--force", action="store_true",
                        help="Collect even if the current time is outside the --segment window.")
    parser.add_argument("--provider", choices=["tomtom", "here"], default="tomtom",
                        help="Flow data source (default tomtom; 'here' needs HERE_API_KEY).")
    parser.add_argument("--max-failed-calls", type=int, default=None,
                        help="Failed calls since the last success that trigger a HARD "
                             "STOP needing a manual resume (default 25). Overrides "
                             "<PROVIDER>_FAILURE_LATCH / PROVIDER_FAILURE_LATCH.")
    parser.add_argument("--request-pause", type=float, default=0.0,
                        help="Seconds to wait between consecutive point requests "
                             "(default 0). Spreads a sweep out to stay under a "
                             "per-second rate limit.")
    parser.add_argument("--max-calls-month", type=int, default=None,
                        help="Cap total provider calls per billing month. Overrides "
                             "<PROVIDER>_MONTHLY_CALL_LIMIT / API_MONTHLY_CALL_LIMIT. "
                             "Counts ALL calls, including any free allowance.")
    args = parser.parse_args()

    label = args.label
    if args.segment:
        label = args.label if args.label != "run" else args.segment
        now = seg.ist_now()
        actual = seg.classify(now)
        if actual != args.segment and not args.force:
            print(f"[collect_flow] Refusing to tag '{args.segment}': it is {now:%a %H:%M} IST, "
                  f"which is the '{actual}' window, not '{args.segment}'.")
            print(f"  Expected window: {seg.SEGMENTS[args.segment]['windows_ist']}")
            print("  Re-run inside the window, or pass --force to record anyway.")
            sys.exit(1)
        if actual != args.segment:
            print(f"[collect_flow] --force: tagging as '{args.segment}' despite "
                  f"being in the '{actual}' window.")

    try:
        collect(args.n, label, segment=args.segment, provider=args.provider,
                max_calls_month=args.max_calls_month,
                request_pause=args.request_pause,
                latch_after=args.max_failed_calls)
    except budget.BudgetExhausted as e:
        print(f"[collect_flow] BUDGET STOP — {e}")
        print("  No call was made. Raise the cap, or wait for the month to roll over.")
        sys.exit(2)
    # Must precede any broad handler: ProviderError is a RuntimeError subclass.
    except incidents.ProviderError as e:
        h = incidents.hold_state(args.provider)
        print(f"[collect_flow] PROVIDER STOP — {e}")
        print(f"  Unissued calls refunded. Holding {h['minutes_remaining']} min "
              f"before the next attempt (python -m src.data.incidents --status).")
        sys.exit(3)


if __name__ == "__main__":
    main()
