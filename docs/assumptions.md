# Assumptions Register

Every simplification in the baseline model is recorded here. Each entry: what we assumed,
why, and what would change it. This is a living document — update it as the model evolves.

| # | Assumption | Rationale | Revisit when |
|---|-----------|-----------|--------------|
| A1 | Traffic assignment remains a WEH Dahisar→Bandra pilot; the wider BMC/MMRDA map is an OSM junction inventory only | Regional geometry can guide fresh collection without inventing traffic conditions outside the calibrated corridor | Expand assignment only after enough real intersection readings and OD evidence have been collected |
| A2 | Fixed OD demand before/after interventions (no induced demand) | Correct elasticity needs data we don't have yet; flagged limitation | Adding demand elasticity upgrade (plan §7) |
| A3 | Static User Equilibrium (not Dynamic Traffic Assignment) | Sufficient for "which link is the bottleneck?"; DTA needs time-varying OD | Time-of-day profiles / operational module added |
| A4 | Lane count used directly; road width never measured | No API provides width; OSM tags `lanes=*`; effective width handled via capacity reduction factor | Never (settled decision D3) |
| A5 | BPR parameters α=0.15, β=4 (US defaults) | Standard starting point; not yet calibrated for Indian mixed traffic | Google travel-time calibration (Phase 3) |
| A6 | Free-flow speed imputed from OSM road class where `maxspeed` absent | OSM speed tags sparse in Mumbai (only ~16% of major links tagged) | Manual verification of major links (Phase 1) |
| A7 | CTS-controlled gravity-model OD matrix: Western Suburbs totals use MMRDA CTSU 2026 controls (6.01m population, 2.89m employment), while allocation across the 11 locality zones remains synthetic | CTS Executive Summary publishes cluster controls but not its 1,810-zone planning table or OD matrix | Replace locality shares when CTS TAZ-level data is obtained |
| A8 | Lane counts imputed by road class where OSM untagged (84% of links) | OSM `lanes` sparse | Manual satellite verification of WEH spine links |
| A9 | Capacity in PCU/h; encroachment factor 0.85 (parking/hawkers/bus stops) | Mumbai effective width 30–40% below nominal (plan §3.1) | Calibrate against MCGM counts |
| A10 | Stopped-vehicle bottleneck = capacity multiplier `μ_incident` (§1.5), not microsimulation | Keeps the static-UE baseline; `curve_area` calibrated to HCM incident tables | SUMO microsimulation phase |
| A11 | TAZs are locality-latitude bands, not census wards; connectors are restricted to arterial nodes in the network's largest strongly connected component | Ward shapefiles not yet acquired; mutually reachable connectors prevent silent OD-demand loss | Replace with CTS/ward polygons once GIS data is collected |
| A12 | Incident network-wide TSTT can fall slightly under static UE (converse-Braess) | Static UE assumes instant perfect rerouting; incidents are really dynamic | Report corridor through-time + link delay as the intuitive incident metrics; add DTA later |
| A13 | Regional "processing rate" = per-zone cap on trips emitted/absorbed per hour | Simple gateway/discharge throughput limit | Calibrate against observed zone gateway volumes |
| A14 | Ingoing/outgoing flow rates set via demand-total + per-zone scales | Lets flow regime be varied for robustness (§6.3) without new data | Replace with real time-of-day demand profiles |

## Phase 0 network-extraction notes

- Bounding box: N 19.270 / S 19.045 / E 72.885 / W 72.820 (≈ Dahisar check naka → Bandra, ~2 km buffer).
- osmnx `network_type="drive"`, `simplify=True`, `retain_all=False`.
- Raw extract: **15,106 nodes / 33,788 edges / ~2,954 km** — includes full residential fabric.
  Phase 1 will trim to the ~50–200 major links (motorway/trunk/primary/secondary) that carry corridor traffic.
- Edge speeds/travel times from `ox.add_edge_speeds` + `ox.add_edge_travel_times` (imputed free-flow, pre-calibration).
- The BMC/MMRDA coverage inventory uses OSM only for road geometry, names, classes, and
  junction locations. It assigns no traffic values. A junction remains **awaiting collection**
  until a HERE or TomTom request succeeds at that location.
- BMC membership is stored explicitly and the MMRDA view is constructed as the union of
  BMC and MMRDA-only locations; therefore every BMC junction also appears in MMRDA.
