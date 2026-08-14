# Data Sources & Provenance

Tracks the origin, vintage, and access method of every dataset used in the model.

## Layer 1 — Road network

| Dataset | Source | Vintage | Access | Status |
|---------|--------|---------|--------|--------|
| Corridor road graph | OpenStreetMap via Overpass (osmnx) | Live snapshot @ extraction | `src/network/build_network.py` | ✅ acquired (Phase 0) |
| Lane counts (major links) | OSM `lanes=*` + Google satellite manual verify | — | Manual | ⬜ Phase 1 |
| Link capacities | Computed: `lanes × per-lane capacity × PCU adj.` (IRC values) | — | Derived | ⬜ Phase 1 |

**OSM extract provenance:** bbox N 19.270 / S 19.045 / E 72.885 / W 72.820, `network_type=drive`.
Cached at `data/raw/osm/corridor.graphml` and `data/processed/network_corridor.gpkg`.

## Layer 2 — Demand

| Dataset | Source | Vintage | Access | Status |
|---------|--------|---------|--------|--------|
| Ward population | Census of India | 2011 | censusindia.gov.in | ⬜ Phase 0.8 |
| Employment centers | OSM POI + public knowledge (BKC, Andheri MIDC, …) | — | Public | ⬜ Phase 0.8 |
| Trip generation rates | CTS-2 / IRC guidelines | 2021 / — | Report | ⬜ Phase 2 |

## Travel time / calibration

| Dataset | Source | Access | Status |
|---------|--------|--------|--------|
| Peak-hour OD travel times | Google Routes API (free tier) | API key needed (`.env`) | ⬜ Phase 0.7 blocks this |
| Free-flow travel times | Google Routes API (~3 AM departure) | Same | ⬜ Phase 0.7 |
| Segment speeds (supplementary) | TomTom Traffic Flow API (free tier) | API key | ⬜ optional |

**Caching rule (plan §3.2):** every API response saved to `data/raw/google_api/` or
`data/raw/tomtom/` as timestamped JSON. Never query the same OD pair + departure time twice.

## ToS / licensing notes

- OSM: ODbL — attribution required, share-alike on derived DB.
- Google Maps Platform: use Routes/Distance Matrix API only; do **not** scrape traffic tiles
  or cache map imagery. Response data (times/distances) may be cached for the project. (Plan §3.2)
- TomTom: free tier permits research/non-commercial use.
