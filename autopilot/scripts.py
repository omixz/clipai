"""Narrated "editorial" reels: short voiceover essays with big kinetic type.

Each script is a list of beats. A beat is one screen: the spoken line
(`say`), which words in it are the big keyword (`key`), how the keyword is
set (`style`: bold | italic | serif), and what's behind it (`scene`).

Scenes: "footage:<pexels search>" (licensed stock footage, needs
PEXELS_API_KEY; falls back to `alt`), or a generated scene — "dark", "grid",
"clips", "wave", "timeline", "pc", "count".

Every claim here must be true of the product today.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATED = HERE / "state" / "generated_scripts.json"

SCRIPTS: list[dict] = [
    {"id": "whole-podcast", "title": "nobody watches your whole podcast.", "beats": [
        {"say": "Nobody watches your whole podcast.", "key": "whole podcast", "style": "italic", "scene": "footage:podcast microphone studio", "alt": "dark"},
        {"say": "They watch forty seconds of it.", "key": "forty seconds", "style": "bold", "scene": "footage:person scrolling phone at night", "alt": "clips"},
        {"say": "Finding those seconds takes hours.", "key": "hours", "style": "serif", "scene": "timeline"},
        {"say": "Peakcut listens for the moments that land,", "key": "land", "style": "italic", "scene": "wave"},
        {"say": "and cuts them on full sentences.", "key": "full sentences", "style": "bold", "scene": "grid"},
        {"say": "One long video in. A week of clips out.", "key": "A week", "style": "italic", "scene": "count"},
    ]},
    {"id": "consistency", "title": "the most consistent creators win.", "beats": [
        {"say": "The creators who win aren't the most talented.", "key": "talented", "style": "serif", "scene": "footage:creator filming herself camera", "alt": "dark"},
        {"say": "They're the most consistent.", "key": "consistent", "style": "italic", "scene": "footage:content creator ring light", "alt": "grid"},
        {"say": "But nobody can record seven times a week.", "key": "seven times", "style": "bold", "scene": "count"},
        {"say": "So record once.", "key": "once", "style": "italic", "scene": "footage:podcast recording two people", "alt": "dark"},
        {"say": "Peakcut turns that one episode into a week of clips.", "key": "a week of clips", "style": "bold", "scene": "clips"},
    ]},
    {"id": "mid-sentence", "title": "the clip that ends mid-sentence.", "beats": [
        {"say": "You've seen the clip that ends mid sentence.", "key": "mid sentence", "style": "italic", "scene": "footage:man looking at phone confused", "alt": "dark"},
        {"say": "It feels cheap.", "key": "cheap", "style": "serif", "scene": "grid"},
        {"say": "Most auto clippers cut wherever the timer ends.", "key": "timer", "style": "bold", "scene": "timeline"},
        {"say": "Peakcut waits for the full stop.", "key": "full stop", "style": "italic", "scene": "wave"},
        {"say": "Every clip ends on a finished thought.", "key": "finished thought", "style": "bold", "scene": "clips"},
    ]},
    {"id": "first-second", "title": "the first second decides everything.", "beats": [
        {"say": "The first second decides everything.", "key": "first second", "style": "italic", "scene": "footage:city night timelapse", "alt": "dark"},
        {"say": "So Peakcut scores every moment for its hook,", "key": "hook", "style": "bold", "scene": "wave"},
        {"say": "its energy,", "key": "energy", "style": "italic", "scene": "wave"},
        {"say": "and its pace.", "key": "pace", "style": "serif", "scene": "timeline"},
        {"say": "Then it hands you the strongest clips, captioned.", "key": "strongest", "style": "bold", "scene": "clips"},
    ]},
    {"id": "gpu", "title": "your computer sleeps every night.", "beats": [
        {"say": "Your computer sleeps every night.", "key": "sleeps", "style": "italic", "scene": "footage:gaming pc dark room", "alt": "pc"},
        {"say": "It could be making your clips.", "key": "your clips", "style": "bold", "scene": "pc"},
        {"say": "Pair it with Peakcut.", "key": "Pair it", "style": "serif", "scene": "pc"},
        {"say": "Upload from your phone,", "key": "your phone", "style": "italic", "scene": "footage:hand holding phone street", "alt": "clips"},
        {"say": "and your own hardware does the work.", "key": "own hardware", "style": "bold", "scene": "pc"},
    ]},
    {"id": "credits", "title": "credits are a tax on creators.", "beats": [
        {"say": "Most tools sell you credits.", "key": "credits", "style": "italic", "scene": "grid"},
        {"say": "Credits expire.", "key": "expire", "style": "serif", "scene": "footage:hourglass sand", "alt": "dark"},
        {"say": "Credits run out halfway through a project.", "key": "run out", "style": "bold", "scene": "count"},
        {"say": "Peakcut just counts videos.", "key": "counts videos", "style": "italic", "scene": "clips"},
        {"say": "And cancelling takes one click.", "key": "one click", "style": "bold", "scene": "grid"},
    ]},
    {"id": "attention-myth", "title": "attention spans aren't short.", "beats": [
        {"say": "People don't have short attention spans.", "key": "short", "style": "italic", "scene": "footage:crowd walking city", "alt": "dark"},
        {"say": "They have standards.", "key": "standards", "style": "serif", "scene": "grid"},
        {"say": "A good clip earns the next second.", "key": "next second", "style": "bold", "scene": "wave"},
        {"say": "Peakcut finds the parts of your video that earn it.", "key": "earn it", "style": "italic", "scene": "clips"},
    ]},
    {"id": "captions", "title": "most people watch on mute.", "beats": [
        {"say": "Most people scroll with the sound off.", "key": "sound off", "style": "italic", "scene": "footage:woman on train with phone", "alt": "dark"},
        {"say": "No captions, no viewers.", "key": "no viewers", "style": "bold", "scene": "grid"},
        {"say": "Peakcut burns in captions timed to every word,", "key": "every word", "style": "serif", "scene": "clips"},
        {"say": "and hands you the caption file to edit.", "key": "caption file", "style": "italic", "scene": "timeline"},
    ]},
    {"id": "framing", "title": "stop cropping your guests out.", "beats": [
        {"say": "Vertical crops cut your guest out of frame.", "key": "out of frame", "style": "italic", "scene": "footage:two people podcast interview", "alt": "dark"},
        {"say": "So Peakcut gives you three ways to frame it.", "key": "three ways", "style": "bold", "scene": "clips"},
        {"say": "Fill the screen.", "key": "Fill", "style": "serif", "scene": "grid"},
        {"say": "Show everything.", "key": "everything", "style": "italic", "scene": "grid"},
        {"say": "Or keep the bars.", "key": "bars", "style": "bold", "scene": "grid"},
    ]},
    {"id": "editor", "title": "you don't need an editor for this.", "beats": [
        {"say": "Clipping a podcast used to need an editor.", "key": "an editor", "style": "italic", "scene": "footage:video editor working at night", "alt": "timeline"},
        {"say": "Scrubbing. Cutting. Captioning. Cropping.", "key": "Cropping", "style": "bold", "scene": "timeline"},
        {"say": "Now you drop in the video,", "key": "drop in", "style": "serif", "scene": "pc"},
        {"say": "and get back clips that are ready to post.", "key": "ready to post", "style": "italic", "scene": "clips"},
    ]},
]

STYLES = ("bold", "italic", "serif")
SCENES = ("dark", "grid", "clips", "wave", "timeline", "pc", "count")

BANNED = re.compile(
    r"\b(rich|money|income|cash|earn(?:ing|ings)?\s+(?:money|income)|profit|passive income|millionaire|wealth|"
    r"guarantee\w*|viral guaranteed|opus|klap|vizard|submagic|capcut|scam|#1|number one|unlimited)\b", re.I)


def script_problems(s: dict) -> list[str]:
    """Structural + claims checks; empty list means the script is usable."""
    probs = []
    beats = s.get("beats") or []
    if not 3 <= len(beats) <= 8:
        probs.append("needs 3-8 beats")
    words = 0
    for i, b in enumerate(beats):
        say, key = b.get("say", ""), b.get("key", "")
        if not say or not key or key.lower() not in say.lower():
            probs.append(f"beat {i}: key must appear in say")
        if len(re.findall(r"[\w']+", say)) > 12:
            probs.append(f"beat {i}: too many words for one screen")
        if b.get("style") not in STYLES:
            probs.append(f"beat {i}: bad style")
        scene = b.get("scene", "")
        if not (scene in SCENES or scene.startswith("footage:")):
            probs.append(f"beat {i}: bad scene")
        if BANNED.search(say):
            probs.append(f"beat {i}: banned claim: {BANNED.search(say).group(0)}")
        words += len(re.findall(r"[\w']+", say))
    if words > 70:
        probs.append("too long (keep it under ~25 seconds)")
    return probs


def all_scripts() -> list[dict]:
    extra = []
    try:
        extra = [s for s in json.loads(GENERATED.read_text()) if not script_problems(s)]
    except Exception:
        pass
    return SCRIPTS + extra


def pick(ledger: list[dict]) -> dict:
    """Least recently used script."""
    last = {p.get("script"): i for i, p in enumerate(ledger) if p.get("script")}
    pool = all_scripts()
    unused = [s for s in pool if s["id"] not in last]
    if not unused and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API")):
        fresh = generate_scripts(3)
        unused = fresh
    if unused:
        return unused[0]
    return min(pool, key=lambda s: last.get(s["id"], -1))


def generate_scripts(n: int = 3) -> list[dict]:
    """Optional: Gemini writes new scripts in the same format; only ones that
    pass script_problems() are kept (and saved for future runs)."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API")
    if not key:
        return []
    import requests
    facts = ("Peakcut turns long videos into short captioned vertical clips. It cuts on full sentences, scores moments "
             "by hook/energy/pace, offers fill / show-everything / letterbox framing, word-timed captions plus an .srt, "
             "5 free videos a month, no credits, one-click cancel, and Pro users can process on their own computer.")
    example = json.dumps(SCRIPTS[0])
    prompt = (f"Write {n} new scripts for 15-25 second narrated vertical videos promoting Peakcut, in the calm, punchy "
              f"tone of a business-essay channel. Only use these facts: {facts} Never promise money, income or virality, "
              f"never name other companies. Format exactly like this JSON example: {example}. Each beat is one short "
              f"line (max 10 words); 'key' is 1-3 words copied from 'say'; style is bold|italic|serif; scene is one of "
              f"{list(SCENES)} or 'footage:<stock video search>' with an 'alt' generated scene. Use new ids and avoid "
              f"these titles: {[s['title'] for s in all_scripts()]}. Return only a JSON list.")
    try:
        r = requests.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
                          params={"key": key}, timeout=60,
                          json={"contents": [{"parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": 0.9, "responseMimeType": "application/json"}})
        r.raise_for_status()
        cands = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    except Exception as e:  # noqa: BLE001
        print(f"[autopilot] script generation skipped: {e}")
        return []
    known = {s["id"] for s in all_scripts()}
    fresh = [dict(s, ai=True) for s in cands if isinstance(s, dict) and not script_problems(s) and s.get("id") not in known]
    if fresh:
        try:
            saved = json.loads(GENERATED.read_text())
        except Exception:
            saved = []
        GENERATED.parent.mkdir(parents=True, exist_ok=True)
        GENERATED.write_text(json.dumps(saved + fresh, indent=1))
    return fresh
