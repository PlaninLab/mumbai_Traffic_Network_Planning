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
