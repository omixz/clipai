"""Builds a minimal reel: spec -> HTML page -> frame-exact render -> soft
synthesized soundtrack -> 1080x1920 mp4 (+ a contact sheet for review)."""
from __future__ import annotations

import base64
import json
import math
import struct
import subprocess
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"
FONT = HERE / "assets" / "fonts" / "Outfit-Regular.ttf"
MOTIFS = sorted(p.stem for p in (TEMPLATES / "scenes").glob("*.js"))
THEMES = ("dark", "light", "gold")


def build_page(spec: dict, out_html: Path) -> Path:
    motif = spec["motif"]
    if motif not in MOTIFS:
        raise ValueError(f"unknown motif {motif!r}; have {MOTIFS}")
    html = (TEMPLATES / "shell.html").read_text()
    theme = spec.get("theme") or ("light" if spec.get("light") else "dark")
    page_spec = {"lines": spec["lines"], "theme": theme, "cta": spec.get("cta", "link in bio"),
                 "warp": spec.get("warp")}
    html = (html.replace("__FONT__", base64.b64encode(FONT.read_bytes()).decode())
                .replace("__LIB__", (TEMPLATES / "lib.js").read_text())
                .replace("__SCENE__", (TEMPLATES / "scenes" / f"{motif}.js").read_text())
                .replace("__SPEC__", json.dumps(page_spec)))
    out_html.write_text(html)
    return out_html


# ── Sound: a quiet pad plus a soft tick on each visual event ─────────────────
# Generated in code (no music library, nothing to license). Silence gets less
# reach on TikTok/Reels, but anything loud fights the minimal look.

def synth_audio(duration: float, cues: list[float], out_wav: Path, light: bool = False, sr: int = 44100,
                pad: bool = True):
    n = int(duration * sr)
    out = [0.0] * n
    # pad: a soft open fifth with a slow swell (lighter, airier root on white videos)
    root = 146.83 if light else 110.0
    for f, amp in (((root, .050), (root * 1.5, .030), (root * 2, .018)) if pad else ()):
        w = 2 * math.pi * f / sr
        for i in range(n):
            t = i / sr
            env = min(1.0, t / 1.2) * min(1.0, (duration - t) / 1.0)
            out[i] += amp * env * (0.85 + 0.15 * math.sin(2 * math.pi * 0.23 * t)) * math.sin(w * i)
    # ticks: short, bell-like, decaying sines — the last one (the full stop) lower and longer
    for k, c in enumerate(cues):
        last = k == len(cues) - 1
        freq, length, amp = ((659.25, .9, .16) if last else (1318.5, .22, .11))
        start = int(c * sr)
        for i in range(int(length * sr)):
            j = start + i
            if j >= n:
                break
            t = i / sr
            env = (1 - math.exp(-t * 900)) * math.exp(-t * (5 if last else 22))
            out[j] += amp * env * (math.sin(2 * math.pi * freq * t) + .3 * math.sin(2 * math.pi * freq * 2.01 * t))
    peak = max(1e-9, max(abs(x) for x in out))
    gain = min(1.0, .7 / peak)
    with wave.open(str(out_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x * gain)) * 32767)) for x in out))
    return out_wav


def _node(*args, timeout=900) -> dict:
    proc = subprocess.run(["node", str(HERE / "render.mjs"), *map(str, args)], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"render failed: {proc.stderr[-1500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def render(spec: dict, out_mp4: Path, workdir: Path, fps: int = 30, music_track: Path | None = None,
           quiet_variant: bool = False) -> dict:
    """Renders spec to out_mp4 and returns its metadata.

    With music_track, the most energetic stretch of the track is used and the
    animation is time-warped so its hits land on the beats. With
    quiet_variant, also writes <name>.quiet.mp4 with only the soft synth pad —
    for posts where someone adds a trending sound in the app."""
    workdir.mkdir(parents=True, exist_ok=True)
    spec = dict(spec)
    info = None
    if music_track:
        from . import music
        try:
            info = music.analyze(Path(music_track))
            scene = _node(build_page(spec, workdir / "page.html"), "--meta")
            spec["warp"] = music.plan_warp(scene["sceneCues"], scene["sceneDuration"], info["period"])
        except Exception as e:  # noqa: BLE001 — a bad track must not stop the post
            print(f"[autopilot] music skipped ({Path(music_track).name}): {e}")
            info = None
    page = build_page(spec, workdir / "page.html")
    silent = workdir / "silent.mp4"
    meta = _node(page, silent, fps)
    light = spec.get("theme") == "light" or bool(spec.get("light"))
    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    def mux(audio_args: list[str], filter_complex: str | None, dest: Path):
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(silent), *audio_args]
        if filter_complex:
            cmd += ["-filter_complex", filter_complex, "-map", "0:v", "-map", "[a]"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-shortest", "-movflags", "+faststart", str(dest)]
        subprocess.run(cmd, check=True)

    pad_wav = synth_audio(meta["duration"], meta["cues"], workdir / "sound.wav", light=light)
    if info:
        from . import music
        start = music.best_start(info, meta["duration"])
        d = meta["duration"]
        chime = synth_audio(d, meta["cues"][-1:], workdir / "chime.wav", light=light, pad=False)
        mux(["-ss", f"{start:.3f}", "-t", f"{d + 0.5:.3f}", "-i", str(music_track), "-i", str(chime)],
            f"[1:a]aresample=44100,afade=t=in:d=0.02,afade=t=out:st={max(0, d - 1.1):.2f}:d=1.1,"
            f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=44100[m];[2:a]volume=0.55[c];"
            f"[m][c]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.7:attack=2:release=60:level=disabled[a]", out_mp4)
        meta.update(track=Path(music_track).name, bpm=float(info["bpm"]), track_start=round(start, 2), warp=spec.get("warp"))
    else:
        mux(["-i", str(pad_wav)], None, out_mp4)
    if quiet_variant:
        quiet = out_mp4.with_suffix(".quiet.mp4")
        mux(["-i", str(pad_wav)], "[1:a]volume=0.5[a]", quiet)
        meta["quiet_path"] = str(quiet)
    return meta


def contact_sheet(mp4: Path, out_jpg: Path, cols: int = 6) -> Path:
    """One image with frames across the video and the platforms' UI-safe area
    marked, for a quick look in the workflow artifact."""
    subprocess.run([
        "ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-vf",
        "fps=1.2,scale=270:480,drawbox=x=0:y=0:w=iw:h=ih*0.11:color=red@0.18:t=fill,"
        "drawbox=x=0:y=ih*0.78:w=iw:h=ih*0.22:color=red@0.18:t=fill,"
        "drawbox=x=iw*0.86:y=ih*0.45:w=iw*0.14:h=ih*0.33:color=red@0.18:t=fill,"
        f"tile={cols}x2:padding=6:color=gray",
        "-frames:v", "1", str(out_jpg)], check=True)
    return out_jpg
