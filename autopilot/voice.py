"""Voiceover via Microsoft Edge's free neural voices (edge-tts — the same
route the YT-AUTO repo uses), with per-word timings for the kinetic captions."""
from __future__ import annotations

import asyncio
import os
import re
import ssl
from pathlib import Path

# calm, documentary-style male voices like the reference channel
VOICES = ["en-US-AndrewMultilingualNeural", "en-US-BrianMultilingualNeural", "en-US-ChristopherNeural"]


def _ssl_context():
    """Honour a custom CA bundle if the environment provides one (e.g. a
    corporate proxy); otherwise edge-tts' own default is used."""
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile) if cafile and Path(cafile).exists() else None


async def _speak(text: str, voice: str, out: Path, rate: str) -> list[dict]:
    import edge_tts
    import edge_tts.communicate as ec
    ctx = _ssl_context()
    if ctx is not None:
        ec._SSL_CTX = ctx
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    words = []
    with open(out, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                start = ch["offset"] / 1e7
                words.append({"w": ch["text"], "start": start, "end": start + ch["duration"] / 1e7})
    return words


def speak(text: str, out: Path, voice: str = VOICES[0], rate: str = "+6%", retries: int = 3) -> list[dict]:
    """Writes an mp3 and returns [{"w", "start", "end"}, ...]."""
    last = None
    for _ in range(retries):
        try:
            words = asyncio.run(_speak(text, voice, out, rate))
            if words and out.stat().st_size > 1000:
                return words
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"voiceover failed: {last}")


def norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def align(beats: list[dict], words: list[dict]) -> list[list[dict]]:
    """Splits the voiced word list into each beat's words, by walking the
    script's tokens in order (tolerant of tokens the voice splits/merges)."""
    out, i = [], 0
    for b in beats:
        tokens = [norm(t) for t in re.findall(r"[\w'’]+", b["say"]) if norm(t)]
        mine = []
        for n, tok in enumerate(tokens):
            # find this token within a small window ahead
            j = next((k for k in range(i, min(len(words), i + 4)) if norm(words[k]["w"]) == tok), None)
            if j is None:
                if i < len(words):
                    # the voice merged/split words (e.g. "twenty twenty six" -> "2026"): reuse this
                    # word's timing, but don't consume it if a later token of this line needs it
                    needed_later = norm(words[i]["w"]) in tokens[n + 1:]
                    mine.append({**words[i], "w": tok})
                    if not needed_later:
                        i += 1
                continue
            mine.append({**words[j], "w": tok})
            i = j + 1
        out.append(mine)
    return out
