"""
google_client.py — cached client for the Google Maps Platform Routes API.

Google's real-time + predictive traffic model has the best coverage on Indian
urban roads (huge Android/Maps probe base). We use it for the ONE thing it does
best for this project: **traffic-aware origin→destination travel-time matrices**
to replace the synthetic free-flow cost matrix in the gravity model
(project plan §3.2, decision D17).

Endpoint (Routes API v2, Route Matrix):
    POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix
    headers: X-Goog-Api-Key, X-Goog-FieldMask
    body: { origins[], destinations[], travelMode: DRIVE,
            routingPreference: TRAFFIC_AWARE, departureTime? }

Key: GOOGLE_MAPS_API_KEY (git-ignored .env). Requires a billing-enabled Google
Cloud project with the Routes API enabled (see README §7.7).

CLI:
    python -m src.data.google_client matrix --from 19.25,72.86 --to 19.05,72.84
"""

from __future__ import annotations

import argparse
import json

from src.data import apicache

PROVIDER = "google"
MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"


def _key() -> str:
    return apicache.get_key(
        "GOOGLE_MAPS_API_KEY",
        "Enable the Routes API on a billing-enabled Google Cloud project (README §7.7).")


def _waypoint(latlon: str) -> dict:
    lat, lon = (float(x) for x in latlon.split(","))
    return {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lon}}}}


def route_matrix(origins: list[str], destinations: list[str],
                 departure_time: str | None = None, use_cache: bool = True) -> list[dict]:
    """Traffic-aware OD matrix. origins/destinations = 'lat,lon' strings.

    Returns the raw element list: each item has originIndex, destinationIndex,
    duration ('123s'), distanceMeters, condition. `departure_time` is an RFC-3339
    UTC timestamp (must be in the future for live traffic; omit for now).
    """
    key = _key()
    body = {
        "origins": [_waypoint(o) for o in origins],
        "destinations": [_waypoint(d) for d in destinations],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }
    if departure_time:
        body["departureTime"] = departure_time
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": "originIndex,destinationIndex,duration,distanceMeters,condition",
    }
    ident = {"origins": origins, "destinations": destinations, "dep": departure_time}
    return apicache.cached_request(PROVIDER, "route_matrix", MATRIX_URL,
                                   ident=ident, method="POST", json_body=body,
                                   headers=headers, use_cache=use_cache)


def elements_to_minutes(elems: list[dict], n: int, m: int) -> list[list[float]]:
    """Pure parse of Route Matrix elements -> n×m minutes matrix (inf where missing)."""
    M = [[float("inf")] * m for _ in range(n)]
    for e in elems:
        i, j = e.get("originIndex"), e.get("destinationIndex")
        dur = e.get("duration")  # e.g. "845s"
        if i is None or j is None or dur is None:
            continue
        M[i][j] = float(str(dur).rstrip("s")) / 60.0
    return M


def matrix_minutes(origins: list[str], destinations: list[str],
                   departure_time: str | None = None) -> list[list[float]]:
    """Convenience: n_origins × n_destinations travel-time matrix in MINUTES."""
    elems = route_matrix(origins, destinations, departure_time)
    return elements_to_minutes(elems, len(origins), len(destinations))


def _cli() -> None:
    ap = argparse.ArgumentParser(description="Query Google Routes Route Matrix (cached).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pm = sub.add_parser("matrix")
    pm.add_argument("--from", dest="origin", required=True, help="'lat,lon'")
    pm.add_argument("--to", dest="destination", required=True, help="'lat,lon'")
    args = ap.parse_args()
    if args.cmd == "matrix":
        M = matrix_minutes([args.origin], [args.destination])
        print(json.dumps(M, indent=2))
        print(f"\nTravel time: {M[0][0]:.1f} min")


if __name__ == "__main__":
    _cli()
