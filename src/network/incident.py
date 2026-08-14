"""
incident.py — Stopped-vehicle (incident) capacity-reduction model.

Motivation (project plan §1.5): on a multi-lane link, a stopped / broken-down
vehicle does NOT merely remove its own footprint from the roadway. Approaching
traffic must deflect laterally around it — a swerve over a taper length — creating
a turbulence "shadow" (the flow "curve") that is unusable for through movement.
The effective cross-section available to moving traffic shrinks by MORE than the
vehicle's physical size.

Geometric model (as specified):

    effective_area = total_area - N * curve_area

    total_area   = W * L_infl         (carriageway width x influence-window length)
    N            = number of stopped vehicles in the (side) lane
    curve_area   = deflection-shadow plan area rendered unusable per stopped vehicle
                 ~= veh_width * (veh_length + 2 * taper_length)

Capacity scales with the effective cross-section, so define the incident
capacity-reduction factor:

    mu_incident = effective_area / total_area = 1 - N * (curve_area / total_area)
    C_effective = C_nominal * max(mu_floor, mu_incident)

This factor multiplies the nominal link capacity and feeds straight into BPR:

    t_a(v) = t_a^0 * [ 1 + alpha * (v / C_effective)^beta ]

Because a fully lane-blocking incident also triggers rubbernecking (turbulence
larger than pure geometry predicts), the model can be CALIBRATED to the HCM
incident capacity-reduction tables via `calibrate_curve_area()`.

All parameters are defaults to be calibrated (like BPR alpha/beta) — see
docs/calibration_log.md.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- Default geometric parameters (metres) ---
LANE_WIDTH_M = 3.5          # IRC urban lane width
INFLUENCE_LENGTH_M = 100.0  # longitudinal influence window of an incident
CAR_WIDTH_M = 1.8
CAR_LENGTH_M = 4.5
TAPER_LENGTH_M = 30.0       # approach deceleration / lane-change taper (speed-dependent)
MU_FLOOR = 0.0              # remaining-capacity floor (0 = all lanes can be blocked)


@dataclass
class IncidentParams:
    """Bundle of stopped-vehicle model parameters (metres)."""
    lane_width_m: float = LANE_WIDTH_M
    influence_length_m: float = INFLUENCE_LENGTH_M
    veh_width_m: float = CAR_WIDTH_M
    veh_length_m: float = CAR_LENGTH_M
    taper_length_m: float = TAPER_LENGTH_M
    mu_floor: float = MU_FLOOR


def curve_area_geometric(p: IncidentParams = IncidentParams()) -> float:
    """Deflection-shadow plan area (m^2) of one stopped vehicle.

    curve_area = veh_width * (veh_length + 2 * taper_length)
    The taper terms capture the upstream swerve-in and downstream recovery zones,
    which dominate the shadow at speed.
    """
    return p.veh_width_m * (p.veh_length_m + 2.0 * p.taper_length_m)


def total_area(lanes: int, p: IncidentParams = IncidentParams()) -> float:
    """Plan area (m^2) of the link influence window: width x influence length."""
    return lanes * p.lane_width_m * p.influence_length_m


def incident_capacity_factor(
    lanes: int,
    n_stopped: int,
    curve_area: float | None = None,
    p: IncidentParams = IncidentParams(),
) -> float:
    """Return mu_incident in [mu_floor, 1]: fraction of nominal capacity remaining.

    mu = 1 - N * (curve_area / total_area), floored at p.mu_floor.
    """
    if n_stopped <= 0:
        return 1.0
    if curve_area is None:
        curve_area = curve_area_geometric(p)
    frac_lost = n_stopped * curve_area / total_area(lanes, p)
    return max(p.mu_floor, 1.0 - frac_lost)


def effective_capacity(
    capacity_nominal: float,
    lanes: int,
    n_stopped: int,
    curve_area: float | None = None,
    p: IncidentParams = IncidentParams(),
) -> float:
    """Nominal capacity reduced by the stopped-vehicle factor (veh/h)."""
    return capacity_nominal * incident_capacity_factor(lanes, n_stopped, curve_area, p)


def calibrate_curve_area(
    target_remaining_fraction: float,
    lanes: int,
    n_stopped: int = 1,
    p: IncidentParams = IncidentParams(),
) -> float:
    """Back-solve curve_area (m^2) so mu matches a target remaining-capacity fraction.

    Use to align the geometric model with empirical HCM incident tables, e.g.
    HCM 6th ed. freeway "1 of 3 lanes blocked" -> ~0.49 remaining.

        curve_area = (1 - target) * total_area / N
    """
    return (1.0 - target_remaining_fraction) * total_area(lanes, p) / n_stopped


# HCM 6th ed. incident capacity-reduction reference (fraction of capacity REMAINING).
# Source: Highway Capacity Manual, freeway incident analysis. For calibration only.
HCM_REMAINING_CAPACITY = {
    (2, "shoulder"): 0.81,
    (2, "one_lane_blocked"): 0.35,
    (3, "shoulder"): 0.83,
    (3, "one_lane_blocked"): 0.49,
    (4, "shoulder"): 0.85,
    (4, "one_lane_blocked"): 0.58,
}


if __name__ == "__main__":
    # Self-check: print the model's behaviour and its HCM-calibrated equivalent.
    ca = curve_area_geometric()
    print(f"Geometric curve_area (car, default taper): {ca:.1f} m^2")
    print("\nGeometric-default mu_incident (fraction capacity remaining):")
    for lanes in (2, 3, 4):
        for n in (1, 2, 3):
            mu = incident_capacity_factor(lanes, n)
            print(f"  {lanes} lanes, N={n} stopped: mu={mu:.2f}  ({(1-mu)*100:.0f}% capacity lost)")

    print("\nHCM-calibrated curve_area (one lane fully blocked, N=1):")
    for lanes in (2, 3, 4):
        target = HCM_REMAINING_CAPACITY[(lanes, "one_lane_blocked")]
        ca_cal = calibrate_curve_area(target, lanes, n_stopped=1)
        print(f"  {lanes} lanes -> target remaining {target:.2f}  =>  curve_area={ca_cal:.0f} m^2")
