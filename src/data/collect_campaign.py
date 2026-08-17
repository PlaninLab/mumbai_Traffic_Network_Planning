"""Run the fixed 17–19 August 2026 MMRDA junction collection campaign.

This is deliberately separate from ``collect_day``. The campaign has absolute
IST timestamps, a one-off 45-minute phase offset, and a hard end. Each normal
sweep calls the complete mapped MMRDA inventory plus the 16 existing WEH points.

Default schedule (IST):

* 2026-08-17 23:00 through 2026-08-18 21:30, every 90 minutes (16 slots)
* 2026-08-18 23:45 through 2026-08-19 22:15, every 90 minutes (16 slots)
* hard stop at 2026-08-19 23:15; this is a cutoff, not a collection slot

Each paid slot is claimed in SQLite before its first request. A restart will
never replay a claimed slot, and missed slots are never bunched up as catch-up
calls. Run ``--dry-run`` to print the full zero-call plan.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.data import collect_flow, collect_intersections, segments as seg, store

DEFAULT_CAMPAIGN = "mmrda_20260817_48h_offset45"
DEFAULT_START = "2026-08-17T23:00:00+05:30"
DEFAULT_STOP = "2026-08-19T23:15:00+05:30"
DEFAULT_INTERVAL_MINUTES = 90
DEFAULT_OFFSET_AFTER_HOURS = 24
DEFAULT_OFFSET_MINUTES = 45
DEFAULT_EXPECTED_JUNCTIONS = 2003
DEFAULT_CORRIDOR_POINTS = 16
DEFAULT_EXPECTED_CALLS_PER_SWEEP = 2019
DEFAULT_LATE_GRACE_MINUTES = 5


@dataclass(frozen=True)
class CampaignSlot:
    number: int
    at: datetime

    @property
    def run_id(self) -> str:
        return f"campaign_mmrda_{self.at:%Y%m%d_%H%M}_ist"

    @property
    def label(self) -> str:
        return f"campaign_{self.at:%Y%m%d_%H%M}_ist"


def parse_ist(value: str) -> datetime:
    """Parse an explicit timestamp and normalize it to fixed-offset IST."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"invalid ISO timestamp {value!r}; include the +05:30 offset"
        ) from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone offset")
    return parsed.astimezone(seg.IST)


def build_schedule(
    start: datetime,
    stop: datetime,
    *,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    offset_after_hours: int = DEFAULT_OFFSET_AFTER_HOURS,
    offset_minutes: int = DEFAULT_OFFSET_MINUTES,
) -> list[CampaignSlot]:
    """Build the two-phase absolute schedule, with ``stop`` as a hard cutoff."""
    if start.tzinfo is None or stop.tzinfo is None:
        raise ValueError("start and stop must be timezone-aware")
    if stop <= start:
        raise ValueError("stop must be after start")
    if interval_minutes <= 0 or offset_after_hours <= 0 or offset_minutes < 0:
        raise ValueError("campaign intervals must be positive; offset cannot be negative")

    start = start.astimezone(seg.IST)
    stop = stop.astimezone(seg.IST)
    interval = timedelta(minutes=interval_minutes)
    phase_change = start + timedelta(hours=offset_after_hours)

    times: list[datetime] = []
    at = start
    while at < phase_change and at <= stop:
        times.append(at)
        at += interval

    at = phase_change + timedelta(minutes=offset_minutes)
    while at <= stop:
        times.append(at)
        at += interval

    return [CampaignSlot(i + 1, at) for i, at in enumerate(times)]


def _print_plan(
    campaign: str,
    slots: list[CampaignSlot],
    *,
    stop: datetime,
    junctions: int,
    corridor_points: int,
    provider: str,
) -> None:
    calls_per_sweep = junctions + corridor_points
    total_calls = len(slots) * calls_per_sweep
    print(f"[collect_campaign] Campaign: {campaign}")
    print(
        f"[collect_campaign] Scope: all {junctions:,} mapped MMRDA junctions "
        f"+ {corridor_points} existing WEH points"
    )
    print(f"[collect_campaign] Provider: {provider.upper()} (uncached live requests)")
    print(f"[collect_campaign] Sweeps: {len(slots)}")
    print(
        f"[collect_campaign] Calls/sweep: {junctions:,} + {corridor_points} "
        f"= {calls_per_sweep:,}"
    )
    print(f"[collect_campaign] Maximum planned calls: {total_calls:,}")
    print(f"[collect_campaign] Hard stop: {stop:%Y-%m-%d %H:%M} IST")
    print("[collect_campaign] Slots:")
    for slot in slots:
        print(f"  {slot.number:>2}. {slot.at:%a %Y-%m-%d %H:%M} IST")


def _wait_until(target: datetime) -> None:
    """Wait against the wall clock in short chunks so clock changes are noticed."""
    while True:
        remaining = (target - seg.ist_now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))


def _idle_forever() -> None:
    """Keep Docker's restart policy from relaunching a completed campaign."""
    print("[collect_campaign] Campaign is closed. Idling with zero further API calls.")
    while True:
        time.sleep(3600)


def _log_sweep_failure(
    *, campaign: str, run_id: str, provider: str,
    kind: str, detail: str, request_issued: bool | None = None,
    error: BaseException | None = None,
) -> None:
    """Best-effort durable audit for a whole-slot failure or hard stop."""
    evidence = getattr(error, "evidence", {}) or {}
    try:
        store.insert_collection_failure({
            "campaign": campaign,
            "run_id": run_id,
            "stage": "campaign_sweep",
            "provider": provider,
            "kind": kind,
            "request_issued": request_issued,
            "http_status": getattr(error, "status", None),
            "detail": detail,
            "endpoint": evidence.get("endpoint"),
            "correlation_id": evidence.get("correlation_id"),
            "request_id": evidence.get("request_id"),
            "slo": evidence.get("slo"),
            "server_date": evidence.get("server_date"),
            "latency_ms": evidence.get("latency_ms"),
            "response_body": evidence.get("response_body"),
        })
    except Exception as log_error:  # noqa: BLE001 — logging must not alter the grid
        print(f"[collect_campaign] WARNING: failure audit write failed: {log_error}")


def run_campaign(
    *,
    campaign: str = DEFAULT_CAMPAIGN,
    start: datetime,
    stop: datetime,
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    offset_after_hours: int = DEFAULT_OFFSET_AFTER_HOURS,
    offset_minutes: int = DEFAULT_OFFSET_MINUTES,
    expected_junctions: int = DEFAULT_EXPECTED_JUNCTIONS,
    corridor_points: int = DEFAULT_CORRIDOR_POINTS,
    expected_calls_per_sweep: int = DEFAULT_EXPECTED_CALLS_PER_SWEEP,
    late_grace_minutes: int = DEFAULT_LATE_GRACE_MINUTES,
    scope: str = "mmrda",
    provider: str = "here",
    dry_run: bool = False,
    stay_alive: bool = False,
) -> None:
    """Execute the fixed campaign without replaying or catching up paid slots."""
    if not campaign.strip():
        raise ValueError("campaign must not be empty")
    if late_grace_minutes < 0:
        raise ValueError("late_grace_minutes must be zero or greater")
    if expected_junctions <= 0 or corridor_points <= 0 or expected_calls_per_sweep <= 0:
        raise ValueError("expected junctions, corridor points and calls must be positive")

    slots = build_schedule(
        start,
        stop,
        interval_minutes=interval_minutes,
        offset_after_hours=offset_after_hours,
        offset_minutes=offset_minutes,
    )
    _coverage, _preview, junctions = collect_intersections.inventory_batch(
        scope, limit=1, offset=0
    )
    if junctions != expected_junctions:
        raise RuntimeError(
            f"cost guard: expected exactly {expected_junctions:,} mapped {scope.upper()} "
            f"junctions, but the deployed inventory contains {junctions:,}; "
            "no provider call was made"
        )
    calls_per_sweep = junctions + corridor_points
    if calls_per_sweep != expected_calls_per_sweep:
        raise RuntimeError(
            f"cost guard: {junctions:,} mapped junctions + {corridor_points:,} WEH "
            f"points = {calls_per_sweep:,}, not the expected "
            f"{expected_calls_per_sweep:,} calls per sweep; no provider call was made"
        )

    stop = stop.astimezone(seg.IST)
    _print_plan(
        campaign,
        slots,
        stop=stop,
        junctions=junctions,
        corridor_points=corridor_points,
        provider=provider,
    )
    if dry_run:
        print("[collect_campaign] DRY RUN — no slot claimed and no API call made.")
        return

    print("[collect_campaign] Monthly cap: explicitly disabled for this campaign.")
    grace = timedelta(minutes=late_grace_minutes)
    hard_stop_utc = stop.astimezone(timezone.utc)

    for slot in slots:
        now = seg.ist_now()
        if now < slot.at:
            print(f"[collect_campaign] Waiting for slot {slot.number} at {slot.at:%F %H:%M} IST.")
            _wait_until(slot.at)
            now = seg.ist_now()

        slot_utc = slot.at.astimezone(timezone.utc).isoformat()
        slot_ist = slot.at.isoformat()
        if now > slot.at + grace:
            if store.claim_campaign_sweep(
                campaign,
                slot_utc,
                slot_ist,
                scope=scope,
                provider=provider,
                expected_calls=calls_per_sweep,
            ):
                store.finish_campaign_sweep(
                    campaign,
                    slot_utc,
                    "missed",
                    requested=0,
                    issued=0,
                    inserted=0,
                    failed=0,
                    detail=(
                        f"Not started: clock was beyond the {late_grace_minutes}-minute "
                        "grace. No catch-up calls were made."
                    ),
                )
            print(
                f"[collect_campaign] MISSED slot {slot.number} ({slot.at:%F %H:%M} IST); "
                "no catch-up calls."
            )
            continue

        claimed = store.claim_campaign_sweep(
            campaign,
            slot_utc,
            slot_ist,
            scope=scope,
            provider=provider,
            expected_calls=calls_per_sweep,
        )
        if not claimed:
            print(
                f"[collect_campaign] SKIP slot {slot.number} ({slot.at:%F %H:%M} IST): "
                "already claimed before this process started."
            )
            continue

        print(
            f"[collect_campaign] START slot {slot.number}/{len(slots)} at "
            f"{slot.at:%F %H:%M} IST — {calls_per_sweep:,} calls maximum."
        )
        corridor_result: dict | None = None
        try:
            corridor_summary = collect_flow.collect(
                corridor_points,
                label=slot.label,
                segment=seg.classify(slot.at),
                provider=provider,
                max_calls_month=0,
                request_pause=0,
                run_id=slot.run_id,
                stop_at=hard_stop_utc,
                return_summary=True,
                campaign=campaign,
            )
            if not isinstance(corridor_summary, dict):
                raise TypeError("campaign corridor collector did not return a summary")
            corridor_result = corridor_summary
            if corridor_result.get("deadline_reached"):
                regional_result = {
                    "run_id": slot.run_id,
                    "requested": junctions,
                    "issued": 0,
                    "inserted": 0,
                    "failed": 0,
                    "deadline_reached": True,
                }
            else:
                regional_result = collect_intersections.collect(
                    scope,
                    limit=None,
                    offset=0,
                    label=slot.label,
                    segment=seg.classify(slot.at),
                    provider=provider,
                    # Zero explicitly overrides any stale cap in the environment.
                    max_calls_month=0,
                    request_pause=0,
                    run_id=slot.run_id,
                    stop_at=hard_stop_utc,
                    campaign=campaign,
                )
        except Exception as error:  # noqa: BLE001 — record failure, then preserve the grid
            _log_sweep_failure(
                campaign=campaign,
                run_id=slot.run_id,
                provider=provider,
                kind=str(getattr(error, "kind", "sweep_error")),
                detail=f"{type(error).__name__}: {error}",
                request_issued=None,
                error=error,
            )
            store.finish_campaign_sweep(
                campaign,
                slot_utc,
                "failed",
                requested=calls_per_sweep,
                issued=(int(corridor_result["issued"]) if corridor_result else None),
                inserted=(int(corridor_result["inserted"]) if corridor_result else None),
                failed=(int(corridor_result["failed"]) if corridor_result else None),
                run_id=slot.run_id,
                detail=f"{type(error).__name__}: {error}",
            )
            print(f"[collect_campaign] FAILED slot {slot.number}: {error}")
            continue

        result = {
            "run_id": slot.run_id,
            "requested": calls_per_sweep,
            "issued": int(corridor_result["issued"]) + int(regional_result["issued"]),
            "inserted": (
                int(corridor_result["inserted"]) + int(regional_result["inserted"])
            ),
            "failed": int(corridor_result["failed"]) + int(regional_result["failed"]),
            "deadline_reached": bool(
                corridor_result.get("deadline_reached")
                or regional_result.get("deadline_reached")
            ),
        }
        status = "partial" if result["deadline_reached"] else "completed"
        if status == "partial":
            _log_sweep_failure(
                campaign=campaign,
                run_id=slot.run_id,
                provider=provider,
                kind="hard_stop",
                detail=(
                    f"Campaign hard stop reached after {result['issued']:,} of "
                    f"{calls_per_sweep:,} planned requests."
                ),
                request_issued=False,
            )
        store.finish_campaign_sweep(
            campaign,
            slot_utc,
            status,
            requested=int(result["requested"]),
            issued=int(result["issued"]),
            inserted=int(result["inserted"]),
            failed=int(result["failed"]),
            run_id=str(result["run_id"]),
            detail="Hard stop reached during sweep." if status == "partial" else None,
        )
        print(
            f"[collect_campaign] {status.upper()} slot {slot.number}: "
            f"issued={result['issued']:,}, stored={result['inserted']:,}, "
            f"failures={result['failed']:,}."
        )

    print(
        f"[collect_campaign] No more collection slots. Hard stop remains "
        f"{stop:%Y-%m-%d %H:%M} IST."
    )
    if stay_alive:
        _idle_forever()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the fixed, restart-safe August 2026 MMRDA collection campaign."
    )
    parser.add_argument("--campaign", default=DEFAULT_CAMPAIGN)
    parser.add_argument("--start", type=parse_ist, default=parse_ist(DEFAULT_START))
    parser.add_argument("--stop", type=parse_ist, default=parse_ist(DEFAULT_STOP))
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    parser.add_argument("--offset-after-hours", type=int, default=DEFAULT_OFFSET_AFTER_HOURS)
    parser.add_argument("--offset-minutes", type=int, default=DEFAULT_OFFSET_MINUTES)
    parser.add_argument("--expected-junctions", type=int, default=DEFAULT_EXPECTED_JUNCTIONS)
    parser.add_argument("--corridor-points", type=int, default=DEFAULT_CORRIDOR_POINTS)
    parser.add_argument(
        "--expected-calls-per-sweep",
        type=int,
        default=DEFAULT_EXPECTED_CALLS_PER_SWEEP,
    )
    parser.add_argument("--late-grace-minutes", type=int, default=DEFAULT_LATE_GRACE_MINUTES)
    parser.add_argument("--scope", choices=["bmc", "mmrda"], default="mmrda")
    parser.add_argument("--provider", choices=["here", "tomtom"], default="here")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--stay-alive",
        action="store_true",
        help="Idle after completion so Docker cannot restart the closed campaign.",
    )
    args = parser.parse_args()

    run_campaign(
        campaign=args.campaign,
        start=args.start,
        stop=args.stop,
        interval_minutes=args.interval_minutes,
        offset_after_hours=args.offset_after_hours,
        offset_minutes=args.offset_minutes,
        expected_junctions=args.expected_junctions,
        corridor_points=args.corridor_points,
        expected_calls_per_sweep=args.expected_calls_per_sweep,
        late_grace_minutes=args.late_grace_minutes,
        scope=args.scope,
        provider=args.provider,
        dry_run=args.dry_run,
        stay_alive=args.stay_alive,
    )


if __name__ == "__main__":
    main()
