"""
tomtom_client.py — Cached client for TomTom traffic-data APIs.

Primary traffic-data source for the project (project plan §3.2, decision D5).
Wraps three TomTom endpoints, each with on-disk JSON caching so we never pay a
request twice:

  - Flow Segment Data  -> per-point currentSpeed / freeFlowSpeed / travel times
  - Routing API        -> point-to-point travel time (with traffic) + geometry
  - Matrix Routing API -> batch origin x destination travel-time matrix

Caching rule (plan §3.2): "The cache IS the dataset — the API is just the
collection mechanism. Never query the same coordinate + time-of-day twice."
Every response is written to data/raw/tomtom/<endpoint>/<hash>.json together with
the request parameters and a UTC timestamp. A repeat call with identical
parameters returns the cached copy and makes no network request.

The API key is read from the TOMTOM_API_KEY environment variable (loaded from the
git-ignored .env file by `load_env()`), never hard-coded.

Free tier: 2,500 requests/day across all endpoints (HTTP 429 when exhausted —
no charge, resume next day).

CLI:
    python -m src.data.tomtom_client flow --point 19.250,72.856
    python -m src.data.tomtom_client route --from 19.250,72.856 --to 19.055,72.840
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = REPO_ROOT / "data" / "raw" / "tomtom"
ENV_PATH = REPO_ROOT / ".env"

BASE = "https://api.tomtom.com"
DEFAULT_TIMEOUT = 30


# --------------------------------------------------------------------------- #
# Environment / key handling
# --------------------------------------------------------------------------- #
def load_env(env_path: Path = ENV_PATH) -> None:
    """Minimal .env loader (avoids a python-dotenv dependency).

    Sets os.environ for KEY=VALUE lines not already present in the environment.
    """
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def get_api_key() -> str:
    load_env()
    key = os.environ.get("TOMTOM_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "TOMTOM_API_KEY not set. Copy .env.example to .env and add your key, "
            "or export TOMTOM_API_KEY in the environment."
        )
    return key


# --------------------------------------------------------------------------- #
# Caching core
# --------------------------------------------------------------------------- #
def _cache_key(endpoint: str, params: dict) -> str:
    """Stable hash of the request identity (key excluded so it never hits disk)."""
    ident = {k: v for k, v in params.items() if k != "key"}
    ident["_endpoint"] = endpoint
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _cache_path(endpoint: str, params: dict) -> Path:
    return CACHE_ROOT / endpoint / f"{_cache_key(endpoint, params)}.json"


def _read_cache(path: Path):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _write_cache(path: Path, params: dict, response: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "params": {k: v for k, v in params.items() if k != "key"},
        "response": response,
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _get(url: str, params: dict, endpoint: str, use_cache: bool = True) -> dict:
    """GET with transparent on-disk caching. Returns the parsed response body."""
    path = _cache_path(endpoint, params)
    if use_cache:
        cached = _read_cache(path)
        if cached is not None:
            return cached["response"]

    # Typed failures — see the note in apicache.cached_request.
    from src.data import incidents

    point = params.get("point", "")
    started = time.monotonic()
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise incidents.ProviderError(
            f"TomTom unreachable: {e}", kind="network",
            evidence={"endpoint": url.split("?")[0][:300], "sample_point": point or None,
                      "latency_ms": int((time.monotonic() - started) * 1000),
                      "response_body": f"{type(e).__name__}: {e}"[:2000]}) from e
    ev = incidents.evidence_from_response(
        resp, url=url, latency_ms=(time.monotonic() - started) * 1000, sample_point=point)
    if resp.status_code == 429:
        raise incidents.ProviderError(
            "TomTom free-tier daily limit hit (HTTP 429). Resume tomorrow.",
            kind="rate_limit", status=429, evidence=ev)
    if resp.status_code in (401, 403):
        raise incidents.ProviderError(
            f"TomTom rejected the API key (HTTP {resp.status_code}).",
            kind="auth", status=resp.status_code, evidence=ev)
    if resp.status_code >= 500:
        raise incidents.ProviderError(
            f"TomTom server error (HTTP {resp.status_code}).",
            kind="server_error", status=resp.status_code, evidence=ev)
    resp.raise_for_status()
    body = resp.json()
    _write_cache(path, params, body)
    return body


# --------------------------------------------------------------------------- #
# Endpoint wrappers
# --------------------------------------------------------------------------- #
def flow_segment(point: str, zoom: int = 10, unit: str = "KMPH", use_cache: bool = True) -> dict:
    """Flow Segment Data for a lat,lon point. Returns the flowSegmentData dict.

    Keys: currentSpeed, freeFlowSpeed, currentTravelTime, freeFlowTravelTime,
    confidence, roadClosure, coordinates.
    """
    key = get_api_key()
    url = f"{BASE}/traffic/services/4/flowSegmentData/absolute/{zoom}/json"
    params = {"point": point, "unit": unit, "key": key}
    body = _get(url, params, endpoint="flow_segment", use_cache=use_cache)
    return body["flowSegmentData"]


def route(origin: str, destination: str, use_cache: bool = True) -> dict:
    """Point-to-point route with live traffic. origin/destination = 'lat,lon'.

    Returns a summary dict: lengthInMeters, travelTimeInSeconds,
    trafficDelayInSeconds, noTrafficTravelTimeInSeconds.
    """
    key = get_api_key()
    loc = f"{origin}:{destination}"
    url = f"{BASE}/routing/1/calculateRoute/{loc}/json"
    params = {"traffic": "true", "key": key}
    body = _get(url, params, endpoint="route", use_cache=use_cache)
    return body["routes"][0]["summary"]


def matrix(origins: list[str], destinations: list[str], use_cache: bool = True) -> dict:
    """Batch OD travel-time matrix via TomTom Matrix Routing (sync, v2).

    origins/destinations: lists of 'lat,lon' strings. Returns the raw response.
    Note: Matrix Routing is a POST endpoint; caching keys off the JSON body.
    """
    key = get_api_key()
    url = f"{BASE}/routing/matrix/2"

    def _pts(items: list[str]) -> list[dict]:
        out = []
        for s in items:
            lat, lon = (float(x) for x in s.split(","))
            out.append({"point": {"latitude": lat, "longitude": lon}})
        return out

    payload = {"origins": _pts(origins), "destinations": _pts(destinations)}
    # Cache identity includes the payload + endpoint.
    cache_params = {"body": json.dumps(payload, sort_keys=True)}
    path = _cache_path("matrix", cache_params)
    if use_cache:
        cached = _read_cache(path)
        if cached is not None:
            return cached["response"]

    resp = requests.post(
        url,
        params={"key": key, "routeType": "fastest", "traffic": "live"},
        json=payload,
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code == 429:
        raise RuntimeError("TomTom free-tier daily limit hit (HTTP 429). Resume tomorrow.")
    resp.raise_for_status()
    body = resp.json()
    _write_cache(path, cache_params, body)
    return body


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli() -> None:
    parser = argparse.ArgumentParser(description="Query TomTom traffic APIs (cached).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_flow = sub.add_parser("flow", help="Flow Segment Data for a point")
    p_flow.add_argument("--point", required=True, help="'lat,lon'")

    p_route = sub.add_parser("route", help="Point-to-point route")
    p_route.add_argument("--from", dest="origin", required=True, help="'lat,lon'")
    p_route.add_argument("--to", dest="destination", required=True, help="'lat,lon'")

    args = parser.parse_args()

    if args.cmd == "flow":
        d = flow_segment(args.point)
        tti = d["currentTravelTime"] / d["freeFlowTravelTime"]
        print(json.dumps(d, indent=2))
        print(f"\nTTI (current/free-flow travel time) = {tti:.3f}")
    elif args.cmd == "route":
        s = route(args.origin, args.destination)
        print(json.dumps(s, indent=2))


if __name__ == "__main__":
    _cli()
