"""Original, copyright-free backing tracks in the styles that dominate short
video right now (phonk, house, lo-fi), synthesized in code for each post.

Why generate instead of using hit songs: these reels are ads for a product.
Platforms mute or remove commercial posts that carry copyrighted music, and
strikes throttle the account — the opposite of going viral. Generated beats
can't be flagged, and because we know their exact tempo, the animation
lands on every hit. (For TikTok's real trending sounds, use trend hand-off.)
"""
from __future__ import annotations

import wave
import json  # noqa: F401
from pathlib import Path

import numpy as np

SR = 44100
STYLES = ("phonk", "house", "lofi")


def _env(n, attack=0.002, decay=0.2):
    t = np.arange(n) / SR
    a = np.minimum(1, t / max(attack, 1e-4))
    return a * np.exp(-t / max(decay, 1e-4))


def _kick(punch=1.0, dur=0.35):
    n = int(dur * SR)
    t = np.arange(n) / SR
    f = 45 + 110 * np.exp(-t * 32)
    ph = 2 * np.pi * np.cumsum(f) / SR
    return punch * np.sin(ph) * _env(n, 0.001, 0.16) + 0.25 * np.tanh(4 * np.sin(ph)) * _env(n, 0.001, 0.05)


def _noise(rng, dur, decay, hp=True):
    n = int(dur * SR)
    x = rng.standard_normal(n)
    if hp:  # crude high-pass: difference
        x = np.diff(np.concatenate([[0], x]))
    return x * _env(n, 0.0005, decay)


def _clap(rng, dur=0.25):
    x = _noise(rng, dur, 0.06, hp=False)
    for d in (0.008, 0.017):  # the "flam" of a clap
        k = int(d * SR)
        x[k:] += 0.6 * x[:-k]
    return 0.5 * x


def _tone(freq, dur, kind="sine", decay=0.4, attack=0.004):
    n = int(dur * SR)
    t = np.arange(n) / SR
    ph = 2 * np.pi * freq * t
    if kind == "square":
        w = np.tanh(3 * np.sin(ph))
    elif kind == "saw":
        w = 2 * ((freq * t) % 1) - 1
    elif kind == "keys":  # soft electric-piano-ish
        w = np.sin(ph) + 0.35 * np.sin(2 * ph) * np.exp(-t * 6) + 0.12 * np.sin(3 * ph)
    else:
        w = np.sin(ph)
    return w * _env(n, attack, decay)


def _cowbell(freq, dur=0.18):
    # the signature phonk lead: two detuned square tones through a fast decay
    return 0.5 * (_tone(freq, dur, "square", 0.09) + _tone(freq * 1.48, dur, "square", 0.07))


def _lowpass(x, cutoff):
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):  # one-pole; fine for a few seconds of pads
        acc = (1 - a) * v + a * acc
        y[i] = acc
    return y


def _place(buf, sample, at, gain=1.0):
    i = int(at * SR)
    if i >= len(buf):
        return
    j = min(len(buf), i + len(sample))
    buf[i:j] += gain * sample[: j - i]


NOTE = lambda midi: 440.0 * 2 ** ((midi - 69) / 12)  # noqa: E731

PROGRESSIONS = {  # minor-key loops that read as "current"
    "phonk": [[45, 48, 52], [41, 45, 48], [43, 46, 50], [40, 43, 47]],
    "house": [[57, 60, 64], [53, 57, 60], [55, 59, 62], [52, 55, 59]],
    "lofi": [[50, 53, 57, 60], [55, 58, 62, 65], [48, 52, 55, 59], [53, 57, 60, 64]],
}


def generate(style: str, seconds: float, out: Path, seed: int = 0) -> dict:
    """Writes a mono 44.1kHz WAV; returns {"style", "bpm", "path"}."""
    rng = np.random.default_rng(seed)
    bpm = {"phonk": 130, "house": 124, "lofi": 84}[style] + int(rng.integers(-3, 4))
    beat = 60 / bpm
    n = int(seconds * SR)
    drums = np.zeros(n)
    bass = np.zeros(n)
    music = np.zeros(n)
    prog = PROGRESSIONS[style]
    if rng.random() < .5:
        prog = prog[1:] + prog[:1]
    bars = int(seconds / (4 * beat)) + 1
    hook = rng.choice([0, 3, 5, 7, 10, 12], size=8)  # minor-pentatonic hook, fixed per track

    for bar in range(bars):
        chord = prog[bar % len(prog)]
        b0 = bar * 4 * beat
        for k in range(4):
            t = b0 + k * beat
            if style == "lofi":
                swing = 0.06 * beat
                if k in (0, 2) or (k == 3 and bar % 2):
                    _place(drums, _kick(.9), t)
                if k in (1, 3):
                    _place(drums, _noise(rng, .18, .05, hp=False) * .45, t)
                for e in (0, .5):
                    _place(drums, _noise(rng, .05, .015) * .12, t + e * beat + (swing if e else 0))
            else:
                _place(drums, _kick(1.0), t)
                if k in (1, 3):
                    _place(drums, _clap(rng), t, .8 if style == "house" else .7)
                if style == "house":
                    _place(drums, _noise(rng, .06, .02) * .3, t + beat / 2)
                else:  # phonk: rolling hats with triplet fills
                    subs = (0, 1 / 3, 2 / 3) if k == 3 else (0, .5)
                    for e in subs:
                        _place(drums, _noise(rng, .04, .012) * .18, t + e * beat)
        root = NOTE(chord[0] - 12)
        if style == "phonk":
            # sliding 808 on the downbeat + cowbell hook on 8ths
            n808 = int(2 * beat * SR)
            tt = np.arange(n808) / SR
            f = root * (1 + .5 * np.exp(-tt * 18))
            s808 = np.tanh(2.2 * np.sin(2 * np.pi * np.cumsum(f) / SR)) * _env(n808, .002, .9)
            _place(bass, s808, b0, .55)
            _place(bass, s808, b0 + 2.5 * beat, .4)
            for i in range(8):
                _place(music, _cowbell(NOTE(chord[0] + 24 + int(hook[i]))), b0 + i * beat / 2, .22)
        elif style == "house":
            for i in range(8):  # offbeat bass + chord stabs
                if i % 2:
                    _place(bass, _tone(root, beat / 2, "saw", .12), b0 + i * beat / 2, .28)
            for k in (0.5, 1.5, 2.75, 3.5):
                for m in chord:
                    _place(music, _tone(NOTE(m), .22, "saw", .09), b0 + k * beat, .07)
        else:  # lofi keys + soft sub
            for m in chord:
                _place(music, _tone(NOTE(m), 4 * beat, "keys", 1.6, .01), b0, .09)
            _place(bass, _tone(root, 2 * beat, "sine", .9), b0, .35)
            _place(bass, _tone(root, 2 * beat, "sine", .9), b0 + 2 * beat, .3)

    if style == "lofi":
        music = _lowpass(music, 1800)
        music += 0.004 * rng.standard_normal(n) * (rng.random(n) < .002) * 40  # vinyl crackle
    if style == "house":
        music = _lowpass(music, 2600)
    mix = 0.9 * drums + bass + music
    mix = np.tanh(1.3 * mix)  # glue
    mix /= np.abs(mix).max() + 1e-9
    mix *= 0.89
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype("<i2").tobytes())
    Path(str(out) + ".json").write_text(f'{{"bpm": {bpm}, "first_beat": 0.0, "style": "{style}"}}')
    return {"style": style, "bpm": bpm, "path": str(out)}


def pick_style(recent: list[str]) -> str:
    last = {s: i for i, s in enumerate(recent)}
    return min(STYLES, key=lambda s: last.get(s, -1))
