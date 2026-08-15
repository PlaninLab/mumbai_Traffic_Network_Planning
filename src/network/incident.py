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

import math
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


# ---------------------------------------------------------------------------
# Deterministic (input-output) queuing model
# ---------------------------------------------------------------------------
# A capacity-reducing incident turns the link into a temporary bottleneck. While
# the incident is active, vehicles ARRIVE faster than the reduced capacity can
# SERVE them, so a queue accumulates upstream. This is the classic deterministic
# (cumulative input-output) queuing diagram — the same triangular queue used in
# HCM freeway incident analysis:
#
#     cumulative
#     vehicles |          arrivals (slope = arrival rate v)
#              |         /
#              |        /___ departures during incident (slope = C_incident)
#              |       /   /
#              |      /   /____ departures after clearance (slope = C_nominal)
#              |     /   /    /
#              +----+---+----+------------------> time
#                   t0  t0+T  t_clear
#
#   - Queue GROWS at (v - C_incident) while the incident is active (duration T).
#   - Max queue (vehicles) = (v - C_incident) * T, reached the instant it clears.
#   - Queue DISSIPATES at (C_nominal - v) once full capacity is restored.
#   - Physical queue length = queued vehicles / (lanes * jam density).
#
# Assumptions: point bottleneck, constant arrival rate over the incident, FIFO,
# no rerouting away from the queue (a WORST-CASE upper bound — static UE would
# reroute some of it, so treat this as the "if drivers hold their route" figure).

JAM_DENSITY_VEH_KM_LANE = 130.0   # jam density (~7.7 m/veh spacing) per lane
DEFAULT_INCIDENT_DURATION_H = 0.5  # typical urban breakdown clearance (30 min)


@dataclass
class QueueParams:
    """Parameters for the deterministic bottleneck queue."""
    jam_density_veh_km_lane: float = JAM_DENSITY_VEH_KM_LANE
    incident_duration_h: float = DEFAULT_INCIDENT_DURATION_H


def deterministic_queue(
    arrival_flow: float,
    capacity_incident: float,
    lanes: int,
    capacity_nominal: float | None = None,
    p: QueueParams = QueueParams(),
) -> dict:
    """Triangular deterministic queue behind an incident bottleneck.

    Args:
        arrival_flow:      demand arriving at the link (PCU/h) — the flow that
                           WANTS to pass (use the pre-incident equilibrium flow).
        capacity_incident: reduced capacity during the incident (PCU/h), i.e.
                           ``effective_capacity(...)``.
        lanes:             lanes on the link (for physical length).
        capacity_nominal:  full capacity after clearance (PCU/h). Defaults to
                           ``arrival_flow`` if omitted (no baseline-saturation clamp).
        p:                 QueueParams (jam density, incident duration).

    We isolate the INCIDENT-ATTRIBUTABLE queue by clamping the effective arrival
    at nominal capacity: ``a_eff = min(arrival, C_nominal)``. This matters because
    an oversaturated link (arrival > C_nominal) already queues WITHOUT any incident;
    charging that pre-existing backup to the stalled vehicle would overstate its
    effect. With the clamp, the incident's own growth rate is
    ``a_eff - C_incident`` (bounded by the capacity DROP ``C_nominal - C_incident``
    on an already-saturated link), and the queue dissipates at ``C_nominal - a_eff``
    once the incident clears — which is 0 (never clears) exactly when the link was
    already saturated at nominal capacity.

    Returns a dict:
        queued_veh       max vehicles in queue (at the moment the incident clears)
        queue_len_km     physical length of that queue (km)
        growth_rate_pcu_h effective arrival minus incident capacity (>0 => queue)
        clear_time_min   minutes to fully dissipate AFTER the incident ends
                         (inf if the link was already saturated at nominal cap)
        total_delay_veh_h total vehicle-hours of delay (triangle area)
        overloaded       True if a queue forms at all
    """
    T = p.incident_duration_h
    c_nom = capacity_nominal if capacity_nominal is not None else arrival_flow
    # Clamp arrival at nominal capacity so we measure only the incident's effect,
    # not pre-existing oversaturation.
    a_eff = min(arrival_flow, c_nom)
    excess = a_eff - capacity_incident
    if excess <= 0 or T <= 0:
        return {
            "queued_veh": 0.0,
            "queue_len_km": 0.0,
            "growth_rate_pcu_h": round(excess, 1),
            "clear_time_min": 0.0,
            "total_delay_veh_h": 0.0,
            "overloaded": False,
        }

    queued_veh = excess * T
    lanes = max(1, int(lanes or 1))
    queue_len_km = queued_veh / (lanes * p.jam_density_veh_km_lane)

    # Dissipation once full capacity returns. If demand still saturates nominal
    # capacity, the queue never clears within the model horizon.
    discharge = c_nom - a_eff
    if discharge > 0:
        clear_time_h = queued_veh / discharge
        total_delay_veh_h = 0.5 * queued_veh * (T + clear_time_h)
        clear_time_min = clear_time_h * 60.0
    else:
        clear_time_min = float("inf")
        total_delay_veh_h = float("inf")

    return {
        "queued_veh": round(queued_veh, 1),
        "queue_len_km": round(queue_len_km, 3),
        "growth_rate_pcu_h": round(excess, 1),
        "clear_time_min": round(clear_time_min, 1) if math.isfinite(clear_time_min) else clear_time_min,
        "total_delay_veh_h": round(total_delay_veh_h, 1) if math.isfinite(total_delay_veh_h) else total_delay_veh_h,
        "overloaded": True,
    }


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

    print("\nDeterministic queue behind an incident (3-lane link, C_nom=6000 PCU/h,")
    print(f"arrival=5000 PCU/h, {DEFAULT_INCIDENT_DURATION_H*60:.0f}-min incident):")
    c_nom = 6000.0
    for n in (1, 2, 3):
        mu = incident_capacity_factor(3, n)
        c_eff = c_nom * mu
        q = deterministic_queue(5000.0, c_eff, lanes=3, capacity_nominal=c_nom)
        print(f"  N={n}: C_eff={c_eff:.0f}  queue={q['queue_len_km']:.2f} km "
              f"({q['queued_veh']:.0f} veh), clears in {q['clear_time_min']} min "
              f"after incident, delay={q['total_delay_veh_h']} veh-h")
