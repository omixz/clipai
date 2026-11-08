# Music for the reels

Drop audio files here (`.mp3`, `.m4a`, `.wav`, `.ogg`, `.flac`) and commit them. Each run:
- picks the least recently used track;
- detects its tempo and beats;
- uses the most energetic stretch;
- time-warps the animation so the cuts, hops and the final full stop land on the beat.

With no tracks here, reels use the quiet synthesized pad.

## Only use music you're allowed to post

Uploading a popular song inside the video file isn't "using a trending sound". It's re-uploading copyrighted music. TikTok and Instagram mute or remove those videos, and can restrict the account. Business accounts are limited to TikTok's Commercial Music Library. So use tracks whose licence covers social media:

- **Pixabay Music** — free, licence allows use in social videos.
- **YouTube Audio Library** — check each track's licence; some need attribution.
- **Uppbeat**, **Epidemic Sound**, **Artlist** — subscription libraries cleared for TikTok/Instagram.

## Real trending sounds

Neither TikTok's nor Instagram's posting API (which Buffer uses) can attach an in-app sound. For the actual trending audio, turn on **trend hand-off** (`TIKTOK_TREND_HANDOFF=1`, see `autopilot/README.md`). Buffer then sends the TikTok post to your phone with the video and caption ready, you tap a trending sound and publish. That takes about 20 seconds per post. Instagram stays fully automatic.
