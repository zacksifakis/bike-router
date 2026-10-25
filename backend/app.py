"""Preference-aware bike routing API.

Every edge's cost is its length scaled up by how badly it misses each preference:

    cost = length * (1 + w_bike * (1 - bike)
                       + w_green * (1 - green)
                       + w_lively * (1 - lively)   # or w * lively when avoiding crowds
                       + w_dock * dock_penalty)

A weight of 0 ignores that preference; 3 means a segment that fully misses it
"feels" four times as long. Dock penalty is computed live from the GBFS feed.

Run:  uvicorn backend.app:app --reload
"""

import json
import os
import time
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Any GBFS system works here, including Lyft's (Citi Bike, Divvy, Bay Wheels, ...).
GBFS_URL = os.environ.get("GBFS_URL", "https://gbfs.bcycle.com/1/bcycle_madison/gbfs.json")
GBFS_CACHE_S = 60
DOCK_FAR_M = 800        # past this distance from a usable dock the penalty maxes out
SNAP_MAX_M = 500        # refuse to route from points this far from the network
RIDE_SPEED_MS = 4.2     # ~15 km/h, a relaxed city pace
EARTH_R = 6_371_000


def to_xy(lat, lon):
    """Equirectangular projection in metres; plenty accurate across one city."""
    lat0 = np.radians(43.07)
    return np.column_stack([np.radians(lon) * EARTH_R * np.cos(lat0), np.radians(lat) * EARTH_R])


class Graph:
    def __init__(self, path):
        z = np.load(path)
        self.node_lat, self.node_lon = z["node_lat"], z["node_lon"]
        self.u, self.v = z["edge_u"], z["edge_v"]
        self.length, self.bike, self.green, self.lively = z["length"], z["bike"], z["green"], z["lively"]
        self.mid_xy = to_xy(z["mid_lat"], z["mid_lon"])
        self.coords, self.offsets = z["coords"], z["offsets"]
        self.names = json.loads((path.parent / "edge_names.json").read_text())
        self.n = len(self.node_lat)
        self.node_tree = cKDTree(to_xy(self.node_lat, self.node_lon))

    def snap(self, lat, lon):
        dist, idx = self.node_tree.query(to_xy(np.array([lat]), np.array([lon]))[0])
        if dist > SNAP_MAX_M:
            raise HTTPException(400, f"Point is {dist:.0f} m from the nearest bikeable street; pick somewhere in Madison.")
        return int(idx)

    def shortest(self, cost, src, dst):
        # Parallel edges between the same nodes would be summed by csr_matrix; keep the cheapest.
        order = np.lexsort((cost, self.v, self.u))
        u, v, c = self.u[order], self.v[order], cost[order]
        keep = np.ones(len(u), dtype=bool)
        keep[1:] = (u[1:] != u[:-1]) | (v[1:] != v[:-1])
        edge_ids = order[keep]
        m = csr_matrix((c[keep], (u[keep], v[keep])), shape=(self.n, self.n))
        _, pred = dijkstra(m, indices=src, return_predecessors=True)
        if pred[dst] < 0 and src != dst:
            raise HTTPException(404, "No bikeable route between those points.")

        lookup = {(int(a), int(b)): int(e) for a, b, e in zip(u[keep], v[keep], edge_ids)}
        path, node = [], dst
        while node != src:
            prev = pred[node]
            path.append(lookup[(int(prev), int(node))])
            node = prev
        return path[::-1]

    def edge_coords(self, e):
        return self.coords[self.offsets[e]:self.offsets[e + 1]]


graph = Graph(DATA / "graph.npz")
_gbfs = {"at": 0.0, "stations": []}


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


@app.get("/api/route")
def route(
    from_lat: float, from_lon: float, to_lat: float, to_lon: float,
    bike: float = Query(1.5, ge=0, le=3, description="prefer bike paths and lanes"),
    green: float = Query(0, ge=0, le=3, description="prefer parks, woods and water"),
    lively: float = Query(0, ge=-3, le=3, description=">0 seek busy streets, <0 avoid them"),
    dock: float = Query(0, ge=0, le=3, description="stay near bike-share stations"),
):
    src, dst = graph.snap(from_lat, from_lon), graph.snap(to_lat, to_lon)
    dock_dist = dock_distance(gbfs_stations())
    dock_pen = np.clip(dock_dist / DOCK_FAR_M, 0, 1)
    lively_pen = (1 - graph.lively) if lively >= 0 else graph.lively

    cost = graph.length * (
        1 + bike * (1 - graph.bike) + green * (1 - graph.green) + abs(lively) * lively_pen + dock * dock_pen
    )
    preferred = graph.shortest(cost, src, dst)
    shortest = graph.shortest(graph.length.astype(float), src, dst)
    return {"preferred": summarize(preferred, dock_dist), "shortest": summarize(shortest, dock_dist)}


@app.get("/api/stations")
def stations():
    return gbfs_stations()


app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "frontend" / "index.html")
