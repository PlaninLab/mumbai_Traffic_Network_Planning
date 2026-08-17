# Data Sources & Provenance

Tracks the origin, vintage, and access method of every dataset used in the model.

## Layer 1 — Road network

| Dataset | Source | Vintage | Access | Status |
|---------|--------|---------|--------|--------|
| Corridor road graph | OpenStreetMap via Overpass (osmnx) | Live snapshot @ extraction | `src/network/build_network.py` | ✅ acquired (Phase 0) |
| BMC/MMRDA major-road and junction inventory | OpenStreetMap via Overpass (osmnx) | Live snapshot @ extraction | `src/network/coverage.py` | ✅ geometry only; traffic readings start empty |
| Lane counts (major links) | OSM `lanes=*` + Google satellite manual verify | — | Manual | ⬜ Phase 1 |
| Link capacities | Computed: `lanes × per-lane capacity × PCU adj.` (IRC values) | — | Derived | ⬜ Phase 1 |

**OSM extract provenance:** bbox N 19.270 / S 19.045 / E 72.885 / W 72.820, `network_type=drive`.
Cached at `data/raw/osm/corridor.graphml` and `data/processed/network_corridor.gpkg`.

The regional inventory is deliberately not a traffic model. OSM supplies only the real
major-road topology and junction coordinates. Provider observations collected later are
stored separately in SQLite; the UI never substitutes modeled or synthetic values for an
uncollected regional junction. BMC is a subset of the MMRDA inventory by construction.

## Layer 2 — Demand

| Dataset | Source | Vintage | Access | Status |
|---------|--------|---------|--------|--------|
| Ward population | Census of India | 2011 | censusindia.gov.in | ⬜ Phase 0.8 |
| Employment centers | OSM POI + public knowledge (BKC, Andheri MIDC, …) | — | Public | ⬜ Phase 0.8 |
| Trip generation rates | CTS-2 / IRC guidelines | 2021 / — | Report | ⬜ Phase 2 |

## Travel time / calibration — PRIMARY: TomTom (plan §3.2, decision D5)

| Dataset | Source (endpoint) | Access | Status |
|---------|-------------------|--------|--------|
| Per-segment current + free-flow speed | TomTom **Flow Segment Data** | Key in `.env` | ✅ key active; collection pending |
| Corridor OD travel times (validation) | TomTom **Routing API** | Same key | ✅ tested; collection pending |
| TAZ-to-TAZ cost matrix (gravity model) | TomTom **Matrix Routing API** | Same key | ⬜ Phase 2 |
| Segment/route travel times (fallback) | Google Routes API | Needs billing account | ⬜ only if TomTom gaps found |

**Client:** [`src/data/tomtom_client.py`](../src/data/tomtom_client.py) — cached wrapper for all
three endpoints. Key read from `TOMTOM_API_KEY` (git-ignored `.env`).

**Caching rule (plan §3.2):** every API response saved to `data/raw/tomtom/<endpoint>/<hash>.json`
with a UTC timestamp and the query params. Never query the same coordinate + time-of-day twice —
**the cache IS the dataset.**

**Collection schedule (2,500/day free tier, ~1,000–1,500 calls over ~10 days):**
- Day 1–3: Flow Segment — ~50 WEH points × AM peak / PM peak / off-peak (~450 calls)
- Day 4–5: Routing — 25–30 OD pairs × AM/PM peak (~120 calls)
- Day 6: Matrix Routing — TAZ-to-TAZ matrix (1–2 calls)
- Day 7–10: repeat Day 1–3 on other weekdays for day-to-day consistency

## ToS / licensing notes

- OSM: ODbL — attribution required, share-alike on derived DB.
- TomTom: free tier permits research/non-commercial use; per-segment `currentSpeed` +
  `freeFlowSpeed` + `confidence` in one call. HTTP 429 on daily-limit — no charge.
- Google Maps Platform (fallback only): use Routes/Distance Matrix API programmatically; do
  **not** scrape traffic tiles or cache map imagery. Response data may be cached. (Plan §3.2)
