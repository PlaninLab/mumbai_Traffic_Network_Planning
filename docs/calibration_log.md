# Calibration Log

Parameter-tuning history. Each entry records a parameter change, the target it was tuned
against, and the resulting fit. Empty until Phase 2–3 (demand + assignment calibration).

## Parameters to calibrate

| Parameter | Symbol | Default | Calibration target | Status |
|-----------|--------|---------|--------------------|--------|
| BPR congestion coefficient | α | 0.15 | Google congested/free-flow travel-time ratio | ⬜ pending |
| BPR exponent | β | 4 | Same | ⬜ pending |
| Gravity deterrence exponent | β_grav | 2 | Plausible corridor trip volumes (100K–200K veh/day WEH) | ⬜ pending |
| Per-lane capacity | C_lane | IRC road-class values (1200–2000 PCU/h) | MCGM/CTS traffic counts | ⬜ pending |
| Encroachment factor | — | 0.85 | MCGM counts vs. modelled capacity | ⬜ pending |
| Mode split (private vehicle share) | — | ~30% | CTS data | ⬜ pending |
| Incident `curve_area` | — | 116 m² geometric (car, side-lane); HCM-calibrated 455–588 m² (full lane block) | HCM incident tables / Mumbai breakdown observations | ⬜ pending |
| Incident influence length | L_infl | 100 m | Field observation of queue-taper length | ⬜ pending |

## Demand / assignment parameters currently in use (Phase 2–3 baseline)

| Parameter | Value | Where | Note |
|-----------|-------|-------|------|
| Gravity β | 2.0 | `gravity_model.DEFAULT_BETA` | synthetic; sensitivity pending |
| Peak trip rate | 0.12 trips/person/hr | `generation.PEAK_TRIP_RATE` | synthetic |
| Private-vehicle mode share | 0.30 | `gravity_model.PRIVATE_VEHICLE_SHARE` | ~Mumbai (plan §Phase 2) |
| Avg occupancy | 1.4 persons/veh | `gravity_model.AVG_OCCUPANCY` | synthetic |
| **Target total demand** | **18,000 PCU/h** | `gravity_model.TARGET_TOTAL_PCU` | calibration knob — set so worst V/C ≈ 2 (realistic WEH peak) |
| BPR α, β | 0.15, 4 | `bpr.ALPHA/BETA` | US defaults; calibrate to TomTom TTI |
| FW convergence tol | 0.001 (scenarios) | `evaluate._run` | keeps ΔTSTT below smallest reported effect |

## Log

- **2026-08-14 — Phase 2–3 baseline established.** Gravity OD (β=2) scaled to 18k PCU/h;
  Frank-Wolfe UE converges to gap <0.001 in ~10 iters. Base case reproduces the WEH as the
  corridor bottleneck (worst V/C ≈ 1.97). All-cases sweep runs (base + A/B/C + incident N=1..3).
  Demand total is a synthetic calibration knob — replace with TomTom-calibrated OD (Zhang et al.
  upgrade) and BPR calibration against the collected TTI data before trusting absolute numbers.

- **2026-08-14 — First BPR calibration (`src/demand/calibration.py`).** Fitted α, β to the
  evening TomTom snapshot (`flow_evening_20260814_2139.csv`, 44 matched points):
  **α = 0.183, β = 1.0** (β hit the lower bound), RMSE 0.31 on TTI. **PRELIMINARY / weak:**
  the evening data is off-peak (observed TTI 1.0–1.54, nearly flat) while modelled v/C spans
  0.07–2.47, so the fit can't constrain the curve's steepness. **Action: recollect at AM peak
  (8–10 AM) and PM peak (6–8 PM)** for a real calibration — `python -m src.data.collect_flow
  --label am_peak`. Demand scale should be jointly calibrated (held fixed here).

- **2026-08-15 — Pooled calibration (3 readings, 132 points).** Pooled all collected
  snapshots (Aug-14 21:39, Aug-15 18:31, Aug-15 19:07). Fit unchanged: **α=0.183, β=1.0**
  (β still pinned to floor), RMSE 0.31. Root cause is now conclusive: all three snapshots are
  holiday / off-peak with observed TTI ≤ 1.54, so there is no high-congestion signal to lift β.
  **More off-peak readings will not help** — need a genuine working-day AM (8–10) or PM (6–8)
  peak, ideally a Mon–Fri, to observe TTI 2–3. Until then the "calibrated" config is illustrative.

- **2026-08-14 — Robustness sweep (`src/scenarios/robustness.py`).** Re-ran the all-cases
  simulation under 5 configs (default / calibrated BPR / low_flow 12k / high_flow 24k /
  capped_proc). **Directional conclusions stable in every config:** widen = only improvement
  (rank 1), incidents always harmful & monotonic (N1<N2<N3), close always ~+20% (worst),
  add-link always backfires (Braess). Only B_addlink's magnitude swings (+2% to +33%). Per
  §6.3 this means the model is structurally useful despite approximate absolute numbers.
