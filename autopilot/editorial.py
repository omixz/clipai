"""Editorial reels: a narrated mini-essay with two-tier kinetic type over
cinematic footage or designed scenes — the business-essay look (big heavy
keyword, small lead-in words, blurred ghost of the last word zooming out,
graded footage, grain, vignette).

Pipeline: script -> voiceover with word timings -> beat timeline ->
transparent overlay (typography + generated scenes, rendered frame-exactly)
-> background track (licensed footage, graded, slow push-in) -> composite
-> grain + vignette -> voice over a ducked beat, loudness-normalized.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path

from . import footage, voice

HERE = Path(__file__).resolve().parent
FONTS = HERE / "assets" / "fonts"
W, H, FPS = 1080, 1920, 30
END_CARD = 2.3


def tokens_for(beat: dict, words: list[dict]) -> list[dict]:
    """Each spoken word gets a role: lead (small, above), key (huge), tail
    (small, below), with the time it should appear."""
    toks = [voice.norm(t) for t in re.findall(r"[\w'’]+", beat["say"]) if voice.norm(t)]
    display = [t for t in re.findall(r"[\w'’]+", beat["say"])]
    key_toks = [voice.norm(t) for t in re.findall(r"[\w'’]+", beat["key"])]
    start = next((i for i in range(len(toks)) if toks[i:i + len(key_toks)] == key_toks), max(0, len(toks) - 1))
    end = start + len(key_toks)
    out = []
    for i, text in enumerate(display):
        role = "lead" if i < start else ("key" if i < end else "tail")
        t = words[i]["start"] if i < len(words) else (words[-1]["end"] if words else 0)
        nxt = display[i + 1] if i + 1 < len(display) else None
        same_row = nxt is not None and (("lead" if i + 1 < start else ("key" if i + 1 < end else "tail")) == role)
        out.append({"text": text, "role": role, "t": round(t, 3), "space": same_row})
    return out


def build_timeline(script: dict, per_beat: list[list[dict]], total_audio: float) -> dict:
    beats = []
    for i, (b, words) in enumerate(zip(script["beats"], per_beat)):
        start = 0.0 if i == 0 else max(0.0, (words[0]["start"] if words else 0) - 0.12)
        beats.append({"start": round(start, 3), "style": b["style"], "scene": b["scene"],
                      "keyText": b["key"], "tokens": tokens_for(b, words)})
    for i in range(len(beats) - 1):
        beats[i]["end"] = beats[i + 1]["start"]
    end = round(max(total_audio, (per_beat[-1][-1]["end"] if per_beat[-1] else total_audio)) + 0.5, 3)
    beats[-1]["end"] = end
    return {"beats": beats, "end": end, "duration": round(end + END_CARD, 3)}


def build_page(timeline: dict, out_html: Path) -> Path:
    html = (HERE / "templates" / "editorial.html").read_text()
    html = (html.replace("__FONT_INTER__", (FONTS / "Inter.ttf").as_uri())
                .replace("__FONT_INTER_ITALIC__", (FONTS / "Inter-Italic.ttf").as_uri())
                .replace("__FONT_SERIF__", (FONTS / "InstrumentSerif-Italic.ttf").as_uri())
                .replace("__TIMELINE__", json.dumps(timeline)))
    out_html.write_text(html)
    return out_html


def _probe(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def background(timeline: dict, clips: dict[int, Path], out: Path):
    """Footage for footage beats (cover-cropped, graded, slow push-in),
    black elsewhere; the overlay paints the designed scenes on top."""
    inputs, chains = [], []
    segs = [(b["end"] - b["start"], clips.get(i)) for i, b in enumerate(timeline["beats"])]
    segs.append((END_CARD, None))
    for k, (dur, clip) in enumerate(segs):
        dur = max(0.1, dur)
        if clip:
            src_len = _probe(clip)
            ss = max(0.0, min(1.0, src_len - dur - 0.1))
            inputs += ["-ss", f"{ss:.2f}", "-t", f"{dur + 0.2:.2f}", "-i", str(clip)]
            frames = int(dur * FPS) + 6
            chains.append(
                f"[{len(inputs) // 6 - 1}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                f"zoompan=z='1+0.07*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},"
                f"eq=contrast=1.08:saturation=0.82:brightness=-0.03,trim=duration={dur:.3f},setpts=PTS-STARTPTS,setsar=1[s{k}]")
        else:
            chains.append(f"color=c=0x0a0a0b:s={W}x{H}:r={FPS}:d={dur:.3f},setsar=1[s{k}]")
    graph = ";".join(chains) + ";" + "".join(f"[s{k}]" for k in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[v]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-map", "[v]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(out)], check=True)


def render(script: dict, out_mp4: Path, work: Path, music_track: Path | None = None, used_footage: set[int] | None = None,
           voice_name: str | None = None) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    rng = random.Random(script["id"])
    voice_name = voice_name or rng.choice(voice.VOICES)
    text = " ".join(b["say"] for b in script["beats"])
    vo = work / "voice.mp3"
    words = voice.speak(text, vo, voice=voice_name)
    per_beat = voice.align(script["beats"], words)
    timeline = build_timeline(script, per_beat, _probe(vo))

    # footage for footage beats, falling back to the designed alt scene
    clips, used, footage_ids = {}, set(used_footage or ()), []
    for i, (b, tb) in enumerate(zip(script["beats"], timeline["beats"])):
        if b["scene"].startswith("footage:"):
            got = footage.find(b["scene"].split(":", 1)[1], used, work / "footage") if footage.available() else None
            if got:
                clips[i], fid = got
                used.add(fid)
                footage_ids.append(fid)
                tb["scene"] = "footage"
            else:
                tb["scene"] = b.get("alt", "dark")

    page = build_page(timeline, work / "editorial.html")
    overlay = work / "overlay.mov"
    proc = subprocess.run(["node", str(HERE / "render.mjs"), str(page), str(overlay), str(FPS), "--alpha"],
                          capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"overlay render failed: {proc.stderr[-1500:]}")
    bg = work / "background.mp4"
    background(timeline, clips, bg)

    # composite + film look, then audio: voice over a ducked beat
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    d = timeline["duration"]
    # voice chain: compress (even, present read) -> clean up lows; the bed sits under it
    a_inputs, a_graph = ["-i", str(vo)], ("[2:a]aresample=44100,highpass=f=80,"
                                          "acompressor=threshold=0.06:ratio=6:attack=3:release=100:makeup=3,apad[vo]")
    if music_track:
        a_inputs += ["-stream_loop", "-1", "-i", str(music_track)]
        a_graph += (f";[3:a]aresample=44100,volume=0.16,afade=t=in:d=0.4,afade=t=out:st={max(0, d - 1.5):.2f}:d=1.5[bed]"
                    ";[vo][bed]amix=inputs=2:duration=first:normalize=0[mix]")
    else:
        a_graph += ";[vo]anull[mix]"
    # limit first, then normalize: loudnorm's true-peak control keeps it under 0 dBFS
    a_graph += (f";[mix]atrim=duration={d:.3f},alimiter=limit=0.5:attack=2:release=60:level=disabled,"
                f"loudnorm=I=-14:TP=-1.5:LRA=7,aresample=44100[a]")
    v_graph = ("[0:v][1:v]overlay=format=auto,format=yuv420p,"
               "vignette=angle=PI/4.5,noise=alls=7:allf=t+u,eq=gamma=0.98[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(bg), "-i", str(overlay), *a_inputs,
                    "-filter_complex", v_graph + ";" + a_graph, "-map", "[v]", "-map", "[a]", "-t", f"{d:.3f}",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_mp4)], check=True)
    return {"duration": d, "voice": voice_name, "footage_ids": footage_ids, "timeline": timeline,
            "track": Path(music_track).name if music_track else None}
