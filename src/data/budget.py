"""
budget.py — monthly API-call cap for the metered flow providers.

WHY THIS EXISTS
---------------
`collect_day` takes a reading immediately on startup, before its first sleep.
Deployed with `restart: unless-stopped`, any crash that recurs becomes a restart
loop that bills a full corridor sweep per cycle — against a provider that charges
per transaction. Nothing in the collector stopped that.

This module is the client-side stop. The count lives in the SAME SQLite file as
the readings (data/processed/traffic.db), which sits on the deployment's
persistent volume, so it SURVIVES A RESTART — an in-memory counter would be
useless against exactly the failure it is meant to bound.

RESERVE, DON'T REPORT
---------------------
Calls are reserved BEFORE a sweep runs, not recorded after it finishes. A sweep
killed halfway still consumes its reservation. That over-counts slightly when a
run is interrupted, which is the safe direction: the alternative (record on
completion) lets a crash loop bill indefinitely while never recording anything.

MONTH BOUNDARY
--------------
Months are keyed in UTC. The provider's real reset boundary is not documented
here and may be account-anniversary based, so treat this as an approximation —
it is defence in depth, not the authority. Set a spending alert with the provider
too; that is the hard stop.

CLI:
    python -m src.data.budget --status --provider here --limit 38000
    python -m src.data.budget --reset --provider here
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone

from src.data import store

# Per-provider override first, then the generic fallback.
ENV_TEMPLATE = "{provider}_MONTHLY_CALL_LIMIT"
ENV_GENERIC = "API_MONTHLY_CALL_LIMIT"


class BudgetExhausted(RuntimeError):
    """Raised when a sweep would push the month past its configured call cap."""


def month_key(now: datetime | None = None) -> str:
    """Billing month as 'YYYY-MM', in UTC. See the module docstring."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def resolve_limit(provider: str, cli_value: int | None = None) -> int | None:
    """Monthly cap for a provider: CLI wins, then env, then unset (no cap).

    A cap of 0 or a negative number is treated as 'no cap' so an empty env var
    can never accidentally freeze collection.
    """
    if cli_value is not None:
        return cli_value if cli_value > 0 else None
    for var in (ENV_TEMPLATE.format(provider=provider.upper()), ENV_GENERIC):
        raw = os.environ.get(var, "").strip()
        if raw:
            try:
                val = int(raw)
            except ValueError:
                raise RuntimeError(
                    f"{var}={raw!r} is not an integer. Set it to a whole number of "
                    f"API calls per month, or unset it to run without a cap.")
            return val if val > 0 else None
    return None


def used(provider: str, month: str | None = None,
         conn: sqlite3.Connection | None = None) -> int:
    """Calls already reserved for this provider in the given month (default: now)."""
    month = month or month_key()
    own = conn is None
    conn = conn or store.connect()
    try:
        row = conn.execute(
            "SELECT calls FROM api_usage WHERE provider = ? AND month = ?",
            (provider, month)).fetchone()
    finally:
        if own:
            conn.close()
    return int(row[0]) if row else 0


def reserve(provider: str, n_calls: int, limit: int | None,
            month: str | None = None) -> int:
    """Reserve n_calls against the month's cap. Returns the new running total.

    Raises BudgetExhausted (making NO reservation) when the sweep would exceed
    the cap. With limit=None the reservation is still recorded, so usage stays
    visible even when no cap is set.
    """
    month = month or month_key()
    conn = store.connect()
    try:
        # One transaction: read and write cannot interleave with another sweep.
        conn.execute("BEGIN IMMEDIATE")
        current = used(provider, month, conn=conn)
        if limit is not None and current + n_calls > limit:
            conn.rollback()
            raise BudgetExhausted(
                f"{provider}: {current:,} of {limit:,} calls used this month "
                f"({month} UTC); a sweep of {n_calls} would exceed the cap by "
                f"{current + n_calls - limit:,}.")
        conn.execute(
            "INSERT INTO api_usage (provider, month, calls) VALUES (?, ?, ?) "
            "ON CONFLICT(provider, month) DO UPDATE SET calls = calls + excluded.calls",
            (provider, month, n_calls))
        conn.commit()
        return current + n_calls
    finally:
        conn.close()


def refund(provider: str, n_calls: int, month: str | None = None) -> int:
    """Return calls that were reserved but never issued. Returns the new total.

    Only ever called after a sweep decides to stop early, so a process killed
    mid-sweep still forfeits its reservation — that forfeit is what bounds a
    crash loop, and refunding on completion does not weaken it.
    """
    if n_calls <= 0:
        return used(provider, month)
    month = month or month_key()
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE api_usage SET calls = MAX(0, calls - ?) WHERE provider = ? AND month = ?",
            (n_calls, provider, month))
        conn.commit()
        return used(provider, month, conn=conn)
    finally:
        conn.close()


def status(provider: str, limit: int | None = None, month: str | None = None) -> dict:
    """Human/JSON-friendly view of this month's usage."""
    month = month or month_key()
    n = used(provider, month)
    out = {
        "provider": provider,
        "month_utc": month,
        "calls_used": n,
        "calls_limit": limit,
        "exhausted": bool(limit is not None and n >= limit),
    }
    if limit:
        out["calls_remaining"] = max(0, limit - n)
        out["pct_used"] = round(100 * n / limit, 1)
    return out


def reset(provider: str, month: str | None = None) -> None:
    """Clear a provider's counter for a month. Admin/testing only."""
    month = month or month_key()
    conn = store.connect()
    try:
        conn.execute("DELETE FROM api_usage WHERE provider = ? AND month = ?",
                     (provider, month))
        conn.commit()
    finally:
        conn.close()


def all_usage() -> list[dict]:
    """Every (provider, month, calls) row, newest month first — for the /data page."""
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT provider, month, calls FROM api_usage ORDER BY month DESC, provider"
        ).fetchall()
    finally:
        conn.close()
    return [{"provider": p, "month_utc": m, "calls": c} for p, m, c in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="Monthly API-call cap for flow providers.")
    ap.add_argument("--provider", default="here", help="Provider key (default here).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap to report against (default: from the environment).")
    ap.add_argument("--status", action="store_true", help="Show this month's usage.")
    ap.add_argument("--reset", action="store_true", help="Clear this month's counter.")
    args = ap.parse_args()

    if args.reset:
        reset(args.provider)
        print(f"[budget] Cleared {args.provider} usage for {month_key()} (UTC).")

    if args.status or not args.reset:
        limit = resolve_limit(args.provider, args.limit)
        s = status(args.provider, limit)
        cap = f"{limit:,}" if limit else "no cap set"
        print(f"[budget] {s['provider']} — {s['month_utc']} (UTC)")
        print(f"[budget] Used {s['calls_used']:,} of {cap}"
              + (f"  ({s['pct_used']}%, {s['calls_remaining']:,} left)" if limit else ""))
        if s["exhausted"]:
            print("[budget] EXHAUSTED — the collector will make no further calls this month.")


if __name__ == "__main__":
    main()
