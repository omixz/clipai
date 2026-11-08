"""Real footage and photos from Pexels — free for commercial use, no
attribution required (we still credit Pexels in the post, as their API
guidelines ask). Needs PEXELS_API_KEY; without it, scenes fall back to their
designed `alt`. Same API the YT-AUTO repo uses.

Why Pexels and not Pinterest/Google images: those images belong to their
photographers, scraping Pinterest is against its terms, and recognisable
people in an ad need their consent — exactly what gets commercial posts
taken down. Pexels images are licensed for this use.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

VIDEO_SEARCH = "https://api.pexels.com/videos/search"
PHOTO_SEARCH = "https://api.pexels.com/v1/search"


def available() -> bool:
    return bool(os.environ.get("PEXELS_API_KEY"))


def _get(url: str, params: dict) -> dict:
    r = requests.get(url, headers={"Authorization": os.environ["PEXELS_API_KEY"]}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120) as dl:
        dl.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in dl.iter_content(1 << 20):
                f.write(chunk)
    return dest


def find(query: str, used: set, out_dir: Path) -> tuple[Path, str] | None:
    """A portrait video clip for `query` not used recently -> (path, "v<id>")."""
    if not available():
        return None
    try:
        for video in _get(VIDEO_SEARCH, {"query": query, "orientation": "portrait", "size": "medium", "per_page": 15}).get("videos", []):
            key = f"v{video['id']}"
            if key in used or video["id"] in used or video.get("duration", 0) < 4:
                continue
            files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"
                     and f.get("height", 0) >= 1280 and f.get("width", 0) < f.get("height", 0)]
            if files:
                best = min(files, key=lambda f: abs(f["height"] - 1920))
                return _download(best["link"], out_dir / f"pexels_{video['id']}.mp4"), key
    except Exception as e:  # noqa: BLE001 — visuals are optional
        print(f"[autopilot] footage for {query!r} unavailable: {e}")
    return None


def find_photo(query: str, used: set, out_dir: Path) -> tuple[Path, str] | None:
    """A real photo for `query` not used recently -> (path, "p<id>")."""
    if not available():
        return None
    try:
        for photo in _get(PHOTO_SEARCH, {"query": query, "orientation": "portrait", "size": "large", "per_page": 15}).get("photos", []):
            key = f"p{photo['id']}"
            if key in used or photo.get("height", 0) < 1600:
                continue
            src = photo["src"]["original"] + "?auto=compress&cs=tinysrgb&h=2400"
            return _download(src, out_dir / f"pexels_photo_{photo['id']}.jpg"), key
    except Exception as e:  # noqa: BLE001
        print(f"[autopilot] photo for {query!r} unavailable: {e}")
    return None


def find_any(query: str, used: set, out_dir: Path, prefer: str = "video") -> tuple[Path, str] | None:
    order = (find, find_photo) if prefer == "video" else (find_photo, find)
    for fn in order:
        got = fn(query, used, out_dir)
        if got:
            return got
    return None
