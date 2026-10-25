"""Download Madison's bike network from OpenStreetMap and score every street segment.

Each edge gets three static scores in [0, 1]:
  bike    - how good the bike infrastructure is (1 = separated path, 0 = arterial with nothing)
  green   - how close it runs to parks, woods and water
  lively  - how many shops, cafes and other destinations line it

Bike-share proximity is not computed here; it depends on live dock availability,
so the API works it out per request.

Usage:  python scripts/build_graph.py [--place "Madison, Wisconsin, USA"]
Output: data/graph.npz and data/edge_names.json
"""

import argparse
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import osmnx as ox
from scipy.spatial import cKDTree
from shapely import STRtree, points as make_points
from shapely.geometry import LineString

DATA = Path(__file__).resolve().parent.parent / "data"

# Tags osmnx drops by default but we need for scoring bike infrastructure.
ox.settings.useful_tags_way += [
    "cycleway", "cycleway:left", "cycleway:right", "cycleway:both",
    "bicycle", "segregated", "surface",
]

GREEN_TAGS = {
    "leisure": ["park", "nature_reserve", "garden", "golf_course"],
    # landuse=grass is left out: OSM uses it for medians and lawn strips all over downtown.
    "landuse": ["forest", "meadow", "recreation_ground", "village_green"],
    "natural": ["wood", "water", "wetland", "scrub", "grassland", "beach"],
    "waterway": ["river", "stream", "canal"],
}

# Amenities that draw people. Plain amenity=True would pull in benches and parking lots.
LIVELY_AMENITIES = [
    "restaurant", "cafe", "bar", "pub", "fast_food", "ice_cream", "food_court",
    "biergarten", "library", "theatre", "cinema", "arts_centre", "marketplace",
    "community_centre", "nightclub",
]
LIVELY_TAGS = {"amenity": LIVELY_AMENITIES, "shop": True, "tourism": True}

GREEN_MIN_AREA_M2 = 5000  # ignore pocket lawns and planters smaller than half a hectare
GREEN_RADIUS_M = 120   # green score falls to 0 at this distance from green space
LIVELY_RADIUS_M = 120  # count destinations within this distance of the segment
LIVELY_SATURATION = 25  # this many destinations nearby = fully lively


def first(value):
    """osmnx collapses merged ways into lists; take the first tag value."""
    return value[0] if isinstance(value, list) else value


def bike_score(d):
    hw = first(d.get("highway", ""))
    bicycle = first(d.get("bicycle", ""))
    cycleway = " ".join(
        str(first(d.get(k, ""))) for k in ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both")
    )

    if hw == "cycleway":
        return 1.0
    if hw in ("path", "footway", "track", "bridleway", "pedestrian"):
        if bicycle == "designated":
            return 1.0
        return 0.75 if hw == "path" else 0.45
    if "track" in cycleway:
        return 0.85
    if "lane" in cycleway and "shared_lane" not in cycleway:
        return 0.6
    if "shared_lane" in cycleway or "share_busway" in cycleway:
        return 0.35
    return {
        "living_street": 0.45,
        "residential": 0.35,
        "service": 0.3,
        "unclassified": 0.25,
        "tertiary": 0.15,
        "tertiary_link": 0.15,
        "secondary": 0.05,
        "secondary_link": 0.05,
    }.get(hw, 0.0)


def sample_points(lines, fractions=(0.25, 0.5, 0.75)):
    """Points along each line, shape (n_lines, len(fractions), 2)."""
    return np.array([[ln.interpolate(f, normalized=True).coords[0] for f in fractions] for ln in lines])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--place", default="Madison, Wisconsin, USA")
    parser.add_argument("--buffer-m", type=float, default=1500, help="extend the area past city limits")
    parser.add_argument("--overpass-url", default=None,
                        help="Overpass mirror, e.g. https://overpass.private.coffee/api if the default is busy")
    args = parser.parse_args()
    if args.overpass_url:
        ox.settings.overpass_url = args.overpass_url
    DATA.mkdir(exist_ok=True)

    print(f"Geocoding {args.place} ...")
    area = ox.geocode_to_gdf(args.place)
    area_proj = ox.projection.project_gdf(area)
    crs = area_proj.crs
    polygon = area_proj.buffer(args.buffer_m).to_crs(4326).iloc[0]

    print("Downloading bike network ...")
    G = ox.graph_from_polygon(polygon, network_type="bike", retain_all=False)
    print(f"  {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    print("Downloading parks and water ...")
    green = ox.features_from_polygon(polygon, GREEN_TAGS).to_crs(crs)
    green = green[green.geometry.notna()]
    is_area = green.geom_type.isin(["Polygon", "MultiPolygon"])
    green = green[~is_area | (green.area >= GREEN_MIN_AREA_M2)]
    print(f"  {len(green)} green/water features")

    print("Downloading shops and destinations ...")
    # A plain rectangle is much cheaper for Overpass than the city's ragged outline.
    pois = ox.features_from_polygon(polygon.envelope, LIVELY_TAGS).to_crs(crs)
    poi_xy = np.array([(g.x, g.y) for g in pois.geometry.representative_point()])
    print(f"  {len(poi_xy)} destinations")

    # Node arrays, indexed 0..n-1.
    node_ids = list(G.nodes)
    index = {n: i for i, n in enumerate(node_ids)}
    node_lon = np.array([G.nodes[n]["x"] for n in node_ids])
    node_lat = np.array([G.nodes[n]["y"] for n in node_ids])

    edges_u, edges_v, length, bike, names, lines = [], [], [], [], [], []
    for u, v, d in G.edges(data=True):
        geom = d.get("geometry")
        if geom is None:
            geom = LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
        # Make sure the geometry runs u -> v so routes draw continuously.
        x0, y0 = geom.coords[0]
        if (x0 - G.nodes[u]["x"]) ** 2 + (y0 - G.nodes[u]["y"]) ** 2 > (x0 - G.nodes[v]["x"]) ** 2 + (y0 - G.nodes[v]["y"]) ** 2:
            geom = LineString(list(geom.coords)[::-1])
        edges_u.append(index[u])
        edges_v.append(index[v])
        length.append(d["length"])
        bike.append(bike_score(d))
        name = first(d.get("name", ""))
        names.append(name if isinstance(name, str) else "")
        lines.append(geom)

    print("Scoring segments ...")
    lines_proj = gpd.GeoSeries(lines, crs=4326).to_crs(crs)
    pts = sample_points(lines_proj)  # (E, 3, 2)
    flat = pts.reshape(-1, 2)

    tree = STRtree(green.geometry.values)
    _, dist = tree.query_nearest(make_points(flat), return_distance=True, all_matches=False)
    green_score = np.clip(1 - dist / GREEN_RADIUS_M, 0, 1).reshape(pts.shape[:2]).mean(axis=1)

    poi_tree = cKDTree(poi_xy)
    counts = np.array([len(c) for c in poi_tree.query_ball_point(pts[:, 1, :], LIVELY_RADIUS_M)])
    lively_score = np.clip(np.log1p(counts) / np.log1p(LIVELY_SATURATION), 0, 1)

    mid = gpd.GeoSeries(lines, crs=4326).interpolate(0.5, normalized=True)

    # Flatten geometries into one coordinate array plus offsets.
    coords = [np.asarray(ln.coords) for ln in lines]
    offsets = np.cumsum([0] + [len(c) for c in coords])
    coords_flat = np.concatenate(coords)

    np.savez_compressed(
        DATA / "graph.npz",
        node_lat=node_lat, node_lon=node_lon,
        edge_u=np.array(edges_u, dtype=np.int32), edge_v=np.array(edges_v, dtype=np.int32),
        length=np.array(length), bike=np.array(bike), green=green_score, lively=lively_score,
        mid_lat=mid.y.values, mid_lon=mid.x.values,
        coords=coords_flat.astype(np.float32), offsets=offsets.astype(np.int64),
    )
    (DATA / "edge_names.json").write_text(json.dumps(names))

    km = np.array(length)
    print(f"Done: {len(names)} edges, {km.sum() / 1000:.0f} km of network")
    print(f"  bike infra (score >= 0.6): {km[np.array(bike) >= 0.6].sum() / km.sum():.0%} of length")
    print(f"  near green (score >= 0.5): {km[green_score >= 0.5].sum() / km.sum():.0%} of length")
    print(f"  lively     (score >= 0.5): {km[lively_score >= 0.5].sum() / km.sum():.0%} of length")


if __name__ == "__main__":
    main()
