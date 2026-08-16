"""
segments.py — weekday traffic time-segment definitions.

The user asked for TWO distinct weekday data segments, each answering a different
planning question:

  1. PEAK  (office / commute peak) — the AM and PM office-hour windows. This is the
     high-congestion state: it tells us the MAXIMUM time savings an intervention
     could unlock, WHICH circuits (links) are congested, and it is the correct
     demand state to calibrate the OD matrix against (peak-hour assignment).

  2. AVG   (average delayed daytime) — the inter-peak working-day window (late
     morning to late afternoon). This is the "typical delayed" baseline: not
     free-flow, not the sharpest peak — the everyday background delay. Comparing
     PEAK against AVG quantifies how much of the delay is peak-specific (and thus
     addressable by spreading demand / peak interventions) vs. structural.

Anything else (nights, and any weekend / public-holiday reading) is OFFPEAK and is
kept for reference but excluded from the peak/avg planning analysis.

All windows are in India Standard Time (IST, UTC+05:30). Weekday = Mon–Fri.
Public holidays are NOT auto-detected here — pass `--force` to the collector only
when you deliberately want to log a holiday, and mark it OFFPEAK downstream.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

# India Standard Time — no DST.
IST = timezone(timedelta(hours=5, minutes=30))

# Segment time windows in IST (start inclusive, end exclusive), weekday-only.
# Peak has two windows (AM + PM office commute); avg is the inter-peak daytime.
PEAK_WINDOWS = [
    (time(8, 0), time(11, 0)),    # morning office peak
    (time(17, 30), time(20, 30)),  # evening office peak
]
AVG_WINDOWS = [
    (time(11, 0), time(17, 30)),   # inter-peak working-day daytime
]

# Human-facing metadata (used by the collector, summary, and web dashboard).
SEGMENTS = {
    "peak": {
        "name": "Peak / office hours",
        "windows_ist": "Mon–Fri 08:00–11:00 & 17:30–20:30 IST",
        "purpose": "Max time-savings potential, congested circuits, OD-matrix calibration",
    },
    "avg": {
        "name": "Average delayed (daytime)",
        "windows_ist": "Mon–Fri 11:00–17:30 IST",
        "purpose": "Typical everyday delay baseline (compare against peak)",
    },
    "offpeak": {
        "name": "Off-peak / weekend / night",
        "windows_ist": "Nights, weekends, holidays",
        "purpose": "Reference only — excluded from peak/avg planning analysis",
    },
}


def ist_now() -> datetime:
    """Current time in IST."""
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    """Coerce any (aware or naive-UTC) datetime to IST."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def _in_any(t: time, windows) -> bool:
    return any(start <= t < end for start, end in windows)


def classify(dt: datetime) -> str:
    """Classify a datetime into 'peak' | 'avg' | 'offpeak' (interpreted in IST)."""
    d = to_ist(dt)
    if d.weekday() >= 5:          # 5=Sat, 6=Sun
        return "offpeak"
    t = d.time()
    if _in_any(t, PEAK_WINDOWS):
        return "peak"
    if _in_any(t, AVG_WINDOWS):
        return "avg"
    return "offpeak"


def classify_utc_iso(iso_str: str) -> str:
    """Classify from an ISO-8601 UTC string (as stored in collected CSVs)."""
    try:
        return classify(datetime.fromisoformat(iso_str))
    except (ValueError, TypeError):
        return "offpeak"


def current_segment() -> str:
    """The segment the current moment falls into."""
    return classify(ist_now())


def is_weekday(dt: datetime | None = None) -> bool:
    return to_ist(dt or ist_now()).weekday() < 5


if __name__ == "__main__":
    now = ist_now()
    print(f"IST now: {now:%Y-%m-%d %H:%M} ({now:%A})")
    print(f"Current segment: {current_segment()}")
    print("\nSegment windows:")
    for key, meta in SEGMENTS.items():
        print(f"  {key:8s} {meta['windows_ist']}")
        print(f"           -> {meta['purpose']}")
