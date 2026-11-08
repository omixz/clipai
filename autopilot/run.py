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

from . import content, music, video

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
    ap.add_argument("--format", choices=["editorial", "minimal"], default=os.environ.get("AUTOPILOT_FORMAT") or "editorial",
                    help="editorial = narrated kinetic-type essay (default); minimal = line-art reel")
    ap.add_argument("--script", help="editorial: force a script id")
    ap.add_argument("--platforms", default=",".join(PLATFORMS))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--force", action="store_true", help="post even if the last post was under 3h ago")
    ap.add_argument("--draft", action="store_true", help="save as Buffer drafts instead of publishing")
    ap.add_argument("--trend-handoff", action="store_true", default=os.environ.get("TIKTOK_TREND_HANDOFF") == "1",
                    help="TikTok: Buffer pings your phone so you can add a trending sound before posting")
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
    track = music.pick_track([p.get("track") for p in ledger if p.get("track")])
    if not track and os.environ.get("AUTOPILOT_BEATS", "1") != "0":
        # no tracks of your own: generate an original beat in a trending style
        from . import beats
        style = os.environ.get("AUTOPILOT_BEAT_STYLE") or beats.pick_style(
            [p["track"].split("-")[1] for p in ledger if str(p.get("track", "")).startswith("generated-")])
        if style not in beats.STYLES:
            style = beats.STYLES[0]
        gen = beats.generate(style, 24, out / "work" / f"generated-{style}.wav", seed=int(now.timestamp()))
        track = Path(gen["path"])
    meta = None
    if args.format == "editorial" and not args.motif:
        from . import editorial, scripts
        script = next((s for s in scripts.all_scripts() if s["id"] == args.script), None) or scripts.pick(ledger)
        try:
            used = {fid for p in ledger[-40:] for fid in (p.get("footage_ids") or [])}
            meta = editorial.render(script, mp4, out / "work", music_track=track, used_footage=used)
            choice = {"motif": "editorial", "script": script["id"], "lines": [script["title"]], "theme": "editorial",
                      "caption": script["title"], "cta": choice["cta"], "ai_written": bool(script.get("ai")),
                      "credit": "visuals: pexels" if meta.get("footage_ids") else ""}
            summary(f"## 🎬 editorial — “{script['title']}”\n"
                    f"rendered in {time.time() - t0:.0f}s · {meta['duration']:.1f}s · voice {meta['voice']} · "
                    f"{len(meta['footage_ids'])} stock clips · 🎵 {(meta.get('track') or 'no bed').removesuffix('.wav')}\n")
        except Exception as e:  # noqa: BLE001 — never skip a post: fall back to the minimal reel
            summary(f"⚠️ editorial render failed ({str(e)[:200]}); posting a minimal reel instead.")
            meta = None
    if meta is None:
        meta = video.render({"motif": choice["motif"], "lines": choice["lines"], "theme": choice.get("theme"), "cta": "link in bio"},
                            mp4, out / "work", music_track=track, quiet_variant=args.trend_handoff and "tiktok" in platforms)
        sound = (f"🎵 {meta['track'].removesuffix('.wav')} ({meta['bpm']:.0f} bpm, beat-synced)" if meta.get("track")
                 else "🎵 soft synth pad")
        summary(f"## 🎬 {choice['motif']} — “{' / '.join(choice['lines'])}”\n"
                f"rendered in {time.time() - t0:.0f}s · {choice.get('theme', 'dark')} · {sound}"
                f"{' · lines written by Gemini' if choice['ai_written'] else ''}\n")
    video.contact_sheet(mp4, out / "sheet.jpg")

    if args.dry_run:
        summary(f"Dry run — not posted. Video: `{mp4}`")
        return 0

    from . import publish
    record = {"at": now.isoformat(timespec="seconds"), "id": run_id,
              **{k: choice.get(k) for k in ("motif", "script", "lines", "theme", "caption", "ai_written")},
              "track": (meta.get("track") or "").removesuffix(".wav") or None,
              "voice": meta.get("voice"), "footage_ids": meta.get("footage_ids"), "platforms": {}}
    try:
        url = publish.host_on_github_release(mp4, f"{run_id}.mp4")
        record["video_url"] = url
        quiet_url = None
        if meta.get("quiet_path"):
            quiet_url = publish.host_on_github_release(Path(meta["quiet_path"]), f"{run_id}.quiet.mp4")
    except Exception as e:  # noqa: BLE001
        summary(f"❌ Hosting failed: {e}")
        return 1

    ok = 0
    for platform in platforms:
        try:
            handoff = platform == "tiktok" and args.trend_handoff
            post_id = publish.post_video(
                platform, (quiet_url or url) if handoff else url, content.post_text(choice, platform),
                title=choice["caption"], ai_generated=choice["ai_written"], draft=args.draft, handoff=handoff,
                first_comment=content.hashtags("instagram") if platform == "instagram" else "")
            record["platforms"][platform] = {"ok": True, "post_id": post_id, "handoff": handoff}
            summary(f"- ✅ {platform}: Buffer post `{post_id}`"
                    + (" — check your phone: add a trending sound and publish" if handoff else ""))
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
