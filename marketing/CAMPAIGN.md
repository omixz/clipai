# Peakcut launch campaign: "Clips that don't cut you off"

> **Automated TikTok + Instagram posting is built.** `autopilot/` makes a minimal black-and-white reel and posts it twice a day via Buffer, entirely from GitHub Actions. Setup: [`autopilot/README.md`](../autopilot/README.md). The manual channels below (Product Hunt, Reddit, DMs) are still worth doing once at launch; the autopilot keeps the TikTok/Instagram presence going afterwards.

A four-week, mostly $0 campaign. It's built on what people complain about most in reviews of the established AI clippers, and every claim below matches something the product does today.

---

## 1. What people complain about (research, Nov 2026)

Across Trustpilot, G2, Capterra and review blogs covering OpusClip, Klap and Vizard, the same complaints keep coming back:

| Complaint | How often it comes up | Peakcut's answer (shipped) |
|---|---|---|
| Clips start or end mid-sentence, "awkward starts and endings" | One aggregator counted it in 142 of 1k+ OpusClip reviews | Clips only start and end on sentence boundaries or pauses, with padding on each cut |
| Charged after cancelling, can't find how to cancel, unexpected renewals | The most repeated billing theme on Trustpilot | One-click cancel through Stripe's portal in Settings. If you were charged by mistake, a human fixes it within 7 days |
| Credits expire, or unused credits are forfeited on cancel | Common in G2 and Trustpilot reviews | No credits at all: a plain number of videos per month, and failed videos don't count |
| AI picks boring moments | The top complaint for Klap | Scoring for hooks and full sentences, plus a choice of clip length (short, medium, long) |
| Vertical crop cuts people out of frame | Common for two-person podcasts | Three framing modes, including "show everything + blurred fill" |
| Slow, stuck or failed projects | Several Trustpilot reviews | Live stage and queue position, an email when the clips are ready, automatic refunds on failure |
| Free tier felt misleading, clips vanish | Reddit/blog summaries | 5 free videos a month, stated up front. A 24-hour expiry notice sits on every result |

Sources: [eesel.ai OpusClip reviews](https://www.eesel.ai/blog/opusclip-reviews), [Tooliverse OpusClip review](https://tooliverse.ai/tools/opusclip), [BlitzCut ratings roundup](https://blitzcutai.com/blog/best-ai-clipping-tools), [unkoa OpusClip review](https://www.unkoa.com/opus-clip-in-2025-the-smartest-way-to-turn-long-videos-into-viral-shorts/), [Trustpilot – opus.pro](https://www.trustpilot.com/review/opus.pro?page=7), [G2 – OpusClip](https://www.g2.com/products/opusclip), [toolsforhumans – Klap](https://www.toolsforhumans.ai/ai-tools/klap), [Capterra – Vizard](https://www.capterra.com/p/10009818/Vizard/reviews/).

> **Rule for every piece of copy:** talk about the problems, not the competitors. Write "other clippers cut you off mid-sentence", never "OpusClip is a scam". Every claim has to stay true, so update this file if a feature changes.

---

## 2. Positioning

**One-liner:** Peakcut turns long videos into short clips that end where the sentence ends.

**Tagline options (A/B test these on the homepage `<h1>` and in ads):**
1. "Clips that don't cut you off."
2. "The AI clipper with no credits, no tricks, no mid-sentence cuts."
3. "Long video in. Postable clips out. Cancel in one click."

**Who we target first:** each of these groups has lots of long-form talking content and little editing budget.
1. **Small podcasters (under 1k listeners).** They record weekly and know they should post clips but never get round to it.
2. **Churches and pastors.** Weekly sermons are a big, underserved niche, and "sermon clips" is a real search term.
3. **Small Twitch and YouTube streamers.** VODs full of moments, no editor.
4. **Coaches, educators and course creators.** Webinars and lectures.

**Proof points to repeat everywhere:** 5 free videos/month, no card · full-sentence cuts · word-timed captions plus an editable .srt · fill/blur/letterbox framing · one-click cancel · failed videos never count.

---

## 3. The offer

- **Launch code `LAUNCH50`:** 50% off Pro for 3 months. Create it in Stripe → Products → Coupons → Promotion codes. Checkout already accepts promo codes (`allow_promotion_codes=True`).
- **Creator seeding:** a free month of Pro Plus for 30 hand-picked small creators, in exchange for honest feedback. No posting is required, and nobody gets paid for reviews.
- **Built-in viral loop:** every free clip carries "Made with Peakcut". Each free user who posts is a small ad.

---

## 4. Four-week plan

### Week 0: prep (2–3 days)
- [ ] Set real `CONTACT_EMAIL`, `SITE_URL`, Stripe prices and Google sign-in in production.
- [ ] Create the `LAUNCH50` promo code.
- [ ] Record a 60-second screen capture: drop in a podcast, then show the clips, the sentence-boundary cut and the blurred-fill framing. It becomes the hero asset for every channel.
- [ ] Make 10 sample clips from your own or Creative Commons talks and keep them as the demo reel. Never clip other creators' content without permission.
- [ ] Set up analytics on signups → first video → first download → upgrade (Plausible or Umami are free to self-host).

### Week 1: seed communities (organic)
- [ ] Post one genuinely useful thread per community. Read each community's self-promotion rules first, and if a community bans self-promotion, don't post there:
  r/podcasting, r/NewTubers, r/Twitch, r/SmallYTChannel, r/churchtech, r/content_marketing, r/SideProject, r/IndieHackers.
- [ ] Send 30 personal DMs to small podcasters, pastors and streamers offering the free Pro Plus month (template in §5.5).
- [ ] Post the demo video natively on TikTok, Reels, Shorts and LinkedIn.

### Week 2: Product Hunt + build-in-public
- [ ] Launch on Product Hunt on a Tuesday, Wednesday or Thursday at 12:01am PT. Copy is in §5.2.
- [ ] Post the X/Twitter thread (§5.3) on launch morning.
- [ ] Reply to every comment within an hour on launch day.
- [ ] Email anyone who opted in to updates.

### Week 3: SEO + comparison content
Write honest, useful pages. Each one targets a search phrase people type when they're unhappy with what they use now:
- [ ] "Free AI clip maker that doesn't cut mid-sentence"
- [ ] "How to turn a sermon into Reels in 5 minutes" (church niche)
- [ ] "OpusClip alternatives without credits": a fair comparison that also lists where competitors are better (more features, more languages, auto-posting).
- [ ] "Podcast to TikTok: the free workflow"
- [ ] Submit the sitemap (`/sitemap.xml`, now served by the app) to Google Search Console.

### Week 4: double down on what worked
- [ ] Look at which channel brought users who processed a video, not just visitors.
- [ ] Turn the best seeded-creator feedback into testimonials, with written permission.
- [ ] Optional paid test: $100–200 on TikTok Spark Ads boosting the best organic clip, targeted at podcasters and streamers.
- [ ] Email free users who hit their limit (template in §5.6).

---

## 5. Ready-to-use copy

### 5.1 Homepage / ad headlines
- "Clips that don't cut you off."
- "Your podcast has 10 great clips in it. Peakcut finds them in 3 minutes."
- "No credits. No expiry. No mid-sentence cuts. 5 free videos a month."

### 5.2 Product Hunt
**Name:** Peakcut
**Tagline (≤60 chars):** AI clips that end where the sentence ends
**Description:**
> Drop in a podcast, stream or talk and Peakcut gives you the best moments as captioned vertical clips. It only cuts on full sentences, lets you choose crop or "show everything" framing so nobody gets cut out of frame, and gives you an editable .srt for every clip. 5 free videos a month, no card. Paid plans have no credits that expire, and you can cancel in one click.

**First comment (maker):**
> Hey PH 👋 I built Peakcut after reading hundreds of reviews of AI clippers. Three complaints kept coming up: clips that cut people off mid-sentence, credits that expire, and subscriptions that are hard to cancel. So Peakcut only cuts on sentence boundaries, has no credits (just videos per month, and failed ones don't count), and cancelling is one click in Settings. It's free to try with 5 videos a month. I'd love brutal feedback on which clips it picks. What's a moment you'd have chosen instead?

### 5.3 X/Twitter thread
1. I read a lot of reviews of AI video clippers. The same 3 complaints show up over and over. So I built one that fixes them. 🧵
2. Complaint #1: "it cut me off mid-sentence." Peakcut only starts and ends clips on sentence boundaries or natural pauses, and pads every cut so no word gets chopped. [GIF: before/after]
3. Complaint #2: "my credits expired." There are no credits. You get a number of videos per month, and if one fails it doesn't count.
4. Complaint #3: "I couldn't cancel." Settings → Manage billing → Cancel. That's the whole flow.
5. Bonus: "show everything" framing so two-person podcasts don't get cropped, and an .srt file with every clip so you can fix captions.
6. 5 free videos a month, no card: [link]. Tell me which clip it picked that you'd change.

### 5.4 Reddit (value-first; adapt to each sub's rules)
**Title:** I analysed the most common complaints about AI clip tools. Here's a checklist before you pay for one
**Body:** Share the complaint table from §1 as a buyer's checklist (does it cut mid-sentence? do credits expire? can you cancel without emailing anyone? can you get the captions file?). At the end, in one line: "Disclosure: I built a free one called Peakcut that tries to pass this checklist. Happy to get roasted."

### 5.5 Creator DM / email
> Hi {name}, I really liked your episode on {topic}, especially the bit about {specific moment}. I make a small tool that turns full episodes into captioned vertical clips that end on full sentences. Would a free month of the top plan be useful to you? No strings and no need to post about it. I'd just love to know which clips it got wrong. — {you}

### 5.6 Lifecycle emails
- **Limit reached (free):** Subject: "You made 5 videos' worth of clips this month 🎬". Body: what they made, when the allowance resets, and "Pro is $15/mo — 20 videos, no watermark, 5 clips each. Use LAUNCH50 for 50% off 3 months. Cancel in one click."
- **Clips ready but not downloaded after 12h:** "Your clips are deleted in 12 hours. Grab them here."
- **Churned Pro:** "Sorry to see you go. What should we fix?" One question, plain text, no discount wall.

### 5.7 Short-form video scripts (post natively, vertical)
1. **"POV: the AI clipper cut you off mid-sen—"** (hard cut). Then: "Peakcut ends clips where the sentence ends," followed by a split-screen demo.
2. **"I gave an AI my 1-hour podcast. Here's what it picked."** Show the top 3 clips and their virality scores, and ask viewers if they agree.
3. **"Pastors: your sermon has 5 Reels in it."** Show a 30-second walkthrough with the blurred-fill framing.

---

## 6. Metrics and targets (first 30 days)

| Metric | Why it matters | Target |
|---|---|---|
| Visitors → signed-in | Is the landing page message working? | 25% |
| Signed-in → first video processed | Activation | 60% |
| First video → a clip downloaded | Did it deliver value? | 70% |
| Free → paid | Revenue | 2–4% |
| Free users hitting their limit | Demand signal for Pro | Track weekly |
| Refund / chargeback rate | Trust (our whole positioning) | < 1% |

These are starting targets to measure against, not benchmarks. Revise them after week 2.

---

## 7. Guardrails
- Only clip content you have rights to for demos.
- Never buy reviews or upvotes, and don't run sockpuppet accounts. Product Hunt and Reddit both ban them, and the whole brand is "no tricks".
- Disclose that you're the maker in every community post.
- Keep pricing claims identical across `pricing.html`, the FAQ, emails and this file.
