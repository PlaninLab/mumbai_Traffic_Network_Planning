"""
apicache.py — shared on-disk cache for third-party API calls.

Generalises the caching rule already used for TomTom (plan §3.2: "the cache IS
the dataset") so the HERE and Google clients reuse the exact same behaviour:
every response is written to data/raw/<provider>/<endpoint>/<hash>.json with the
request identity (secrets excluded) and a UTC timestamp; a repeat call with the
same identity returns the cached copy and makes no network request.

The .env loader is shared with tomtom_client so all provider keys
(TOMTOM_API_KEY, HERE_API_KEY, GOOGLE_MAPS_API_KEY) load from the one git-ignored
.env file.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.data.tomtom_client import load_env  # reuse the single .env loader

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = REPO_ROOT / "data" / "raw"
DEFAULT_TIMEOUT = 30


def get_key(env_var: str, hint: str) -> str:
    """Read an API key from the environment (loading .env first)."""
    load_env()
    val = os.environ.get(env_var, "").strip()
    if not val:
        raise RuntimeError(
            f"{env_var} not set. Add it to your git-ignored .env file "
            f"(see .env.example). {hint}")
    return val


def _cache_path(provider: str, endpoint: str, ident: dict) -> Path:
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return RAW_ROOT / provider / endpoint / f"{h}.json"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write(path: Path, ident: dict, response) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "identity": ident,
        "response": response,
    }
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def cached_request(provider: str, endpoint: str, url: str, *,
                   ident: dict, method: str = "GET",
                   params: dict | None = None, json_body: dict | None = None,
                   headers: dict | None = None, use_cache: bool = True,
                   timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Cached HTTP request. `ident` uniquely identifies the request WITHOUT secrets
    (used as the cache key). Returns the parsed JSON body."""
    path = _cache_path(provider, endpoint, {**ident, "_endpoint": endpoint})
    if use_cache:
        hit = _read(path)
        if hit is not None:
            return hit["response"]

    if method.upper() == "POST":
        resp = requests.post(url, params=params, json=json_body, headers=headers, timeout=timeout)
    else:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)

    if resp.status_code == 429:
        raise RuntimeError(f"{provider} rate/quota limit hit (HTTP 429). Try later.")
    resp.raise_for_status()
    body = resp.json()
    _write(path, {**ident, "_endpoint": endpoint}, body)
    return body
