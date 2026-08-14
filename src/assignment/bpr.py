"""
bpr.py — Bureau of Public Roads link performance function.

    t_a(v) = t_a^0 * [ 1 + alpha * (v / C_a)^beta ]

t_a^0 = free-flow travel time, v = link flow (PCU/h), C_a = link capacity (PCU/h).
alpha=0.15, beta=4 are the standard defaults (plan §1.2) — to be calibrated for
Indian mixed-traffic conditions against TomTom travel-time indices (Phase 3 / D5).

Capacity passed in here should already be the EFFECTIVE capacity, i.e. after any
stopped-vehicle incident reduction (see network/incident.py). That keeps the
assignment loop agnostic to whether a link has an incident on it.
"""

from __future__ import annotations

ALPHA = 0.15
BETA = 4.0


def bpr_time(t0: float, flow: float, capacity: float,
             alpha: float = ALPHA, beta: float = BETA) -> float:
    """Congested travel time for a single link (same units as t0)."""
    if capacity <= 0:
        return float("inf")
    return t0 * (1.0 + alpha * (flow / capacity) ** beta)


def bpr_integral(t0: float, flow: float, capacity: float,
                 alpha: float = ALPHA, beta: float = BETA) -> float:
    """Integral of the BPR function from 0 to `flow` — the Beckmann objective term.

        ∫_0^v t0 (1 + alpha (x/C)^beta) dx
      = t0 * [ v + alpha * v^(beta+1) / ((beta+1) * C^beta) ]

    Minimising the sum of these integrals over all links is equivalent to the
    User Equilibrium (Wardrop) conditions — the basis of the Frank-Wolfe line search.
    """
    if capacity <= 0:
        return t0 * flow
    return t0 * (flow + alpha * flow ** (beta + 1) / ((beta + 1) * capacity ** beta))
