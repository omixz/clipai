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


def render_dubbed_clip(video_path, seg, out_dir, rank, target_lang, source_lang="en", watermark=True):
    """Same visual pipeline as pipeline_lib.render_clip (vertical crop,
    burned captions, watermark) but with the audio track replaced by
    translated, synthesized speech time-stretched to fit the clip, and
    captions burned from the translated text instead of the original."""
    translated = translate_text(seg["text"], target_lang, source_lang)

    raw_wav = os.path.join(out_dir, f"clip_{rank}_dub_raw.wav")
    synthesize_speech(translated, target_lang, raw_wav)

    clip_duration = max(0.1, seg["end"] - seg["start"])
    tts_duration = _wav_duration(raw_wav)
    tempo_filter = _atempo_chain(tts_duration / clip_duration)

    stretched_wav = os.path.join(out_dir, f"clip_{rank}_dub.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-i", raw_wav, "-af", tempo_filter, stretched_wav],
        capture_output=True, text=True,
    )
    os.unlink(raw_wav)

    # Even-spaced pseudo word-chunk captions across the clip's duration —
    # Piper doesn't give us per-word timestamps the way Whisper does for the
    # original-language captions, so this is a linear approximation rather
    # than a true forced alignment.
    srt_path = os.path.join(out_dir, f"clip_{rank}.srt")
    words = translated.split()
    chunk_size = 3
    lines, idx = [], 1
    n_chunks = max(1, (len(words) + chunk_size - 1) // chunk_size)
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i + chunk_size]
        chunk_idx = i // chunk_size
        start = clip_duration * chunk_idx / n_chunks
        end = clip_duration * (chunk_idx + 1) / n_chunks
        lines.append(f"{idx}\n{pipeline_lib.srt_timestamp(start)} --> {pipeline_lib.srt_timestamp(end)}\n{' '.join(chunk)}\n")
        idx += 1
    with open(srt_path, "w") as f:
        f.write("\n".join(lines))

    out_path = os.path.join(out_dir, f"clip_{rank}.mp4")
    vf = (
        "scale=1080:-2,pad=1080:1920:0:(1920-ih)/2:color=0x1a1a2e,"
        f"subtitles={srt_path}:force_style='FontName=DejaVu Sans,FontSize=30,"
        "PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=1,Outline=3,"
        "Bold=1,Alignment=2,MarginV=140'"
    )
    if watermark:
        vf += (
            f",drawtext=text='{pipeline_lib.WATERMARK}':fontcolor=white@0.6:fontsize=18:"
            "x=(w-text_w)/2:y=h-70"
        )
    cmd = [
        "ffmpeg", "-y", "-ss", str(seg["start"]), "-to", str(seg["end"]),
        "-i", video_path, "-i", stretched_wav,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", vf, "-shortest",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(stretched_wav)
    return out_path, translated, result.returncode == 0, result.stderr[-800:]
