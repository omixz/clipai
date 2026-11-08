"""Licensed stock footage from Pexels (free for commercial use, no
attribution required) for "footage:" scenes. Needs PEXELS_API_KEY; without
it, scenes fall back to their generated `alt`. Same API YT-AUTO uses."""
from __future__ import annotations

import os
from pathlib import Path

import requests

SEARCH = "https://api.pexels.com/videos/search"


def available() -> bool:
    return bool(os.environ.get("PEXELS_API_KEY"))


def find(query: str, used: set[int], out_dir: Path) -> tuple[Path, int] | None:
    """Downloads a portrait clip for `query` not used recently; returns
    (path, pexels_id) or None."""
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(SEARCH, headers={"Authorization": key}, timeout=30,
                         params={"query": query, "orientation": "portrait", "size": "medium", "per_page": 15})
        r.raise_for_status()
        for video in r.json().get("videos", []):
            if video["id"] in used or video.get("duration", 0) < 4:
                continue
            files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"
                     and f.get("height", 0) >= 1280 and f.get("width", 0) < f.get("height", 0)]
            if not files:
                continue
            best = min(files, key=lambda f: abs(f["height"] - 1920))
            dest = out_dir / f"pexels_{video['id']}.mp4"
            out_dir.mkdir(parents=True, exist_ok=True)
            with requests.get(best["link"], stream=True, timeout=120) as dl:
                dl.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in dl.iter_content(1 << 20):
                        f.write(chunk)
            return dest, video["id"]
    except Exception as e:  # noqa: BLE001 — footage is optional
        print(f"[autopilot] footage for {query!r} unavailable: {e}")
    return None
