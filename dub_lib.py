"""Multi-language dubbing: translate a clip's transcript and re-voice it with
a self-hosted TTS model, so the output can be posted to audiences that don't
speak the source language — Klap's strongest differentiator among the
competitors researched.

Translation was originally Argos Translate (fully offline), but measured
peak RSS with Argos + Piper both loaded was 1.15GB — Argos pulls in spaCy
and Stanza as dependencies, and importing it alone costs ~600-900MB, more
than Render's entire 512MB free-tier cap by itself. Switched to
deep-translator (a thin wrapper around Google's free web translate
endpoint): ~30MB total, no API key, still free, but needs outbound
internet at request time instead of being fully offline. Piper TTS stays
self-hosted (~30-75MB per voice, no such issue).

v1 scope: source video must be English (checked via Whisper's detected
language) since only {es,fr,pt} Piper voices are warmed into the Docker
image. Widening this to more languages later just means warming another
voice — no code changes needed here.
"""
import logging
import os
import subprocess

from deep_translator import GoogleTranslator
from piper import PiperVoice

import pipeline_lib

log = logging.getLogger("clipai.dub")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VOICES_DIR = os.environ.get("PIPER_VOICES_DIR", os.path.join(BASE_DIR, "voices"))

# name shown to users -> piper voice file (without .onnx)
DUB_LANGUAGES = {
    "es": {"label": "Spanish", "voice": "es_ES-carlfm-x_low"},
    "fr": {"label": "French", "voice": "fr_FR-siwis-low"},
    "pt": {"label": "Portuguese", "voice": "pt_BR-faber-medium"},
}

_voice_cache: dict = {}


def get_voice(lang_code: str) -> PiperVoice:
    if lang_code not in _voice_cache:
        model_path = os.path.join(VOICES_DIR, f"{DUB_LANGUAGES[lang_code]['voice']}.onnx")
        _voice_cache[lang_code] = PiperVoice.load(model_path)
    return _voice_cache[lang_code]


def translate_text(text: str, target_lang: str, source_lang: str = "en") -> str:
    return GoogleTranslator(source=source_lang, target=target_lang).translate(text)


def synthesize_speech(text: str, target_lang: str, out_wav_path: str):
    voice = get_voice(target_lang)
    import wave
    with wave.open(out_wav_path, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)


def _wav_duration(path: str) -> float:
    import wave
    with wave.open(path, "rb") as f:
        return f.getnframes() / float(f.getframerate())


def _atempo_chain(factor: float) -> str:
    """ffmpeg's atempo filter only accepts 0.5-2.0 per instance; chain
    multiple instances for factors outside that range (rare, but a very
    short or very long TTS render relative to the clip could hit this)."""
    factor = max(0.2, min(5.0, factor))
    filters = []
    remaining = factor
    while remaining < 0.5 or remaining > 2.0:
        step = 2.0 if remaining > 2.0 else 0.5
        filters.append(f"atempo={step}")
        remaining /= step
    filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def render_dubbed_clip(video_path, seg, out_dir, rank, target_lang, source_lang="en", watermark=True,
                       clip_format="vertical", caption_style="bold", layout="crop", watermark_text=None,
                       watermark_color=None, duration=None):
    """Same visual pipeline as pipeline_lib.render_clip (reframe, burned
    captions, watermark) but with the audio replaced by translated,
    synthesized speech time-stretched to fit the clip, and captions burned
    from the translated text."""
    start, end = pipeline_lib.clip_bounds(seg, duration)
    translated = translate_text(seg["text"], target_lang, source_lang)
    virality = seg.get("virality_score", 0)
    stem = f"peakcut_rank{rank}_v{virality}"

    raw_wav = os.path.join(out_dir, f"{stem}_dub_raw.wav")
    synthesize_speech(translated, target_lang, raw_wav)

    clip_duration = max(0.1, end - start)
    tts_duration = _wav_duration(raw_wav)
    tempo_filter = _atempo_chain(tts_duration / clip_duration)

    stretched_wav = os.path.join(out_dir, f"{stem}_dub.wav")
    stretch = subprocess.run(
        ["ffmpeg", "-y", "-i", raw_wav, "-af", tempo_filter, stretched_wav],
        capture_output=True, text=True,
    )
    os.unlink(raw_wav)
    out_path = os.path.join(out_dir, f"{stem}_{clip_format}.mp4")
    if stretch.returncode != 0:
        return out_path, translated, False, stretch.stderr[-800:]

    # Piper gives no per-word timestamps, so captions are spaced evenly
    # across the clip — an approximation, not a forced alignment.
    srt_path = os.path.join(out_dir, f"{stem}.srt")
    with open(srt_path, "w") as f:
        f.write(pipeline_lib.build_even_srt(translated, clip_duration))

    wm = (watermark_text or pipeline_lib.WATERMARK) if watermark else None
    ok, err = pipeline_lib.render_segment(
        video_path, start, end, srt_path, out_path, clip_format=clip_format, layout=layout,
        caption_style=caption_style, watermark_text=wm, watermark_color=watermark_color, audio_path=stretched_wav,
    )
    try:
        os.unlink(stretched_wav)
    except OSError:
        pass
    return out_path, translated, ok, err
