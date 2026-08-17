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
import csv
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from src.data import store

# Hold length by consecutive-failure count: 15, 30, 60, 120, then capped.
BACKOFF_MINUTES = [15, 30, 60, 120, 240]

# Failed CALLS since the last successful sweep that trip the hard stop. The timed
# back-off handles a blip; this handles a provider that is simply broken, and it
# does not clear itself — a person has to look and decide.
DEFAULT_LATCH_AFTER = 25

# A rate limit, a rejected key or a MISSING key will not fix itself inside one
# sweep — stop at once. A network blip or a 5xx might, so allow a couple of
# points to fail first.
ABORT_IMMEDIATELY = {"rate_limit", "auth", "config"}
ABORT_AFTER_CONSECUTIVE = 3

# Failures where the request never left this machine, so the provider never saw
# it and nobody can bill for it. The sweep must give these back to the monthly
# budget in full. "config" is the unset-key case: without a key there is no
# request to send, and charging the cap for it would silently drain the month's
# quota while collecting nothing — which is exactly what happened on the first
# deployment, 16 phantom calls per sweep.
UNSENT_KINDS = {"network", "config"}


class ProviderError(RuntimeError):
    """A provider-level failure: the API itself is unavailable, refusing, or angry.

    Inherits RuntimeError so existing broad handlers keep working. `kind` is one
    of rate_limit | auth | server_error | network | other.

    `evidence` carries the provider's own identifiers for the failed call —
    correlation id, request id, SLO tier, their Date header, latency and the
    error payload. Those are what a support ticket has to quote, so they are
    captured at the moment of failure and never reconstructed later.
    """

    def __init__(self, message: str, kind: str = "other", status: int | None = None,
                 evidence: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.evidence = evidence or {}


def evidence_from_response(resp, url: str = "", latency_ms: float | None = None,
                           sample_point: str = "") -> dict:
    """Pull the provider's trace identifiers off a response.

    HERE returns X-Correlation-ID, X-Request-Id and x-slo (the service tier the
    call was rated against). Their Date header is the timestamp in THEIR clock,
    which is what matches their logs during a dispute.
    """
    h = getattr(resp, "headers", {}) or {}
    body = ""
    try:
        body = (resp.text or "")[:2000]
    except Exception:  # noqa: BLE001 — a body we cannot read must not mask the failure
        body = ""
    return {
        "endpoint": url.split("?")[0][:300],
        "correlation_id": h.get("X-Correlation-ID") or h.get("x-correlation-id"),
        "request_id": h.get("X-Request-Id") or h.get("x-request-id"),
        "slo": h.get("x-slo") or h.get("X-SLO"),
        "server_date": h.get("Date"),
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "response_body": body,
        "sample_point": sample_point or None,
    }


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


def resolve_latch_threshold(provider: str, cli_value: int | None = None) -> int:
    """Failed calls that trip the hard stop. CLI, then env, then DEFAULT_LATCH_AFTER."""
    if cli_value is not None:
        return cli_value if cli_value > 0 else 10 ** 9
    for var in (f"{provider.upper()}_FAILURE_LATCH", "PROVIDER_FAILURE_LATCH"):
        raw = os.environ.get(var, "").strip()
        if raw:
            try:
                val = int(raw)
            except ValueError:
                raise RuntimeError(
                    f"{var}={raw!r} is not an integer. Set it to a whole number of "
                    f"failed calls, or unset it for the default of "
                    f"{DEFAULT_LATCH_AFTER}.")
            return val if val > 0 else 10 ** 9
    return DEFAULT_LATCH_AFTER


def record(provider: str, err: ProviderError, requests_issued: int = 0,
           failed_calls: int = 1, latch_after: int | None = None) -> dict:
    """Log a failure with its evidence, extend the hold, and latch if warranted.

    `failed_calls` is how many individual calls failed in this sweep. Once the
    running total since the last success reaches the latch threshold, collection
    STOPS until a human resets it — a timed back-off alone would keep probing a
    provider that has been broken for hours.
    """
    threshold = latch_after if latch_after is not None else resolve_latch_threshold(provider)
    ev = err.evidence or {}
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT consecutive, failed_calls, latched_utc FROM api_hold WHERE provider = ?",
            (provider,)).fetchone()
        consecutive = (int(row[0]) if row else 0) + 1
        total_failed = (int(row[1]) if row and row[1] else 0) + max(0, failed_calls)
        already_latched = bool(row and row[2])
        mins = backoff_minutes(consecutive)
        until = _now() + timedelta(minutes=mins)

        latched_utc = row[2] if already_latched else None
        latch_reason = None
        if not already_latched and total_failed >= threshold:
            latched_utc = _now().isoformat()
            latch_reason = (f"{total_failed} failed calls since the last successful "
                            f"sweep (limit {threshold}); last failure: {err.kind}"
                            + (f" HTTP {err.status}" if err.status else ""))

        conn.execute(
            "INSERT INTO api_incidents (provider, occurred_utc, kind, http_status, detail, "
            "requests_issued, consecutive, endpoint, correlation_id, request_id, slo, "
            "server_date, latency_ms, response_body, sample_point) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (provider, _now().isoformat(), err.kind, err.status, str(err)[:500],
             requests_issued, consecutive, ev.get("endpoint"), ev.get("correlation_id"),
             ev.get("request_id"), ev.get("slo"), ev.get("server_date"),
             ev.get("latency_ms"), ev.get("response_body"), ev.get("sample_point")))
        conn.execute(
            "INSERT INTO api_hold (provider, consecutive, hold_until_utc, failed_calls, "
            "latched_utc, latch_reason) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET consecutive = excluded.consecutive, "
            "hold_until_utc = excluded.hold_until_utc, failed_calls = excluded.failed_calls, "
            "latched_utc = COALESCE(api_hold.latched_utc, excluded.latched_utc), "
            "latch_reason = COALESCE(api_hold.latch_reason, excluded.latch_reason)",
            (provider, consecutive, until.isoformat(), total_failed,
             latched_utc, latch_reason))
        conn.commit()
    finally:
        conn.close()
    return {"provider": provider, "kind": err.kind, "consecutive": consecutive,
            "hold_minutes": mins, "hold_until_utc": until.isoformat(),
            "failed_calls": total_failed, "latch_threshold": threshold,
            "latched": bool(latched_utc), "latch_reason": latch_reason}


def record_success(provider: str) -> None:
    """Reset the back-off and the failed-call tally after a sweep that collected.

    Deliberately does NOT clear the latch. Once the hard stop has engaged only a
    person clears it, which is the whole point of it.
    """
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT consecutive, failed_calls FROM api_hold WHERE provider = ?",
            (provider,)).fetchone()
        if row and (int(row[0]) != 0 or int(row[1] or 0) != 0):
            conn.execute(
                "UPDATE api_hold SET consecutive = 0, hold_until_utc = NULL, "
                "failed_calls = 0 WHERE provider = ?", (provider,))
            conn.commit()
    finally:
        conn.close()


def latch_state(provider: str) -> dict:
    """Hard-stop state: {latched, latched_utc, latch_reason, failed_calls, threshold}."""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT failed_calls, latched_utc, latch_reason FROM api_hold WHERE provider = ?",
            (provider,)).fetchone()
    finally:
        conn.close()
    try:
        threshold = resolve_latch_threshold(provider)
    except RuntimeError:
        threshold = DEFAULT_LATCH_AFTER
    return {"provider": provider,
            "latched": bool(row and row[1]),
            "latched_utc": row[1] if row else None,
            "latch_reason": row[2] if row else None,
            "failed_calls": int(row[0] or 0) if row else 0,
            "threshold": threshold}


def reset_latch(provider: str, by: str = "manual") -> dict:
    """Clear the hard stop and the back-off so collection resumes on the next slot."""
    conn = store.connect()
    try:
        conn.execute(
            "UPDATE api_hold SET latched_utc = NULL, latch_reason = NULL, "
            "failed_calls = 0, consecutive = 0, hold_until_utc = NULL WHERE provider = ?",
            (provider,))
        conn.execute(
            "INSERT INTO api_incidents (provider, occurred_utc, kind, detail, "
            "requests_issued, consecutive) VALUES (?,?,?,?,?,?)",
            (provider, _now().isoformat(), "resumed",
             f"Collection resumed by {by}.", 0, 0))
        conn.commit()
    finally:
        conn.close()
    return latch_state(provider)


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


_EVIDENCE_COLUMNS = (
    "id", "provider", "occurred_utc", "kind", "http_status", "endpoint",
    "correlation_id", "request_id", "slo", "server_date", "latency_ms",
    "requests_issued", "consecutive", "sample_point", "detail", "response_body",
)


def recent(limit: int = 20) -> list[dict]:
    """Newest incidents first — for the /data page."""
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT provider, occurred_utc, kind, http_status, detail, requests_issued, "
            "consecutive, correlation_id, request_id, slo, latency_ms "
            "FROM api_incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    keys = ("provider", "occurred_utc", "kind", "http_status", "detail",
            "requests_issued", "consecutive", "correlation_id", "request_id",
            "slo", "latency_ms")
    return [dict(zip(keys, r)) for r in rows]


def outages(provider: str = "here", gap_minutes: int = 60) -> list[dict]:
    """Group consecutive failures into outage WINDOWS.

    A dispute is argued per outage, not per request: 'you were down from X to Y,
    we made N calls into it, here are the correlation ids'. Failures more than
    `gap_minutes` apart start a new window.
    """
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT occurred_utc, kind, http_status, correlation_id, request_id, slo, "
            "requests_issued FROM api_incidents WHERE provider = ? AND kind != 'resumed' "
            "ORDER BY occurred_utc", (provider,)).fetchall()
    finally:
        conn.close()

    windows: list[dict] = []
    for occurred, kind, status, corr, req, slo, issued in rows:
        t = datetime.fromisoformat(occurred)
        if windows and (t - datetime.fromisoformat(windows[-1]["last_utc"])
                        ).total_seconds() <= gap_minutes * 60:
            w = windows[-1]
            w["last_utc"] = occurred
            w["failures"] += 1
            w["billable_calls"] += int(issued or 0)
            w["kinds"].add(kind)
            if status:
                w["statuses"].add(status)
            if corr:
                w["correlation_ids"].append(corr)
            if req:
                w["request_ids"].append(req)
        else:
            windows.append({
                "provider": provider, "first_utc": occurred, "last_utc": occurred,
                "failures": 1, "billable_calls": int(issued or 0),
                "kinds": {kind}, "statuses": {status} if status else set(),
                "correlation_ids": [corr] if corr else [],
                "request_ids": [req] if req else [], "slo": slo,
            })
    for w in windows:
        span = (datetime.fromisoformat(w["last_utc"])
                - datetime.fromisoformat(w["first_utc"])).total_seconds() / 60
        w["duration_minutes"] = round(span, 1)
        w["kinds"] = sorted(w["kinds"])
        w["statuses"] = sorted(w["statuses"])
    return list(reversed(windows))


def export_evidence(provider: str = "here", path: str | None = None,
                    since: str | None = None) -> str:
    """Write every recorded failure to CSV, with the provider's own trace ids.

    This is the attachment for a support ticket. It carries, per failed call:
    the provider's X-Correlation-ID and X-Request-Id, the SLO tier the call was
    rated against, their Date header (their clock, not ours), our latency, and
    their error payload.
    """
    path = path or f"{provider}_api_incidents.csv"
    where = "WHERE provider = ?"
    args: list = [provider]
    if since:
        where += " AND occurred_utc >= ?"
        args.append(since)
    conn = store.connect()
    try:
        rows = conn.execute(
            f"SELECT {','.join(_EVIDENCE_COLUMNS)} FROM api_incidents {where} "
            f"ORDER BY occurred_utc", args).fetchall()
    finally:
        conn.close()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_EVIDENCE_COLUMNS)
        w.writerows(rows)
    return path


def clear(provider: str) -> None:
    """Drop the hold so the next sweep runs immediately. Admin/testing only."""
    conn = store.connect()
    try:
        conn.execute("DELETE FROM api_hold WHERE provider = ?", (provider,))
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Provider failure log, back-off and hard stop.")
    ap.add_argument("--provider", default="here")
    ap.add_argument("--status", action="store_true", help="Show state + recent failures.")
    ap.add_argument("--clear", action="store_true", help="Drop the timed hold only.")
    ap.add_argument("--resume", action="store_true",
                    help="Clear the HARD STOP and resume collection.")
    ap.add_argument("--outages", action="store_true", help="Group failures into windows.")
    ap.add_argument("--export", metavar="CSV", nargs="?", const="",
                    help="Write the full evidence log to CSV for a support ticket.")
    args = ap.parse_args()

    if args.resume:
        st = reset_latch(args.provider, by="CLI")
        print(f"[incidents] Hard stop cleared for {args.provider}. Collection resumes "
              f"at the next scheduled slot.")

    if args.clear:
        clear(args.provider)
        print(f"[incidents] Timed hold cleared for {args.provider}.")

    if args.export is not None:
        p = export_evidence(args.provider, args.export or None)
        print(f"[incidents] Evidence written -> {p}")
        print("  Columns include the provider's own X-Correlation-ID / X-Request-Id, "
              "the SLO tier and their Date header.")

    if args.outages:
        ws = outages(args.provider)
        if not ws:
            print(f"[incidents] No outages recorded for {args.provider}.")
        for w in ws:
            print(f"\n[outage] {w['first_utc'][:19]} -> {w['last_utc'][:19]} UTC "
                  f"({w['duration_minutes']} min)")
            print(f"  failures={w['failures']}  calls that reached them="
                  f"{w['billable_calls']}  kinds={','.join(w['kinds'])}"
                  + (f"  http={','.join(str(s) for s in w['statuses'])}" if w["statuses"] else ""))
            if w["slo"]:
                print(f"  SLO tier: {w['slo']}")
            if w["correlation_ids"]:
                ids = w["correlation_ids"]
                print(f"  correlation ids ({len(ids)}): {', '.join(ids[:4])}"
                      + (" ..." if len(ids) > 4 else ""))

    if args.status or not (args.clear or args.resume or args.outages
                           or args.export is not None):
        lat = latch_state(args.provider)
        h = hold_state(args.provider)
        if lat["latched"]:
            print(f"[incidents] {args.provider}: STOPPED since {lat['latched_utc'][:19]} UTC")
            print(f"  {lat['latch_reason']}")
            print(f"  No calls will be made until you resume: "
                  f"python -m src.data.incidents --resume --provider {args.provider}")
        elif h["holding"]:
            print(f"[incidents] {args.provider}: HOLDING for another "
                  f"{h['minutes_remaining']} min ({h['consecutive']} consecutive failures)")
        else:
            print(f"[incidents] {args.provider}: collecting normally")
        print(f"  failed calls since last success: {lat['failed_calls']}/{lat['threshold']}")
        rows = recent(10)
        if rows:
            print("\nRecent failures:")
            for r in rows:
                st = f" HTTP {r['http_status']}" if r["http_status"] else ""
                cid = f"  corr={r['correlation_id'][:8]}" if r.get("correlation_id") else ""
                print(f"  {r['occurred_utc'][:19]}  {r['kind']:12s}{st}  "
                      f"issued={r['requests_issued']}  #{r['consecutive']}{cid}")
        else:
            print("\nNo failures recorded.")


if __name__ == "__main__":
    main()
