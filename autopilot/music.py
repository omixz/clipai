"""Music for the reels: picks a track from autopilot/assets/music/, finds its
beats, chooses the most energetic stretch, and works out a time-warp so the
animation's hits (cuts, hops, the final full stop) land on the beat.

Only use tracks you're allowed to use on TikTok/Instagram — see
assets/music/README.md. With no tracks, video.py falls back to the quiet
synthesized pad.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MUSIC_DIR = HERE / "assets" / "music"
EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}
SR = 22050
HOP = 512


def tracks() -> list[Path]:
    if not MUSIC_DIR.is_dir():
        return []
    return sorted(p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in EXTS and p.stat().st_size > 10_000)


def pick_track(recent: list[str]) -> Path | None:
    """Least recently used track (by file name)."""
    all_tracks = tracks()
    if not all_tracks:
        return None
    last = {name: i for i, name in enumerate(recent)}
    return min(all_tracks, key=lambda p: last.get(p.name, -1))


def decode(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def onset_envelope(y: np.ndarray, max_hz: float | None = None) -> np.ndarray:
    """Spectral flux on a log-magnitude spectrogram (frame rate SR/HOP).
    max_hz limits it to the low end (kick drum / bass)."""
    n_fft = 2048
    if len(y) < n_fft:
        return np.zeros(1)
    frames = 1 + (len(y) - n_fft) // HOP
    idx = np.arange(n_fft)[None, :] + HOP * np.arange(frames)[:, None]
    spec = np.abs(np.fft.rfft(y[idx] * np.hanning(n_fft), axis=1))
    if max_hz:
        spec = spec[:, : max(2, int(max_hz * n_fft / SR))]
    logspec = np.log1p(100 * spec)
    flux = np.maximum(0, np.diff(logspec, axis=0)).sum(axis=1)
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # remove slow trend
    flux = np.maximum(flux, 0)
    return flux / (flux.max() + 1e-9)


FRAME_OFFSET = 1024 / SR  # an onset registers when the hit reaches the window centre


def estimate_period(env: np.ndarray) -> float:
    """Beat period in seconds: autocorrelation peak in 70-180 BPM with a soft
    preference for ~120 BPM, refined to sub-frame precision (an integer lag
    alone drifts by a third of a second over a song)."""
    fps = SR / HOP
    e = env - env.mean()
    ac = np.correlate(e, e, mode="full")[len(e) - 1:]
    lags = np.arange(len(ac), dtype=float)
    bpm = np.where(lags > 0, 60 * fps / np.maximum(lags, 1), 0)
    prior = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1) / 120) / 0.6) ** 2)
    score = np.where((bpm >= 70) & (bpm <= 180), ac * prior, -np.inf)
    lag = int(np.argmax(score))
    if 0 < lag < len(ac) - 1:
        y0, y1, y2 = ac[lag - 1], ac[lag], ac[lag + 1]
        denom = y0 - 2 * y1 + y2
        if denom != 0:
            lag = lag + 0.5 * (y0 - y2) / denom
    # sharpen with longer lags (multiples of the period) for precision
    k_best = lag
    for mult in (2, 4, 8):
        center = lag * mult
        lo, hi = int(center - 3), int(center + 4)
        if hi >= len(ac):
            break
        seg = ac[lo:hi]
        k = lo + int(np.argmax(seg))
        if 0 < k < len(ac) - 1:
            y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
            d = y0 - 2 * y1 + y2
            k = k + (0.5 * (y0 - y2) / d if d else 0)
        k_best = k / mult
    return k_best / fps


def track_beats(env: np.ndarray, period: float, duration: float) -> np.ndarray:
    """Beat times: start at the strongest onset in the first two bars, then
    step one period at a time, snapping to the local onset peak (within 12%
    of a period) so the grid follows the music instead of drifting."""
    fps = SR / HOP
    pf = period * fps
    first = int(np.argmax(env[: max(2, int(pf * 8))]))
    # walk back to the earliest beat at or after 0
    pos = first - pf * int(first // pf)
    beats = []
    while pos < len(env):
        lo, hi = int(max(0, pos - .12 * pf)), int(min(len(env), pos + .12 * pf + 1))
        if hi > lo and env[lo:hi].max() > .15:
            pos = lo + int(np.argmax(env[lo:hi]))
        beats.append(pos / fps + FRAME_OFFSET)
        pos += pf
    return np.array([b for b in beats if b < duration])


def analyze(path: Path) -> dict:
    y = decode(path)
    env = onset_envelope(y)
    period = estimate_period(env)
    duration = len(y) / SR
    beats = track_beats(env, period, duration)
    # Hi-hats often make the off-beats the loudest onsets. The kick/bass
    # decides where the beat really is: if the low end hits harder half a
    # beat later, shift the grid by half a beat.
    low = onset_envelope(y, max_hz=180)
    fps = SR / HOP

    def low_at(times):
        idx = np.clip(((times - FRAME_OFFSET) * fps).astype(int), 0, len(low) - 1)
        return float(np.mean([low[max(0, i - 2):i + 3].max() for i in idx])) if len(idx) else 0.0

    half = beats + period / 2
    if low_at(half) > 1.2 * low_at(beats):
        beats = half[half < duration]
    # loudness per beat, to find the strongest stretch
    rms = np.array([np.sqrt(np.mean(y[int(b * SR):int((b + period) * SR)] ** 2) + 1e-12) for b in beats])
    return {"path": str(path), "bpm": round(float(60 / period), 1), "period": period, "beats": beats, "rms": rms, "duration": duration}


def best_start(info: dict, length: float) -> float:
    """Start of the most energetic `length`-second stretch, on a bar line
    (every 4th beat), skipping the first few seconds of intro."""
    beats, rms, period = info["beats"], info["rms"], info["period"]
    n = max(1, int(round(length / period)))
    best, best_i = -1.0, 0
    for i in range(0, max(1, len(beats) - n), 4):
        if beats[i] < min(4.0, info["duration"] * 0.1):
            continue
        e = float(rms[i:i + n].mean())
        if e > best:
            best, best_i = e, i
    return float(beats[best_i]) if len(beats) else 0.0


def plan_warp(scene_cues: list[float], scene_duration: float, period: float) -> list[list[float]]:
    """Anchors [[video_t, scene_t], ...] so each cue lands on a beat of a grid
    starting at video t=0. A global tempo fit first, then per-cue snapping,
    keeping every segment's speed within 0.7x-1.45x so nothing looks rushed."""
    cues = sorted(c for c in scene_cues if 0 < c < scene_duration)
    if not cues or period <= 0:
        return [[0.0, 0.0]]
    best_k, best_err = 1.0, float("inf")
    for k in np.linspace(0.8, 1.25, 46):
        err = sum(min(abs((c * k) / period - round((c * k) / period)), 0.5) for c in cues)
        if err < best_err:
            best_k, best_err = float(k), err
    anchors = [[0.0, 0.0]]
    for c in cues:
        v = round(c * best_k / period) * period
        pv, ps = anchors[-1]
        if v <= pv or ps >= c:
            continue
        speed = (c - ps) / (v - pv)
        if 0.7 <= speed <= 1.45:
            anchors.append([round(v, 4), round(c, 4)])
    return anchors


def video_time(anchors: list[list[float]], s: float) -> float:
    """Inverse of the warp (scene time -> video time); 1x after the last anchor."""
    for (v0, s0), (v1, s1) in zip(anchors, anchors[1:]):
        if s <= s1:
            return v0 + (s - s0) * (v1 - v0) / max(1e-9, s1 - s0)
    v, s_last = anchors[-1]
    return v + (s - s_last)
