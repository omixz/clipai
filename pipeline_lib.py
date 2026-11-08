"""Core clipping pipeline: transcribe -> build sentence-aware candidate windows
-> score -> pick the top N non-overlapping -> render captioned clips.

Design notes (each one maps to a common complaint about AI clippers):

* Clips used to be single Whisper segments of 3-16s, which often started or
  ended mid-thought. Candidates are now *windows* of consecutive segments that
  start at a sentence boundary and end on one, sized by the user's chosen
  clip length (short / medium / long), with a little padding so the first and
  last syllables aren't clipped.
* Loudness used to be measured with one ffmpeg process per segment (hundreds
  of processes on a long podcast). It's now one pass over the audio that
  yields a loudness timeline, which every window is scored against.
* Vertical output used to be the horizontal video letterboxed into a tiny
  strip. Default is now a centre crop that fills the frame; "blur" keeps the
  full picture over a blurred fill, "fit" is the old letterbox look.
"""
import json
import os
import re
import subprocess
from collections import Counter

from faster_whisper import WhisperModel

WATERMARK = "Made with Peakcut"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")

FONT_DIR_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]

FORMATS = {"vertical": (1080, 1920), "square": (1080, 1080), "horizontal": (1920, 1080)}
LAYOUTS = ("crop", "blur", "fit")
CLIP_LENGTHS = {"short": (12.0, 30.0), "medium": (25.0, 60.0), "long": (45.0, 90.0)}

# ASS styles. SRT subtitles are rendered on a 384x288 virtual canvas by
# libass and scaled to the real frame, so sizes here are relative to that.
CAPTION_STYLES = {
    "bold": "FontName=DejaVu Sans,FontSize={fs},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Bold=1,Alignment=2,MarginV={mv}",
    "outline": "FontName=DejaVu Sans,FontSize={fs},PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,Bold=0,Alignment=2,MarginV={mv}",
    "subtle": "FontName=DejaVu Sans,FontSize={fs_small},PrimaryColour=&H00E0E0E0,OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=0,Bold=0,Alignment=2,MarginV={mv}",
    # ASS colours are &HAABBGGRR — 00FFFF is yellow.
    "neon": "FontName=DejaVu Sans,FontSize={fs},PrimaryColour=&H0000FFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=1,Bold=1,Alignment=2,MarginV={mv}",
    "boxed": "FontName=DejaVu Sans,FontSize={fs},PrimaryColour=&H00FFFFFF,OutlineColour=&H80000000,BackColour=&H80000000,BorderStyle=3,Outline=6,Shadow=0,Bold=1,Alignment=2,MarginV={mv}",
}

_model = None


def get_model():
    global _model
    if _model is None:
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def unload_model():
    """Free the cached Whisper model. A dub job also needs Piper TTS loaded;
    dropping Whisper right after transcription keeps peak RSS under a 512MB
    free-tier cap at the cost of a reload on the next job."""
    global _model
    import gc
    _model = None
    gc.collect()


def _font_file():
    for p in FONT_DIR_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# ── Transcription ────────────────────────────────────────────────────────────

def transcribe(video_path):
    model = get_model()
    # vad_filter skips long silences: faster on real-world uploads and stops
    # Whisper hallucinating text over music/silence.
    segments, info = model.transcribe(video_path, word_timestamps=True, vad_filter=True)
    out = []
    for seg in segments:
        words = [{"word": w.word.strip(), "start": w.start, "end": w.end} for w in (seg.words or []) if w.word.strip()]
        text = seg.text.strip()
        if not text:
            continue
        out.append({"start": seg.start, "end": seg.end, "text": text, "words": words})
    return out, info.duration, info.language


# ── Audio energy ─────────────────────────────────────────────────────────────

def loudness_timeline(video_path, step=0.5):
    """One ffmpeg pass -> list of (time_seconds, rms_db) every `step` seconds."""
    sr = 8000
    n = int(sr * step)
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", video_path, "-vn", "-af",
        f"aresample={sr},asetnsamples=n={n}:p=0,astats=metadata=1:reset=1,"
        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except Exception:
        return []
    timeline, t = [], None
    for line in result.stdout.splitlines():
        if line.startswith("frame:"):
            m = re.search(r"pts_time:([\d.]+)", line)
            t = float(m.group(1)) if m else None
        elif "RMS_level=" in line and t is not None:
            val = line.split("=", 1)[1].strip()
            try:
                db = float(val)
            except ValueError:
                db = -90.0  # "-inf" (digital silence)
            if db != db or db == float("-inf"):  # NaN / -inf guard
                db = -90.0
            timeline.append((t, db))
    return timeline


def energy_between(timeline, start, end):
    vals = [db for t, db in timeline if start <= t <= end]
    if not vals:
        return -60.0
    return sum(vals) / len(vals)


def audio_energy_db(video_path, start, end):
    """Kept for backwards compatibility; prefer loudness_timeline()."""
    return energy_between(loudness_timeline(video_path), start, end)


# ── Scoring ──────────────────────────────────────────────────────────────────

CONTRAST_WORDS = ("but", "yet", "however", "because", "actually", "instead")
HOOK_PATTERNS = [
    r"\bhere'?s (why|how|what|the)\b", r"\bthe (secret|truth|problem|reason|mistake)\b",
    r"\bnobody\b", r"\bno one\b", r"\bnever\b", r"\bbiggest\b", r"\bworst\b", r"\bbest\b",
    r"\bstop\b", r"\bwhy\b", r"\bhow to\b", r"\byou need\b", r"\bimagine\b", r"\bwhat if\b",
    r"\bcrazy\b", r"\binsane\b", r"\bhonestly\b", r"\bthe truth is\b", r"\bi was wrong\b",
    r"\b\d+\b",
]
_HOOK_RE = re.compile("|".join(HOOK_PATTERNS), re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?…][\"')\]]*$")


def _ends_sentence(text):
    return bool(_SENTENCE_END_RE.search(text.strip()))


def text_signal_score(text):
    score = 0.0
    score += min(text.count("!"), 3) * 2.0
    score += min(text.count("?"), 3) * 1.0
    lower = text.lower()
    score += sum(len(re.findall(rf"\b{w}\b", lower)) for w in CONTRAST_WORDS) * 1.5
    words = text.split()
    if words:
        avg_word_len = sum(len(w) for w in words) / len(words)
        score += max(0, 6 - avg_word_len)
    score += min(len(words), 20) * 0.1
    return score


def hook_score(first_sentence):
    """The first ~3 seconds decide whether a short gets watched — reward
    windows that open with a question, a number, or a strong claim."""
    s = first_sentence.strip()
    score = 0.0
    if s.endswith("?"):
        score += 2.0
    score += min(len(_HOOK_RE.findall(s)), 2) * 1.0
    if len(s.split()) <= 12:
        score += 0.5  # short punchy opener
    return score


def virality_score(composite):
    """Maps the raw composite onto 35-99 so it reads like the "virality score"
    other clippers show — same signal, familiar presentation."""
    return int(min(99, max(35, round(composite * 4 + 15))))


def build_windows(segments, min_dur, max_dur, strict=True):
    """(i, j) index pairs of consecutive segments forming a candidate clip.
    strict=True requires the window to start and end on a sentence boundary
    (or a pause of 0.7s+, which is where people breathe between thoughts)."""
    n = len(segments)
    out = []
    for i in range(n):
        if strict and i > 0:
            prev = segments[i - 1]
            if not _ends_sentence(prev["text"]) and segments[i]["start"] - prev["end"] < 0.7:
                continue
        for j in range(i, n):
            dur = segments[j]["end"] - segments[i]["start"]
            if dur > max_dur:
                break
            if dur < min_dur:
                continue
            if strict and j < n - 1:
                if not _ends_sentence(segments[j]["text"]) and segments[j + 1]["start"] - segments[j]["end"] < 0.7:
                    continue
            out.append((i, j))
    return out


def score_candidates(video_path, segments, clip_length="medium", timeline=None, duration=None):
    min_dur, max_dur = CLIP_LENGTHS.get(clip_length, CLIP_LENGTHS["medium"])
    if timeline is None:
        timeline = loudness_timeline(video_path)
    if not segments:
        return []

    windows = build_windows(segments, min_dur, max_dur, strict=True)
    if not windows:
        windows = build_windows(segments, min_dur, max_dur, strict=False)
    if not windows:
        # Video (or its speech) is shorter than the minimum clip length —
        # fall back to the longest stretch we can make, rather than failing.
        total = segments[-1]["end"] - segments[0]["start"]
        if total >= 3.0:
            j = len(segments) - 1
            while j > 0 and segments[j]["end"] - segments[0]["start"] > max_dur:
                j -= 1
            windows = [(0, j)]

    scored = []
    for i, j in windows:
        segs = segments[i:j + 1]
        start, end = segs[0]["start"], segs[-1]["end"]
        text = " ".join(s["text"] for s in segs)
        energy = energy_between(timeline, start, end)
        energy_score = max(0.0, (energy + 40) / 4)
        text_score = sum(text_signal_score(s["text"]) for s in segs) / len(segs)
        hook = hook_score(segs[0]["text"])
        complete = 1.0 if _ends_sentence(segs[-1]["text"]) else 0.0
        words = [w for s in segs for w in s["words"]]
        pace = len(text.split()) / max(1.0, end - start)  # words per second
        pace_score = 1.0 if 2.0 <= pace <= 4.0 else 0.0
        composite = energy_score + text_score + hook + complete + pace_score
        scored.append({
            "start": start, "end": end, "text": text, "words": words,
            "energy_db": round(energy, 2), "composite": round(composite, 2),
            "virality_score": virality_score(composite),
        })
    scored.sort(key=lambda r: r["composite"], reverse=True)
    return scored


def pick_top_n(scored, n=3, min_gap=2.0):
    picked = []
    for cand in scored:
        overlaps = any(not (cand["end"] < p["start"] - min_gap or cand["start"] > p["end"] + min_gap) for p in picked)
        if not overlaps:
            picked.append(cand)
        if len(picked) >= n:
            break
    return picked


# ── Metadata helpers ─────────────────────────────────────────────────────────

_STOPWORDS = set("""a an and are as at be been but by can could did do does doing don't for from had has have
having he her here hers him his how i i'm if in into is it it's its just know like me more most my no not
now of on one only or our out really right say said she so some than that that's the their them then there
these they thing things think this those to too um uh up us very was we were what when where which who why
will with would yeah you you're your going gonna want get got make about all also because been being over
thats theres youre theyre were dont didnt cant wont isnt doesnt every single still never always people
nobody someone something anything everything nothing maybe little
""".split())


def suggest_title(text, max_len=70):
    """First sentence with some substance (a lone "Why?" makes a bad title)."""
    sentences = [x.strip(" \"'") for x in re.split(r"(?<=[.!?])\s+", text.strip()) if x.strip()]
    if not sentences:
        return ""
    title = next((x for x in sentences if len(x.split()) >= 4), sentences[0])
    if len(title) > max_len:
        title = title[:max_len].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return title


def suggest_hashtags(text, n=3):
    words = [re.sub(r"[^a-z0-9]", "", w.lower().replace("'", "").replace("’", "")) for w in text.split()]
    words = [w for w in words if len(w) >= 5 and w not in _STOPWORDS and not w.isdigit()]
    top = [w for w, _ in Counter(words).most_common(n)]
    return [f"#{w}" for w in top] + ["#shorts", "#fyp"]


# ── Captions ─────────────────────────────────────────────────────────────────

def srt_timestamp(t):
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# Caption-only cleanup: filler words stay in the audio, just not the captions.
FILLER_WORDS = {"um", "uh", "uhh", "umm", "erm", "ah", "hm", "hmm"}


def _is_filler(word_text):
    return re.sub(r"[^a-z]", "", word_text.lower()) in FILLER_WORDS


def build_word_chunk_srt(words, clip_start, chunk_size=4, clean_fillers=True, clip_end=None):
    if clean_fillers:
        words = [w for w in words if not _is_filler(w["word"])]
    if clip_end is not None:
        words = [w for w in words if w["start"] < clip_end]
    # Break chunks at sentence ends as well as every `chunk_size` words, so a
    # caption never shows the end of one sentence glued to the next.
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= chunk_size or _ends_sentence(w["word"]) or w["word"].endswith(","):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)

    lines = []
    for idx, chunk in enumerate(chunks, 1):
        start = chunk[0]["start"] - clip_start
        end = chunk[-1]["end"] - clip_start
        if end <= start:
            end = start + 0.4
        text = " ".join(w["word"] for w in chunk)
        lines.append(f"{idx}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{text}\n")
    return "\n".join(lines)


def build_even_srt(text, duration, chunk_size=3):
    """Evenly spaced captions for when there are no word timestamps (dubs)."""
    words = text.split()
    n_chunks = max(1, (len(words) + chunk_size - 1) // chunk_size)
    lines = []
    for k, i in enumerate(range(0, len(words), chunk_size)):
        start = duration * k / n_chunks
        end = duration * (k + 1) / n_chunks
        lines.append(f"{k + 1}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{' '.join(words[i:i + chunk_size])}\n")
    return "\n".join(lines)


# ── Rendering ────────────────────────────────────────────────────────────────

def _caption_style(caption_style, clip_format):
    template = CAPTION_STYLES.get(caption_style, CAPTION_STYLES["bold"])
    # Sizes on libass' 288-high virtual canvas. Vertical frames are narrow, so
    # smaller text (≈ 6% of frame width per glyph) keeps 3-4 words on one line.
    if clip_format == "vertical":
        fs, mv = 13, 70
    elif clip_format == "square":
        fs, mv = 16, 40
    else:
        fs, mv = 18, 30
    return template.format(fs=fs, fs_small=max(10, fs - 2), mv=mv)


def _hex_to_ffmpeg_color(color):
    if color and re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
        return "0x" + color.lstrip("#")
    return "white"


def build_filtergraph(clip_format, layout, srt_name, caption_style, watermark_file=None, watermark_color="white"):
    W, H = FORMATS.get(clip_format, FORMATS["vertical"])
    if layout == "fit":
        base = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x1a1a2e,setsar=1[base]")
    elif layout == "blur":
        base = (f"[0:v]split=2[bg0][fg0];"
                f"[bg0]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=20:2[bg];"
                f"[fg0]scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
    else:  # crop — fills the frame, centre-weighted
        base = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[base]"

    style = _caption_style(caption_style, clip_format)
    graph = f"{base};[base]subtitles=filename={srt_name}:force_style='{style}'"
    if watermark_file:
        font = _font_file()
        font_opt = f"fontfile={font}:" if font else ""
        graph += (f",drawtext={font_opt}textfile={watermark_file}:fontcolor={watermark_color}@0.65:"
                  f"fontsize={max(22, H // 48)}:x=(w-text_w)/2:y=h-text_h-{max(40, H // 24)}:"
                  "shadowcolor=black@0.5:shadowx=2:shadowy=2")
    graph += "[v]"
    return graph


def render_segment(video_path, start, end, srt_path, out_path, clip_format="vertical", layout="crop",
                   caption_style="bold", watermark_text=None, watermark_color=None, audio_path=None):
    """Cut [start, end] from video_path, reframe, burn captions + optional
    watermark. If audio_path is given it replaces the original audio (dubs).
    Runs with cwd=out_dir and relative filenames so no path ever needs
    escaping inside the filtergraph."""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    wm_name = None
    if watermark_text:
        wm_name = os.path.basename(out_path) + ".wm.txt"
        with open(os.path.join(out_dir, wm_name), "w") as f:
            f.write(watermark_text)
    graph = build_filtergraph(clip_format, layout, os.path.basename(srt_path), caption_style,
                              watermark_file=wm_name, watermark_color=_hex_to_ffmpeg_color(watermark_color))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", os.path.abspath(video_path)]
    if audio_path:
        cmd += ["-i", os.path.abspath(audio_path)]
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    cmd += ["-map", "1:a:0"] if audio_path else ["-map", "0:a:0?"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    if audio_path:
        cmd += ["-shortest"]
    cmd += [os.path.basename(out_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=out_dir, timeout=1800)
        ok, err = result.returncode == 0 and os.path.exists(out_path), result.stderr[-800:]
    except subprocess.TimeoutExpired:
        ok, err = False, "render timed out"
    finally:
        if wm_name:
            try:
                os.unlink(os.path.join(out_dir, wm_name))
            except OSError:
                pass
    return ok, err


def clip_bounds(seg, duration, pad_before=0.15, pad_after=0.35):
    start = max(0.0, seg["start"] - pad_before)
    end = seg["end"] + pad_after
    if duration:
        end = min(duration, end)
    return start, end


def render_clip(video_path, seg, out_dir, rank, watermark=True, clip_format="vertical", caption_style="bold",
                layout="crop", watermark_text=None, watermark_color=None, duration=None):
    start, end = clip_bounds(seg, duration)
    virality = seg.get("virality_score", 0)
    stem = f"peakcut_rank{rank}_v{virality}"
    srt_path = os.path.join(out_dir, f"{stem}.srt")
    with open(srt_path, "w") as f:
        f.write(build_word_chunk_srt(seg["words"], start, clip_end=end,
                                     chunk_size=3 if clip_format == "vertical" else 4))
    out_path = os.path.join(out_dir, f"{stem}_{clip_format}.mp4")
    wm = (watermark_text or WATERMARK) if watermark else None
    ok, err = render_segment(video_path, start, end, srt_path, out_path, clip_format=clip_format, layout=layout,
                             caption_style=caption_style, watermark_text=wm, watermark_color=watermark_color)
    return out_path, ok, err


def process_video(video_path, out_dir, n_clips=3, watermark=True, dub_lang=None, clip_format="vertical",
                  caption_style="bold", layout="crop", clip_length="medium", watermark_text=None,
                  watermark_color=None, progress_cb=None):
    def progress(stage, pct):
        if progress_cb:
            try:
                progress_cb(stage, pct)
            except Exception:
                pass

    os.makedirs(out_dir, exist_ok=True)
    progress("transcribing", 5)
    segments, duration, source_lang = transcribe(video_path)
    if not segments:
        raise RuntimeError("no speech detected")

    if dub_lang and source_lang != "en":
        raise ValueError(f"Dubbing currently only supports English source videos (detected: {source_lang}).")
    if dub_lang:
        unload_model()

    progress("finding_moments", 45)
    timeline = loudness_timeline(video_path)
    scored = score_candidates(video_path, segments, clip_length=clip_length, timeline=timeline, duration=duration)
    top = pick_top_n(scored, n=n_clips)

    manifest = []
    for i, seg in enumerate(top, 1):
        progress("rendering", 55 + int(40 * (i - 1) / max(1, len(top))))
        start, end = clip_bounds(seg, duration)
        if dub_lang:
            import dub_lib
            out_path, translated_text, ok, err = dub_lib.render_dubbed_clip(
                video_path, seg, out_dir, i, dub_lang, source_lang=source_lang, watermark=watermark,
                clip_format=clip_format, caption_style=caption_style, layout=layout,
                watermark_text=watermark_text, watermark_color=watermark_color, duration=duration,
            )
            text = translated_text
        else:
            out_path, ok, err = render_clip(video_path, seg, out_dir, i, watermark=watermark, clip_format=clip_format,
                                            caption_style=caption_style, layout=layout, watermark_text=watermark_text,
                                            watermark_color=watermark_color, duration=duration)
            text = seg["text"]
        srt_file = os.path.basename(out_path).rsplit("_", 1)[0] + ".srt"
        manifest.append({
            "rank": i, "start": round(start, 2), "end": round(end, 2),
            "score": seg["composite"], "virality_score": seg["virality_score"],
            "text": text, "title": suggest_title(text), "hashtags": suggest_hashtags(text),
            "file": os.path.basename(out_path),
            "srt": srt_file if os.path.exists(os.path.join(out_dir, srt_file)) else None,
            "ok": ok, "error": None if ok else err,
        })

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    progress("done", 100)
    return {"duration": duration, "clips": manifest, "language": source_lang}
