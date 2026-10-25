import itertools
import random

import networkx as nx
import numpy as np
import pytest

from backend.battery import BatteryError, dock_reach, leg_profile, plan, safe_tree
from backend.graph import Graph


def make_graph(n, edges):
    """Graph from (u, v, length, bike_score) tuples; every edge is two-way."""
    both = [(u, v, l, b) for u, v, l, b in edges] + [(v, u, l, b) for u, v, l, b in edges]
    u, v, length, bike = map(np.array, zip(*both))
    lat, lon = np.linspace(43.0, 43.1, n), np.linspace(-89.5, -89.3, n)
    # Each edge is drawn as a straight segment between its two nodes.
    coords = np.stack([np.column_stack([lon[u], lat[u]]), np.column_stack([lon[v], lat[v]])], axis=1).reshape(-1, 2)
    zeros = np.zeros(len(u))
    return Graph(lat, lon, u, v, length, bike, zeros, zeros,
                 lat[u], lon[u], coords, np.arange(0, 2 * len(u) + 1, 2), [""] * len(u))


def nodes_on(g, legs):
    return {int(g.v[e]) for leg in legs for e in leg.edges}


def test_line_needs_one_swap():
    # 0 - 1 - ... - 10, 1 km apart, one dock in the middle at node 5.
    g = make_graph(11, [(i, i + 1, 1000, 0) for i in range(10)])
    net = g.network(g.length)
    legs = plan(net, net, 0, 10, [5], start_range=6000, swap_range=10000)
    assert [leg.swap_at for leg in legs] == [0, None]

    legs = plan(net, net, 0, 10, [5], start_range=15000, swap_range=10000)
    assert len(legs) == 1


def test_errors_when_stranded():
    g = make_graph(11, [(i, i + 1, 1000, 0) for i in range(10)])
    net = g.network(g.length)
    with pytest.raises(BatteryError, match="nearest dock"):
        plan(net, net, 0, 10, [5], start_range=4000, swap_range=10000)
    with pytest.raises(BatteryError, match="No safe route"):
        plan(net, net, 0, 10, [5], start_range=6000, swap_range=9000)


def test_takes_scenic_route_only_when_safe():
    # s=0, t=1. Bike path via node 2 (far from docks) or a plain street via node 3 (has a dock).
    g = make_graph(4, [(0, 2, 3000, 1), (2, 1, 3000, 1), (0, 3, 2500, 0), (3, 1, 2500, 0)])
    cost_net = g.network(g.length * (1 + 3 * (1 - g.bike)))
    len_net = g.network(g.length)

    plenty = plan(cost_net, len_net, 0, 1, [3], start_range=20000, swap_range=20000)
    assert nodes_on(g, plenty) == {2, 1}

    # At node 2 you'd have ridden 3 km and be 5.5 km from the dock: 8.5 km > 8 km of range.
    tight = plan(cost_net, len_net, 0, 1, [3], start_range=8000, swap_range=20000)
    assert nodes_on(g, tight) == {3, 1}
    assert [leg.swap_at for leg in tight] == [None]

    _, margin = leg_profile(g, tight, dock_reach(len_net, [3]))
    assert margin >= 0


@pytest.mark.parametrize("seed", range(25))
def test_safe_tree_matches_brute_force(seed):
    """On plain length, safe_tree reaches a node iff some safe path exists, at the shortest safe distance."""
    rng = random.Random(seed)
    n = 9
    edges = [(a, b, rng.randint(100, 1500), 0)
             for a, b in itertools.combinations(range(n), 2) if rng.random() < 0.35]
    g = make_graph(n, edges)
    net = g.network(g.length)
    docks = rng.sample(range(1, n), 2)
    start_range = rng.randint(1500, 4000)

    G = nx.Graph()
    G.add_nodes_from(range(n))
    G.add_weighted_edges_from((a, b, l) for a, b, l, _ in edges)
    reach = {v: min(nx.shortest_path_length(G, v, d, weight="weight") if nx.has_path(G, v, d) else np.inf
                    for d in docks) for v in range(n)}

    def safe(path):
        along = 0
        for a, b in zip([None] + path[:-1], path):
            along += G[a][b]["weight"] if a is not None else 0
            if along + reach[b] > start_range:
                return None
        return along

    tree = safe_tree(net, 0, start_range, dock_reach(net, docks))
    for t in range(1, n):
        lengths = [safe(p) for p in nx.all_simple_paths(G, 0, t)] if reach[0] <= start_range else []
        best = min([l for l in lengths if l is not None], default=np.inf)
        got = np.inf if tree is None else tree.dist[t]
        assert got == pytest.approx(best, abs=0.01), (t, got, best)
