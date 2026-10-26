"""The browser router (frontend/router.js) must agree with the Python backend.

Random graphs go through both implementations; the ordinary route, battery feasibility, the
battery route's streets and its number of swaps must match. Lengths are random floats so
routes don't tie, but when two docks sit on the same route a swap at either costs the same,
so which one is picked may differ.
Skipped when Node isn't installed (CI installs it).
"""

import json
import random
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from backend.battery import BatteryError, plan
from tests.test_battery import make_graph

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


def random_case(seed):
    rng = random.Random(seed)
    n = 30
    edges = [(a, b, rng.uniform(100, 1500), rng.random())
             for a in range(n) for b in range(a + 1, n) if rng.random() < 0.12]
    g = make_graph(n, edges)
    docks = rng.sample(range(1, n - 1), 4)
    # Odd seeds weight bike infrastructure; even ones route on plain distance.
    weight = 2.0 if seed % 2 else 0.0
    cost = g.length * (1 + weight * (1 - g.bike))
    return g, cost, docks, rng.uniform(800, 4000), rng.uniform(3000, 6000)


def python_result(g, cost, docks, start_range, swap_range):
    cost_net, len_net = g.network(cost), g.network(g.length)
    try:
        legs = [{"edges": l.edges, "swap_at": l.swap_at}
                for l in plan(cost_net, len_net, 0, g.n - 1, docks, start_range, swap_range)]
        error = None
    except BatteryError as e:
        legs, error = None, str(e)
    return {"preferred": cost_net.shortest(0, g.n - 1), "legs": legs, "error": error}


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_browser_router_matches_python():
    seeds = range(40)
    cases, expected = [], []
    for seed in seeds:
        g, cost, docks, start_range, swap_range = random_case(seed)
        cases.append({
            "graph": {k: np.asarray(getattr(g, attr)).ravel().tolist() for k, attr in [
                ("node_lat", "node_lat"), ("node_lon", "node_lon"), ("edge_u", "u"), ("edge_v", "v"),
                ("length", "length"), ("bike", "bike"), ("green", "green"), ("lively", "lively"),
                ("coords", "coords"), ("offsets", "offsets")]}
            | {"mid_lat": g.node_lat[g.u].tolist(), "mid_lon": g.node_lon[g.u].tolist()},
            "cost": cost.tolist(), "src": 0, "dst": g.n - 1, "docks": docks,
            "start_range": start_range, "swap_range": swap_range,
        })
        expected.append(python_result(g, cost, docks, start_range, swap_range))

    out = subprocess.run([NODE, str(ROOT / "tests" / "js" / "run_router.mjs")],
                         input=json.dumps({"cases": cases}), capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)

    def route(legs):
        return [e for leg in legs for e in leg["edges"]], len(legs) - 1

    feasible, swaps = 0, 0
    for seed, js, py in zip(seeds, got, expected):
        assert js["preferred"] == py["preferred"], seed
        assert (js["error"] is None) == (py["error"] is None), (seed, js["error"], py["error"])
        if py["error"] is None:
            assert route(js["legs"]) == route(py["legs"]), seed
            feasible += 1
            swaps += route(py["legs"])[1] > 0
    # The cases should exercise all three outcomes: direct, with swaps, and impossible.
    assert feasible < len(seeds) and swaps > 5 and feasible - swaps > 5
