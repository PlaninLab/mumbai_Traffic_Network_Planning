"""
incidents.py — provider failure recording and back-off.

WHY THIS EXISTS
---------------
A provider outage or a rate-limit wall used to look like this: every sample point
in the sweep raised, each one was caught and logged, the sweep produced no rows,
and 15 minutes later the collector did it again — all day, for as long as the
outage lasted. Worse, the sweep's calls were already RESERVED against the monthly
cap (budget.py), so a day-long outage could eat the month's quota having collected
nothing.

Three things fix that:

  1. RECORD    every failure lands in api_incidents with its kind and HTTP status,
               so an outage is visible in /data instead of buried in a log.
  2. HOLD      after a failure the provider is put in a hold. collect_day checks
               the hold at the TOP of its loop and skips the sweep entirely — no
               reservation, no requests. The hold grows with each consecutive
               failure (15, 30, 60, 120, capped at 240 min) and clears on success.
  3. REFUND    a sweep that aborts early returns the calls it never issued.

The hold lives in the database, NOT in memory. collect_day sweeps immediately on
startup, so under `restart: unless-stopped` an in-memory hold would be forgotten
by exactly the crash an outage tends to cause — the same argument that makes the
budget counter persistent.

CLI:
    python -m src.data.incidents --status --provider here
    python -m src.data.incidents --clear  --provider here
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

import requests

from src.data import store

# Hold length by consecutive-failure count: 15, 30, 60, 120, then capped.
BACKOFF_MINUTES = [15, 30, 60, 120, 240]

# A rate limit or a bad key will not fix itself inside one sweep — stop at once.
# A network blip or a 5xx might, so allow a couple of points to fail first.
ABORT_IMMEDIATELY = {"rate_limit", "auth"}
ABORT_AFTER_CONSECUTIVE = 3


class ProviderError(RuntimeError):
    """A provider-level failure: the API itself is unavailable, refusing, or angry.

    Inherits RuntimeError so existing broad handlers keep working. `kind` is one
    of rate_limit | auth | server_error | network | other.
    """

    def __init__(self, message: str, kind: str = "other", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def classify(exc: BaseException, provider: str = "") -> ProviderError:
    """Map a raised exception onto a ProviderError with a usable `kind`.

    Returns the exception unchanged when it is already a ProviderError.
    """
    if isinstance(exc, ProviderError):
        return exc

    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        code = exc.response.status_code
        if code == 429:
            kind = "rate_limit"
        elif code in (401, 403):
            kind = "auth"
        elif code >= 500:
            kind = "server_error"
        else:
            kind = "other"
        return ProviderError(f"{provider} HTTP {code}", kind=kind, status=code)

    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return ProviderError(f"{provider} unreachable: {exc}", kind="network")

    return ProviderError(f"{provider}: {exc}", kind="other")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def backoff_minutes(consecutive: int) -> int:
    """Hold length for the Nth consecutive failure (1-based)."""
    i = max(1, consecutive) - 1
    return BACKOFF_MINUTES[min(i, len(BACKOFF_MINUTES) - 1)]


def record(provider: str, err: ProviderError, requests_issued: int = 0) -> dict:
    """Log a failure, extend the hold, and return the resulting hold state."""
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT consecutive FROM api_hold WHERE provider = ?", (provider,)).fetchone()
        consecutive = (int(row[0]) if row else 0) + 1
        mins = backoff_minutes(consecutive)
        until = _now() + timedelta(minutes=mins)
        conn.execute(
            "INSERT INTO api_incidents (provider, occurred_utc, kind, http_status, "
            "detail, requests_issued, consecutive) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (provider, _now().isoformat(), err.kind, err.status,
             str(err)[:500], requests_issued, consecutive))
        conn.execute(
            "INSERT INTO api_hold (provider, consecutive, hold_until_utc) VALUES (?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET consecutive = excluded.consecutive, "
            "hold_until_utc = excluded.hold_until_utc",
            (provider, consecutive, until.isoformat()))
        conn.commit()
    finally:
        conn.close()
    return {"provider": provider, "kind": err.kind, "consecutive": consecutive,
            "hold_minutes": mins, "hold_until_utc": until.isoformat()}


def record_success(provider: str) -> None:
    """Clear the hold after a sweep that actually collected. Cheap no-op when clean."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT consecutive FROM api_hold WHERE provider = ?", (provider,)).fetchone()
        if row and int(row[0]) != 0:
            conn.execute(
                "UPDATE api_hold SET consecutive = 0, hold_until_utc = NULL "
                "WHERE provider = ?", (provider,))
            conn.commit()
    finally:
        conn.close()


def hold_state(provider: str) -> dict:
    """Current hold: {holding, consecutive, hold_until_utc, minutes_remaining}."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT consecutive, hold_until_utc FROM api_hold WHERE provider = ?",
            (provider,)).fetchone()
    finally:
        conn.close()
    if not row or not row[1]:
        return {"provider": provider, "holding": False, "consecutive": int(row[0]) if row else 0,
                "hold_until_utc": None, "minutes_remaining": 0}
    until = datetime.fromisoformat(row[1])
    remaining = (until - _now()).total_seconds() / 60
    return {"provider": provider, "holding": remaining > 0, "consecutive": int(row[0]),
            "hold_until_utc": row[1], "minutes_remaining": max(0, round(remaining))}


def recent(limit: int = 20) -> list[dict]:
    """Newest incidents first — for the /data page."""
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT provider, occurred_utc, kind, http_status, detail, requests_issued, "
            "consecutive FROM api_incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    keys = ("provider", "occurred_utc", "kind", "http_status", "detail",
            "requests_issued", "consecutive")
    return [dict(zip(keys, r)) for r in rows]


def clear(provider: str) -> None:
    """Drop the hold so the next sweep runs immediately. Admin/testing only."""
    conn = store.connect()
    try:
        conn.execute("DELETE FROM api_hold WHERE provider = ?", (provider,))
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Provider failure log and back-off state.")
    ap.add_argument("--provider", default="here")
    ap.add_argument("--status", action="store_true", help="Show hold state + recent failures.")
    ap.add_argument("--clear", action="store_true", help="Drop the hold and resume now.")
    args = ap.parse_args()

    if args.clear:
        clear(args.provider)
        print(f"[incidents] Hold cleared for {args.provider}; the next sweep will run.")

    if args.status or not args.clear:
        h = hold_state(args.provider)
        if h["holding"]:
            print(f"[incidents] {args.provider}: HOLDING for another "
                  f"{h['minutes_remaining']} min "
                  f"({h['consecutive']} consecutive failures)")
        else:
            print(f"[incidents] {args.provider}: not holding "
                  f"({h['consecutive']} consecutive failures recorded)")
        rows = recent(10)
        if rows:
            print("\nRecent failures:")
            for r in rows:
                st = f" HTTP {r['http_status']}" if r["http_status"] else ""
                print(f"  {r['occurred_utc'][:19]}  {r['kind']:12s}{st}  "
                      f"issued={r['requests_issued']}  #{r['consecutive']}")
        else:
            print("\nNo failures recorded.")


if __name__ == "__main__":
    main()
