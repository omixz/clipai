#!/usr/bin/env python3
"""Marketing autopilot: make one minimal reel and post it to TikTok and
Instagram. Built to run unattended from GitHub Actions (marketing.yml).

    python -m autopilot.run --dry-run            # render only, no posting
    python -m autopilot.run --motif computer     # force a motif
    python -m autopilot.run                      # render + host + post

Each run: pick the next message (no repeats; least-recently-used motif) ->
render 1080x1920 30fps -> host -> post to each platform independently ->
record it in autopilot/state/ledger.json (the workflow commits that back).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

from . import content, video

HERE = Path(__file__).resolve().parent
LEDGER = HERE / "state" / "ledger.json"
PLATFORMS = ("tiktok", "instagram")
MIN_GAP_HOURS = 3  # a re-triggered run won't double-post


def load_ledger() -> list[dict]:
    try:
        return json.loads(LEDGER.read_text()).get("posts", [])
    except Exception:
        return []


def save_ledger(posts: list[dict]):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps({"posts": posts[-500:]}, indent=1))


def summary(md: str):
    print(md)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--motif", choices=video.MOTIFS)
    ap.add_argument("--platforms", default=",".join(PLATFORMS))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--force", action="store_true", help="post even if the last post was under 3h ago")
    ap.add_argument("--draft", action="store_true", help="save as Buffer drafts instead of publishing")
    args = ap.parse_args(argv)

    platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    ledger = load_ledger()
    now = dt.datetime.now(dt.timezone.utc)
    if ledger and not (args.force or args.dry_run):
        last = dt.datetime.fromisoformat(ledger[-1]["at"])
        if (now - last).total_seconds() < MIN_GAP_HOURS * 3600:
            summary(f"⏭️ Skipped: last post was {(now - last).seconds // 60} min ago.")
            return 0

    choice = content.pick(ledger, motif=args.motif, seed=int(now.timestamp()) // 3600,
                          allow_generate=not args.dry_run)
    run_id = now.strftime("%Y%m%d-%H%M") + f"-{choice['motif']}"
    out = Path(args.out) / run_id
    mp4 = out / "reel.mp4"
    t0 = time.time()
    video.render({"motif": choice["motif"], "lines": choice["lines"], "light": choice["light"], "cta": "link in bio"},
                 mp4, out / "work")
    video.contact_sheet(mp4, out / "sheet.jpg")
    summary(f"## 🎬 {choice['motif']} — “{' / '.join(choice['lines'])}”\n"
            f"rendered in {time.time() - t0:.0f}s · {'light' if choice['light'] else 'dark'}"
            f"{' · lines written by Gemini' if choice['ai_written'] else ''}\n")

    if args.dry_run:
        summary(f"Dry run — not posted. Video: `{mp4}`")
        return 0

    from . import publish
    record = {"at": now.isoformat(timespec="seconds"), "id": run_id, **{k: choice[k] for k in ("motif", "lines", "light", "caption", "ai_written")},
              "platforms": {}}
    try:
        url = publish.host_on_github_release(mp4, f"{run_id}.mp4")
        record["video_url"] = url
    except Exception as e:  # noqa: BLE001
        summary(f"❌ Hosting failed: {e}")
        return 1

    ok = 0
    for platform in platforms:
        try:
            post_id = publish.post_video(platform, url, content.post_text(choice, platform), title=choice["caption"],
                                         ai_generated=choice["ai_written"], draft=args.draft)
            record["platforms"][platform] = {"ok": True, "post_id": post_id}
            summary(f"- ✅ {platform}: Buffer post `{post_id}`")
            ok += 1
        except Exception as e:  # noqa: BLE001 — one platform failing must not block the other
            record["platforms"][platform] = {"ok": False, "error": str(e)[:500]}
            summary(f"- ❌ {platform}: {e}")
    if ok:
        ledger.append(record)
        save_ledger(ledger)
    return 0 if ok == len(platforms) else (2 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
