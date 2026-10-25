"""Publish the app as a Hugging Face Space (Docker).

The Space gets the server code, the frontend, the prebuilt graph in data/ and
a Space README; the build script, tests and cache stay out.

Usage:  huggingface-cli login            # once
        python scripts/deploy_space.py [--name bike-router]
"""

import argparse
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi

ROOT = Path(__file__).resolve().parent.parent

SPACE_README = """---
title: Bike Router
emoji: 🚲
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
short_description: Bike routes that follow your preferences and e-bike battery
---

Live demo of [Bike Router](https://github.com/zacksifakis/bike-router): bike directions in
Madison, WI weighted by bike paths, nature, crowds and live bike-share docks, plus a
battery-safe e-bike mode that adds dock swaps when your charge runs low.

Map data © OpenStreetMap contributors (ODbL). Dock data: Madison BCycle GBFS feed.
"""

FILES = ["Dockerfile", "requirements-server.txt", "backend/__init__.py", "backend/app.py",
         "backend/battery.py", "backend/graph.py", "frontend/index.html", "frontend/app.js",
         "frontend/style.css", "data/graph.npz", "data/edge_names.json"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="bike-router")
    args = parser.parse_args()

    missing = [f for f in FILES if not (ROOT / f).exists()]
    if missing:
        raise SystemExit(f"Missing {missing}; run scripts/build_graph.py first.")

    api = HfApi()
    repo_id = f"{api.whoami()['name']}/{args.name}"
    api.create_repo(repo_id, repo_type="space", space_sdk="docker", exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        for f in FILES:
            (Path(tmp) / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, Path(tmp) / f)
        (Path(tmp) / "README.md").write_text(SPACE_README)
        api.upload_folder(folder_path=tmp, repo_id=repo_id, repo_type="space",
                          commit_message="Deploy from bike-router")

    user, name = repo_id.split("/")
    print(f"Space: https://huggingface.co/spaces/{repo_id}")
    print(f"App:   https://{user}-{name}.hf.space  (first build takes a few minutes)")


if __name__ == "__main__":
    main()
