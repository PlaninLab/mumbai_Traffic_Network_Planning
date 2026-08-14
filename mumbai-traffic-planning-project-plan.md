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
│  OSM, Google Routes API, Census, CTS reports         │
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

| Source | What It Provides | Access | Cost |
|--------|-------------------|--------|------|
| **Google Routes API** | Travel time between two points, with/without traffic, for specified departure time | API key required; $200/month free credit covers ~20,000-40,000 calls — far more than baseline needs | **₹0** (free tier) |
| **Google Distance Matrix API** | OD travel time matrix for multiple origins/destinations simultaneously | Same free credit pool; more efficient for batch OD queries | **₹0** (free tier) |
| **Google Maps Traffic Layer** (JavaScript API) | 4-level congestion classification (green/yellow/orange/red) covering the full visible network simultaneously | Same free credit; can be rasterized into georeferenced data using the World Bank `googletraffic` approach | **₹0** (free tier) |
| **TomTom Traffic Flow API** | Actual speed values per road segment (richer than Google's ordinal colors); current speed + free-flow speed | Free tier: 2,500 requests/day | **₹0** (free tier) |
| **Google Maps Traffic Trends** (as used in Zhang et al. 2025) | Aggregated, anonymized travel time statistics | Research access; not a public API — paper describes methodology; can be approximated via Routes API sampling over multiple days | ₹0 if replicated via Routes API |
| **Mapbox Traffic API** | Live and typical traffic speeds as vector tiles | Requires Mapbox Enterprise for actual speed data | Expensive — skip for baseline |

**Data caching rule:** Every API response is saved to `data/raw/google_api/` or `data/raw/tomtom/` as JSON with a timestamp. Never query the same OD pair + departure time twice. The cache is the dataset — the API is just the collection mechanism. For a pilot corridor, total unique queries should be ~1,000-2,000, well within free tiers.

**Google Maps Platform Terms of Service — key constraints:**
- DO use the Routes/Distance Matrix API to query travel times programmatically. This is the intended use case and is within ToS.
- DO NOT scrape Google Maps traffic layer tile images (the red/orange/green visual overlay) by screenshotting or intercepting tile requests. This violates ToS.
- DO NOT store or cache Google-provided map tiles or imagery. API *response data* (travel times, distances) can be cached for the purpose of the project.
- The `googletraffic` package (World Bank) uses the Maps JavaScript API to render the traffic layer and extract pixel colors — review its ToS compliance for your specific use before relying on it as a primary source. The Routes API is the safer path.
- TomTom's free tier has more permissive terms for research/non-commercial use.

**Baseline approach:**
1. Define ~20-30 OD pairs within the corridor (key entry/exit ramps, major intersections)
2. Query Google Routes API (within free tier) for travel times at 15-minute intervals during a peak period (e.g., 8:00-10:00 AM, weekday)
3. Also query free-flow travel times (e.g., 3:00 AM departure)
4. The ratio of congested/free-flow travel time gives the Travel Time Index (TTI) for each segment — this is the calibration target for BPR parameters
5. Optionally supplement with TomTom segment-level speeds for link-level validation

### 3.3 Demand Data (Layer 2)

| Source | What It Provides | Access |
|--------|-------------------|--------|
| **Census 2011 (ward-level)** | Population, workforce participation by ward/zone | Free, census.gov.in |
| **MCGM Development Plan data** | Zonal land use (residential, commercial, industrial) | Partially public |
| **Employment centers** | BKC, Andheri MIDC, Goregaon IT park, etc. — known major attractors | Public knowledge + OSM POI data |
| **CTS-2 OD data** | If obtainable: calibrated OD matrices for MMR | Needs institutional access (MMRDA/LEA) |
| **Google Routes API (indirect)** | Travel times between zones → infer relative demand via gravity model calibration | ₹0 (free tier) |

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

Zone the corridor into ~10-15 traffic analysis zones (TAZs) based on ward boundaries. Use census population for production, estimated employment for attraction. Calibrate β so that the resulting assignment produces link volumes and travel times roughly consistent with observed Google travel times.

This is explicitly a SYNTHETIC baseline. The OD matrix will be approximate. That's acceptable for a first iteration — the architecture matters more than the calibration at this stage.

### 3.4 Validation Data

| Source | What It Provides | Access |
|--------|-------------------|--------|
| **Google Routes API travel times** | Observed corridor travel times to compare against model predictions | ₹0 (free tier) |
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
| **API interaction** | `requests`, `googlemaps` client library | Google Routes/Distance Matrix API calls |
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
├── Set up Google Cloud project + Routes API key
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
│   └── Use shortest-path on network, or Google Distance Matrix API
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
└── Validation: compare model travel times against Google observed
    travel times for the same OD pairs during peak hour
```

**Deliverable:** An assignment output file with flow, travel time, V/C for every link. A convergence plot showing the algorithm reached equilibrium. A comparison table: model vs. observed travel times.

**This is the critical implementation phase.** If the assignment works, the architecture is validated. If travel times are wildly off, the issue is in the demand or network model (calibration), not the assignment algorithm.

### Phase 4: Scenario Evaluation (Week 6-7)

**Objective:** Demonstrate the tool's planning capability.

```
Tasks:
├── Define 2-3 intervention scenarios
│   ├── Scenario A: Widen a known bottleneck link (+1 lane each direction)
│   ├── Scenario B: Add a new link (e.g., hypothetical connector road)
│   └── Scenario C: Remove a link (test Braess paradox / road closure impact)
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

**Deliverable:** A comparison table/visualization showing base case vs. each scenario. The tool's first actual planning recommendation.

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
| Corridor end-to-end travel time | Google Routes API (peak hour) | Within ±30% for baseline |
| Link-level relative congestion pattern | Google Traffic visual (red = V/C > 0.9, orange = 0.7-0.9, green = < 0.7) | Qualitative match — known bottleneck locations should show high V/C |
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
| Google API free tier exhausted | Cannot collect additional calibration data mid-project | VERY LOW | Baseline needs ~1,000-2,000 calls; free tier allows 20,000-40,000/month; cache every response locally to avoid re-queries; TomTom free tier available as backup |
| Gravity model produces unrealistic OD matrix | Assignment results meaningless | HIGH | Sensitivity analysis; compare against any available CTS/MCGM data; accept that baseline is approximate |
| Frank-Wolfe doesn't converge | No equilibrium solution | LOW | Well-studied algorithm; use proven implementations (AequilibraE) if custom code fails |
| BPR parameters wrong for Indian conditions | Travel times systematically biased | MEDIUM | Calibrate α, β using observed Google travel times; literature has India-specific values |
| Induced demand makes fixed-demand results misleading | Over-optimistic intervention recommendations | HIGH | Document limitation explicitly; add elasticity adjustment in next iteration |
| Scope creep to full MMR before corridor is validated | Never finish anything | HIGH | Strict scope control: corridor first, scale later |

---

## 10. Budget Estimate (Baseline)

| Item | Estimated Cost | Notes |
|------|---------------|-------|
| Google Maps Platform (Routes, Distance Matrix, Traffic Layer) | **₹0** | $200/month free credit = ~₹17,000/month. Baseline needs ~1,000-2,000 calls (~5% of free tier). |
| TomTom Traffic Flow API | **₹0** | Free tier: 2,500 requests/day. Supplementary speed data. |
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
| D5 | Use Google Routes API within free tier (₹0), not paid data | Paid traffic data subscriptions, scraping traffic tile colors | $200/month free credit covers 20-40× baseline needs. Scraping tiles violates ToS and gives only 4 ordinal levels vs. continuous travel time values. |
| D6 | Pilot corridor (WEH Dahisar–Bandra) before full MMR | Full metropolitan region model | Scope control. A corridor model validates the architecture in weeks; a full MMR model takes months and fails for the same calibration reasons, just more expensively. |
| D7 | Gravity model for baseline OD; Zhang et al. approach as upgrade | Household travel surveys, mobile CDR data | Surveys are expensive and outdated (CTS used 25-year-old data). CDR data needs telecom partnerships. Gravity model is synthetic but free and tests the pipeline. Zhang et al. (2025) shows Google travel time data can estimate OD without surveys — this is the priority upgrade. |
| D8 | Open-source stack only (Python, osmnx, AequilibraE, SUMO) | PTV Visum, TransCAD, AIMSUN | ₹0 vs. ₹8-15L/year. Commercial tools are appropriate for institutional planning offices, not for a research/startup baseline. |
| D9 | Static User Equilibrium assignment for baseline | Dynamic Traffic Assignment (DTA) | Static UE is sufficient for planning-level analysis (which link is the bottleneck?). DTA adds time-varying flows but is computationally much harder and requires time-varying OD data. Add in later phase. |
| D10 | Fixed demand for baseline, with induced demand flagged as known limitation | Variable demand with elasticity | Correct induced demand modeling requires calibrated demand elasticity, which needs data we don't have yet. Flagging it ensures we don't over-trust intervention benefit estimates. |

## Appendix C: Folder Structure (Proposed)

```
mumbai-traffic-tool/
├── README.md
├── requirements.txt
├── data/
│   ├── raw/
│   │   ├── osm/                    # OSM extracts
│   │   ├── census/                 # Ward-level population data
│   │   └── google_api/             # Cached API responses
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
