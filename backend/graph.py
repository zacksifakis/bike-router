"""The scored street graph and the shortest-path plumbing shared by every router."""

import json

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

EARTH_R = 6_371_000
EPS = 1e-6  # added to edge weights so zero-length edges still count as edges in csgraph


def to_xy(lat, lon):
    """Equirectangular projection in metres; plenty accurate across one city."""
    lat0 = np.radians(43.07)
    return np.column_stack([np.radians(lon) * EARTH_R * np.cos(lat0), np.radians(lat) * EARTH_R])


class Graph:
    def __init__(self, node_lat, node_lon, edge_u, edge_v, length, bike, green, lively,
                 mid_lat, mid_lon, coords, offsets, names):
        self.node_lat, self.node_lon = np.asarray(node_lat), np.asarray(node_lon)
        self.u, self.v = np.asarray(edge_u), np.asarray(edge_v)
        self.length = np.asarray(length, dtype=float)
        self.bike, self.green, self.lively = np.asarray(bike), np.asarray(green), np.asarray(lively)
        self.mid_xy = to_xy(np.asarray(mid_lat), np.asarray(mid_lon))
        self.coords, self.offsets = np.asarray(coords), np.asarray(offsets)
        self.names = names
        self.n = len(self.node_lat)
        self.node_tree = cKDTree(to_xy(self.node_lat, self.node_lon))

    @classmethod
    def load(cls, path):
        z = np.load(path)
        names = json.loads((path.parent / "edge_names.json").read_text())
        return cls(**{k: z[k] for k in z.files if k not in ("edge_u", "edge_v")},
                   edge_u=z["edge_u"], edge_v=z["edge_v"], names=names)

    def snap(self, lat, lon):
        """Nearest node to a point, and how far away it is in metres."""
        dist, idx = self.node_tree.query(to_xy(np.atleast_1d(lat), np.atleast_1d(lon)))
        return idx.astype(int), dist

    def network(self, cost):
        return Network(self, cost)

    def edge_coords(self, e):
        return self.coords[self.offsets[e]:self.offsets[e + 1]]


class Network:
    """The graph weighted by one cost per edge, ready for scipy's Dijkstra.

    Parallel edges between the same two nodes would be summed by csr_matrix,
    so only the cheapest is kept.
    """

    def __init__(self, graph, cost):
        self.graph = graph
        self.cost = np.asarray(cost, dtype=float)
        order = np.lexsort((self.cost, graph.v, graph.u))
        u, v = graph.u[order], graph.v[order]
        keep = np.ones(len(u), dtype=bool)
        keep[1:] = (u[1:] != u[:-1]) | (v[1:] != v[:-1])
        self.edge_ids = order[keep]
        self.eu, self.ev = u[keep], v[keep]
        shape = (graph.n, graph.n)
        self.csr = csr_matrix((self.cost[self.edge_ids] + EPS, (self.eu, self.ev)), shape=shape)
        self.edge_csr = csr_matrix((self.edge_ids + 1, (self.eu, self.ev)), shape=shape)
        # Upper bound on cost per metre, used to cap searches by riding distance.
        self.max_ratio = float(np.max(self.cost / np.maximum(graph.length, EPS), initial=1.0))

    def restricted(self, allowed):
        """The same network with every edge touching a disallowed node removed."""
        keep = allowed[self.eu] & allowed[self.ev]
        return csr_matrix((self.cost[self.edge_ids[keep]] + EPS, (self.eu[keep], self.ev[keep])),
                          shape=self.csr.shape)

    def tree_edges(self, pred):
        """Edge id used to reach each node in a shortest-path tree (-1 where none)."""
        nodes = np.flatnonzero(pred >= 0)
        out = np.full(len(pred), -1)
        out[nodes] = np.asarray(self.edge_csr[pred[nodes], nodes]).ravel().astype(int) - 1
        return out

    def path(self, pred, src, dst):
        """Edge ids from src to dst following a predecessor array."""
        if dst != src and pred[dst] < 0:
            return None
        edges, node = [], dst
        while node != src:
            prev = pred[node]
            edges.append(int(self.edge_csr[prev, node]) - 1)
            node = prev
        return edges[::-1]

    def shortest(self, src, dst):
        _, pred = dijkstra(self.csr, indices=src, return_predecessors=True)
        return self.path(pred, src, dst)
