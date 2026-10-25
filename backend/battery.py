"""Battery-safe routing for shared e-bikes.

The rule: at every point of the ride, the charge left must be enough to reach a
dock where you can swap for a charged bike. If the destination is out of range,
the route goes through docks and swaps on the way.

It's the electric-car charging-stop problem at bike scale, solved in two levels:

1. Leg search. From a stop (the start, or a dock after a swap) with range r, a
   Dijkstra that refuses to enter any node v where
       distance ridden to v + distance from v to the nearest dock > r.
   Run on plain length this is exact: a node is reached iff some safe path to
   it exists. Run on the preference-weighted cost it gives nicer legs, but can
   miss some; those legs fall back to the exact length-based ones.

2. Stop search. A* over {start, docks, destination} where an edge is any leg
   the search above found safe. Leg cost is preference-weighted, each swap
   adds a fixed penalty, and the heuristic is the unconstrained cost-to-go
   (a lower bound, so A* stays optimal over these legs).
"""

import heapq
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from .graph import EPS

SWAP_PENALTY = 400     # a swap costs as much as riding this far (stopping, re-docking, unlocking)
MAX_EXPANSIONS = 40    # stops to expand before giving up
TOL = 0.5              # metres of slack so float rounding doesn't reject exact-fit routes


class BatteryError(Exception):
    pass


def fmt_dist(m):
    return f"{m:.0f} m" if m < 1000 else f"{m / 1000:.1f} km"


@dataclass
class Tree:
    """A safe shortest-path tree from one stop."""
    net: object
    src: int
    dist: np.ndarray   # cost along the tree (inf where unsafe or unreachable)
    pred: np.ndarray
    edges: np.ndarray = field(default=None)  # tree edge into each node


def dock_reach(len_net, dock_nodes):
    """Riding distance from every node to its nearest dock."""
    if len(dock_nodes) == 0:
        return np.full(len_net.graph.n, np.inf)
    return dijkstra(len_net.csr.T.tocsr(), indices=dock_nodes, min_only=True)


def tree_sum(tree, values):
    """Sum per-edge values along the tree path to every node."""
    nodes = np.flatnonzero(tree.pred >= 0)
    m = csr_matrix((values[tree.edges[nodes]] + EPS, (tree.pred[nodes], nodes)), shape=(len(tree.pred),) * 2)
    return dijkstra(m, indices=tree.src)


def safe_tree(net, src, start_range, reach):
    """Shortest-path tree on net from src that never strands the rider.

    Nodes whose tree path breaks the rule are removed and the search re-run
    until the tree is clean. Removing a node only makes other paths longer, so
    a node that breaks the rule once can never become safe again.
    """
    graph = net.graph
    budget = start_range - reach + TOL
    allowed = budget >= 0
    if not allowed[src]:
        return None
    limit = start_range * net.max_ratio + 1
    while True:
        dist, pred = dijkstra(net.restricted(allowed), indices=src, return_predecessors=True, limit=limit)
        tree = Tree(net, src, dist, pred)
        tree.edges = net.tree_edges(pred)
        along = tree_sum(tree, graph.length)
        bad = np.isfinite(along) & (along > budget)
        if not bad.any():
            tree.dist = np.where(np.isfinite(along), dist, np.inf)
            return tree
        allowed &= ~bad


@dataclass
class Leg:
    edges: list
    start_range: float
    swap_at: int = None  # index into docks of the swap at the end of this leg, if any


def plan(cost_net, len_net, src, dst, docks, start_range, swap_range):
    """Cheapest battery-safe route from src to dst.

    docks: graph node of each dock you can swap at.
    Returns a list of Legs; raises BatteryError when no safe route exists.
    """
    reach = dock_reach(len_net, docks)
    if start_range + TOL < reach[src]:
        raise BatteryError(
            f"Not enough charge: the nearest dock is {fmt_dist(reach[src])} away "
            f"but you have {fmt_dist(start_range)} of range.")

    stops = [src] + list(docks) + [dst]
    DST = len(stops) - 1
    h = dijkstra(cost_net.csr.T.tocsr(), indices=dst)  # unconstrained cost-to-go, never too high

    best = {0: 0.0}
    parent = {}
    heap = [(h[src], 0.0, 0)]
    expanded = set()

    while heap:
        _, g, i = heapq.heappop(heap)
        if i in expanded:
            continue
        expanded.add(i)
        if i == DST:
            return _legs(stops, parent, start_range, swap_range)
        if len(expanded) > MAX_EXPANSIONS:
            break

        r = start_range if i == 0 else swap_range
        exact = safe_tree(len_net, stops[i], r, reach)
        nice = safe_tree(cost_net, stops[i], r, reach)
        exact_cost = tree_sum(exact, cost_net.cost)  # preference cost of the exact tree's paths

        for j in range(1, len(stops)):
            if j in expanded or j == i:
                continue
            node = stops[j]
            if not np.isfinite(exact.dist[node]):
                continue  # no safe way to get there on this charge
            tree, leg_cost = (nice, nice.dist[node]) if np.isfinite(nice.dist[node]) else (exact, exact_cost[node])
            new_g = g + leg_cost + (0 if j == DST else SWAP_PENALTY)
            if new_g < best.get(j, np.inf):
                best[j] = new_g
                parent[j] = (i, tree)
                heapq.heappush(heap, (new_g + h[node], new_g, j))

    raise BatteryError("No safe route: the destination is too far from docks with bikes on this charge.")


def _legs(stops, parent, start_range, swap_range):
    chain, j = [], len(stops) - 1
    while j != 0:
        i, tree = parent[j]
        chain.append((i, j, tree))
        j = i
    legs = []
    for i, j, tree in reversed(chain):
        edges = tree.net.path(tree.pred, stops[i], stops[j])
        legs.append(Leg(edges, start_range if i == 0 else swap_range,
                        swap_at=None if j == len(stops) - 1 else j - 1))
    return legs


def leg_profile(graph, legs, reach):
    """Charge along the route: (fraction of distance, range left) breakpoints and the tightest margin."""
    total = sum(graph.length[l.edges].sum() for l in legs) or 1.0
    points, done, margin = [], 0.0, np.inf
    for leg in legs:
        lengths = graph.length[leg.edges]
        along = np.concatenate([[0.0], np.cumsum(lengths)])
        nodes = np.concatenate([[graph.u[leg.edges[0]]], graph.v[leg.edges]]) if leg.edges else np.array([])
        if len(nodes):
            margin = min(margin, float(np.min(leg.start_range - along - reach[nodes])))
        points.append((done / total, leg.start_range))
        done += along[-1]
        points.append((done / total, leg.start_range - along[-1]))
    return points, margin


def first_unsafe(graph, edges, start_range, reach):
    """Where an ordinary route breaks the rule, as (metres to point of no return, metres until empty)."""
    if not edges:
        return None, None
    along = np.concatenate([[0.0], np.cumsum(graph.length[edges])])
    nodes = np.concatenate([[graph.u[edges[0]]], graph.v[edges]])
    unsafe = np.flatnonzero(along + reach[nodes] > start_range + TOL)
    empty = start_range if along[-1] > start_range else None
    return (float(along[unsafe[0]]) if len(unsafe) else None), empty
