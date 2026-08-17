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
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from src.data import budget, incidents, store

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
PROCESSED = REPO_ROOT / "data" / "processed"
TEMPLATES = Path(__file__).resolve().parent / "templates"

app = FastAPI(title="Mumbai Traffic Network Planning", version="1.0")
templates = Jinja2Templates(directory=str(TEMPLATES))

# Serve docs/ (maps, charts) as static assets for the dashboard.
if DOCS.exists():
    app.mount("/assets", StaticFiles(directory=str(DOCS)), name="assets")


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
    """Provider failures and the current back-off. Never raises."""
    try:
        return {"hold": incidents.hold_state(provider), "recent": incidents.recent(15)}
    except Exception:  # noqa: BLE001 — telemetry must not take the page down
        return {"hold": {"holding": False, "consecutive": 0}, "recent": []}


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
    hold = incidents_view()["hold"]
    inv_totals = store.inventory()["totals"]
    return {"status": "ok",
            "has_segments": (PROCESSED / "segment_overview.json").exists(),
            "has_scenarios": (PROCESSED / "scenario_comparison.csv").exists(),
            "has_report": (DOCS / "report.html").exists(),
            # Collection state — lets a monitor tell "out of quota" apart from
            # "collector is dead", which look identical from the outside.
            "readings": inv_totals.get("readings", 0),
            "last_reading_ist": inv_totals.get("last_ist"),
            "calls_used": b.get("calls_used"),
            "calls_limit": b.get("calls_limit"),
            "budget_exhausted": b.get("exhausted", False),
            # Distinguishes "provider is down" from "collector is dead" and from
            # "quota reached" — three states that look identical from outside.
            "provider_hold": hold.get("holding", False),
            "provider_hold_minutes": hold.get("minutes_remaining", 0),
            "consecutive_failures": hold.get("consecutive", 0)}
