# Mumbai Traffic Network Planning & Decision-Support Tool

A computational tool that models Mumbai's road network under current and projected
traffic demand, identifies bottlenecks, and evaluates the impact of infrastructure
interventions (new tunnels, road widening, new links, signal changes, diversion).

**Core question:** *"If we build intervention X (e.g., Thane–Borivali tunnel), how does
total network congestion (TSTT) change under current and projected 2035/2045 demand?"*

This is a **planning-timescale** tool (months/years), not a real-time monitoring or
navigation system. See [`mumbai-traffic-planning-project-plan.md`](mumbai-traffic-planning-project-plan.md)
for the full baseline plan.

---

## Baseline scope

Single pilot corridor: **Western Express Highway, Dahisar → Bandra (~25 km)** — not all of MMR.

The baseline pipeline (Layers 1→5):

1. **Network model** — attributed road graph from OpenStreetMap (osmnx)
2. **Demand model** — synthetic OD matrix via gravity model
3. **Traffic assignment** — static User Equilibrium (Frank-Wolfe)
4. **Scenario evaluation** — before/after TSTT for interventions
5. **Reporting** — V/C congestion maps, bottleneck ranking

---

## Setup

Requires **Python 3.11+** (developed on 3.12) and `git`.

```bash
# Create and activate a virtual environment
py -3.12 -m venv .venv
.venv\Scripts\activate        # Windows (PowerShell/cmd)
# source .venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt
```

### API keys (Phase 0.7)

Copy `.env.example` to `.env` and add your **TomTom** key (primary traffic-data source;
free tier, no credit card — sign up at [developer.tomtom.com](https://developer.tomtom.com/)).
`.env` is git-ignored.

```bash
cp .env.example .env
```

Test the key and collect data via the cached client:

```bash
python -m src.data.tomtom_client flow  --point 19.250,72.856
python -m src.data.tomtom_client route --from 19.250,72.856 --to 19.055,72.840
```

Every response is cached to `data/raw/tomtom/` — the cache **is** the dataset; the same
query is never paid for twice.

---

## Project structure

```
data/          raw/ (OSM, census, cached API responses), processed/, validation/
src/           network/  demand/  assignment/  scenarios/  viz/
notebooks/     01_explore_network → 05_validation
docs/          assumptions.md, data_sources.md, calibration_log.md
```

## Status

Phase 0 (Setup & Data Acquisition) — in progress. See the project plan for the phased roadmap.
