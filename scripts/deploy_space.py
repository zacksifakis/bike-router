"""Publish the map as a static Hugging Face Space (free; routing runs in the browser).

Exports the graph for the browser, then uploads frontend/ with a Space README.

Usage:  huggingface-cli login            # once, with a write token
        python scripts/deploy_space.py [--name bike-router]
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import export_static  # noqa: E402

SPACE_README = """---
title: Bike Router
emoji: 🚲
colorFrom: green
colorTo: blue
sdk: static
app_file: index.html
pinned: false
short_description: Bike routes that follow your preferences and e-bike battery
---

Bike directions in Madison, WI weighted by bike paths, nature, crowds and live bike-share
docks, plus a battery-safe e-bike mode that adds dock swaps when your charge runs low.
Everything runs in your browser. Source: https://github.com/zacksifakis/bike-router

Map data © OpenStreetMap contributors (ODbL). Dock data: Madison BCycle GBFS feed.
"""

FILES = ["index.html", "app.js", "style.css", "router.js", "worker.js", "data/graph.bin", "data/graph.json"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="bike-router")
    args = parser.parse_args()

    if not (ROOT / "data" / "graph.npz").exists():
        raise SystemExit("data/graph.npz is missing; run scripts/build_graph.py first.")
    export_static.main()

    api = HfApi()
    repo_id = f"{api.whoami()['name']}/{args.name}"
    api.create_repo(repo_id, repo_type="space", space_sdk="static", exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        for f in FILES:
            (Path(tmp) / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "frontend" / f, Path(tmp) / f)
        (Path(tmp) / "README.md").write_text(SPACE_README)
        api.upload_folder(folder_path=tmp, repo_id=repo_id, repo_type="space",
                          commit_message="Deploy from bike-router")

    user, name = repo_id.split("/")
    print(f"Space: https://huggingface.co/spaces/{repo_id}")
    print(f"App:   https://{user}-{name}.static.hf.space")


if __name__ == "__main__":
    main()
