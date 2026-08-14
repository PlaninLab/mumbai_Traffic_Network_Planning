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

## Log

_(no calibration runs yet — Phase 0 complete, Phase 1 network enrichment next)_
