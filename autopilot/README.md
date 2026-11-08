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

## Setup (once, about 10 minutes)

1. **Buffer.** Create a Buffer account and connect your **TikTok** and your **Instagram Business/Creator** account. Instagram personal accounts can't be auto-published. Then create an API key.
2. **Repo secrets** (Settings → Secrets and variables → Actions):
   - `BUFFER_API_KEY`
   - `BUFFER_TIKTOK_CHANNEL_ID` and `BUFFER_INSTAGRAM_CHANNEL_ID`. Run **Actions → Marketing setup check** to list your channel ids.
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
- **Field names.** Buffer's TikTok fields were confirmed in YT-AUTO. The Instagram field names (`type: "reel"`, `shouldShareToFeed`) are best-effort. `publish.py` retries without them if Buffer rejects them, and the setup check prints the exact fields your account accepts.
- **Analytics.** Nothing feeds back into the bandit yet. Once there's a few weeks of posts, the obvious next step is to weight motifs by views, the way YT-AUTO's `sync_analytics.py` does for YouTube.
