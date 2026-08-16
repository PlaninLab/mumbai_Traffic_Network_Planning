"""
collect_day.py — full-day flow collection at segment-dependent intervals.

Instead of a few fixed readings, this samples the WEH continuously through the day
and adapts the cadence to the segment (finer where it matters):

    peak windows      -> every 10 min (default)   [08:00–11:00 & 17:30–20:30 IST]
    everything else   -> every 15 min (default)

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

from src.data import collect_flow, segment_summary
from src.data import segments as seg

TOMTOM_DAILY_BUDGET = 2500


def _interval_for(segment: str, peak_min: int, off_min: int) -> int:
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


def simulate_schedule(start: datetime, end: datetime, peak_min: int, off_min: int):
    """Return the list of (time_ist, segment, interval_min) readings that would run."""
    sched, t = [], start
    while t <= end:
        s = seg.classify(t)
        step = _interval_for(s, peak_min, off_min)
        sched.append((t, s, step))
        t = t + timedelta(minutes=step)
    return sched


def _print_plan(sched, n, start, end, peak_min, off_min):
    calls = len(sched) * n
    by_seg = {}
    for _, s, _step in sched:
        by_seg[s] = by_seg.get(s, 0) + 1
    print(f"[collect_day] Window: {start:%Y-%m-%d %H:%M} -> {end:%H:%M} IST")
    print(f"[collect_day] Cadence: peak={peak_min}min, off-peak/avg={off_min}min, "
          f"points/reading n={n}")
    print(f"[collect_day] Readings: {len(sched)} "
          f"({', '.join(f'{k}:{v}' for k, v in sorted(by_seg.items()))})")
    print(f"[collect_day] Estimated API calls today: {calls}  "
          f"(TomTom free tier {TOMTOM_DAILY_BUDGET}/day; HERE tiers differ)")
    if calls > TOMTOM_DAILY_BUDGET:
        over = calls - TOMTOM_DAILY_BUDGET
        safe_n = max(1, TOMTOM_DAILY_BUDGET // max(1, len(sched)))
        print(f"  !! OVER BUDGET by {over} calls. Lower --n to <= {safe_n}, widen "
              f"intervals, or shorten the window.")
    return calls


def run_day(n=25, until=None, minutes=None, peak_min=10, off_min=15,
            provider="here", dry_run=False) -> None:
    now = seg.ist_now()
    end = _end_time(now, until, minutes)
    sched = simulate_schedule(now, end, peak_min, off_min)
    _print_plan(sched, n, now, end, peak_min, off_min)
    print(f"[collect_day] Flow provider: {provider}")

    if dry_run:
        print("\n[collect_day] --dry-run: schedule preview only (no API calls).")
        for t, s, step in sched[:12]:
            print(f"    {t:%H:%M} IST  [{s:7s}]  next in {step} min")
        if len(sched) > 12:
            print(f"    ... (+{len(sched) - 12} more)")
        return

    print("\n[collect_day] Starting full-day collection. Ctrl-C to stop.\n")
    count = 0
    while seg.ist_now() <= end:
        s = seg.current_segment()
        try:
            collect_flow.collect(n, label=s, segment=s, provider=provider)
            segment_summary.build_summary()   # refresh dashboard data
            count += 1
        except Exception as e:  # noqa: BLE001 — keep the day-long loop alive
            print(f"[collect_day] reading failed: {e}")
        interval = _interval_for(s, peak_min, off_min)
        nxt = seg.ist_now() + timedelta(minutes=interval)
        if nxt > end:
            break
        print(f"[collect_day] reading {count} done; sleeping {interval} min "
              f"(next ~{nxt:%H:%M} IST)\n")
        time.sleep(interval * 60)
    print(f"[collect_day] Done — {count} readings collected through {end:%H:%M} IST.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Full-day WEH flow collection at 10/15-min intervals.")
    ap.add_argument("--n", type=int, default=25, help="Sample points per reading (default 25).")
    ap.add_argument("--until", default=None, help="Stop at this IST time HH:MM (default 23:59).")
    ap.add_argument("--minutes", type=int, default=None,
                    help="Run for this many minutes instead of until a clock time.")
    ap.add_argument("--peak-interval", type=int, default=10, help="Peak cadence, minutes (default 10).")
    ap.add_argument("--offpeak-interval", type=int, default=15,
                    help="Off-peak/avg cadence, minutes (default 15).")
    ap.add_argument("--provider", choices=["here", "tomtom"], default="here",
                    help="Flow data source (default here; needs HERE_API_KEY).")
    ap.add_argument("--dry-run", action="store_true", help="Preview the schedule; no API calls.")
    args = ap.parse_args()

    run_day(n=args.n, until=args.until, minutes=args.minutes,
            peak_min=args.peak_interval, off_min=args.offpeak_interval,
            provider=args.provider, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
