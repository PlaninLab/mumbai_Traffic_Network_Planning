# Mumbai Traffic Network Planning & Decision-Support Tool
## Project Plan — Baseline Build

**Document version:** v1.0 — Baseline Plan (Finalized)  
**Date:** August 14, 2026  
**Status:** Plan finalized. Ready for implementation.

---

## 0. Project Identity

**What this is:** A computational tool that models Mumbai's road network under current and projected traffic demand, identifies bottlenecks, and evaluates the impact of infrastructure interventions (new tunnels, road widening, new links, signal changes, traffic diversion).

**What this is NOT:** A real-time traffic monitoring system. Not an N(t) vehicle-counting system. Not a navigation or routing app. The tool operates on planning timescales (months/years), not operational timescales (minutes/hours).

**Core question the tool answers:** "If we build intervention X (e.g., Thane–Borivali tunnel), how does total network congestion change under current demand and projected 2035/2045 demand?"

---

## 1. Theoretical Foundation

### 1.1 The Transportation Network Design Problem (TNDP)

The project is an instance of the TNDP, a well-studied class of problems in transportation engineering.

**Formal structure (bi-level optimization):**

```
UPPER LEVEL (planner):
    minimize  Z(x, y) = Σ_a [ t_a(v_a) × v_a ]     (Total System Travel Time)
    subject to: budget constraint, feasibility of interventions
    decision variables: which links to add/widen/modify (x)

LOWER LEVEL (travelers):
    User Equilibrium (Wardrop's principle):
    At equilibrium, no traveler can reduce their travel time
    by unilaterally switching routes.
    output: link flows v_a = f(demand, network)
```

**Why bi-level?** The planner chooses infrastructure, but cannot dictate which routes drivers take. Drivers selfishly minimize their own travel time (User Equilibrium). The planner must anticipate how drivers will respond to network changes. This is fundamentally different from optimizing a system you fully control.

### 1.2 Key Concepts

**User Equilibrium (UE):** At equilibrium, all used routes between an OD pair have equal travel time, and no unused route has lower travel time. This is the behavioral model of drivers.

**Link Performance Function (BPR function):**
```
t_a(v_a) = t_a^0 × [1 + α × (v_a / C_a)^β]
```
Where:
- t_a^0 = free-flow travel time on link a
- v_a = flow (vehicles/hour) on link a  
- C_a = capacity of link a
- α = 0.15, β = 4 (standard BPR parameters; will need calibration for Indian conditions)

**Total System Travel Time (TSTT):**
```
TSTT = Σ_a [ t_a(v_a) × v_a ]
```
This is the primary objective to minimize. A lower TSTT after an intervention means the network performs better overall.

### 1.3 Why NOT N(t)

The vehicle conservation equation N(t+Δt) = N(t) + Qin·Δt - Qout·Δt is the LWR continuity equation from traffic flow theory (Lighthill-Whitham-Richards, 1950s). It is mathematically correct but serves a different purpose:

- N(t) is a **dynamic, real-time** state variable — useful for signal control, perimeter metering, and operational management.
- Infrastructure planning requires **equilibrium analysis** — what is the steady-state congestion pattern under a given demand and network? This is answered by traffic assignment, not by tracking vehicle accumulation over time.
- N(t) could re-enter the project if an operational control module is added later (Phase 3+), particularly via the Macroscopic Fundamental Diagram (MFD) framework. But it is not the foundation.

### 1.4 Critical Pitfall: Induced Demand

When road capacity increases, additional traffic is generated — people who previously avoided the trip (or took transit, or traveled off-peak) now drive. Empirical studies show ~20% induced traffic on average for new capacity. 

**Implication for the tool:** If the model assumes fixed OD demand before and after an intervention, it will systematically overestimate benefits. The baseline model can start with fixed demand (simplification), but a note/flag must be added, and a demand elasticity factor should be introduced in a subsequent iteration.

The related Braess Paradox (adding a road can increase total travel time) is a theoretical edge case but worth testing: after adding a candidate link, verify that TSTT actually decreased. If it didn't, the tool should flag this.

### 1.5 Stopped-Vehicle Bottlenecks (Incident Capacity Reduction)

A stopped or broken-down vehicle in a running lane is a common and important cause of Mumbai congestion (breakdowns, illegal parking, bus halts, accidents). Such a vehicle does **not** merely remove its own footprint from the road: approaching traffic must deflect laterally around it — a swerve over a taper length — creating a turbulence "shadow" (a flow *curve*) that is unusable for through movement. The effective cross-section available to moving traffic shrinks by **more** than the vehicle's physical size.

**Geometric model:**

```
effective_area = total_area − N × curve_area

Where:
  total_area = W × L_infl        (carriageway width × influence-window length)
  N          = number of stopped vehicles in the (side) lane
  curve_area = deflection-shadow plan area rendered unusable per stopped vehicle
             ≈ veh_width × (veh_length + 2 × taper_length)
```

**From area to capacity.** Link throughput scales with the effective cross-section, so we define an **incident capacity-reduction factor** that multiplies the nominal link capacity:

```
μ_incident  = effective_area / total_area = 1 − N × (curve_area / total_area)
C_effective = C_nominal × max(μ_floor, μ_incident)
```

This effective capacity feeds directly into the BPR function used everywhere else in the model:

```
t_a(v) = t_a^0 × [ 1 + α × (v / C_effective)^β ]
```

So a stopped-vehicle bottleneck raises travel time on the affected link exactly through the same mechanism as any other capacity change — no special-casing in the assignment loop is needed. It can be applied either as a **per-link attribute** (`n_stopped`, default 0) or as a **scenario** (Section 4: "incident on link X").

**Why the curve, not just the footprint.** Pure lane-subtraction underestimates the impact. Empirically (HCM 6th ed. freeway incident tables), a single vehicle fully blocking **one of three lanes** drops capacity to ~49% remaining — a 51% loss, far more than the 33% a naïve 1-of-3 lane count implies. The extra loss is the rubbernecking/merging turbulence the `curve_area` term represents. The model is therefore **calibrated** two ways:

- *Geometric default* (a car partially intruding from the kerb/side lane, ~116 m² shadow) → 8–17% capacity loss on a 4/3/2-lane road, matching HCM **shoulder-incident** values.
- *HCM-calibrated* (a car fully blocking a lane) → `curve_area` back-solved to ~455–588 m² so μ reproduces the HCM **one-lane-blocked** fractions (0.35 / 0.49 / 0.58 for 2 / 3 / 4 lanes).

`α`, `β`, `curve_area`, `taper_length`, and `L_infl` are all calibration parameters (see `docs/calibration_log.md`). Implemented in [`src/network/incident.py`](src/network/incident.py).

---

## 2. System Architecture

### 2.1 Layered Architecture

```
┌─────────────────────────────────────────────────────┐
│                 LAYER 5: REPORTING                   │
│   Visualization, comparison dashboards, outputs      │
├─────────────────────────────────────────────────────┤
│              LAYER 4: SCENARIO EVALUATOR             │
│  Define interventions → re-run assignment → compare  │
│  TSTT, V/C ratios, bottleneck shifts                 │
├─────────────────────────────────────────────────────┤
│            LAYER 3: TRAFFIC ASSIGNMENT               │
│  Given network + OD demand → compute equilibrium     │
│  link flows (Frank-Wolfe / Method of Successive      │
│  Averages for User Equilibrium)                      │
├─────────────────────────────────────────────────────┤
│            LAYER 2: DEMAND MODEL                     │
│  OD matrix: how many trips from zone i to zone j     │
│  during peak hour. Gravity model baseline,           │
│  calibrated with travel time data.                   │
├─────────────────────────────────────────────────────┤
│            LAYER 1: NETWORK MODEL                    │
│  Road graph: nodes (intersections), links (road      │
│  segments) with attributes (lanes, capacity,         │
│  free-flow speed, length)                            │
├─────────────────────────────────────────────────────┤
│              DATA LAYER (external)                   │
│  OSM, TomTom APIs, Census, CTS reports               │
└─────────────────────────────────────────────────────┘
```

### 2.2 Baseline Scope (What We Build First)

For the baseline, we target a **single corridor or subnetwork**, NOT all of MMR.

**Recommended pilot area:** Western Express Highway corridor, from Dahisar to Bandra (~25 km). Rationale:
- Well-known, severe congestion corridor
- Relatively linear topology (simpler network)
- Mix of highway and arterial connections
- Google travel time data is dense here (high smartphone penetration)
- Existing flyovers and the Metro 2A/7 alignment provide natural intervention scenarios to test

**Baseline deliverables:**
1. A working network graph of the corridor with realistic attributes
2. A synthetic OD matrix (gravity model, calibrated to plausible volumes)
3. A static User Equilibrium assignment
4. TSTT computation for the base case
5. At least one intervention scenario (e.g., widening a bottleneck link) with before/after TSTT comparison
6. A basic visualization of link-level congestion (V/C ratios)

---

## 3. Data Sources & Acquisition Plan

### 3.1 Road Network (Layer 1)

| Source | What It Provides | Access | Limitations |
|--------|-------------------|--------|-------------|
| **OpenStreetMap (OSM)** | Road geometry, road type classification, some lane counts, speed limits, one-way info | Free, via Overpass API or .osm download | Lane counts incomplete for many Mumbai roads; speed limits sparsely tagged; no capacity values |
| **MMRDA CTS-2 Report (2021)** | Network description, corridor-level data, some link capacities | Partially public (executive summary); full report may need RTI/institutional request | Consultant-owned models not publicly available |
| **Google Maps (visual inspection)** | Verify road existence, lane counts, intersection geometry | Free (manual) | Not automatable at scale; not a data API |

**Baseline approach:**
1. Download Mumbai OSM data via Overpass API or Geofabrik extract
2. Use `osmnx` (Python) to build a graph of the pilot corridor
3. Manually verify and correct lane counts for major links (30-50 links in the corridor) using Google Maps satellite view
4. Compute free-flow speed from OSM `maxspeed` tags where available; impute from road class otherwise (IRC standards: urban arterial 50-60 km/h, highway 80 km/h, local 30-40 km/h)
5. Compute capacity using: `C = lanes × per_lane_capacity` where per_lane_capacity depends on road type (use IRC/Indian HCM values, adjusted by a PCU factor for mixed traffic)

**PCU (Passenger Car Unit) baseline values for Mumbai mixed traffic:**

| Vehicle Type | PCU Factor (IRC approximate) |
|---|---|
| Car/taxi | 1.0 |
| Two-wheeler | 0.5 |
| Auto-rickshaw | 0.75 |
| Bus | 3.0 |
| LCV | 1.5 |
| Truck | 3.0 |

Effective capacity = nominal capacity × (1 / weighted average PCU based on local traffic composition). Traffic composition for the corridor can be estimated from MCGM traffic count reports or CTS data.

**Why lane count, NOT road width:** No API provides road carriageway width in meters. Attempting to measure width visually from map tiles or satellite imagery would give an approximate number (say, 14m) that you'd divide by standard lane width (3.5m) to get lane count — arriving at the same place via a harder route. OSM already tags `lanes=*` directly, and where it doesn't, counting lanes from satellite view takes seconds per link. Furthermore, effective width in Mumbai is often 30-40% less than nominal width due to on-street parking, hawker encroachment, bus stops, and construction debris — these factors are better handled by a capacity reduction factor applied to the per-lane capacity than by trying to measure "true" width. **Use lane count directly; never measure width.**

### 3.2 Travel Time / Speed Data (for calibration)

**Primary source: TomTom (no credit card required)**

| TomTom API | What It Provides | Free Tier |
|------------|-----------------|-----------|
| **Flow Segment Data** | Per-segment: `currentSpeed`, `freeFlowSpeed`, `currentTravelTime`, `freeFlowTravelTime`, `confidence`, segment coordinates. Pass any lat/lng, get the nearest road segment's data. | 2,500 requests/day |
| **Routing API** | Point-to-point route: travel time (with traffic), distance, route geometry, `trafficDelayInSeconds` | 2,500 requests/day (shared quota) |
| **Matrix Routing API** | Batch OD travel time matrix: up to 700 origin-destination pairs per request | 2,500 requests/day (shared quota) |

**Why TomTom over Google for this project:**
- No credit card or billing account required — sign up with email at developer.tomtom.com
- Flow Segment Data returns `currentSpeed` AND `freeFlowSpeed` in one call (Google requires separate peak and off-peak queries to compute the same ratio)
- Confidence score per segment tells you which data points to trust
- 2,500/day is more than enough: ~50-100 calls per collection session, spread over 1-2 weeks, gives the full calibration dataset
- If daily limit is hit, requests return HTTP 429 (no charge, no surprise bill)
- More permissive licensing than Google for research/non-commercial use

**Secondary/fallback source (only if TomTom coverage is insufficient):**

| Source | What It Provides | Access | When to Use |
|--------|-------------------|--------|-------------|
| **Google Routes API** | Travel time between two points with/without traffic | Requires billing account with credit card ($200/month free credit) | Only if TomTom coverage gaps found for specific Mumbai segments |
| **Google Maps Traffic Layer** | 4-level congestion visualization (green/yellow/orange/red) | Same Google billing account | Full-network congestion snapshot; coarser than TomTom segment data |

**Data caching rule:** Every API response is saved to `data/raw/tomtom/` as JSON with a timestamp and the query parameters. Never query the same coordinate + time-of-day twice. The cache IS the dataset — the API is just the collection mechanism.

**Data collection schedule — two weekday segments (revised, decision D13).**
Rather than a fixed 10-day cadence, collection is organised around **two weekday
segments**, each serving a distinct planning purpose, and automated so they fill in on
their own (`src/data/segments.py`, `src/data/collect_flow.py --segment`):

| Segment | Window (IST, Mon–Fri) | Purpose |
|---------|----------------------|---------|
| **Peak** (office hours) | 08:00–11:00 & 17:30–20:30 | Max time-savings potential; congested-circuit identification; **peak-hour OD-matrix calibration** and BPR β calibration (needs the high-congestion signal) |
| **Average delay** (daytime) | 11:00–17:30 | Everyday "typical delay" baseline; peak-vs-average comparison isolates the *addressable* peak-specific delay |

- ~50 Flow-Segment points per reading along the WEH spine; each run auto-tags its
  segment and rebuilds `data/processed/segment_overview.json`.
- Automated Mon–Fri via `scripts/register_weekday_tasks.ps1` (Windows) or
  `scripts/crontab.example` (Linux host): peak AM (09:00), peak PM (18:45), avg (14:00).
- Off-peak / weekend / holiday readings are still logged but classified **offpeak** and
  excluded from the peak/avg planning analysis (they cannot calibrate β — TTI stays flat).
- Routing / Matrix API for OD travel times as before. Well within the 2,500/day budget.

**Full-day sampling option (decision D16).** For finer temporal resolution,
`src/data/collect_day.py` samples the whole day at a **segment-adaptive cadence — 10 min
in peak windows, 15 min otherwise** — printing the estimated daily API-call count and
warning before the 2,500/day free tier is exceeded (full-day n=50 ≈ 4,050 calls → use
n≈25). A fixed 3-readings/day schedule remains the light-touch default.

**Tabular storage (decision D16).** All readings are written to a single SQLite table
(`data/processed/traffic.db`, `flow_readings`) via `src/data/store.py`, so the observed
data is queryable with SQL rather than scattered across per-run CSVs. Inserts are
idempotent (`UNIQUE(run_id, idx)`); the segment summary reads from the DB (CSV fallback),
while per-run CSVs are still written for calibration input and human inspection. Legacy
CSVs import with `store --backfill`.

**Google Maps Platform ToS note (if Google is used later):**
- DO use Routes/Distance Matrix API programmatically — within ToS.
- DO NOT scrape traffic layer tile images — violates ToS.
- API response data (travel times, distances) can be cached.

**Baseline approach:**
1. Set up TomTom developer account (no credit card) and obtain API key
2. Query Flow Segment Data for ~50 points along WEH at peak and off-peak times
3. From each response, extract `currentSpeed / freeFlowSpeed` ratio → per-segment Travel Time Index (TTI), the direct calibration target for BPR parameters
4. Query Routing API for 25-30 OD pairs to get corridor-level travel times for validation
5. Use Matrix Routing for the TAZ-to-TAZ cost matrix needed by the gravity model

### 3.3 Demand Data (Layer 2)

| Source | What It Provides | Access |
|--------|-------------------|--------|
| **Census 2011 (ward-level)** | Population, workforce participation by ward/zone | Free, census.gov.in |
| **MCGM Development Plan data** | Zonal land use (residential, commercial, industrial) | Partially public |
| **Employment centers** | BKC, Andheri MIDC, Goregaon IT park, etc. — known major attractors | Public knowledge + OSM POI data |
| **CTS-2 OD data** | If obtainable: calibrated OD matrices for MMR | Needs institutional access (MMRDA/LEA) |
| **TomTom Matrix Routing API** | Travel times between zones → gravity model cost matrix | ₹0 (free tier, no credit card) |

**Baseline approach (Gravity Model):**

```
T_ij = k × (P_i × A_j) / f(c_ij)

Where:
  T_ij = trips from zone i to zone j
  P_i  = production (e.g., residential population of zone i)
  A_j  = attraction (e.g., employment in zone j)
  c_ij = travel cost (free-flow travel time between i and j)
  f()  = deterrence function, typically f(c) = c^(-β) or exp(-β·c)
  k    = scaling constant
  β    = calibration parameter
```

Zone the corridor into ~10-15 traffic analysis zones (TAZs) based on ward boundaries. Use census population for production, estimated employment for attraction. Calibrate β so that the resulting assignment produces link volumes and travel times roughly consistent with observed TomTom travel times.

This is explicitly a SYNTHETIC baseline. The OD matrix will be approximate. That's acceptable for a first iteration — the architecture matters more than the calibration at this stage.

### 3.4 Validation Data

| Source | What It Provides | Access |
|--------|-------------------|--------|
| **TomTom Routing API travel times** | Observed corridor travel times to compare against model predictions | ₹0 (free tier, no credit card) |
| **MCGM traffic count data** | Manual/TIRTL traffic counts at selected intersections | Request from MCGM Traffic Planning dept |
| **Mumbai Traffic Police reports** | Aggregate traffic statistics, accident data | Partially public |
| **Personal observation / field visits** | Ground-truth for specific bottleneck locations, queue lengths | Free |

**Baseline validation target:** The model's predicted travel times for the corridor (post-assignment) should be within ±25-30% of Google-observed travel times during peak hour. This is a realistic tolerance for a baseline model. Professional planning models target ±15% after full calibration.

---

## 4. Tech Stack

### 4.1 Core Stack

| Component | Tool | Rationale |
|-----------|------|-----------|
| **Language** | Python 3.11+ | Ecosystem dominance in transportation modeling, geospatial, scientific computing |
| **Network construction** | `osmnx` | Purpose-built for extracting road networks from OSM into NetworkX graphs. Active maintenance, well-documented. |
| **Graph operations** | `networkx` | Shortest path algorithms (Dijkstra), graph manipulation. Foundation for assignment. |
| **Geospatial** | `geopandas`, `shapely` | Zonal operations, spatial joins, geometry handling |
| **Numeric / scientific** | `numpy`, `scipy` | Matrix operations, optimization (scipy.optimize for gravity model calibration) |
| **Visualization** | `matplotlib`, `folium` or `kepler.gl` | Static plots for analysis; interactive maps for network visualization |
| **Data storage** | GeoJSON / GeoPackage files (baseline); PostGIS if scaling | No need for a database in baseline; flat files are fine |
| **API interaction** | `requests` | TomTom Routing, Traffic Flow, Matrix Routing API calls. No special client library needed — standard REST with API key in query parameter. |
| **Notebook environment** | Jupyter Lab | Iterative exploration, documentation-as-you-go |

### 4.2 Simulation Tools (Post-Baseline)

| Tool | When to Introduce | Use Case |
|------|-------------------|----------|
| **SUMO** (Simulation of Urban Mobility) | Phase 2+ | Microscopic simulation for detailed corridor analysis, signal timing evaluation. Imports OSM networks directly via `netconvert`. Open-source (Eclipse Public License). |
| **MATSim** | Phase 3+ (if scaling to full MMR) | Agent-based, activity-driven simulation. Handles millions of agents. Good for full metropolitan demand modeling. Open-source (GPL). |
| **AequilibraE** | Consider for baseline (recommended) | Open-source Python transportation modeling library. Has traffic assignment (Frank-Wolfe), OD matrix manipulation, network handling, QGIS integration. Actively maintained. **Strongly consider using this instead of writing custom assignment code** — it handles convergence, multi-class assignment, and link performance functions correctly out of the box. Writing your own Frank-Wolfe is educational but error-prone. |

### 4.3 What We Are NOT Using (and Why)

| Tool | Why Not (for baseline) |
|------|----------------------|
| PTV Visum / TransCAD | Commercial, expensive ($10K+/year licenses). Appropriate for institutional use but not for a research/startup baseline. |
| TensorFlow / PyTorch | No ML needed in baseline. The problem is optimization and simulation, not prediction. ML may enter later for demand forecasting. |
| Real-time streaming infra (Kafka, etc.) | This is not a real-time system. |
| QGIS as core tool | Useful for visual inspection but not for computation. Use `geopandas` instead. |

---

## 5. Implementation Plan — Phased

### Phase 0: Setup & Data Acquisition (Week 1-2)

**Objective:** Have raw materials ready.

```
Tasks:
├── Set up Python environment (requirements.txt, git repo)
├── Download Mumbai OSM extract (Geofabrik or Overpass)
├── Extract pilot corridor network using osmnx
│   └── Bounding box: approx Dahisar to Bandra, ±2km buffer
├── Set up TomTom developer account (no credit card) + get API key
├── Collect census ward-level population data for corridor zones
├── Identify major employment centers in/near corridor
└── Read Zhang et al. (2025) arXiv:2507.00306 — full paper
```

**Deliverable:** A git repo with raw data files, environment setup, and a notebook that loads and displays the corridor network on a map.

### Phase 1: Network Model (Week 2-3)

**Objective:** A clean, attributed road graph ready for assignment.

```
Tasks:
├── Clean OSM network graph
│   ├── Remove irrelevant edges (footpaths, service roads)
│   ├── Simplify topology (merge consecutive edges with no intersections)
│   └── Handle one-way streets, turn restrictions (if needed)
├── Attribute enrichment
│   ├── Verify/correct lane counts for all major links (manual + satellite)
│   ├── Assign free-flow speeds by road class
│   ├── Compute link capacity = lanes × base_capacity × PCU_adjustment
│   └── Compute free-flow travel time = length / free_flow_speed
├── Define Traffic Analysis Zones (TAZs)
│   ├── ~10-15 zones along the corridor
│   ├── Map ward boundaries to zone boundaries
│   └── Assign zone centroids (connected to network)
└── Validation: visual check — does the network look right on a map?
    Does the free-flow travel time from end to end match Google's?
```

**Deliverable:** A `network.gpkg` or JSON file containing the graph with all attributes. A map visualization showing the network colored by road type/capacity.

**Key risk:** OSM data quality. Some links may have wrong lane counts or missing connections. Budget time for manual fixes.

### Phase 2: Demand Model (Week 3-4)

**Objective:** A plausible OD matrix for peak-hour demand.

```
Tasks:
├── Compile zonal data
│   ├── Population per TAZ (from census)
│   ├── Employment estimate per TAZ (from known business districts)
│   └── Auto trip generation rate (trips/person, from CTS or IRC guidelines)
├── Compute production vector P_i and attraction vector A_j
├── Get free-flow travel time matrix (TAZ to TAZ)
│   └── Use shortest-path on network, or TomTom Matrix Routing API
├── Implement gravity model
│   ├── T_ij = k × P_i × A_j / c_ij^β
│   ├── Apply doubly-constrained balancing (Furness/IPF)
│   └── Calibrate β (start with β=2, adjust to match known patterns)
├── Convert person-trips to vehicle-trips
│   └── Apply mode split (rough estimate: 30% private vehicle
│       for Mumbai, from CTS data)
└── Validation: does total corridor demand roughly match known
    traffic volumes? (100K-200K vehicles/day on WEH is typical)
```

**Deliverable:** An OD matrix (numpy array or DataFrame), with documentation of assumptions. A sensitivity analysis showing how total trips change with β.

**Known limitation:** This OD matrix is synthetic. It will not capture actual travel patterns accurately. That's acceptable — the goal is to test the architecture, not to produce planning-grade outputs in v0.1.

### Phase 3: Traffic Assignment (Week 4-6)

**Objective:** Given network + demand, compute equilibrium link flows.

```
Tasks:
├── Implement shortest-path assignment (all-or-nothing)
│   └── This is NOT equilibrium — it's the initialization step
├── Implement BPR link performance function
│   └── t_a(v) = t_a^0 × [1 + 0.15 × (v/C)^4]
├── Implement Frank-Wolfe algorithm for User Equilibrium
│   ├── Step 0: All-or-nothing assignment → initial flows
│   ├── Step 1: Update link travel times with BPR
│   ├── Step 2: All-or-nothing assignment with updated times → auxiliary flows
│   ├── Step 3: Line search for optimal step size λ
│   ├── Step 4: Update flows: v = v + λ(auxiliary - v)
│   ├── Step 5: Check convergence (relative gap < 0.01)
│   └── Repeat steps 1-5
│
│   RECOMMENDED: Use AequilibraE library which has this built-in.
│   Only implement custom Frank-Wolfe if you want the educational
│   value or need fine-grained control AequilibraE doesn't expose.
│
├── Compute outputs
│   ├── Link flows (v_a) for all links
│   ├── Link travel times (t_a) for all links
│   ├── Volume-to-Capacity ratio (V/C) per link → identifies bottlenecks
│   ├── Total System Travel Time (TSTT)
│   └── Route travel times for key OD pairs
└── Validation: compare model travel times against TomTom observed
    travel times for the same OD pairs during peak hour
```

**Deliverable:** An assignment output file with flow, travel time, V/C for every link. A convergence plot showing the algorithm reached equilibrium. A comparison table: model vs. observed travel times.

**This is the critical implementation phase.** If the assignment works, the architecture is validated. If travel times are wildly off, the issue is in the demand or network model (calibration), not the assignment algorithm.

### Phase 4: Scenario Evaluation (Week 6-7)

**Objective:** Demonstrate the tool's planning capability.

```
Tasks:
├── Define 2-4 intervention scenarios
│   ├── Scenario A: Widen a known bottleneck link (+1 lane each direction)
│   ├── Scenario B: Add a new link (e.g., hypothetical connector road)
│   ├── Scenario C: Remove a link (test Braess paradox / road closure impact)
│   └── Scenario D: Stopped-vehicle incident on a link (§1.5) — set N stopped
│       vehicles, apply C_effective = C_nominal × μ_incident, re-assign
├── For each scenario:
│   ├── Modify network (change capacity, add/remove link)
│   ├── Re-run UE assignment (same OD demand)
│   ├── Compute new TSTT
│   ├── Compute change in TSTT: ΔTSTT = TSTT_new - TSTT_base
│   ├── Identify which links got better/worse (ΔV/C)
│   └── Check for congestion spillover to parallel routes
├── Compare scenarios
│   └── Rank by ΔTSTT (lower is better)
└── Flag: "These results assume fixed demand. Induced demand
    adjustment will be added in a future iteration."
```

**Simulation requirement (ALL cases):** The tool must **simulate every defined case**,
not a hand-picked subset. A single scenario-runner takes a list of cases and, for each:
runs the full UE assignment, records TSTT / V/C / bottleneck ranking, and renders a
congestion map. The standard case set is:

- **Base case** — current network, current demand (no intervention).
- **Scenario A** — widen each identified bottleneck link (+1 lane).
- **Scenario B** — add candidate connector link(s).
- **Scenario C** — remove/close a link (Braess & closure test).
- **Scenario D (incident sweep)** — stopped-vehicle bottleneck (§1.5) run for
  **N = 1, 2, 3, …** stopped vehicles on each candidate link, so the capacity-loss
  response curve is simulated end to end, not just a single point.

Every case produces: (TSTT, ΔTSTT vs base, top-10 V/C links, congestion map). Results
are collected into one comparison table and one side-by-side visual set so all cases are
directly comparable. This "simulate all cases" sweep is driven by `src/scenarios/evaluate.py`.

**Deliverable:** A comparison table/visualization covering **all** simulated cases (base +
every scenario, including the full incident N-sweep). The tool's first actual planning recommendation.

### Phase 5: Visualization & Reporting (Week 7-8)

**Objective:** Make outputs interpretable for non-technical stakeholders.

```
Tasks:
├── Map visualization
│   ├── Network colored by V/C ratio (green/yellow/red)
│   ├── Side-by-side: base case vs. intervention scenario
│   └── Interactive (folium) or static (matplotlib)
├── Summary metrics dashboard
│   ├── TSTT (total and per-OD-pair)
│   ├── Top 10 bottleneck links (by V/C ratio)
│   ├── Average travel time for key corridors
│   └── Before/after comparison for each scenario
└── Documentation
    ├── Assumptions register (every simplification documented)
    ├── Data sources and vintage
    └── Known limitations and next steps
```

**Reporting deliverables (built):** per-scenario V/C maps, the all-cases comparison
chart + montage, the robustness sweep, a comprehensive non-technical README, and a
self-contained HTML stakeholder report (`docs/report.html`).

**Hosting (Layer 5+, decision D14).** The tool is served by a small read-only FastAPI
app (`src/web/app.py`) so stakeholders reach it in a browser rather than running Python:

```
Routes:
├── /              Live dashboard — the two weekday segments (peak vs average),
│                  most-congested circuits, and the all-cases scenario table
├── /report        The full self-contained HTML stakeholder report
└── /api/*         Read-only JSON — /api/segments, /api/scenarios, /api/health
```

- Read-only by design: the app never calls TomTom, so the API key is never exposed to
  the web; data collection stays a separate scheduled job.
- The dashboard rebuilds from `data/processed/` on each request, so scheduled weekday
  collections appear automatically.
- Deploy: `Dockerfile` + `Procfile` (honours `$PORT`); runs locally via
  `uvicorn src.web.app:app`. See README §7.6.

---

## 6. Validation Strategy

### 6.1 Internal Consistency Checks

- Total vehicles entering = total vehicles exiting at every intermediate node (flow conservation)
- All link flows ≥ 0
- BPR function produces travel time ≥ free-flow time for all links
- Frank-Wolfe converges (relative gap < 0.01)
- Total assigned vehicle-trips = total OD matrix trips

### 6.2 External Validation

| Check | Source | Target |
|-------|--------|--------|
| Corridor end-to-end travel time | TomTom Routing API (peak hour) | Within ±30% for baseline |
| Link-level relative congestion pattern | TomTom Flow Segment Data (currentSpeed/freeFlowSpeed < 0.5 ≈ red, 0.5-0.75 ≈ orange, >0.75 ≈ green) | Qualitative match — known bottleneck locations should show high V/C |
| Total corridor volume | MCGM traffic counts (if obtainable) or CTS reported volumes | Within ±40% (we're using synthetic demand, so tolerance is wider) |

### 6.3 Sensitivity Analysis

Run the assignment with:
- β ± 20% (gravity model parameter) → how much does TSTT change?
- Capacity ± 20% → how sensitive are bottleneck locations?
- Demand ± 20% → how does the system degrade with load?

If small parameter changes cause bottlenecks to jump to completely different locations, the model is unstable and needs recalibration. If the rank order of bottlenecks is stable, the model is producing structurally useful results even if absolute numbers are approximate.

---

## 7. Upgrade Path (Post-Baseline)

Each of these is a discrete upgrade that can be done independently:

| Upgrade | What It Adds | Priority |
|---------|-------------|----------|
| **Calibrated OD from Google travel times** | Replace gravity model with Zhang et al. (2025) approach — invert observed travel times to estimate OD | HIGH — this is the single biggest accuracy improvement |
| **Heterogeneous traffic model** | Replace flat PCU with speed-dependent, composition-dependent PCU factors from Indian HCM / IIT research | HIGH for Mumbai realism |
| **Induced demand elasticity** | After computing TSTT for an intervention, adjust demand upward by an elasticity factor (0.1-0.3 typical) and re-assign | MEDIUM — essential for realistic planning recommendations |
| **Time-of-day profiles** | Multiple assignment runs (AM peak, PM peak, off-peak) instead of single peak | MEDIUM |
| **Scale to full MMR** | Expand network beyond pilot corridor to full metropolitan region | MEDIUM-HIGH — requires SUMO/MATSim for computational feasibility |
| **Signal timing optimization** | For specific intersections, use microsimulation (SUMO) to evaluate signal plans | LOW for infrastructure planning; HIGH if signal optimization is a goal |
| **Future demand scenarios** | Integrate population growth projections, land-use change, vehicle ownership forecasts | MEDIUM — needed for "future loads" requirement |
| **MFD-based network monitoring** | Aggregate N(t) over subnetworks for operational control module | LOW — this is where N(t) re-enters, if operational control becomes a goal |
| **Multi-modal integration** | Include Metro, suburban rail, BEST bus as alternative modes; model mode choice | HIGH for realistic planning but complex |

---

## 8. Key References

### Must-Read (before building)

1. **Zhang et al. (2025)** — "Origin-Destination Travel Demand Estimation: An Approach That Scales Worldwide" — arXiv:2507.00306. Google Research. Directly relevant: uses Google Maps Travel Trends data for OD estimation without needing survey data.

2. **Sheffi (1985)** — "Urban Transportation Networks: Equilibrium Analysis with Mathematical Programming Methods." Free PDF available online. THE textbook for traffic assignment theory. Chapters 1-5 cover everything you need for the baseline.

3. **Johari et al. (2021)** — "Macroscopic network-level traffic models: Bridging fifty years of development toward the next era." Transportation Research Part C. Comprehensive review of MFD literature — read for theoretical context.

### Mumbai-Specific

4. **MMRDA CTS-2 (2021)** — Comprehensive Transportation Study for MMR, updated. LEA Associates / LEA International. Contains demand forecasts, recommended infrastructure, and calibrated network data for horizon years 2026/2031/2041.

5. **MCGM CMP** — Comprehensive Mobility Plan for Greater Mumbai. Contains corridor-level traffic data and infrastructure recommendations.

### Heterogeneous Traffic (India)

6. **Arasan & Krishnamurthy** — "Dynamic PCU Values at Signalised Intersections in India for Mixed Traffic." Foundational work on dynamic PCU for Indian conditions.

7. **Mallikarjuna & Rao (2011)** — "Heterogeneous Traffic Flow Modelling: A Complete Methodology." Transportmetrica. Comprehensive framework.

8. **Mathew (IIT Bombay)** — Multiple papers on cellular automata models for Indian heterogeneous traffic.

### TNDP & Network Design

9. **Yang & Bell (1998)** — "Models and algorithms for road network design: a review and some new developments." Transport Reviews. Classic survey.

10. **Farahani et al. (2013)** — "A review of urban transportation network design problems." European Journal of Operational Research. Comprehensive survey of TNDP formulations.

### Open-Source Tools

11. **osmnx documentation** — https://osmnx.readthedocs.io/
12. **AequilibraE documentation** — https://www.aequilibrae.com/
13. **SUMO documentation** — https://eclipse.dev/sumo/
14. **MATSim documentation** — https://matsim.org/

---

## 9. Risk Register

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| OSM data quality poor for Mumbai internal roads | Network model unreliable | MEDIUM | Manual verification of pilot corridor; limit scope to major roads |
| TomTom free tier exhausted for a day | Data collection paused for 24 hours | VERY LOW | Baseline needs ~100 calls/day spread over 10 days; free tier allows 2,500/day; cache every response; if hit, resume next day — no charge, no escalation |
| Gravity model produces unrealistic OD matrix | Assignment results meaningless | HIGH | Sensitivity analysis; compare against any available CTS/MCGM data; accept that baseline is approximate |
| Frank-Wolfe doesn't converge | No equilibrium solution | LOW | Well-studied algorithm; use proven implementations (AequilibraE) if custom code fails |
| BPR parameters wrong for Indian conditions | Travel times systematically biased | MEDIUM | Calibrate α, β using observed Google travel times; literature has India-specific values |
| Induced demand makes fixed-demand results misleading | Over-optimistic intervention recommendations | HIGH | Document limitation explicitly; add elasticity adjustment in next iteration |
| Scope creep to full MMR before corridor is validated | Never finish anything | HIGH | Strict scope control: corridor first, scale later |

---

## 10. Budget Estimate (Baseline)

| Item | Estimated Cost | Notes |
|------|---------------|-------|
| TomTom APIs (Flow Segment Data, Routing, Matrix Routing) | **₹0** | No credit card needed. 2,500 requests/day free. Baseline needs ~1,000-1,500 total calls over 10 days. |
| Google Maps Platform (fallback only, if needed) | **₹0** | $200/month free credit. Requires billing account with credit card. Use only if TomTom coverage is insufficient. |
| OpenStreetMap data | **₹0** | Free download via Overpass API or Geofabrik. |
| Census / demographic data | **₹0** | Free from censusindia.gov.in. |
| Compute (local machine) | **₹0** | Baseline corridor assignment runs in seconds on any modern laptop. No cloud needed. |
| Software (Python, osmnx, networkx, etc.) | **₹0** | All open-source. |
| CTS/CMP reports (if RTI filed) | ₹10-100 | RTI application fee; optional — executive summaries are publicly available. |
| **Total baseline** | **₹0-100** | The only real cost is engineering time (~6-8 weeks part-time). |

**Why ₹0 works:** The entire baseline can be built on free-tier APIs, open data, and open-source software. The Google Maps Platform free credit alone provides 20-40× more API calls than the baseline needs. The constraint is engineering time, not money.

**What NOT to spend money on:**
- Do NOT purchase commercial traffic data subscriptions (HERE, INRIX) — free-tier Google and TomTom provide sufficient calibration data for a pilot corridor.
- Do NOT purchase commercial simulation licenses (Visum, AIMSUN) — open-source tools (AequilibraE, SUMO) cover the baseline.
- Do NOT pay for cloud compute — the pilot corridor network (~50-200 links) runs Frank-Wolfe equilibrium in seconds on a laptop.

---

## 11. Success Criteria for Baseline

The baseline is "done" when:

1. The tool can load a network, assign demand, and compute TSTT — end to end, without manual intervention
2. Model travel times for the corridor are within ±30% of Google-observed travel times for at least 60% of OD pairs
3. Known bottleneck locations (Dahisar check naka, Goregaon junction, Andheri flyover approaches — well-known to any Mumbaikar) show up as high V/C in the model output
4. At least one intervention scenario produces a measurable change in TSTT
5. All assumptions and limitations are documented in an assumptions register

These criteria are deliberately modest. A baseline that meets them proves the architecture works and is worth investing further in. A baseline that fails them tells you where the model breaks and what to fix — which is equally valuable.

---

## Appendix A: Glossary

| Term | Definition |
|------|-----------|
| **BPR** | Bureau of Public Roads — the standard link performance function |
| **CTS** | Comprehensive Transportation Study (MMRDA) |
| **MFD** | Macroscopic Fundamental Diagram — relates network-wide flow to accumulation |
| **OD Matrix** | Origin-Destination matrix — tabulates trips between every zone pair |
| **PCU** | Passenger Car Unit — equivalence factor for mixed vehicle types |
| **TAZ** | Traffic Analysis Zone — spatial unit for demand modeling |
| **TNDP** | Transportation Network Design Problem |
| **TSTT** | Total System Travel Time — the sum of travel time × flow across all links |
| **UE** | User Equilibrium — Wardrop's first principle |
| **V/C** | Volume-to-Capacity ratio — a link's congestion level (>1.0 = over capacity) |

## Appendix B: Decision Log

Decisions made during project planning, with rationale preserved.

| # | Decision | Alternatives Considered | Rationale |
|---|----------|------------------------|-----------|
| D1 | Infrastructure planning tool, NOT real-time monitoring | Real-time N(t) monitoring system | The core question is "where should we build?" not "how congested is it right now?" Google Maps already answers the latter. |
| D2 | N(t) vehicle conservation equation is NOT the starting point | N(t) accumulation tracking per segment | N(t) is a correct but irrelevant state variable for planning. Planning needs OD demand + network equilibrium. N(t) may re-enter for operational control in future phases. |
| D3 | Use lane count, not road width in meters | Estimate road width from maps/satellite imagery | No API provides width. Measuring pixels → dividing by lane width → getting lane count is a harder route to the same answer. OSM provides lane counts directly. |
| D4 | Do not use average car length × road area for vehicle estimation | area ÷ 5m = vehicle count | This computes jam density (theoretical max), not current occupancy. It's circular — to get current occupancy from geometry you need density, which is the thing you're estimating. |
| D5 | TomTom as primary traffic data API (no credit card) | Google Routes API (requires billing account), Mapbox (expensive), scraping traffic tile colors | TomTom gives per-segment currentSpeed + freeFlowSpeed + confidence in one call — richer than Google's route-level travel time. No credit card required. 2,500/day free tier is 25× baseline daily needs. Google kept as documented fallback only. |
| D6 | Pilot corridor (WEH Dahisar–Bandra) before full MMR | Full metropolitan region model | Scope control. A corridor model validates the architecture in weeks; a full MMR model takes months and fails for the same calibration reasons, just more expensively. |
| D7 | Gravity model for baseline OD; Zhang et al. approach as upgrade | Household travel surveys, mobile CDR data | Surveys are expensive and outdated (CTS used 25-year-old data). CDR data needs telecom partnerships. Gravity model is synthetic but free and tests the pipeline. Zhang et al. (2025) shows Google travel time data can estimate OD without surveys — this is the priority upgrade. |
| D8 | Open-source stack only (Python, osmnx, AequilibraE, SUMO) | PTV Visum, TransCAD, AIMSUN | ₹0 vs. ₹8-15L/year. Commercial tools are appropriate for institutional planning offices, not for a research/startup baseline. |
| D9 | Static User Equilibrium assignment for baseline | Dynamic Traffic Assignment (DTA) | Static UE is sufficient for planning-level analysis (which link is the bottleneck?). DTA adds time-varying flows but is computationally much harder and requires time-varying OD data. Add in later phase. |
| D10 | Fixed demand for baseline, with induced demand flagged as known limitation | Variable demand with elasticity | Correct induced demand modeling requires calibrated demand elasticity, which needs data we don't have yet. Flagging it ensures we don't over-trust intervention benefit estimates. |
| D11 | Model stopped vehicles as a capacity-reduction factor (effective_area = total_area − N × curve_area), not as microscopic obstacles | Microsimulate each stalled vehicle (SUMO); ignore incidents entirely | A capacity multiplier μ_incident feeds the existing BPR/UE machinery with no new solver — consistent with the static-equilibrium baseline (D9). The `curve_area` term captures rubbernecking turbulence (calibrated to HCM incident tables), which pure lane-subtraction misses. Microsimulation is deferred to a later SUMO phase. |
| D12 | Report incident impact via corridor through-time + link delay, not TSTT alone; apply incidents over a link stretch | Report network TSTT only; incident on a single 96 m edge | Under static UE, instant perfect rerouting can make network TSTT fall slightly when one link is choked (converse-Braess) — counterintuitive for an incident. Corridor through-time and the affected-link delay always rise, matching physical reality. Applying the incident across a contiguous WEH stretch (not one short segment) prevents trivial bypass. The TSTT paradox is itself flagged as a static-assignment limitation (true dynamics need DTA). |
| D13 | Collect live data in two weekday segments (peak + average-delay), automated | Fixed 10-day round-the-clock cadence; single unlabelled snapshots | Each segment answers a distinct planning question: peak → max time savings, congested circuits, OD/β calibration; average → the everyday baseline peak is compared against. Weekday windows (`src/data/segments.py`) exclude holiday/off-peak readings that cannot calibrate β (flat TTI). Scheduled jobs fill both segments with no manual effort. |
| D14 | Host via a read-only FastAPI web app | Ship Python scripts only; a heavier Streamlit/Dash app | A browser dashboard + report + JSON API reaches non-technical stakeholders without them running Python. Read-only keeps the TomTom key off the web (collection stays a separate scheduled job) and makes it trivially deployable (Docker/Procfile, `$PORT`). FastAPI is light and standard vs. a heavier framework. |
| D15 | Translate incidents into a physical queue via a deterministic (input-output) model, clamping arrival at nominal capacity | Report capacity loss only; a shockwave/LWR queue model | The deterministic triangular queue gives an interpretable "queue is X km / clears in Y min" figure directly from the capacity reduction, consistent with the static baseline. Clamping arrival at nominal capacity isolates the *incident-attributable* queue rather than pre-existing oversaturation; an already-saturated link correctly reports a non-clearing queue. A full shockwave model is deferred to the SUMO phase. |
| D16 | Store readings in a tabular SQLite DB; offer full-day 10/15-min adaptive sampling | Keep per-run CSVs only; a fixed uniform interval; a client-server DB (Postgres) | SQLite is a zero-dependency single-file SQL store — queryable, idempotent (`UNIQUE(run_id, idx)`), and openable in any SQL tool — without running a DB server. Segment-adaptive cadence (10 min peak / 15 min off-peak) spends finer resolution where congestion actually changes; the built-in daily-call estimate keeps collection inside the 2,500/day TomTom budget. CSVs remain as a human-readable export + fallback. |

## Appendix C: Folder Structure (Proposed)

```
mumbai-traffic-tool/
├── README.md
├── requirements.txt
├── data/
│   ├── raw/
│   │   ├── osm/                    # OSM extracts
│   │   ├── census/                 # Ward-level population data
│   │   └── tomtom/                 # Cached TomTom API responses (JSON + timestamp)
│   ├── processed/
│   │   ├── network.gpkg            # Cleaned, attributed network
│   │   ├── zones.gpkg              # TAZ boundaries
│   │   └── od_matrix.csv           # OD demand matrix
│   └── validation/
│       └── observed_travel_times.csv
├── src/
│   ├── network/
│   │   ├── build_network.py        # OSM → graph pipeline
│   │   ├── enrich_attributes.py    # Lane counts, capacity, speed
│   │   └── zones.py                # TAZ definition and centroid connectors
│   ├── demand/
│   │   ├── gravity_model.py        # Trip distribution
│   │   ├── generation.py           # Trip production/attraction
│   │   └── calibration.py          # β parameter calibration
│   ├── assignment/
│   │   ├── bpr.py                  # Link performance function
│   │   ├── shortest_path.py        # All-or-nothing loading
│   │   ├── frank_wolfe.py          # UE assignment algorithm
│   │   └── metrics.py              # TSTT, V/C, bottleneck ranking
│   ├── scenarios/
│   │   ├── define_scenario.py      # Network modification logic
│   │   └── evaluate.py             # Before/after comparison
│   └── viz/
│       ├── network_map.py          # Folium/matplotlib maps
│       └── dashboard.py            # Summary metrics
├── notebooks/
│   ├── 01_explore_network.ipynb
│   ├── 02_demand_model.ipynb
│   ├── 03_assignment.ipynb
│   ├── 04_scenarios.ipynb
│   └── 05_validation.ipynb
└── docs/
    ├── assumptions.md              # Every simplification documented
    ├── data_sources.md             # Provenance of all data
    └── calibration_log.md          # Parameter tuning history
```
