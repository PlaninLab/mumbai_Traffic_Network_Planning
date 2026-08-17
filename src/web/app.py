"""
app.py — FastAPI host for the Mumbai Traffic decision-support tool.

Serves three things:
  - a live DASHBOARD (/) summarising the two weekday data segments (peak vs
    average-delay) and the scenario comparison, rebuilt from disk on each request
    so scheduled collections show up immediately;
  - the full stakeholder REPORT (/report), the self-contained HTML deliverable;
  - a small read-only JSON API (/api/segments, /api/scenarios, /api/health) for
    embedding or programmatic access.

It is intentionally READ-ONLY: it never calls TomTom (so the API key is never
exposed to the web) — data collection is a separate scheduled job. Run it with:

    uvicorn src.web.app:app --host 0.0.0.0 --port 8000

See the README "Hosting" section for Docker / PaaS deployment.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src.data import budget, incidents, store

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
PROCESSED = REPO_ROOT / "data" / "processed"
TEMPLATES = Path(__file__).resolve().parent / "templates"
COVERAGE_SEED = Path(
    os.environ.get("COVERAGE_SEED_PATH", REPO_ROOT / "data-seed" / "coverage.json")
)

app = FastAPI(title="Mumbai Traffic Network Planning", version="1.0")
templates = Jinja2Templates(directory=str(TEMPLATES))

# Serve docs/ (maps, charts) as static assets for the dashboard.
if DOCS.exists():
    app.mount("/assets", StaticFiles(directory=str(DOCS)), name="assets")

# Static assets for the interactive corridor map (JS app, CSS, vendored deck.gl).
STATIC = Path(__file__).resolve().parent / "static"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# --- data loaders (read fresh each call; degrade gracefully if missing) --------

def load_segments() -> dict:
    p = PROCESSED / "segment_overview.json"
    if not p.exists():
        return {"segments": {}, "peak_vs_avg": {"note": "No segment summary yet."},
                "missing": True}
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def load_scenarios() -> list[dict]:
    p = PROCESSED / "scenario_comparison.csv"
    if not p.exists():
        return []
    df = pd.read_csv(p)
    # JSON-safe: replace inf/NaN (e.g. non-clearing queue) with a sentinel string.
    df = df.where(pd.notna(df), None)
    records = df.to_dict(orient="records")
    for r in records:
        for k, v in list(r.items()):
            if isinstance(v, float) and v == float("inf"):
                r[k] = "∞"
    return records


# --- routes --------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    segments = load_segments()
    scenarios = load_scenarios()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"segments": segments, "scenarios": scenarios,
         "has_report": (DOCS / "report.html").exists()},
    )


@app.get("/map", response_class=HTMLResponse)
def corridor_map(request: Request):
    return templates.TemplateResponse(request, "map.html", {})


@app.get("/glossary", response_class=HTMLResponse)
def glossary(request: Request):
    return templates.TemplateResponse(request, "glossary.html", {})


# Payload names the map API may serve — a fixed allowlist, never the raw path.
# ``coverage`` is the real OSM major-road/junction inventory for the nested BMC
# and MMRDA views. It deliberately contains no inferred traffic observations.
MAP_PAYLOADS = {"network", "intersections", "frames", "od", "here", "summary",
                "coverage"}


@app.get("/api/map/{name}")
def api_map(name: str):
    if name not in MAP_PAYLOADS:
        return JSONResponse({"error": "unknown payload"}, status_code=404)
    p = PROCESSED / "map" / f"{name}.json"
    if name == "coverage" and not p.exists() and COVERAGE_SEED.exists():
        # An existing Docker named volume can hide the copy shipped under
        # data/processed. Serve the immutable seed until the collector installs
        # its writable copy into that shared volume on the first sweep.
        p = COVERAGE_SEED
    if not p.exists():
        return JSONResponse(
            {"error": f"{name}.json not built yet. "
                      "Run: python -m src.viz.map_export"}, status_code=404)
    return FileResponse(p, media_type="application/json")


@app.get("/report", response_class=HTMLResponse)
def report():
    p = DOCS / "report.html"
    if not p.exists():
        return HTMLResponse("<h1>Report not generated yet.</h1>"
                            "<p>Run <code>python -m src.viz.report</code>.</p>", status_code=404)
    return FileResponse(p)


@app.get("/api/segments")
def api_segments():
    return JSONResponse(load_segments())


@app.get("/api/scenarios")
def api_scenarios():
    return JSONResponse(load_scenarios())


def budget_view(provider: str = "here") -> dict:
    """This month's metered-call usage. Never raises — the dashboard must render
    even when the store is missing entirely."""
    try:
        limit = budget.resolve_limit(provider)
        return budget.status(provider, limit)
    except Exception:  # noqa: BLE001 — a broken budget row must not take the page down
        return {"provider": provider, "calls_used": None, "calls_limit": None,
                "exhausted": False}


def incidents_view(provider: str = "here") -> dict:
    """Provider failures, back-off and hard-stop state. Never raises."""
    try:
        return {"hold": incidents.hold_state(provider),
                "latch": incidents.latch_state(provider),
                "recent": incidents.recent(15),
                "outages": incidents.outages(provider)[:5],
                "can_resume": bool(os.environ.get("ADMIN_TOKEN", "").strip())}
    except Exception:  # noqa: BLE001 — telemetry must not take the page down
        return {"hold": {"holding": False, "consecutive": 0},
                "latch": {"latched": False, "failed_calls": 0,
                          "threshold": incidents.DEFAULT_LATCH_AFTER},
                "recent": [], "outages": [], "can_resume": False}


@app.post("/api/collector/resume")
async def resume_collection(request: Request, provider: str = "here"):
    """Clear the hard stop so collection restarts at the next slot.

    Guarded by ADMIN_TOKEN. This endpoint restarts spending against a metered API,
    so on a dashboard that may be publicly reachable it must not be open — with no
    token configured it REFUSES rather than defaulting to open.

    The token is read from the urlencoded body (or an X-Admin-Token header) rather
    than declared with fastapi.Form, which would pull in python-multipart purely
    for this one field — and would fail at import time, taking the whole dashboard
    down, on any deployment that lacks it.
    """
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        return JSONResponse(
            {"error": "Resume is disabled. Set ADMIN_TOKEN on the web service to "
                      "enable it, or run: python -m src.data.incidents --resume"},
            status_code=403)

    token = request.headers.get("X-Admin-Token", "")
    if not token:
        # Parse the urlencoded body directly. Starlette's request.form() needs
        # python-multipart to be present to return anything here, and adding that
        # dependency for one field would also make the whole dashboard fail to
        # import wherever it is missing.
        try:
            raw = (await request.body()).decode("utf-8", "replace")
            token = parse_qs(raw).get("token", [""])[0]
        except Exception:  # noqa: BLE001 — a malformed body is simply not a token
            token = ""
    if not secrets.compare_digest(str(token).strip(), expected):
        return JSONResponse({"error": "Wrong token."}, status_code=403)

    incidents.reset_latch(provider, by="dashboard")
    return RedirectResponse("/data", status_code=303)


@app.get("/data", response_class=HTMLResponse)
def data_inventory(request: Request):
    return templates.TemplateResponse(
        request,
        "data.html",
        {"inv": store.inventory(), "budget": budget_view(),
         "usage_history": budget.all_usage(), "incidents": incidents_view()},
    )


@app.get("/api/data")
def api_data():
    return JSONResponse({"inventory": store.inventory(), "budget": budget_view(),
                         "incidents": incidents_view()})


@app.get("/api/health")
def health():
    b = budget_view()
    iv = incidents_view()
    hold, latch = iv["hold"], iv["latch"]
    inv_totals = store.inventory()["totals"]
    regional = store.intersection_inventory()
    coverage_path = PROCESSED / "map" / "coverage.json"
    return {"status": "ok",
            "has_segments": (PROCESSED / "segment_overview.json").exists(),
            "has_scenarios": (PROCESSED / "scenario_comparison.csv").exists(),
            "has_report": (DOCS / "report.html").exists(),
            "has_regional_coverage": coverage_path.exists() or COVERAGE_SEED.exists(),
            # Collection state — lets a monitor tell "out of quota" apart from
            # "collector is dead", which look identical from the outside.
            "readings": inv_totals.get("readings", 0),
            "last_reading_ist": inv_totals.get("last_ist"),
            "regional_readings": regional["readings"],
            "regional_rows": regional["rows"],
            "regional_points_collected": regional["points"],
            "last_regional_reading_utc": regional["last_utc"],
            "regional_collection_scope": os.environ.get(
                "REGIONAL_COLLECTION_SCOPE"
            ),
            "regional_collection_mode": os.environ.get(
                "REGIONAL_COLLECTION_MODE"
            ),
            "regional_mmrda_next_offset": store.load_collection_cursor(
                "intersection_readings:mmrda"
            ),
            "calls_used": b.get("calls_used"),
            "calls_limit": b.get("calls_limit"),
            "budget_exhausted": b.get("exhausted", False),
            # Distinguishes "provider is down" from "collector is dead" and from
            # "quota reached" — three states that look identical from outside.
            "provider_hold": hold.get("holding", False),
            "provider_hold_minutes": hold.get("minutes_remaining", 0),
            "consecutive_failures": hold.get("consecutive", 0),
            # Hard stop: collection will NOT resume without a person.
            "collection_stopped": latch.get("latched", False),
            "stopped_reason": latch.get("latch_reason"),
            "failed_calls": latch.get("failed_calls", 0),
            "failed_calls_limit": latch.get("threshold")}
