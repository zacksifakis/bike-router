"""Pack data/graph.npz into files the browser router can load.

Writes frontend/data/graph.bin (little-endian typed arrays back to back) and
frontend/data/graph.json (where each array starts, plus the street-name table).

Usage:  python scripts/export_static.py
"""

import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data"
OUT = ROOT / "frontend" / "data"


def main():
    z = np.load(SRC / "graph.npz")
    names = json.loads((SRC / "edge_names.json").read_text())
    table = sorted(set(names))
    index = {name: i for i, name in enumerate(table)}

    arrays = {
        "node_lat": z["node_lat"].astype("<f4"),
        "node_lon": z["node_lon"].astype("<f4"),
        "edge_u": z["edge_u"].astype("<i4"),
        "edge_v": z["edge_v"].astype("<i4"),
        "length": z["length"].astype("<f4"),
        "bike": z["bike"].astype("<f4"),
        "green": z["green"].astype("<f4"),
        "lively": z["lively"].astype("<f4"),
        "mid_lat": z["mid_lat"].astype("<f4"),
        "mid_lon": z["mid_lon"].astype("<f4"),
        "coords": z["coords"].astype("<f4").ravel(),
        "offsets": z["offsets"].astype("<i4"),
        "name_idx": np.array([index[n] for n in names], dtype="<u2"),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    meta, offset = {}, 0
    with open(OUT / "graph.bin", "wb") as f:
        for key, arr in arrays.items():
            pad = -offset % 4  # typed arrays need aligned starts
            f.write(b"\0" * pad)
            offset += pad
            meta[key] = {"offset": offset, "length": int(arr.size), "dtype": arr.dtype.str}
            f.write(arr.tobytes())
            offset += arr.nbytes

    (OUT / "graph.json").write_text(json.dumps({
        "nodes": len(arrays["node_lat"]), "edges": len(arrays["edge_u"]),
        "arrays": meta, "names": table,
    }))
    print(f"Wrote {offset / 1e6:.1f} MB to {OUT / 'graph.bin'} and {len(table)} street names")


if __name__ == "__main__":
    main()
