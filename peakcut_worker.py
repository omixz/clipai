#!/usr/bin/env python3
"""Peakcut worker: make clips on your own hardware.

Two ways to use it:

1. Paired with your Peakcut account (upload from anywhere, your computer does
   the work). In Peakcut: Settings -> Your computers -> Pair this computer,
   then run the command it shows:

       python peakcut_worker.py --server https://your-peakcut-site --token pd_...

   Pick "My computer" when uploading. Videos your computer processes don't use
   your monthly allowance. Leave this running; it polls for work.

2. Fully offline, no account, no upload:

       python peakcut_worker.py --local podcast.mp4 --out clips/ --format vertical --clips 5

GPU: Whisper runs on CUDA automatically if a usable NVIDIA GPU is found, and
rendering uses NVENC / Quick Sync / VideoToolbox / AMF when ffmpeg supports
them (falls back to CPU otherwise). Override with --whisper-device / --encoder.

The site owner can also run this with WORKER_TOKEN (--token <that secret>)
to add their own machines as capacity for the whole cloud queue.

Setup: Python 3.10+, ffmpeg 5+, then `pip install -r requirements-worker.txt`
(this file sits next to pipeline_lib.py — get both from /worker/bundle.zip on
your Peakcut site or from the repository).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

VERSION = "1.0"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


# ── Hardware detection ───────────────────────────────────────────────────────

def detect_cuda() -> bool:
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def gpu_name() -> str:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except Exception:
        pass
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "Apple Silicon"
    return ""


def detect_encoder() -> str:
    """First hardware H.264 encoder that actually works on this machine
    (being listed by ffmpeg isn't enough — the driver must be present)."""
    candidates = ["h264_nvenc", "h264_qsv", "h264_amf"]
    if platform.system() == "Darwin":
        candidates.insert(0, "h264_videotoolbox")
    for enc in candidates:
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                 "-c:v", enc, "-f", "null", "-"], capture_output=True, timeout=20)
            if r.returncode == 0:
                return enc
        except Exception:
            continue
    return "libx264"


def configure(args) -> dict:
    """Sets the pipeline's env knobs. Must run before pipeline_lib is imported."""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is not installed or not on PATH (https://ffmpeg.org/download.html).")
    cuda = detect_cuda() if args.whisper_device in ("auto", "cuda") else False
    device = "cuda" if (args.whisper_device == "cuda" or (args.whisper_device == "auto" and cuda)) else "cpu"
    # Own hardware usually has far more headroom than the 512MB server, so
    # default to a more accurate model: better captions, better clip picks.
    model = args.model or ("small" if device == "cuda" else "base")
    encoder = detect_encoder() if args.encoder == "auto" else args.encoder
    os.environ["WHISPER_MODEL"] = model
    os.environ["WHISPER_DEVICE"] = device
    os.environ["PEAKCUT_VIDEO_ENCODER"] = encoder
    os.environ.pop("HF_HUB_OFFLINE", None)  # let the model download on first run
    info = {"hostname": platform.node()[:60], "gpu": gpu_name() or ("CUDA" if device == "cuda" else "none"),
            "whisper_model": f"{model} ({device})", "encoder": encoder, "version": VERSION}
    print(f"[peakcut] whisper={model} on {device} · encoder={encoder} · gpu={info['gpu']}")
    return info


# ── Offline mode ─────────────────────────────────────────────────────────────

def run_local(args):
    configure(args)
    import pipeline_lib
    out_root = Path(args.out).expanduser()
    for src in args.local:
        src_path = Path(src).expanduser()
        if not src_path.is_file():
            print(f"[peakcut] skipping {src}: not a file")
            continue
        out_dir = out_root / src_path.stem if len(args.local) > 1 else out_root
        print(f"[peakcut] {src_path.name} -> {out_dir}")
        t0 = time.time()
        result = pipeline_lib.process_video(
            str(src_path), str(out_dir), n_clips=args.clips, watermark=not args.no_watermark,
            clip_format=args.format, caption_style=args.captions, layout=args.layout, clip_length=args.length,
            watermark_text=args.watermark_text, dub_lang=args.dub,
            progress_cb=lambda stage, pct: print(f"  {stage:<16} {pct:>3}%"),
        )
        for c in result["clips"]:
            status = "ok " if c["ok"] else "ERR"
            print(f"  [{status}] #{c['rank']} {c['virality_score']}/100  {c['start']:.1f}-{c['end']:.1f}s  {c['title']}")
            if not c["ok"]:
                print(f"        {c['error']}")
        print(f"[peakcut] done in {time.time() - t0:.0f}s")


# ── Paired mode ──────────────────────────────────────────────────────────────

class Server:
    def __init__(self, base: str, token: str):
        import httpx
        self.base = base.rstrip("/")
        self.http = httpx.Client(headers={"Authorization": f"Bearer {token}", "User-Agent": f"peakcut-worker/{VERSION}"},
                                 timeout=httpx.Timeout(60, read=600), follow_redirects=True)

    def url(self, path: str) -> str:
        return self.base + path

    def post_json(self, path: str, body: dict):
        return self.http.post(self.url(path), json=body)


class Heartbeat(threading.Thread):
    """Keeps the job's lease alive during long steps (a 2-hour podcast can
    transcribe for a while without a progress callback)."""

    def __init__(self, server: Server, job_id: str):
        super().__init__(daemon=True)
        self.server, self.job_id = server, job_id
        self.stage, self.pct = "transcribing", 5
        self.stop = threading.Event()

    def update(self, stage, pct):
        self.stage, self.pct = stage, pct
        self.send()

    def send(self):
        try:
            self.server.post_json(f"/worker/jobs/{self.job_id}/progress", {"stage": self.stage, "progress": self.pct})
        except Exception:
            pass

    def run(self):
        while not self.stop.wait(30):
            self.send()


def process_one(server: Server, spec: dict):
    import pipeline_lib
    job_id = spec["job_id"]
    opts = spec["options"]
    print(f"[peakcut] job {job_id}: {opts.get('clip_format')} · {opts.get('clip_length')} · {opts.get('n_clips')} clips")
    hb = Heartbeat(server, job_id)
    hb.start()
    work = Path(tempfile.mkdtemp(prefix=f"peakcut_{job_id}_"))
    try:
        src = work / f"input{spec.get('input_ext') or '.mp4'}"
        with server.http.stream("GET", server.url(spec["input_url"])) as r:
            r.raise_for_status()
            with open(src, "wb") as f:
                for chunk in r.iter_bytes(1024 * 1024):
                    f.write(chunk)
        out_dir = work / "out"
        try:
            result = pipeline_lib.process_video(
                str(src), str(out_dir), n_clips=opts.get("n_clips") or 3, watermark=spec.get("watermark", True),
                dub_lang=opts.get("dub_lang"), clip_format=opts.get("clip_format") or "vertical",
                caption_style=opts.get("caption_style") or "bold", layout=opts.get("layout") or "crop",
                clip_length=opts.get("clip_length") or "medium", watermark_text=opts.get("watermark_text"),
                watermark_color=opts.get("watermark_color"), progress_cb=hb.update,
            )
        except ValueError as e:
            server.post_json(f"/worker/jobs/{job_id}/fail", {"error": str(e), "user_error": True})
            print(f"[peakcut] job {job_id} rejected: {e}")
            return
        clips = [c for c in result["clips"] if c.get("ok")]
        if not clips:
            raise RuntimeError("no usable clips found")
        hb.update("uploading", 96)
        files = []
        handles = []
        try:
            for c in clips:
                for name in filter(None, (c["file"], c.get("srt"))):
                    fh = open(out_dir / name, "rb")
                    handles.append(fh)
                    files.append(("files", (name, fh, "video/mp4" if name.endswith(".mp4") else "application/x-subrip")))
            data = {"manifest": json.dumps(clips), "duration": str(result.get("duration") or 0),
                    "language": result.get("language") or ""}
            r = server.http.post(server.url(f"/worker/jobs/{job_id}/result"), data=data, files=files)
        finally:
            for fh in handles:
                fh.close()
        if r.status_code >= 400:
            raise RuntimeError(f"upload rejected ({r.status_code}): {r.text[:200]}")
        print(f"[peakcut] job {job_id} done — {len(clips)} clips uploaded")
    except Exception as e:
        print(f"[peakcut] job {job_id} failed: {e}")
        try:
            server.post_json(f"/worker/jobs/{job_id}/fail", {"error": str(e)[:300]})
        except Exception:
            pass
    finally:
        hb.stop.set()
        shutil.rmtree(work, ignore_errors=True)


def run_paired(args):
    info = configure(args)
    server = Server(args.server, args.token)
    print(f"[peakcut] connected to {server.base} — waiting for videos (Ctrl+C to stop)")
    backoff = args.poll
    while True:
        try:
            r = server.post_json("/worker/claim", info)
            if r.status_code == 401:
                sys.exit("[peakcut] this token was rejected — pair the computer again in Settings.")
            if r.status_code == 200:
                process_one(server, r.json())
                backoff = args.poll
                if args.once:
                    return
                continue
            if r.status_code != 204:
                print(f"[peakcut] server said {r.status_code}: {r.text[:120]}")
            if args.once:
                print("[peakcut] no jobs waiting")
                return
            backoff = args.poll
        except KeyboardInterrupt:
            raise
        except Exception as e:
            backoff = min(120, max(args.poll, backoff * 2))
            print(f"[peakcut] can't reach the server ({e.__class__.__name__}); retrying in {backoff}s")
        time.sleep(backoff)


def main(argv=None):
    p = argparse.ArgumentParser(description="Make Peakcut clips on your own hardware.")
    p.add_argument("--server", default=os.environ.get("PEAKCUT_SERVER"), help="your Peakcut site URL (paired mode)")
    p.add_argument("--token", default=os.environ.get("PEAKCUT_TOKEN"), help="device token from Settings (paired mode)")
    p.add_argument("--local", nargs="+", metavar="VIDEO", help="process these files offline instead")
    p.add_argument("--out", default="peakcut_clips", help="output folder for --local")
    p.add_argument("--format", default="vertical", choices=["vertical", "square", "horizontal"])
    p.add_argument("--layout", default="crop", choices=["crop", "blur", "fit"])
    p.add_argument("--length", default="medium", choices=["short", "medium", "long"])
    p.add_argument("--captions", default="bold", choices=["bold", "boxed", "neon", "outline", "subtle"])
    p.add_argument("--clips", type=int, default=5)
    p.add_argument("--dub", choices=["es", "fr", "pt"], help="dub English videos (needs piper-tts + deep-translator)")
    p.add_argument("--no-watermark", action="store_true", help="--local only")
    p.add_argument("--watermark-text", help="--local only: custom watermark text")
    p.add_argument("--model", help="Whisper model: tiny, base, small, medium, large-v3 (default: small on GPU, base on CPU)")
    p.add_argument("--whisper-device", default="auto", choices=["auto", "cpu", "cuda"])
    p.add_argument("--encoder", default="auto", help="auto, libx264, h264_nvenc, h264_qsv, h264_videotoolbox, h264_amf")
    p.add_argument("--poll", type=int, default=5, help="seconds between checks for new work")
    p.add_argument("--once", action="store_true", help="process at most one job, then exit")
    args = p.parse_args(argv)

    if args.local:
        return run_local(args)
    if not (args.server and args.token):
        p.error("give --server and --token (from Settings -> Your computers), or use --local FILE")
    try:
        run_paired(args)
    except KeyboardInterrupt:
        print("\n[peakcut] stopped")


if __name__ == "__main__":
    main()
