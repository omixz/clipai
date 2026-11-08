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


def build_page(spec: dict, out_html: Path) -> Path:
    motif = spec["motif"]
    if motif not in MOTIFS:
        raise ValueError(f"unknown motif {motif!r}; have {MOTIFS}")
    html = (TEMPLATES / "shell.html").read_text()
    page_spec = {"lines": spec["lines"], "light": bool(spec.get("light")), "cta": spec.get("cta", "link in bio")}
    html = (html.replace("__FONT__", base64.b64encode(FONT.read_bytes()).decode())
                .replace("__LIB__", (TEMPLATES / "lib.js").read_text())
                .replace("__SCENE__", (TEMPLATES / "scenes" / f"{motif}.js").read_text())
                .replace("__SPEC__", json.dumps(page_spec)))
    out_html.write_text(html)
    return out_html


# ── Sound: a quiet pad plus a soft tick on each visual event ─────────────────
# Generated in code (no music library, nothing to license). Silence gets less
# reach on TikTok/Reels, but anything loud fights the minimal look.

def synth_audio(duration: float, cues: list[float], out_wav: Path, light: bool = False, sr: int = 44100):
    n = int(duration * sr)
    out = [0.0] * n
    # pad: a soft open fifth with a slow swell (lighter, airier root on white videos)
    root = 146.83 if light else 110.0
    for f, amp in ((root, .050), (root * 1.5, .030), (root * 2, .018)):
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


def render(spec: dict, out_mp4: Path, workdir: Path, fps: int = 30) -> dict:
    """Renders spec to out_mp4. Returns {"duration", "cues", "frames"}."""
    workdir.mkdir(parents=True, exist_ok=True)
    page = build_page(spec, workdir / "page.html")
    silent = workdir / "silent.mp4"
    proc = subprocess.run(["node", str(HERE / "render.mjs"), str(page), str(silent), str(fps)],
                          capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"render failed: {proc.stderr[-1500:]}")
    meta = json.loads(proc.stdout.strip().splitlines()[-1])
    wav = synth_audio(meta["duration"], meta["cues"], workdir / "sound.wav", light=bool(spec.get("light")))
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-i", str(wav), "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(out_mp4)], check=True)
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
