"""
frank_wolfe.py — Static User Equilibrium traffic assignment (Frank-Wolfe).

Given a network (with free-flow times t0 and effective capacities C) and an OD
demand matrix, compute equilibrium link flows satisfying Wardrop's first principle
(no traveler can reduce travel time by unilaterally switching routes).

Algorithm (plan §Phase 3):
  0. All-or-nothing (AON) assignment on free-flow times -> initial flows
  1. Update link times with BPR at current flows
  2. AON assignment on updated times -> auxiliary flows y
  3. Line search for step size lambda in [0,1] minimising the Beckmann objective
  4. x <- x + lambda (y - x)
  5. Convergence: relative gap < tol; else repeat from 1

The network is a networkx MultiDiGraph; we operate on a fixed edge list and carry
flows in parallel arrays for speed. Shortest paths use Dijkstra on the current
BPR time; for parallel edges we always load the minimum-time edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx

from src.assignment.bpr import bpr_time, bpr_integral, ALPHA, BETA


@dataclass
class AssignmentResult:
    flow: dict          # edge (u,v,k) -> flow (PCU/h)
    time: dict          # edge (u,v,k) -> congested travel time (s)
    t0: dict            # edge (u,v,k) -> free-flow travel time (s)
    capacity: dict      # edge (u,v,k) -> effective capacity (PCU/h)
    gaps: list = field(default_factory=list)   # relative gap per iteration
    tstt: float = 0.0   # total system travel time (PCU·s)
    iterations: int = 0
    converged: bool = False


def _edge_arrays(G, t0_attr: str, cap_attr: str):
    """Extract parallel dicts of t0 and capacity keyed by edge (u,v,k)."""
    t0, cap = {}, {}
    for u, v, k, d in G.edges(keys=True, data=True):
        t0[(u, v, k)] = float(d.get(t0_attr, 0.0) or 0.0)
        cap[(u, v, k)] = float(d.get(cap_attr, 1.0) or 1.0)
    return t0, cap


def _set_times(G, times: dict) -> None:
    for (u, v, k), t in times.items():
        G[u][v][k]["_cur_time"] = t


def _min_edge_key(G, u, v):
    """Return the key of the minimum current-time parallel edge between u and v."""
    best_k, best_t = None, float("inf")
    for k, d in G.get_edge_data(u, v).items():
        t = d.get("_cur_time", d.get("length", 1.0))
        if t < best_t:
            best_k, best_t = k, t
    return best_k


def all_or_nothing(G, od_pairs, times: dict):
    """Load each OD demand entirely onto the current shortest path.

    od_pairs: iterable of (origin_node, dest_node, demand).
    Returns (aux_flow dict, sptt) where sptt = sum(demand * shortest-path cost).
    """
    _set_times(G, times)
    aux = {e: 0.0 for e in times}
    sptt = 0.0

    # Group by origin to reuse single-source Dijkstra.
    from collections import defaultdict
    dests_by_origin = defaultdict(list)
    for o, d, dem in od_pairs:
        if dem > 0 and o != d:
            dests_by_origin[o].append((d, dem))

    for o, dests in dests_by_origin.items():
        lengths, paths = nx.single_source_dijkstra(G, o, weight="_cur_time")
        for d, dem in dests:
            if d not in paths:
                continue
            sptt += dem * lengths[d]
            path = paths[d]
            for a, b in zip(path[:-1], path[1:]):
                k = _min_edge_key(G, a, b)
                aux[(a, b, k)] += dem
    return aux, sptt


def _line_search(t0, cap, x, y, alpha, beta, iters=40):
    """Bisection on the Beckmann-objective derivative to find optimal lambda in [0,1].

    phi(lambda) = sum_a integral_0^{x+lambda(y-x)} t_a
    phi'(lambda) = sum_a (y_a - x_a) * t_a( x_a + lambda (y_a - x_a) )
    """
    def dphi(lam):
        s = 0.0
        for e in x:
            xa, ya = x[e], y[e]
            flow = xa + lam * (ya - xa)
            s += (ya - xa) * bpr_time(t0[e], flow, cap[e], alpha, beta)
        return s

    lo, hi = 0.0, 1.0
    dlo, dhi = dphi(lo), dphi(hi)
    if dlo >= 0:      # already optimal at lambda=0
        return 0.0
    if dhi <= 0:      # optimum at the boundary
        return 1.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if dphi(mid) > 0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def assign(G, od_pairs, t0_attr="free_flow_travel_time_s",
           cap_attr="capacity_eff_pcu_hr", alpha=ALPHA, beta=BETA,
           max_iter=100, tol=0.01, verbose=True) -> AssignmentResult:
    """Run Frank-Wolfe User Equilibrium. Returns an AssignmentResult.

    Does not mutate G's stored attributes except a transient '_cur_time'.
    """
    t0, cap = _edge_arrays(G, t0_attr, cap_attr)
    od_pairs = [(o, d, float(dem)) for o, d, dem in od_pairs]

    # Step 0: AON on free-flow times.
    x, _ = all_or_nothing(G, od_pairs, t0)

    gaps = []
    converged = False
    it = 0
    for it in range(1, max_iter + 1):
        # Step 1: current congested times.
        times = {e: bpr_time(t0[e], x[e], cap[e], alpha, beta) for e in x}
        # Step 2: auxiliary AON on current times.
        y, sptt = all_or_nothing(G, od_pairs, times)
        # Relative gap = (TSTT - SPTT) / SPTT.
        tstt = sum(x[e] * times[e] for e in x)
        gap = (tstt - sptt) / sptt if sptt > 0 else 0.0
        gaps.append(gap)
        if verbose:
            print(f"  [FW] iter {it:>3}  gap={gap:.5f}  TSTT={tstt/3600:.1f} PCU·h")
        if gap < tol:
            converged = True
            break
        # Step 3: line search.
        lam = _line_search(t0, cap, x, y, alpha, beta)
        # Step 4: move.
        x = {e: x[e] + lam * (y[e] - x[e]) for e in x}

    final_times = {e: bpr_time(t0[e], x[e], cap[e], alpha, beta) for e in x}
    tstt = sum(x[e] * final_times[e] for e in x)
    return AssignmentResult(
        flow=x, time=final_times, t0=t0, capacity=cap,
        gaps=gaps, tstt=tstt, iterations=it, converged=converged,
    )
