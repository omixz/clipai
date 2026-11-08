"""What to post: a curated bank of minimal two-line messages per motif, the
post captions, and (optionally) fresh lines written by Gemini — always run
through the same claims guard so the account never says something untrue.

Every line follows the minimal style: lowercase, short, one thought, ends
with a full stop, no emojis, no exclamation marks.
"""
from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATED = HERE / "state" / "generated_lines.json"

# Each motif tells one true thing about Peakcut. Keep claims in sync with the
# product (pricing.html, README); the guard below blocks the risky ones.
BANK: dict[str, list[list[str]]] = {
    "sentence": [
        ["most clippers cut here.", "we wait for the full stop."],
        ["mid-sentence is not a clip.", "a full thought is."],
        ["the cut is the whole craft.", "so we cut on full stops."],
        ["nobody likes being cut off.", "neither do your viewers."],
        ["a clip should finish its sentence.", "ours always do."],
        ["your guest was mid-thought.", "we let them finish."],
    ],
    "cut": [
        ["your video is an hour long.", "the clip is forty seconds."],
        ["you recorded for an hour.", "you only need the best minute."],
        ["the best part is in there.", "we cut straight to it."],
        ["long video in.", "short clip out."],
        ["somewhere in that podcast,", "there is a clip."],
        ["you don't need to scrub.", "we already found it."],
    ],
    "waveform": [
        ["most of it is quiet.", "we find the part that isn't."],
        ["an hour of talking.", "a few moments that land."],
        ["listen for the peak.", "cut around it."],
        ["the energy is in the audio.", "so that is where we look."],
        ["not every minute is a moment.", "we find the ones that are."],
    ],
    "split": [
        ["one long video.", "three clips, captioned."],
        ["one upload.", "a week of posts."],
        ["record once.", "post all week."],
        ["one episode.", "every platform."],
        ["horizontal in.", "vertical out."],
    ],
    "credits": [
        ["credits that expire.", "we just count videos."],
        ["no credits.", "no expiry dates."],
        ["minutes that run out.", "or videos you can count."],
        ["cancelling should be easy.", "ours is one click."],
        ["fine print is a feature.", "so we left it out."],
    ],
    "growth": [
        ["one video a week.", "seven posts a week."],
        ["record once.", "show up every day."],
        ["consistency compounds.", "clips make it effortless."],
        ["same recording.", "seven chances to be found."],
        ["you don't need more content.", "you need more clips of it."],
        ["the people who grow post daily.", "now you can too."],
    ],
    "computer": [
        ["your computer is idle tonight.", "let it make the clips."],
        ["you already own a gpu.", "put it to work."],
        ["upload from your phone.", "your pc does the rest."],
        ["your hardware.", "your clips."],
        ["the fans are quiet.", "they don't have to be."],
    ],
}

# The post text under each video. Plain, lowercase, one CTA, no hype.
CAPTIONS: dict[str, list[str]] = {
    "sentence": ["clips that end where the sentence ends.", "full sentences only. your viewers notice."],
    "cut": ["drop in a long video, get the clip back.", "the best minute of your podcast, found for you."],
    "waveform": ["peakcut listens for the moments that land.", "finds the peaks so you don't have to scrub."],
    "split": ["one long video in, captioned vertical clips out.", "record once, post everywhere."],
    "credits": ["no credits, no expiry, cancel in one click.", "a plain number of videos a month. that's it."],
    "computer": ["process clips on your own computer or gpu.", "pair your pc. upload from anywhere."],
    "growth": ["one long video a week becomes a week of daily posts.", "show up every day without recording every day."],
}
CTAS = ["5 free videos a month. link in bio.", "try it free, link in bio.", "free to try — link in bio."]

HASHTAGS = {
    "tiktok": "#contentcreator #podcastclips #creatortips #videoediting #minimal",
    "instagram": "#contentcreator #podcastclips #reels #videoediting #creatortools #minimaldesign #podcasting",
}

# ── Claims guard ─────────────────────────────────────────────────────────────
BANNED = [
    r"\bbest\b(?! part| minute| moments?)", r"#1", r"\bnumber one\b", r"\bguarantee", r"\bviral\b", r"\bmillions?\b",
    r"\bunlimited\b", r"\bforever\b", r"\bopus\b", r"\bklap\b", r"\bvizard\b", r"\bsubmagic\b", r"\bcapcut\b",
    r"\bscam\b", r"\bsecret\b", r"\bhack\b", r"\bai\b", r"\bfree\b", r"\binstantly\b", r"\bseconds?\b(?! to)",
    # No money/income promises: they're false for a clipping tool, illegal as
    # unsubstantiated earnings claims, and get-rich content is against TikTok
    # and Instagram rules (reach throttled, account at risk).
    r"\brich\b", r"\bmoney\b", r"\bincome\b", r"\bcash\b", r"\bearn", r"\bprofit", r"\bpassive\b",
    r"\bmillionaire", r"\bwealth", r"\bpaid\b", r"\bdollars?\b", r"\bsalary\b", r"\bquit your job\b",
]
_BANNED_RE = re.compile("|".join(BANNED), re.IGNORECASE)
ALLOWED_NUMBER_WORDS = ("forty seconds",)


def line_ok(line: str) -> tuple[bool, str]:
    if not isinstance(line, str) or not line:
        return False, "empty"
    if len(line) > 34:
        return False, "too long"
    if line != line.lower():
        return False, "not lowercase"
    if not re.fullmatch(r"[a-z0-9 ,.'’-]+", line):
        return False, "symbols/emoji"
    if not line.endswith((".", ",")):
        return False, "must end with a full stop"
    if re.search(r"\d", line):
        return False, "digits"
    hit = _BANNED_RE.search(line)
    if hit and not any(ok in line for ok in ALLOWED_NUMBER_WORDS):
        return False, f"banned word: {hit.group(0)}"
    return True, ""


def pair_ok(pair) -> bool:
    return isinstance(pair, list) and 1 <= len(pair) <= 2 and all(line_ok(l)[0] for l in pair)


# ── Optional: Gemini writes new lines when the bank is used up ───────────────

def load_generated() -> dict:
    try:
        data = json.loads(GENERATED.read_text())
        return {m: [p for p in pairs if pair_ok(p)] for m, pairs in data.items() if m in BANK}
    except Exception:
        return {}


def all_pairs(motif: str) -> list[list[str]]:
    return BANK[motif] + load_generated().get(motif, [])


MOTIF_BRIEF = {
    "sentence": "clips never cut someone off mid-sentence; they end on a full stop",
    "cut": "a long video goes in, the single best short moment comes out",
    "waveform": "it listens to the audio and finds the moments with energy",
    "split": "one long video becomes several captioned vertical clips",
    "credits": "no credits that expire, a plain number of videos per month, one-click cancel",
    "computer": "you can process clips on your own computer or gpu instead of the cloud",
    "growth": "one long video becomes a week of daily posts, so you can be consistent and show up every day",
}


def generate_lines(motif: str, n: int = 6, api_key: str | None = None) -> list[list[str]]:
    """Asks Gemini for new two-line pairs in the house style, keeps only the
    ones that pass the guard, and saves them for future runs."""
    api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API")
    if not api_key:
        return []
    import requests

    existing = all_pairs(motif)
    prompt = (
        "Write short two-line captions for a minimal black-and-white motion graphic advertising Peakcut, "
        f"an app that turns long videos into short clips. The idea to express: {MOTIF_BRIEF[motif]}.\n"
        "Rules: every line lowercase, at most 30 characters, ends with a full stop (line one may end with a comma), "
        "plain words, no emojis, no exclamation marks, no numbers or digits, no hype words (best, viral, secret, free, "
        "unlimited, ai), never name another company. Line one sets up, line two turns it. Quiet and confident.\n"
        f"Do not repeat these: {json.dumps(existing)}\n"
        f"Return only JSON: a list of {n} pairs, each a list of two strings."
    )
    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key}, timeout=60,
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"}},
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        pairs = json.loads(text)
    except Exception as e:  # noqa: BLE001 — generation is a nice-to-have
        print(f"[autopilot] gemini generation skipped: {e}")
        return []
    seen = {tuple(p) for p in existing}
    fresh = [p for p in pairs if pair_ok(p) and tuple(p) not in seen]
    if fresh:
        data = load_generated()
        data.setdefault(motif, []).extend(fresh)
        GENERATED.parent.mkdir(parents=True, exist_ok=True)
        GENERATED.write_text(json.dumps(data, indent=1))
    return fresh


# ── Picking the next post ────────────────────────────────────────────────────

def pick(ledger: list[dict], motif: str | None = None, seed: int | None = None, allow_generate: bool = True) -> dict:
    """Least-recently-used motif, then a line pair it hasn't posted yet (or the
    least recently used one), alternating dark and light every third post."""
    rng = random.Random(seed)
    last_used = {}
    for i, post in enumerate(ledger):
        last_used[post.get("motif")] = i
    if not motif:
        motifs = list(BANK)
        rng.shuffle(motifs)
        motif = min(motifs, key=lambda m: last_used.get(m, -1))
    used = {tuple(p.get("lines", [])): i for i, p in enumerate(ledger) if p.get("motif") == motif}
    pairs = all_pairs(motif)
    unused = [p for p in pairs if tuple(p) not in used]
    ai_written = False
    if not unused and allow_generate:
        fresh = generate_lines(motif)
        if fresh:
            unused, ai_written = fresh, True
    if unused:
        lines = unused[0] if not ai_written else rng.choice(unused)
        ai_written = ai_written or lines not in BANK[motif]
    else:
        lines = min(pairs, key=lambda p: used.get(tuple(p), -1))
        ai_written = lines not in BANK[motif]
    caption = rng.choice(CAPTIONS[motif])
    # gold for the growth story (the "momentum" look), otherwise mostly dark
    # with every third post light so the grid doesn't look repetitive
    theme = "gold" if motif == "growth" else ("light" if len(ledger) % 3 == 2 else "dark")
    return {
        "motif": motif,
        "lines": lines,
        "theme": theme,
        "light": theme == "light",
        "caption": caption,
        "cta": rng.choice(CTAS),
        "ai_written": ai_written,
    }


HASHTAGS_FILE = HERE / "state" / "hashtags.json"


def hashtags(platform: str) -> str:
    """Built-in tags, or your own current ones from state/hashtags.json
    ({"tiktok": "#a #b", "instagram": "#c"}) — edit that file any time
    a tag is trending in your niche; the next post picks it up."""
    try:
        custom = json.loads(HASHTAGS_FILE.read_text()).get(platform)
        if custom and isinstance(custom, str):
            return custom[:400]
    except Exception:
        pass
    return HASHTAGS[platform]


def post_text(choice: dict, platform: str) -> str:
    if platform == "instagram":
        # hashtags go in the first comment on Instagram (see publish.py)
        return f"{choice['caption']}\n\n{choice['cta']}"
    return f"{choice['caption']}\n\n{choice['cta']}\n\n{hashtags(platform)}"
