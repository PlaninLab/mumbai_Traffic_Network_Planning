# BMC / Greater-Mumbai scale-up (hybrid)

Scales the corridor model from the WEH pilot to the whole **BMC major-road network**.
Chosen approach: **scope = BMC**, **method = hybrid** (assignment where we can build demand,
observation-driven per-junction metrics elsewhere).

## What runs

| Step | Module | Output |
|------|--------|--------|
| Download BMC major roads (motorway/trunk/primary/secondary) | `src/network/build_area.py` | `data/raw/osm/bmc.graphml` — 3,940 nodes / 6,922 links / **1,369 km** |
| Enrich (lanes, speed, capacity, measured-width overrides) | `enrich_attributes --input … --tag bmc` | `network_bmc_enriched.graphml` |
| Grid TAZs + gravity OD + Frank-Wolfe UE + per-junction metrics | `src/scenarios/bmc_scale.py` | `data/processed/bmc/…` |

```bash
python -m src.network.build_area --scope bmc --tag bmc          # once (slow OSM ~a few min)
python -m src.network.enrich_attributes --input data/raw/osm/bmc.graphml --tag bmc
python -m src.scenarios.bmc_scale                               # grid 8, tol 0.001
```

## Baseline result (synthetic demand)

- 37 TAZs, 1,332 OD pairs, target 60,000 PCU/h; UE converged (gap < 0.001), **TSTT ≈ 9,386 PCU-h**.
- Network mean V/C ≈ 0.22, max ≈ 2.75, ~32 links over capacity.
- **867 BMC junctions** scored: arriving volume, worst-approach V/C, standing-queue length.
- Worst junctions are real Mumbai chokepoints — **BKC–Sealink** (V/C 1.57, ~6.7 km standing queue),
  **Eastern Express Hwy × King's Circle**, **Sion/Tilak**, **Amar Mahal** — i.e. the model finds the
  right places without being told, the same validation the corridor passed.

## Hybrid `source` per junction

Every junction in `bmc_junction_metrics.json` carries a `source`:
- `model` — metrics from the UE assignment (current state: all 867).
- `observation` — filled from live junction TTI once `store.intersection_readings` has rows
  (the junction collector). The hook is built; it activates automatically as data arrives.
- `capacity_only` — a junction with measured/derived capacity but no flow yet.

## Honest limits (baseline, not planning-grade yet)

- **Demand is synthetic**: per-zone production/attraction uses an intersection-density *proxy* scaled
  to a target total — no per-ward census wired in for BMC yet. Absolute volumes are illustrative;
  structure (which junctions choke) is the trustworthy output.
- **Coarse grid TAZs** with single point-connectors concentrate demand at a few nodes, so the very
  highest link V/Cs include a connector-loading artifact. A ward-based zone system (next step) fixes this.
- **Queues** are the standing "if drivers hold their route" upper bound (peak-hour input–output).

## Next to make it planning-grade
1. Ward-based TAZs + CTS demand controls for all of BMC (replace the density proxy).
2. Run the junction collector so `source` flips to `observation` at measured junctions.
3. Measure widths (Phase 0 worklist now covers all 867 BMC junctions) → capacity from effective width.
4. Surface the BMC junction layer on the interactive map.
