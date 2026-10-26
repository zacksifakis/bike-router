"""Preference-aware bike routing API.

Every edge's cost is its length scaled up by how badly it misses each preference:

    cost = length * (1 + w_bike * (1 - bike)
                       + w_green * (1 - green)
                       + w_lively * (1 - lively)   # or w * lively when avoiding crowds
                       + w_dock * dock_penalty)

A weight of 0 ignores that preference; 3 means a segment that fully misses it
"feels" four times as long. Dock penalty is computed live from the GBFS feed.

With a battery level set, the route must also never leave the rider unable
to reach a dock; see battery.py.

Run:  uvicorn backend.app:app --reload
"""

import json
import os
import time
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from scipy.spatial import cKDTree

from .battery import BatteryError, dock_reach, first_unsafe, leg_profile, plan
from .graph import Graph, to_xy

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Any GBFS system works here, including Lyft's (Citi Bike, Divvy, Bay Wheels, ...).
GBFS_URL = os.environ.get("GBFS_URL", "https://gbfs.bcycle.com/1/bcycle_madison/gbfs.json")
GBFS_CACHE_S = 60
DOCK_FAR_M = 800        # past this distance from a usable dock the penalty maxes out
SNAP_MAX_M = 500        # refuse to route from points this far from the network
RIDE_SPEED_MS = 4.2     # ~15 km/h, a relaxed city pace
DOCK_SNAP_M = 150       # stations farther than this from the street network are ignored
SWAP_CHARGE = 0.9       # GBFS doesn't report each bike's charge; assume a docked bike is at 90%


graph = Graph.load(DATA / "graph.npz")
_gbfs = {"at": 0.0, "stations": []}


def snap(lat, lon):
    idx, dist = graph.snap(lat, lon)
    if dist[0] > SNAP_MAX_M:
        raise HTTPException(400, f"Point is {dist[0]:.0f} m from the nearest bikeable street; pick somewhere in Madison.")
    return int(idx[0])


def gbfs_stations():
    if time.time() - _gbfs["at"] < GBFS_CACHE_S:
        return _gbfs["stations"]
    try:
        with httpx.Client(timeout=10) as client:
            feeds = client.get(GBFS_URL).json()["data"]
            feeds = next(iter(feeds.values()))["feeds"]
            urls = {f["name"]: f["url"] for f in feeds}
            info = client.get(urls["station_information"]).json()["data"]["stations"]
            status = {s["station_id"]: s for s in client.get(urls["station_status"]).json()["data"]["stations"]}
    except Exception:
        return _gbfs["stations"]  # keep serving the last good snapshot

    stations = []
    for s in info:
        st = status.get(s["station_id"], {})
        stations.append({
            "id": s["station_id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
            "bikes": st.get("num_bikes_available", 0), "docks": st.get("num_docks_available", 0),
            # BCycle nests e-bike counts by type; Lyft feeds use a flat field.
            "ebikes": st.get("num_bikes_available_types", {}).get("electric", st.get("num_ebikes_available", 0)),
            "active": bool(st.get("is_renting", 0)),
        })
    _gbfs.update(at=time.time(), stations=stations)
    return stations


def dock_distance(stations):
    """Metres from each edge midpoint to the nearest station you could swap a bike at."""
    usable = [s for s in stations if s["active"] and (s["bikes"] > 0 or s["docks"] > 0)]
    if not usable:
        return np.full(len(graph.length), DOCK_FAR_M, dtype=float)
    tree = cKDTree(to_xy(np.array([s["lat"] for s in usable]), np.array([s["lon"] for s in usable])))
    dist, _ = tree.query(graph.mid_xy)
    return dist


def summarize(path, dock_dist):
    length = graph.length[path]
    total = float(length.sum())
    w = length / total if total else length
    coords = np.concatenate([graph.edge_coords(e) for e in path]) if path else np.empty((0, 2))
    streets = []
    for e in path:
        name = graph.names[e]
        if name and (not streets or streets[-1] != name):
            streets.append(name)
    return {
        "geometry": {"type": "LineString", "coordinates": coords.round(6).tolist()},
        "distance_m": round(total),
        "duration_min": round(total / RIDE_SPEED_MS / 60, 1),
        "bike_infra_pct": round(100 * float(w[graph.bike[path] >= 0.6].sum())),
        "green_pct": round(100 * float(w[graph.green[path] >= 0.5].sum())),
        "lively_pct": round(100 * float(w[graph.lively[path] >= 0.5].sum())),
        "max_dock_gap_m": round(float(dock_dist[path].max())) if path else 0,
        "streets": streets[:12],
    }


app = FastAPI(title="Bike Router")


def swap_docks(stations):
    """Stations where you could trade your bike for a charged one, snapped to the graph."""
    usable = [s for s in stations if s["active"] and s["docks"] > 0 and s["ebikes"] > 0]
    if not usable:
        return [], []
    idx, dist = graph.snap([s["lat"] for s in usable], [s["lon"] for s in usable])
    seen, nodes, kept = set(), [], []
    for s, i, d in zip(usable, idx, dist):
        if d <= DOCK_SNAP_M and i not in seen:
            seen.add(i)
            nodes.append(int(i))
            kept.append(s)
    return nodes, kept


def battery_route(cost_net, len_net, src, dst, unconstrained, battery, range_km, dock_dist):
    stations = gbfs_stations()
    nodes, docks = swap_docks(stations)
    full = range_km * 1000
    start_range, swap_range = full * battery / 100, full * SWAP_CHARGE
    try:
        legs = plan(cost_net, len_net, src, dst, nodes, start_range, swap_range)
    except BatteryError as e:
        raise HTTPException(422, str(e))

    reach = dock_reach(len_net, nodes)
    edges = [e for leg in legs for e in leg.edges]
    profile, margin = leg_profile(graph, legs, reach)
    swaps, done = [], 0.0
    for leg in legs:
        done += graph.length[leg.edges].sum()
        if leg.swap_at is not None:
            d = docks[leg.swap_at]
            swaps.append({"name": d["name"], "lat": d["lat"], "lon": d["lon"], "at_m": round(done), "ebikes": d["ebikes"]})
    unsafe_at, empty_at = first_unsafe(graph, unconstrained, start_range, reach)

    out = summarize(edges, dock_dist)
    out["battery"] = {
        "profile": [[round(f, 5), round(100 * r / full, 1)] for f, r in profile],
        "arrive_pct": round(100 * profile[-1][1] / full),
        "swaps": swaps,
        "min_margin_m": round(margin),
        "without": {"unsafe_at_m": unsafe_at and round(unsafe_at), "empty_at_m": empty_at and round(empty_at)},
    }
    return out


@app.get("/api/route")
def route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
    bike: float = Query(1.5, ge=0, le=3, description="prefer bike paths and lanes"),
    green: float = Query(0, ge=0, le=3, description="prefer parks, woods and water"),
    lively: float = Query(0, ge=-3, le=3, description=">0 seek busy streets, <0 avoid them"),
    dock: float = Query(0, ge=0, le=3, description="stay near bike-share stations"),
    battery: float | None = Query(None, ge=0, le=100, description="e-bike charge %; enables battery-safe routing"),
    range_km: float = Query(40, gt=0, le=200, description="range of a fully charged e-bike"),
):
    src, dst = snap(from_lat, from_lon), snap(to_lat, to_lon)
    dock_dist = dock_distance(gbfs_stations())
    dock_pen = np.clip(dock_dist / DOCK_FAR_M, 0, 1)
    lively_pen = (1 - graph.lively) if lively >= 0 else graph.lively

    cost = graph.length * (
        1 + bike * (1 - graph.bike) + green * (1 - graph.green) + abs(lively) * lively_pen + dock * dock_pen
    )
    cost_net, len_net = graph.network(cost), graph.network(graph.length)
    preferred, shortest = cost_net.shortest(src, dst), len_net.shortest(src, dst)
    if preferred is None:
        raise HTTPException(404, "No bikeable route between those points.")

    if battery is not None:
        chosen = battery_route(cost_net, len_net, src, dst, preferred, battery, range_km, dock_dist)
    else:
        chosen = summarize(preferred, dock_dist)
    return {"preferred": chosen, "shortest": summarize(shortest, dock_dist)}


@app.get("/api/stations")
def stations():
    return gbfs_stations()


# The map page routes in the browser (frontend/router.js); the API above is the reference
# implementation. Run scripts/export_static.py first so frontend/data/ exists.
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
