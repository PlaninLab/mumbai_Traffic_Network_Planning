"""
collect_day.py — scheduled WEH and regional flow collection.

Instead of a few fixed readings, this samples the WEH continuously through the day
and can collect either every Greater Mumbai junction or a rotating regional batch
on the same cadence. Batched mode uses a persistent cursor; production deployment
uses full-inventory mode so every mapped MMRDA/BMC junction is called each sweep.

    peak windows      -> every 10 min (default)   [08:00–11:00 & 17:30–20:30 IST]
    everything else   -> every 15 min (default)
    night (optional)  -> --night-interval          [23:00–06:00 IST, every day]

The night tier is off unless --night-interval is given. It overrides the segment
cadence for the overnight hours only, so the quiet hours can be sampled coarsely
and the saved API calls spent on a higher --n by day. It changes sampling rate
only — a night reading is still tagged 'offpeak' by seg.classify(), as before.

Every reading is tagged with its segment and written to the tabular SQLite store
(src/data/store.py); the segment summary is refreshed after each one so the live
dashboard tracks the day as it unfolds.

BUDGET NOTE: TomTom's free tier is 2,500 requests/day and each reading makes `--n`
calls (one per WEH sample point). Full-day 10/15-min sampling adds up fast — this
script prints the estimated daily call count up front and warns if it exceeds the
budget. Lower `--n`, widen the intervals, or shorten the window to stay under it.

Usage:
    python -m src.data.collect_day --dry-run                 # preview the schedule
    python -m src.data.collect_day --n 25                    # run now until 23:59 IST
    python -m src.data.collect_day --until 22:00 --n 25
    python -m src.data.collect_day --minutes 120 --peak-interval 10 --offpeak-interval 15
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, time as dtime, timedelta

from src.data import (
    budget,
    collect_flow,
    collect_intersections,
    incidents,
    segment_summary,
)
from src.data import segments as seg

TOMTOM_DAILY_BUDGET = 2500


def _interval_for(when: datetime, segment: str, peak_min: int, off_min: int,
                  night_min: int | None = None) -> int:
    """Sampling cadence for one moment, in minutes.

    The night window wins over the segment cadence when --night-interval is set,
    so the quiet overnight hours can be sampled coarsely and the saved API calls
    spent on daytime resolution instead. This changes only HOW OFTEN we sample;
    the reading is still classified by seg.classify() exactly as before.
    """
    if night_min is not None and seg.is_night(when):
        return night_min
    return peak_min if segment == "peak" else off_min


def _end_time(now_ist: datetime, until: str | None, minutes: int | None) -> datetime:
    if minutes is not None:
        return now_ist + timedelta(minutes=minutes)
    if until:
        hh, mm = (int(x) for x in until.split(":"))
        end = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end <= now_ist:              # time already passed today -> next day
            end += timedelta(days=1)
        return end
    # Default: end of today (23:59 IST).
    return now_ist.replace(hour=23, minute=59, second=0, microsecond=0)


def simulate_schedule(start: datetime, end: datetime, peak_min: int, off_min: int,
                      night_min: int | None = None):
    """Return the list of (time_ist, segment, interval_min) readings that would run."""
    sched, t = [], start
    while t <= end:
        s = seg.classify(t)
        step = _interval_for(t, s, peak_min, off_min, night_min)
        sched.append((t, s, step))
        t = t + timedelta(minutes=step)
    return sched


def _print_plan(sched, n, start, end, peak_min, off_min, night_min=None,
                intersection_scope=None, intersection_batch=0,
                intersection_total=None, all_intersections=False,
                provider="here"):
    regional_calls = (
        intersection_total if all_intersections and intersection_total else intersection_batch
    )
    calls_per_sweep = n + regional_calls
    calls = len(sched) * calls_per_sweep
    by_seg = {}
    for _, s, _step in sched:
        by_seg[s] = by_seg.get(s, 0) + 1
    night_reads = sum(1 for t, _s, _step in sched if seg.is_night(t))
    cadence = f"peak={peak_min}min, off-peak/avg={off_min}min"
    if night_min is not None:
        ns, ne = seg.NIGHT_WINDOW
        cadence += f", night({ns:%H:%M}-{ne:%H:%M})={night_min}min"
    print(f"[collect_day] Window: {start:%Y-%m-%d %H:%M} -> {end:%H:%M} IST")
    print(f"[collect_day] Cadence: {cadence}")
    print(f"[collect_day] Sweeps: {len(sched)} "
          f"({', '.join(f'{k}:{v}' for k, v in sorted(by_seg.items()))})")
    regional = ""
    if intersection_scope and regional_calls:
        qualifier = "all " if all_intersections else ""
        regional = f" + {qualifier}{regional_calls:,} {intersection_scope.upper()} junctions"
    print(f"[collect_day] Calls/sweep: {n} WEH points{regional} = {calls_per_sweep}")
    if (intersection_scope and intersection_batch and intersection_total
            and not all_intersections):
        batches = (intersection_total + intersection_batch - 1) // intersection_batch
        print(f"[collect_day] Regional rotation: {intersection_total:,} junctions; "
              f"one full pass every ~{batches:,} sweeps (persistent cursor)")
    if night_min is not None:
        print(f"[collect_day]   of which night: {night_reads} "
              f"(day: {len(sched) - night_reads})")
    print(f"[collect_day] Estimated {provider.upper()} API calls today: {calls:,}")
    if provider == "tomtom" and calls > TOMTOM_DAILY_BUDGET:
        over = calls - TOMTOM_DAILY_BUDGET
        safe_total = max(1, TOMTOM_DAILY_BUDGET // max(1, len(sched)))
        print(f"  !! Over TomTom's {TOMTOM_DAILY_BUDGET:,}/day free tier by "
              f"{over:,} calls. Lower the combined points/sweep "
              f"to <= {safe_total}, widen intervals, or shorten the window.")
    return calls


def run_day(n=25, until=None, minutes=None, peak_min=10, off_min=15,
            night_min=None, provider="here", max_calls_month=None,
            request_pause=0.0, latch_after=None, dry_run=False,
            intersection_scope=None, intersection_batch=0,
            all_intersections=False) -> None:
    if n <= 0:
        raise ValueError("n must be greater than zero")
    if intersection_scope not in (None, "bmc", "mmrda"):
        raise ValueError("intersection_scope must be 'bmc', 'mmrda', or None")
    if intersection_batch < 0:
        raise ValueError("intersection_batch must be zero or greater")
    if all_intersections and intersection_batch:
        raise ValueError(
            "--all-intersections and --intersection-batch are mutually exclusive"
        )
    if bool(intersection_scope) != bool(intersection_batch or all_intersections):
        raise ValueError(
            "--intersection-scope requires --all-intersections or a positive "
            "--intersection-batch"
        )

    now = seg.ist_now()
    end = _end_time(now, until, minutes)
    sched = simulate_schedule(now, end, peak_min, off_min, night_min)
    intersection_total = None
    if intersection_scope:
        # Validates (and, on an upgraded Docker volume, seeds) the real inventory
        # before the day-long loop begins. This never calls a traffic provider.
        _coverage, _preview, intersection_total = collect_intersections.inventory_batch(
            intersection_scope, limit=1
        )
    planned_calls = _print_plan(
        sched, n, now, end, peak_min, off_min, night_min,
        intersection_scope, intersection_batch, intersection_total,
        all_intersections, provider,
    )
    print(f"[collect_day] Flow provider: {provider}")

    limit = budget.resolve_limit(provider, max_calls_month)
    s = budget.status(provider, limit)
    if limit:
        print(f"[collect_day] Monthly cap: {s['calls_used']:,}/{limit:,} calls used "
              f"({s['month_utc']} UTC, {s['calls_remaining']:,} left)")
        if planned_calls > s["calls_remaining"]:
            print(f"  !! This day's plan needs {planned_calls:,} calls but only "
                  f"{s['calls_remaining']:,} remain. Collection will stop part-way.")
    else:
        print(f"[collect_day] Monthly cap: none set — {s['calls_used']:,} calls used so far "
              f"({s['month_utc']} UTC). Set <PROVIDER>_MONTHLY_CALL_LIMIT to bound spend.")

    if dry_run:
        print("\n[collect_day] --dry-run: schedule preview only (no API calls).")
        for t, s, step in sched[:12]:
            print(f"    {t:%H:%M} IST  [{s:7s}]  next in {step} min")
        if len(sched) > 12:
            print(f"    ... (+{len(sched) - 12} more)")
        return

    print("\n[collect_day] Starting full-day collection. Ctrl-C to stop.\n")
    corridor_count, regional_count = 0, 0
    starved, held, stopped = False, False, False
    while seg.ist_now() <= end:
        s = seg.current_segment()
        # Provider back-off is a GATE, not an extra sleep. Skipping the sweep here
        # means no reservation and no requests, and the loop still falls through to
        # its normal interval below — so the sampling grid stays intact and the
        # collector simply misses the slots the provider cannot serve.
        lat = incidents.latch_state(provider)
        if lat["latched"]:
            if not stopped:
                print(f"[collect_day] STOPPED — {lat['latch_reason']}")
                print(f"[collect_day] No further calls. Resume from the dashboard, or: "
                      f"python -m src.data.incidents --resume --provider {provider}")
                stopped = True
            interval = _interval_for(seg.ist_now(), s, peak_min, off_min, night_min)
            nxt = seg.ist_now() + timedelta(minutes=interval)
            if nxt > end:
                break
            time.sleep(interval * 60)
            continue
        stopped = False

        hold = incidents.hold_state(provider)
        if hold["holding"]:
            if not held:
                print(f"[collect_day] PROVIDER HOLD — {provider} failed "
                      f"{hold['consecutive']} time(s); skipping sweeps for another "
                      f"{hold['minutes_remaining']} min.")
                held = True
            interval = _interval_for(seg.ist_now(), s, peak_min, off_min, night_min)
            nxt = seg.ist_now() + timedelta(minutes=interval)
            if nxt > end:
                break
            time.sleep(interval * 60)
            continue
        held = False
        try:
            collect_flow.collect(n, label=s, segment=s, provider=provider,
                                 max_calls_month=max_calls_month,
                                 request_pause=request_pause,
                                 latch_after=latch_after)
            segment_summary.build_summary()   # refresh dashboard data
            corridor_count += 1
            if intersection_scope:
                common = {
                    "label": f"scheduled_{s}",
                    "segment": s,
                    "provider": provider,
                    "max_calls_month": max_calls_month,
                    "request_pause": request_pause,
                    "latch_after": latch_after,
                }
                if all_intersections:
                    regional = collect_intersections.collect(
                        intersection_scope, limit=None, offset=0, **common
                    )
                else:
                    regional = collect_intersections.collect_next_batch(
                        intersection_scope, limit=intersection_batch, **common
                    )
                regional_count += 1
                tail = "full inventory requested."
                if not all_intersections:
                    tail = (
                        f"next {intersection_scope.upper()} offset "
                        f"{regional['next_offset']:,}/{regional['total_in_scope']:,}."
                    )
                print(f"[collect_day] Regional batch {regional_count}: "
                      f"{regional['inserted']}/{regional['requested']} stored; {tail}")
            starved = False
        except budget.BudgetExhausted as e:
            # Keep the process alive but make NO calls: exiting here would restart-loop
            # under `restart: unless-stopped`. The counter rolls over with the month,
            # so collection resumes on its own. /api/health reports this state, so it
            # is visible without reading the log.
            if not starved:
                print(f"[collect_day] BUDGET STOP — {e}")
                print("[collect_day] Holding: no further calls until the cap rises or "
                      "the month rolls over. Sleeping on schedule.")
                starved = True
        # MUST precede `except Exception`: ProviderError subclasses RuntimeError,
        # so a later clause would be unreachable and the hold would never be seen.
        except incidents.ProviderError as e:
            print(f"[collect_day] PROVIDER STOP — {e}")
        except Exception as e:  # noqa: BLE001 — keep the day-long loop alive
            print(f"[collect_day] reading failed: {e}")
        interval = _interval_for(seg.ist_now(), s, peak_min, off_min, night_min)
        nxt = seg.ist_now() + timedelta(minutes=interval)
        if nxt > end:
            break
        print(f"[collect_day] sweeps done: WEH={corridor_count}, "
              f"regional={regional_count}; sleeping {interval} min "
              f"(next ~{nxt:%H:%M} IST)\n")
        time.sleep(interval * 60)
    print(f"[collect_day] Done — WEH sweeps={corridor_count}, "
          f"regional batches={regional_count} through {end:%H:%M} IST.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Scheduled WEH plus Greater Mumbai junction flow collection."
    )
    ap.add_argument("--n", type=int, default=25, help="Sample points per reading (default 25).")
    ap.add_argument("--until", default=None, help="Stop at this IST time HH:MM (default 23:59).")
    ap.add_argument("--minutes", type=int, default=None,
                    help="Run for this many minutes instead of until a clock time.")
    ap.add_argument("--peak-interval", type=int, default=10, help="Peak cadence, minutes (default 10).")
    ap.add_argument("--offpeak-interval", type=int, default=15,
                    help="Off-peak/avg cadence, minutes (default 15).")
    ap.add_argument("--night-interval", type=int, default=None,
                    help="Overnight cadence, minutes (23:00-06:00 IST, every day). "
                         "Omit to sample the night at the off-peak cadence, as before. "
                         "Coarser nights free up API calls for a higher --n by day.")
    ap.add_argument("--provider", choices=["here", "tomtom"], default="here",
                    help="Flow data source (default here; needs HERE_API_KEY).")
    ap.add_argument(
        "--intersection-scope", choices=["bmc", "mmrda"], default=None,
        help="Collect this regional inventory. MMRDA includes all BMC junctions.",
    )
    intersection_mode = ap.add_mutually_exclusive_group()
    intersection_mode.add_argument(
        "--intersection-batch", type=int, default=0,
        help="Regional junction calls per sweep (requires --intersection-scope). "
             "The restart-safe cursor advances to the next batch each interval.",
    )
    intersection_mode.add_argument(
        "--all-intersections", action="store_true",
        help="Call every junction in --intersection-scope during every sweep.",
    )
    ap.add_argument("--max-calls-month", type=int, default=None,
                    help="Cap total provider calls per billing month. Overrides "
                         "<PROVIDER>_MONTHLY_CALL_LIMIT / API_MONTHLY_CALL_LIMIT. "
                         "Counts ALL calls, including any free allowance.")
    ap.add_argument("--request-pause", type=float, default=0.0,
                    help="Seconds between consecutive point requests (default 0).")
    ap.add_argument("--max-failed-calls", type=int, default=None,
                    help="Failed calls since the last success that trigger a HARD STOP "
                         "needing a manual resume (default 25).")
    ap.add_argument("--dry-run", action="store_true", help="Preview the schedule; no API calls.")
    args = ap.parse_args()

    run_day(n=args.n, until=args.until, minutes=args.minutes,
            peak_min=args.peak_interval, off_min=args.offpeak_interval,
            night_min=args.night_interval, provider=args.provider,
            max_calls_month=args.max_calls_month,
            request_pause=args.request_pause, latch_after=args.max_failed_calls,
            dry_run=args.dry_run, intersection_scope=args.intersection_scope,
            intersection_batch=args.intersection_batch,
            all_intersections=args.all_intersections)


if __name__ == "__main__":
    main()
