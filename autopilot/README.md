# Marketing autopilot: minimal reels for TikTok and Instagram

Twice a day, with nobody involved, GitHub Actions:

1. **Picks the message.** It takes the least-recently-used motif and a line pair it hasn't posted before, from a curated bank (`content.py`). When a motif's bank runs out, Gemini can write fresh lines. Every line, built-in or generated, has to pass a claims guard: lowercase, ≤34 characters, no hype words, no competitor names, no numbers.
2. **Renders a ~7.5-second 1080×1920 reel** in the minimal black-and-white style. The style is inspired by the [minimalistic-graphics](https://github.com/artemnovitckii/claude-animation-skills) skill: hairline strokes, one white dot, one quiet lowercase line. Every reel ends on **peakcut.**, where the dot glides in to become the full stop, with "link in bio" underneath.
   - Rendering is frame-exact: every scene is a pure function of time, stepped by Playwright and encoded by ffmpeg.
   - The soundtrack is a soft pad with a tick on each visual beat, synthesized in code, so there's no music licence to worry about.
3. **Hosts the video** on a rolling `marketing-media` GitHub Release of this public repo, then **posts it through Buffer** to TikTok and Instagram (as a Reel). This is the same route the YT-AUTO repo already uses successfully for TikTok.
4. **Records the post** in `state/ledger.json`, which is committed back, so it never repeats itself and a re-run can't double-post. If a run fails, it opens (or comments on) an issue labelled `autopilot`.

| Motif | The picture | The idea |
|---|---|---|
| `sentence` | dashes for words; a cut tries to land mid-sentence, shakes, fades; it lands on the full stop | clips end on full sentences |
| `cut` | a long timeline; two cut marks; the piece lifts into a 9:16 frame | long video in, best moment out |
| `waveform` | a waveform; a playhead scans; brackets snap around the loud part | it finds the moments with energy |
| `split` | one wide frame sliced into three vertical clips | one video, many posts |
| `credits` | a grid of credits quietly expiring; one steady dot with twelve month ticks | no credits, no expiry |
| `computer` | a monitor outline; the dot drops in; three clips appear | process on your own computer |
| `growth` (gold) | a compounding line climbs while a number counts 1 → 7 posts a week | one recording becomes daily posting |

## Sound: beat-synced music, and real trending sounds

- **Generated beats (default, fully automatic).** Each post gets an original backing track synthesized in code, rotating through **phonk → house → lo-fi**, the styles all over short video right now. The animation is warped to its exact beat grid, and the mix is normalized to TikTok/Instagram's -14 LUFS. Nothing can be flagged for copyright. Pin one style with the repo variable `AUTOPILOT_BEAT_STYLE=phonk|house|lofi`, or turn beats off with `AUTOPILOT_BEATS=0`.

- **Music library.** Put licensed tracks in `assets/music/` (see the README there for safe sources). Each post:
  - takes the next track in rotation;
  - detects its tempo and beats (to within about one frame);
  - uses the track's most energetic stretch;
  - time-warps the animation so every cut, hop and the final full stop lands on a beat.

  With no tracks, reels use a soft synthesized pad.
- **Trending sounds (TikTok).** No posting API can attach an in-app trending sound, and pasting a hit song into the file gets videos muted. Set the repo **variable** `TIKTOK_TREND_HANDOFF=1`, or tick `trend_handoff` on a manual run. TikTok posts then go out in Buffer's *notification* mode with a quiet version of the reel: Buffer pings your phone, you tap a trending sound and post. That's about 20 seconds a day. Instagram stays fully automatic.
- **Hashtags.** Put current tags in `state/hashtags.json` (`{"tiktok": "#a #b", "instagram": "#c #d"}`); the next post uses them. Instagram's hashtags go in the first comment, keeping the caption clean.

## On "make people feel rich"

The `growth` motif and gold theme give the aspirational, momentum feel: one recording becomes seven posts a week. The bot deliberately **won't claim people will make money**. The claims guard blocks words like rich, money, income, earn, passive and quit your job, for three reasons:
- Peakcut doesn't make anyone money, so the claim would be false.
- Unsubstantiated earnings claims are illegal advertising (FTC).
- TikTok and Instagram throttle or remove get-rich content, which kills reach rather than helping it go viral.

## Setup (once, about 10 minutes)

1. **Buffer.** Create a Buffer account and connect your **TikTok** and your **Instagram Business/Creator** account. Instagram personal accounts can't be auto-published. Then create an API key.
2. **Repo secret** `BUFFER_API_KEY` (Settings → Secrets and variables → Actions). That's all the bot needs: it finds your TikTok and Instagram channels on the account automatically.
   - Optional: `BUFFER_TIKTOK_CHANNEL_ID` / `BUFFER_INSTAGRAM_CHANNEL_ID`, only to pick specific channels when you have several.
   - Optional: `GEMINI_API_KEY`, a free key from aistudio.google.com. It's only used once the built-in lines run out.
3. **Prove it.** Run **Marketing setup check** with "probe" ticked. It saves a 3-second **draft** on each channel; nothing is published. Delete the drafts in Buffer afterwards.
4. **Preview.** Run **Marketing autopilot** with `dry_run` ticked and download the `reel-…` artifact (the mp4 plus a contact sheet with the platform UI areas marked).
5. **Set your bio link** on TikTok and Instagram to your Peakcut URL. Every video says "link in bio".

That's it. From then on it posts at about 11:17 and 19:43 US Eastern every day. You can change `cron` in `.github/workflows/marketing.yml`.

## Run it locally

```bash
npm install --prefix autopilot && npx --prefix autopilot playwright install chromium
python -m autopilot.run --dry-run                 # writes autopilot/out/<run>/reel.mp4 + sheet.jpg
python -m autopilot.run --dry-run --motif computer
```

## Changing what it says

- Add line pairs to `BANK` in `content.py`. Each pair is a setup and a turn, lowercase, ending with a full stop. `tests/test_autopilot.py` fails if a line breaks the guard.
- To add a motif, drop a `templates/scenes/<name>.js` that defines `SCENE_FACTORY` (copy an existing one), then add its lines and captions to `content.py`. The style rules are pure black or white, hairline strokes, one dot and one line of text. Keep the important parts away from the top ~11%, the bottom ~22% and the right-hand button column; the contact sheet marks those areas in red.

## Notes

- **AI labels.** TikTok's `isAiGenerated` is set only when Gemini wrote the lines. The motion graphics themselves are made by code.
- **Field names.** The Buffer field names were confirmed by introspecting the live schema in November 2026:
  - Instagram: `type: "reel"` and `shouldShareToFeed` (both required), plus `firstComment` and `isAiGenerated`.
  - TikTok: `title` and `isAiGenerated`.

  The setup check re-prints them, in case Buffer changes its schema.
- **Analytics.** Nothing feeds back into the bandit yet. Once there's a few weeks of posts, the obvious next step is to weight motifs by views, the way YT-AUTO's `sync_analytics.py` does for YouTube.
