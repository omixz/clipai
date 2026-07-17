import json
import re
import subprocess
import os
from faster_whisper import WhisperModel

WATERMARK = "FREE PLAN — clipai.app"

_model = None


def get_model():
    global _model
    if _model is None:
        _model = WhisperModel("small", device="cpu", compute_type="int8")
    return _model


def transcribe(video_path):
    model = get_model()
    segments, info = model.transcribe(video_path, word_timestamps=True)
    out = []
    for seg in segments:
        words = [{"word": w.word.strip(), "start": w.start, "end": w.end} for w in (seg.words or [])]
        out.append({"start": seg.start, "end": seg.end, "text": seg.text.strip(), "words": words})
    return out, info.duration


def audio_energy_db(video_path, start, end):
    duration = max(0.1, end - start)
    cmd = [
        "ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(duration),
        "-i", video_path, "-af", "astats=metadata=1:reset=1", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    matches = re.findall(r"RMS level dB:\s*(-?\d+\.?\d*)", result.stderr)
    vals = [float(m) for m in matches if m != "-inf"]
    return sum(vals) / len(vals) if vals else -60.0


def text_signal_score(text):
    score = 0.0
    score += text.count("!") * 2.0
    score += text.count("?") * 1.0
    contrast_words = ["but", "yet", "however", "so", "because"]
    score += sum(text.lower().count(w) for w in contrast_words) * 1.5
    words = text.split()
    if words:
        avg_word_len = sum(len(w) for w in words) / len(words)
        score += max(0, 6 - avg_word_len)
    score += min(len(words), 20) * 0.1
    return score


def score_candidates(video_path, segments, min_dur=3.0, max_dur=16.0):
    scored = []
    for seg in segments:
        dur = seg["end"] - seg["start"]
        if dur < min_dur or dur > max_dur:
            continue
        energy = audio_energy_db(video_path, seg["start"], seg["end"])
        energy_score = max(0.0, (energy + 40) / 4)
        text_score = text_signal_score(seg["text"])
        composite = energy_score + text_score
        scored.append({**seg, "energy_db": round(energy, 2), "composite": round(composite, 2)})
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


def srt_timestamp(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_word_chunk_srt(words, clip_start, chunk_size=4):
    lines = []
    idx = 1
    for i in range(0, len(words), chunk_size):
        chunk = words[i:i + chunk_size]
        if not chunk:
            continue
        start = chunk[0]["start"] - clip_start
        end = chunk[-1]["end"] - clip_start
        if end <= start:
            end = start + 0.4
        text = " ".join(w["word"] for w in chunk)
        lines.append(f"{idx}\n{srt_timestamp(max(0, start))} --> {srt_timestamp(end)}\n{text}\n")
        idx += 1
    return "\n".join(lines)


def render_clip(video_path, seg, out_dir, rank):
    srt_text = build_word_chunk_srt(seg["words"], seg["start"])
    srt_path = os.path.join(out_dir, f"clip_{rank}.srt")
    with open(srt_path, "w") as f:
        f.write(srt_text)

    out_path = os.path.join(out_dir, f"clip_{rank}.mp4")
    vf = (
        "scale=1080:-2,pad=1080:1920:0:(1920-ih)/2:color=0x1a1a2e,"
        f"subtitles={srt_path}:force_style='FontName=DejaVu Sans,FontSize=30,"
        "PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=1,Outline=3,"
        "Bold=1,Alignment=2,MarginV=140',"
        f"drawtext=text='{WATERMARK}':fontcolor=white@0.6:fontsize=18:"
        "x=(w-text_w)/2:y=h-70"
    )
    cmd = [
        "ffmpeg", "-y", "-ss", str(seg["start"]), "-to", str(seg["end"]),
        "-i", video_path, "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return out_path, result.returncode == 0, result.stderr[-800:]


def process_video(video_path, out_dir, n_clips=3):
    os.makedirs(out_dir, exist_ok=True)
    segments, duration = transcribe(video_path)
    scored = score_candidates(video_path, segments)
    top = pick_top_n(scored, n=n_clips)

    manifest = []
    for i, seg in enumerate(top, 1):
        out_path, ok, err = render_clip(video_path, seg, out_dir, i)
        manifest.append({
            "rank": i, "start": seg["start"], "end": seg["end"],
            "score": seg["composite"], "text": seg["text"],
            "file": os.path.basename(out_path), "ok": ok,
            "error": None if ok else err,
        })

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    return {"duration": duration, "clips": manifest}
