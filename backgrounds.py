"""Stock background library for the split-screen (Pro/Pro Plus) feature —
the "top half is the talking-head clip, bottom half is a looping satisfying/
gameplay video" format. See assets/backgrounds/README.md for what files need
to be dropped in before this is actually usable.

Deliberately a small, hand-maintained allowlist rather than reading whatever
happens to be sitting in the directory: background_id comes straight from
user input on /process, and resolving it through a fixed dict (instead of,
say, treating it as a filename) means it can never be anything other than
one of these exact keys — no path-traversal surface at all, unlike a scheme
where the id doubled as a filename.
"""
import logging
import os

log = logging.getLogger("clipai.backgrounds")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKGROUNDS_DIR = os.environ.get("BACKGROUNDS_DIR", os.path.join(BASE_DIR, "assets", "backgrounds"))

# id -> {label shown in the UI, filename inside BACKGROUNDS_DIR}
# Add entries here once you've dropped the matching file in
# assets/backgrounds/ — see that folder's README.md for format/license
# requirements. Nothing breaks if a file is missing yet: get_background_path
# reports it as unavailable rather than crashing a render.
STOCK_BACKGROUNDS = {
    "parkour": {"label": "Parkour / Free Running", "file": "parkour.mp4"},
    "slime": {"label": "Slime / ASMR", "file": "slime.mp4"},
    "ball_pit": {"label": "Ball Pit", "file": "ball_pit.mp4"},
    "soap_cutting": {"label": "Soap Cutting", "file": "soap_cutting.mp4"},
}


def list_backgrounds() -> list[dict]:
    """For the frontend: every registered background and whether its file
    actually exists yet, so the UI can grey out ones that aren't ready
    instead of letting a user pick something that'll fail at render time."""
    out = []
    for bg_id, info in STOCK_BACKGROUNDS.items():
        path = os.path.join(BACKGROUNDS_DIR, info["file"])
        out.append({"id": bg_id, "label": info["label"], "available": os.path.isfile(path)})
    return out


def get_background_path(background_id: str) -> str | None:
    """Returns the resolved file path for a registered, present background,
    or None if the id isn't registered or the file hasn't been added yet.
    Never raises on a bad id -- callers treat None as "not available"."""
    info = STOCK_BACKGROUNDS.get(background_id)
    if not info:
        return None
    path = os.path.join(BACKGROUNDS_DIR, info["file"])
    if not os.path.isfile(path):
        log.warning("background '%s' is registered but %s doesn't exist yet", background_id, path)
        return None
    return path
