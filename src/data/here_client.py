"""
here_client.py — cached client for the HERE Traffic API v7 (flow).

HERE gives the same segment-flow primitive as TomTom (current speed + free-flow
speed per road segment) plus a jam factor (0–10 congestion index), from a deep
connected-vehicle probe fleet. Used as a high-quality alternative flow source for
BPR/TTI calibration (project plan §3.2, decision D17).

Endpoint (v7):
    GET https://data.traffic.hereapi.com/v7/flow
        ?in=circle:LAT,LON;r=RADIUS
        &locationReferencing=shape
        &apiKey=...

Response speeds are in METRES/SECOND — normalised to km/h here. We return the
same normalised shape every collector understands:
    {current_kph, free_kph, confidence, jam_factor, road_closure}

Key: HERE_API_KEY (git-ignored .env). Get one at https://platform.here.com.

CLI:
    python -m src.data.here_client flow --point 19.115,72.860
"""

from __future__ import annotations

import argparse
import json

from src.data import apicache

PROVIDER = "here"
FLOW_URL = "https://data.traffic.hereapi.com/v7/flow"
MS_TO_KPH = 3.6


def _key() -> str:
    return apicache.get_key("HERE_API_KEY",
                            "Get one free at https://platform.here.com (Traffic API v7).")


def flow_point(lat: float, lon: float, radius_m: int = 150, use_cache: bool = True) -> dict:
    """Normalised flow near a point. Returns
    {current_kph, free_kph, confidence, jam_factor, road_closure}."""
    key = _key()
    ident = {"lat": round(lat, 5), "lon": round(lon, 5), "r": radius_m}
    params = {
        "in": f"circle:{lat:.5f},{lon:.5f};r={radius_m}",
        "locationReferencing": "shape",
        "apiKey": key,
    }
    body = apicache.cached_request(PROVIDER, "flow", FLOW_URL,
                                   ident=ident, params=params, use_cache=use_cache)
    return _normalize(body)


def _normalize(body: dict) -> dict:
    """Pick the highest-confidence result and normalise to km/h."""
    results = body.get("results", []) if isinstance(body, dict) else []
    best = None
    for r in results:
        cf = r.get("currentFlow") or {}
        if cf.get("speed") is None:
            continue
        conf = cf.get("confidence", 0) or 0
        if best is None or conf > (best.get("confidence") or 0):
            best = cf
    if best is None:
        return {"current_kph": None, "free_kph": None, "confidence": None,
                "jam_factor": None, "road_closure": None}
    cur = best.get("speed")
    free = best.get("freeFlow")
    traversability = best.get("traversability", "open")
    return {
        "current_kph": round(cur * MS_TO_KPH, 1) if cur is not None else None,
        "free_kph": round(free * MS_TO_KPH, 1) if free is not None else None,
        "confidence": best.get("confidence"),
        "jam_factor": best.get("jamFactor"),
        "road_closure": (traversability == "closed"),
    }


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Query HERE Traffic v7 flow (cached).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("flow")
    pf.add_argument("--point", required=True, help="'lat,lon'")
    args = ap.parse_args()
    if args.cmd == "flow":
        lat, lon = (float(x) for x in args.point.split(","))
        d = flow_point(lat, lon)
        print(json.dumps(d, indent=2))
        if d["current_kph"] and d["free_kph"]:
            print(f"\nTTI = free/current = {d['free_kph'] / d['current_kph']:.3f}")


if __name__ == "__main__":
    _cli()
