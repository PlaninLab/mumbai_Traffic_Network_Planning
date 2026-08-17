"""Collect fresh traffic readings for the Greater Mumbai junction inventory.

This collector never fabricates observations. It reads the planned point
inventory from ``data/processed/map/coverage.json``, calls HERE or TomTom for a
bounded batch, and writes only successful provider responses to the dedicated
``intersection_readings`` SQLite table.

MMRDA is the containing scope: ``--scope mmrda`` selects every inventory point;
``--scope bmc`` selects only points whose ``in_bmc`` flag is true. ``--limit``
and ``--offset`` make quota-safe resumable batches.

Examples:
    python -m src.data.collect_intersections --scope bmc --limit 50 --offset 0
    python -m src.data.collect_intersections --scope mmrda --limit 50 --offset 250 \
        --provider tomtom --label pm_peak --segment peak
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.data import budget, here_client, incidents
from src.data import segments as seg
from src.data import store
from src.data import tomtom_client as tt

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_PATH = REPO_ROOT / "data" / "processed" / "map" / "coverage.json"
COVERAGE_SEED_PATH = Path(
    os.environ.get("COVERAGE_SEED_PATH", REPO_ROOT / "data-seed" / "coverage.json")
)
VALID_SCOPES = ("bmc", "mmrda")


def ensure_coverage(
    path: Path = COVERAGE_PATH, seed_path: Path = COVERAGE_SEED_PATH,
) -> Path:
    """Ensure a writable coverage inventory exists on the processed-data volume.

    Existing Docker volumes hide files shipped under ``data/processed`` in a new
    image. The image therefore carries an immutable seed outside that mount; on
    the first regional sweep it is copied into the shared volume. Local builds
    already have ``coverage.json`` and never touch the seed path.
    """
    if path.exists():
        return path
    if not seed_path.exists():
        raise FileNotFoundError(
            f"Junction inventory not found: {path}. Coverage seed also missing: "
            f"{seed_path}. Generate it with `python -m src.network.coverage --download`."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed_path, path)
    print(f"[collect_intersections] Seeded regional inventory -> {path}")
    return path


def _load_coverage(path: Path = COVERAGE_PATH) -> tuple[dict, list[dict]]:
    """Load and validate the real junction inventory before reserving API calls."""
    ensure_coverage(path)
    try:
        coverage = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid junction inventory JSON at {path}: {e}") from e
    if not isinstance(coverage, dict) or not isinstance(coverage.get("junctions"), list):
        raise ValueError(f"{path} must be an object containing a 'junctions' array")

    junctions = coverage["junctions"]
    seen: set[str] = set()
    for i, point in enumerate(junctions):
        if not isinstance(point, dict):
            raise ValueError(f"junctions[{i}] must be an object")
        missing = [key for key in ("id", "lat", "lon", "in_bmc") if key not in point]
        if missing:
            raise ValueError(f"junctions[{i}] is missing: {', '.join(missing)}")
        point_id = str(point["id"]).strip()
        if not point_id:
            raise ValueError(f"junctions[{i}].id must not be empty")
        if point_id in seen:
            raise ValueError(f"duplicate junction id in coverage inventory: {point_id}")
        seen.add(point_id)
        try:
            lat, lon = float(point["lat"]), float(point["lon"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"junction {point_id} has non-numeric coordinates") from e
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"junction {point_id} has invalid coordinates: {lat},{lon}")
        if not isinstance(point["in_bmc"], bool):
            raise ValueError(f"junction {point_id}.in_bmc must be true or false")
    return coverage, junctions


def inventory_batch(
    scope: str,
    limit: int | None = 50,
    offset: int = 0,
    coverage_path: Path = COVERAGE_PATH,
) -> tuple[dict, list[dict], int]:
    """Return ``(coverage, batch, total_in_scope)`` in stable inventory order."""
    if scope not in VALID_SCOPES:
        raise ValueError("scope must be 'bmc' or 'mmrda'")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be greater than zero")

    coverage, junctions = _load_coverage(coverage_path)
    scoped = junctions if scope == "mmrda" else [p for p in junctions if p["in_bmc"]]
    end = None if limit is None else offset + limit
    return coverage, scoped[offset:end], len(scoped)


def _flow_reading(provider: str, lat: float, lon: float):
    """Fetch one uncached live reading and return normalised provider fields."""
    if provider == "here":
        reading = here_client.flow_point(lat, lon, use_cache=False)
        return (
            reading.get("current_kph"), reading.get("free_kph"),
            reading.get("confidence"), reading.get("road_closure"),
        )
    reading = tt.flow_segment(f"{lat:.5f},{lon:.5f}", use_cache=False)
    return (
        reading.get("currentSpeed"), reading.get("freeFlowSpeed"),
        reading.get("confidence"), reading.get("roadClosure"),
    )


def _validate_provider_config(provider: str) -> None:
    """Fail before reservation when a provider key is absent."""
    if provider == "tomtom":
        try:
            tt.get_api_key()
        except RuntimeError as e:
            raise incidents.ProviderError(str(e), kind="config") from e
    else:
        # HERE's key helper already raises a typed, non-billable config failure.
        here_client._key()


def _update_coverage_latest(path: Path, rows: list[dict], run_id: str) -> int:
    """Cache successful latest readings in coverage.json, preserving all metadata.

    SQLite remains authoritative (``store.load_latest_intersection_readings``).
    This small cache lets the generated map reflect a just-completed collection
    before its next export. Failed/unattempted points are never modified.
    """
    if not rows:
        return 0
    coverage, junctions = _load_coverage(path)
    successful = {str(row["point_id"]): row for row in rows}
    changed = 0
    for point in junctions:
        row = successful.get(str(point["id"]))
        if row is None:
            continue
        point["status"] = "collected"
        point["latest"] = {
            "run_id": run_id,
            "fetched_utc": row["fetched_utc"],
            "provider": row["provider"],
            "current_speed_kph": row["current_speed_kph"],
            "free_speed_kph": row["free_speed_kph"],
            "tti": row["tti"],
            "confidence": row["confidence"],
            "road_closure": row["road_closure"],
        }
        changed += 1

    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(coverage, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return changed


def _persist_successes(path: Path, rows: list[dict], run_id: str) -> tuple[int, int]:
    """Durably store usable rows and best-effort refresh their map cache."""
    if not rows:
        return 0, 0
    inserted = store.insert_intersection_readings(rows, run_id)
    try:
        coverage_updates = _update_coverage_latest(path, rows, run_id)
    except (OSError, ValueError) as e:
        # SQLite is authoritative; a later map export can rebuild this cache.
        coverage_updates = 0
        print(f"[collect_intersections] WARNING: coverage cache not updated: {e}")
    return inserted, coverage_updates


def collect(
    scope: str,
    *,
    limit: int | None = 50,
    offset: int = 0,
    label: str = "run",
    segment: str | None = None,
    provider: str = "here",
    max_calls_month: int | None = None,
    request_pause: float = 0.0,
    latch_after: int | None = None,
    coverage_path: Path = COVERAGE_PATH,
) -> dict:
    """Collect one bounded inventory batch and return a run summary.

    No new-inventory row is written to the legacy ``flow_readings`` table, and
    no reading is inserted unless the provider returns a usable current speed.
    """
    if provider not in ("here", "tomtom"):
        raise ValueError("provider must be 'here' or 'tomtom'")
    if request_pause < 0:
        raise ValueError("request_pause must be zero or greater")

    latch = incidents.latch_state(provider)
    if latch["latched"]:
        raise incidents.ProviderError(
            f"{provider} collection is STOPPED since {latch['latched_utc'][:19]} UTC — "
            f"{latch['latch_reason']}. Resume it before collecting again.",
            kind="latched",
        )

    _coverage, points, total_in_scope = inventory_batch(
        scope, limit=limit, offset=offset, coverage_path=coverage_path
    )
    if not points:
        print(
            f"[collect_intersections] No {scope.upper()} junctions in batch "
            f"offset={offset}, limit={limit}; inventory has {total_in_scope}."
        )
        return {
            "run_id": None, "scope": scope, "total_in_scope": total_in_scope,
            "requested": 0, "issued": 0, "inserted": 0, "failed": 0,
            "offset": offset, "limit": limit, "provider": provider,
        }

    # Missing credentials cannot consume provider quota, so check before reserve.
    try:
        _validate_provider_config(provider)
    except incidents.ProviderError as e:
        state = incidents.record(
            provider, e, requests_issued=0, failed_calls=1, latch_after=latch_after
        )
        tail = (
            f"; STOPPED — {state['latch_reason']}" if state["latched"]
            else f"; holding {state['hold_minutes']} min"
        )
        raise incidents.ProviderError(
            f"{e}{tail}", kind=e.kind, status=e.status, evidence=e.evidence
        ) from e

    requested = len(points)
    monthly_limit = budget.resolve_limit(provider, max_calls_month)
    reserved = budget.reserve(provider, requested, monthly_limit)
    if monthly_limit:
        print(
            f"[collect_intersections] Budget: {reserved:,}/{monthly_limit:,} "
            f"{provider} calls used this month ({monthly_limit - reserved:,} left)."
        )

    now_utc = datetime.now(timezone.utc)
    run_id = f"intersections_{scope}_{label}_{now_utc:%Y%m%d_%H%M%S_%f}"
    segment = segment or seg.classify(now_utc)
    rows: list[dict] = []
    issued = 0
    provider_fails = 0
    failures = 0

    try:
        print(
            f"[collect_intersections] Collecting {requested} of {total_in_scope} "
            f"{scope.upper()} junctions (offset={offset}, provider={provider}) ..."
        )
        for i, point in enumerate(points):
            point_id = str(point["id"])
            lat, lon = float(point["lat"]), float(point["lon"])
            coordinate = f"{lat:.5f},{lon:.5f}"
            pause_after = bool(request_pause and i + 1 < requested)
            try:
                current, free, confidence, closure = _flow_reading(provider, lat, lon)
                issued += 1
                provider_fails = 0
            except incidents.ProviderError as e:
                issued += 0 if e.kind in incidents.UNSENT_KINDS else 1
                provider_fails += 1
                failures += 1
                print(f"  [{offset + i:>4}] {point_id}  {e.kind.upper()}: {e}")
                if (
                    e.kind in incidents.ABORT_IMMEDIATELY
                    or provider_fails >= incidents.ABORT_AFTER_CONSECUTIVE
                ):
                    budget.refund(provider, max(0, requested - issued))
                    if rows:
                        # Full-inventory sweeps are long. Never throw away valid
                        # readings fetched before a later point hits a provider
                        # limit or outage.
                        try:
                            partial, _updates = _persist_successes(
                                coverage_path, rows, run_id
                            )
                            print(
                                f"[collect_intersections] Stored {partial} partial "
                                "row(s) before aborting the sweep."
                            )
                        except Exception as persist_error:  # noqa: BLE001
                            print(
                                "[collect_intersections] WARNING: could not store "
                                f"partial rows: {persist_error}"
                            )
                    state = incidents.record(
                        provider, e, requests_issued=issued,
                        failed_calls=provider_fails, latch_after=latch_after,
                    )
                    tail = (
                        f"; STOPPED — {state['latch_reason']}" if state["latched"]
                        else f"; holding {state['hold_minutes']} min "
                        f"(failure #{state['consecutive']})"
                    )
                    raise incidents.ProviderError(
                        f"{e} — aborted after {issued} of {requested} requests{tail}",
                        kind=e.kind, status=e.status, evidence=e.evidence,
                    ) from e
                if pause_after:
                    time.sleep(request_pause)
                continue
            except Exception as e:  # noqa: BLE001 -- a bad point must not lose the batch
                issued += 1
                failures += 1
                print(f"  [{offset + i:>4}] {point_id}  ERROR: {e}")
                if pause_after:
                    time.sleep(request_pause)
                continue

            # A valid HTTP response with no current-speed observation is not data.
            # It consumes a call but never becomes a placeholder SQLite row.
            if current is None:
                failures += 1
                print(f"  [{offset + i:>4}] {point_id}  NO USABLE FLOW DATA at {coordinate}")
                if pause_after:
                    time.sleep(request_pause)
                continue

            tti = (free / current) if free is not None and current else None
            row = {
                "point_id": point_id,
                "scope": "bmc" if point["in_bmc"] else "mmrda",
                "lat": lat,
                "lon": lon,
                "name": point.get("name") or point_id,
                "current_speed_kph": current,
                "free_speed_kph": free,
                "tti": round(tti, 3) if tti is not None else None,
                "confidence": confidence,
                "road_closure": closure,
                "provider": provider,
                "label": label,
                "segment": segment,
                # A full MMRDA sweep can take several minutes. Record the point's
                # real response time rather than stamping all 2,003 rows with the
                # sweep start time.
                "fetched_utc": datetime.now(timezone.utc).isoformat(),
            }
            rows.append(row)
            flag = "" if tti is None or tti < 1.2 else (
                "  <-- congested" if tti < 2 else "  <-- SEVERE"
            )
            print(
                f"  [{offset + i:>4}] {point_id}  cur={current} free={free} "
                f"TTI={tti and round(tti, 2)}{flag}"
            )
            if pause_after:
                time.sleep(request_pause)

        if not rows:
            budget.refund(provider, max(0, requested - issued))
            err = incidents.ProviderError(
                f"{provider}: {requested} junctions attempted, none returned usable data.",
                kind="other",
            )
            incidents.record(
                provider, err, requests_issued=issued,
                failed_calls=max(1, failures), latch_after=latch_after,
            )
            raise err

        inserted, coverage_updates = _persist_successes(coverage_path, rows, run_id)
        incidents.record_success(provider)
        if issued < requested:
            budget.refund(provider, requested - issued)

        print(
            f"\n[collect_intersections] Stored {inserted} fresh rows in "
            f"{store.DB_PATH.name} (run_id={run_id}); {failures} point(s) had no reading."
        )
        return {
            "run_id": run_id, "scope": scope, "total_in_scope": total_in_scope,
            "requested": requested, "issued": issued, "inserted": inserted,
            "failed": failures, "offset": offset, "limit": limit,
            "provider": provider, "coverage_updates": coverage_updates,
        }
    except incidents.ProviderError:
        raise
    except BaseException:
        budget.refund(provider, max(0, requested - issued))
        if rows:
            # Also preserve progress on Ctrl-C, termination during a deploy, or
            # an unexpected non-provider exception in a full-inventory sweep.
            try:
                _persist_successes(coverage_path, rows, run_id)
            except Exception as persist_error:  # noqa: BLE001
                print(
                    "[collect_intersections] WARNING: could not store interrupted "
                    f"sweep rows: {persist_error}"
                )
        raise


def collect_next_batch(
    scope: str,
    *,
    limit: int = 8,
    label: str = "scheduled",
    segment: str | None = None,
    provider: str = "here",
    max_calls_month: int | None = None,
    request_pause: float = 0.0,
    latch_after: int | None = None,
    coverage_path: Path = COVERAGE_PATH,
) -> dict:
    """Collect and advance one restart-safe round-robin regional batch.

    The cursor advances only after the batch returns normally. It wraps at the
    selected scope's inventory size, so ``mmrda`` continuously visits all MMRDA
    junctions—including every BMC junction—without favoring the first page.
    """
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    _coverage, _preview, total = inventory_batch(
        scope, limit=1, offset=0, coverage_path=coverage_path
    )
    if total <= 0:
        raise ValueError(f"coverage inventory has no {scope.upper()} junctions")

    stream = f"intersection_readings:{scope}"
    offset = store.load_collection_cursor(stream) % total
    result = collect(
        scope,
        limit=limit,
        offset=offset,
        label=label,
        segment=segment,
        provider=provider,
        max_calls_month=max_calls_month,
        request_pause=request_pause,
        latch_after=latch_after,
        coverage_path=coverage_path,
    )
    next_offset = (offset + int(result["requested"])) % total
    store.save_collection_cursor(stream, next_offset)
    result["next_offset"] = next_offset
    return result


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect fresh provider readings for Greater Mumbai junctions."
    )
    parser.add_argument(
        "--scope", choices=VALID_SCOPES, required=True,
        help="BMC is the strict subset; MMRDA includes every BMC junction.",
    )
    parser.add_argument(
        "--limit", type=_positive_int, default=50,
        help="Maximum junctions to call in this batch (default 50).",
    )
    parser.add_argument(
        "--offset", type=_nonnegative_int, default=0,
        help="Start position within the selected scope (default 0).",
    )
    parser.add_argument("--label", default="run", help="Session label (e.g. am_peak).")
    parser.add_argument(
        "--segment", choices=["peak", "avg"], default=None,
        help="Tag as a weekday planning segment; use --force outside its window.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Collect even if now is outside the requested --segment window.",
    )
    parser.add_argument(
        "--provider", choices=["here", "tomtom"], default="here",
        help="Flow provider (default here).",
    )
    parser.add_argument(
        "--max-calls-month", type=int, default=None,
        help="Override the provider monthly call cap for this run.",
    )
    parser.add_argument(
        "--request-pause", type=float, default=0.0,
        help="Seconds between point requests (default 0).",
    )
    parser.add_argument(
        "--max-failed-calls", type=int, default=None,
        help="Failed calls since success that engage the persistent hard stop.",
    )
    args = parser.parse_args()

    label = args.label
    if args.segment:
        label = args.label if args.label != "run" else args.segment
        now = seg.ist_now()
        actual = seg.classify(now)
        if actual != args.segment and not args.force:
            print(
                f"[collect_intersections] Refusing to tag '{args.segment}': it is "
                f"{now:%a %H:%M} IST, the '{actual}' window."
            )
            print(f"  Expected window: {seg.SEGMENTS[args.segment]['windows_ist']}")
            print("  Re-run inside the window, or pass --force to record anyway.")
            sys.exit(1)
        if actual != args.segment:
            print(
                f"[collect_intersections] --force: tagging as '{args.segment}' "
                f"despite being in the '{actual}' window."
            )

    try:
        collect(
            args.scope, limit=args.limit, offset=args.offset, label=label,
            segment=args.segment, provider=args.provider,
            max_calls_month=args.max_calls_month, request_pause=args.request_pause,
            latch_after=args.max_failed_calls,
        )
    except budget.BudgetExhausted as e:
        print(f"[collect_intersections] BUDGET STOP — {e}")
        print("  No call was made. Raise the cap, or wait for the month to roll over.")
        sys.exit(2)
    except incidents.ProviderError as e:
        hold = incidents.hold_state(args.provider)
        print(f"[collect_intersections] PROVIDER STOP — {e}")
        print(
            f"  Unissued calls refunded. Holding {hold['minutes_remaining']} min "
            "before the next attempt."
        )
        sys.exit(3)


if __name__ == "__main__":
    main()
