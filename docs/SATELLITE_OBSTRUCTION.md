# Design: Satellite-based obstruction detection → junction capacity

**Status:** design / feasibility (not built). **Author:** planning team.
**Goal:** detect physical obstructions on the roads at the junctions we already track,
from satellite imagery, and turn each detection into a capacity reduction that the
existing assignment/scenario model can use — paired with the traffic (TTI) observed at
that junction at that time.

---

## 1. What you asked for, and the honest reality first

You want: *past satellite photo + the traffic for that time & day → detect obstructions →
attribute them to the junctions we track.* Before the design, the two hard constraints that
shape everything, because pretending they don't exist would waste money:

### 1a. Satellites do not photograph continuously
There is **no** "satellite image of this junction at 6:45 PM on a given Tuesday." Optical
satellites pass a given point at a fixed local time, every few days:

| Source | Resolution | Revisit | Overpass (local) | Sees a car? |
|--------|-----------|---------|------------------|-------------|
| Sentinel‑2 (free) | 10 m/px | ~5 days | ~10:30 | ❌ (car ≈ sub-pixel) |
| Landsat 8/9 (free) | 30 m/px | 16 days | ~10:30 | ❌ |
| Planet **PlanetScope** | ~3 m/px | ~daily | ~09:30–11:30 | ⚠️ trucks/buses only |
| Planet **SkySat** (tasked) | ~0.5 m/px | on request | daytime | ✅ |
| Maxar / Airbus Pléiades | ~0.3–0.5 m/px | archive/tasking | ~10:30 | ✅ |
| Sentinel‑1 **SAR** (free) | ~10 m | ~6–12 days | day **and** night, through cloud | ❌ vehicles, ✅ water/flood |

Consequences:
- You **cannot** match a satellite frame to your PM-peak window (17:30–20:30) in general —
  optical overpasses cluster near ~10:30 AM. Night is impossible for optical.
- **Mumbai monsoon (Jun–Sep)** = persistent cloud → optical frequently useless; SAR is the
  only all-weather option and it can't see vehicles.

### 1b. Resolution gates what "obstruction" can mean
To see an individual **stalled vehicle** you need ≤ 0.5 m/px → commercial only. To see
**construction, encroachment, road narrowing, waterlogging, debris, illegal parking rows**
you can work at 3 m (Planet) or even 10 m (Sentinel‑2) for large features.

### The reframe that makes this worth doing
Satellite is the **wrong tool for transient stalled-vehicle incidents** (minutes–hours) — your
live flow data + [`observed_queue.py`](../src/data/observed_queue.py) already catch those. It is
the **right tool for persistent obstructions** that sit on a junction for days/weeks and quietly
cut its capacity:

- construction / road works / unfinished flyover works
- encroachment (markets, stalls, parked-vehicle rows, hawkers)
- carriageway narrowing / lane blockage
- **waterlogging / flooding** (huge in Mumbai; SAR sees this even under cloud)
- debris, dug-up roads (utility works)

These are exactly the things that make a Mumbai junction chronically worse than its lane count
implies — and they map cleanly onto the capacity model you already have.

---

## 2. What this connects to (already in the repo)

| Existing piece | Role here |
|----------------|-----------|
| Junction inventory — `coverage.json` (2,003 junctions: `id`, `lat`, `lon`, `name`, `scopes`) | The list of places to inspect; obstructions attach to these `id`s |
| `store.py` → `flow_readings` / `intersection_readings` (TTI per point per time) | The **traffic for that time & day** to pair with each image |
| `incident.py` — `effective_area = total_area − N·curve_area`, capacity multiplier μ | The hook: an obstruction → μ on the affected links, same math as a stalled vehicle |
| `observed_queue.py` | Independent check: does a detected obstruction coincide with a real jam? |

The obstruction pipeline is a **new input** to the same capacity/assignment machinery — not a
parallel model.

---

## 3. Pipeline architecture

```
 for each tracked junction (id, lat, lon) and target date:
   1. AOI          buffer the junction (≈150 m) + its approach links (from OSM)
   2. Acquire      query the provider archive for the nearest CLEAR scene to that date
   3. Preprocess   orthorectify / pan-sharpen, cloud-mask, clip to the junction AOI, tile
   4. Road-mask    rasterize OSM carriageway (lines buffered by lane width) → detect on-road only
   5. Detect       obstruction agent (see §4) → {type, lanes_blocked, area_m², confidence, geom}
   6. Attribute    spatial-join each detection → nearest junction id + affected link(s)
   7. Capacity     obstruction → μ on those links via incident.effective_capacity()
   8. Pair traffic join the scene datetime → nearest-in-time TTI at that junction (store.py)
   9. Persist      new table junction_obstructions; surface as a map layer; feed scenarios
```

Output record (one per detected obstruction):
```json
{
  "junction_id": "osm-junction-10151560371",
  "captured_utc": "2026-05-12T05:10:00Z",
  "scene_id": "...", "source": "planet_skysat", "cloud_pct": 4,
  "type": "encroachment", "lanes_blocked": 1.0, "area_m2": 320, "confidence": 0.82,
  "capacity_mu": 0.74,
  "traffic_at_capture": {"tti": 1.9, "reading_utc": "..."},
  "geometry": { "...": "polygon in the junction AOI" }
}
```

---

## 4. The detection "agent" — three build tiers

**Tier A — VLM agent (build this first; no training).**
Feed each junction chip to a vision LLM (e.g. Claude vision) with a structured prompt:
*"Is the carriageway obstructed? Classify {construction, encroachment, parked_vehicles,
flooding, debris, road_works, none}; estimate lanes blocked (0–N); confidence 0–1."* Returns
JSON per junction. This literally **is** an obstruction-detection agent, needs zero labelled
data, and handles Mumbai-specific scenes well. Ideal MVP and label-bootstrapper.

**Tier B — trained object detector (scale/automation).**
Fine-tune YOLOv8 / Detectron2 (or start from aerial datasets **DOTA, xView, iSAID**) for
vehicles / construction / debris classes. Needs labelled chips + a GPU. Use for running over
thousands of junctions × dates automatically.

**Tier C — geospatial foundation / segmentation model.**
Prithvi (NASA-IBM), SatlasPretrain, Clay, or **Segment-Anything** for pixel masks of
flooding / encroachment area. Best when you need obstruction **area** (→ μ) not just a box, and
for monsoon flood mapping (pair with Sentinel‑1 SAR).

**Tier D — change detection (robust, label-light; run alongside any tier).**
Keep one clear "baseline" scene per junction; difference each new scene against it to flag
*new* obstructions. Cuts false positives from permanent roadside clutter.

---

## 5. Requirements

**Imagery access (pick per budget — cost is not the blocker here):**
- Free: **Google Earth Engine** account → Sentinel‑2 (large features, change detection) and
  **Sentinel‑1 SAR** (monsoon flood, all-weather).
- Vehicle/encroachment detail: **Planet** (PlanetScope 3 m daily + SkySat 0.5 m tasking) API
  key, or **Maxar** / **Airbus OneAtlas** archive (0.3–0.5 m).

**Compute:** Tier A uses a vision-LLM API (no local GPU). Tiers B/C need a GPU for
training/inference.

**Python libraries:** `rasterio`, `gdal`, `geopandas`, `shapely`, `pyproj`, `scikit-image`
(geo/IO); `earthengine-api` or `planet` SDK (acquisition); `torch` + `ultralytics`/`detectron2`
or `segment-anything` (Tiers B/C); Anthropic SDK (Tier A). None of these are in
`requirements.txt` yet — this is a separate optional extra (`requirements-geo.txt`).

**Data you already have:** OSM network + 2,003-junction inventory, traffic history, and the
capacity model to plug obstructions into.

**Georegistration:** imagery must align to OSM within ~3–5 m, or detections attach to the wrong
junction. Budget a per-scene alignment/QC step.

**Legal / licensing (important):** commercial imagery licences forbid redistributing the raw
tiles — so **do not** serve imagery on the public dashboard. Storing and showing *derived
detections* (polygons, types, μ) is fine. Keep raw scenes private.

---

## 6. Pairing with "traffic of that time & day"

For every detection at scene time *t*, query `store.py` for the TTI at (or near) that junction
at the nearest reading to *t*. Because optical *t* ≈ 10:30 AM, that will usually be an **avg**
segment reading, occasionally a morning-peak one — another reason satellite suits *persistent*
obstructions (which are present across the day, so any same-day reading is representative).
This produces the paired dataset you described: **(obstruction features) ↔ (observed congestion)**,
used to (a) validate detections and (b) quantify how much TTI a given obstruction type adds.

---

## 7. Phased plan

1. **MVP (weeks):** Tier A VLM agent + Tier D change detection on Planet/high-res chips for a
   handful of tracked junctions; write `junction_obstructions`; spatial-join to `id`; convert to
   μ; show an "obstruction" layer on the existing map; sanity-check against TTI.
2. **Validate:** does obstruction presence coincide with elevated TTI at that junction? Report
   precision/recall against the flow data.
3. **Scale:** Tier B/C detector over all 2,003 junctions × a date range; automate as a scheduled
   agent (like the collector), one pass per available clear scene.
4. **Monsoon layer:** Sentinel‑1 SAR flood mapping → waterlogging obstructions when optical is
   clouded out.

---

## 8. Complementary sources (often better than satellite for road-level obstruction)

Satellite is the **wide-area, low-cadence** layer. For obstruction detail, these are worth pairing
and may carry more of the load:
- **Mapillary / KartaView** — crowdsourced *street-level* imagery, timestamped, free API. Sees
  encroachment/obstruction from the road, at times satellites can't. Strong complement.
- **Mumbai Traffic Police CCTV / ITMS feeds** — real-time, if access can be arranged; best for
  *transient* incidents satellites miss.
- **Dashcam / fleet video** — same idea, transient obstruction at road level.

**Recommended stance:** satellite (persistent obstruction + flood, wide area) **+** street-level
(road-level detail) **+** your existing flow data (transient incidents) = full coverage. Don't
ask satellite to do the transient-incident job the flow data already does.

---

## 9. Bottom line

- **Feasible and valuable** for *persistent* obstruction/encroachment/flooding → junction capacity,
  attached to the junctions you already track, validated against your traffic history.
- **Not** a real-time stalled-vehicle detector — the revisit/resolution/cloud limits forbid it;
  the flow-data model already covers that.
- **Start with the Tier-A VLM agent** on a few junctions (buildable now, no training), prove the
  obstruction→μ→TTI link, then scale.
```
