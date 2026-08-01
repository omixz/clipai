# Split-screen background library

This folder is where the bottom-half "satisfying" loop videos for the
split-screen feature live. It ships **empty** — the feature is fully wired
up in code, but every background shows as unavailable in the UI until you
drop the actual video files in here.

## What to add

Four files, matching the registry in `backgrounds.py`:

| File               | Shown as              |
|---------------------|------------------------|
| `parkour.mp4`        | Parkour / Free Running |
| `slime.mp4`           | Slime / ASMR           |
| `ball_pit.mp4`        | Ball Pit                |
| `soap_cutting.mp4`    | Soap Cutting            |

Want different ones, more, or fewer? Edit `STOCK_BACKGROUNDS` in
`backgrounds.py` (repo root) to match — the id/label/filename are all
defined there, nothing else needs to change.

## Requirements for each file

- **Seamlessly loopable.** `pipeline_lib.py` plays these on a hard loop
  (`ffmpeg -stream_loop -1`) to fill whatever length the top clip needs — a
  jump-cut at the loop point will be visible and look bad. 10-30 seconds of
  continuous, evenly-paced motion works best.
- **License you can actually ship.** CC0 / public domain is strongly
  preferred — this repo is public, so anything with an attribution
  requirement or a "non-commercial" clause is a real legal risk once a
  paying customer's video has it burned in permanently. Good places to check
  first: [Pexels Videos](https://www.pexels.com/videos/) and
  [Pixabay Videos](https://pixabay.com/videos/) (both offer a CC0-equivalent
  license for videos — read each individual clip's license page, it varies
  per contributor).
- **Reasonable size.** These get shipped in the Docker image and read on
  every split-screen render — a few MB to a few tens of MB each is plenty
  for a short vertical/square loop. There's no hard size enforcement here
  (unlike user uploads), so use judgment.
- **Any resolution/aspect ratio is fine.** `pipeline_lib.py`'s split-screen
  compositor scales and center-crops each half to fit, the same way the
  normal single-video render already handles arbitrary input resolutions.

## Verifying a file is picked up

Once a file is in place, restart the app and check `GET /api/backgrounds` —
its `available` field flips to `true` the moment the file exists at the
path `backgrounds.py` expects. No code change or redeploy needed beyond
adding the file itself (as long as the id is already registered).
