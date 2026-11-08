import json
import shutil
import subprocess

import pytest

from autopilot import content, publish, run, video


def test_every_bank_line_passes_the_claims_guard():
    for motif, pairs in content.BANK.items():
        assert len(pairs) >= 5, motif
        for pair in pairs:
            assert content.pair_ok(pair), (motif, pair, [content.line_ok(l) for l in pair])
    assert set(content.BANK) == set(video.MOTIFS) == set(content.CAPTIONS)


def test_instagram_text_keeps_hashtags_for_the_first_comment():
    c = content.pick([], motif="growth", seed=3, allow_generate=False)
    assert "#" not in content.post_text(c, "instagram") and "#" in content.post_text(c, "tiktok")
    assert c["theme"] == "gold"


@pytest.mark.parametrize("line", [
    "get rich with clips.", "earn while you sleep.", "make money posting.", "passive income.", "quit your job.",
    "this will go viral.", "the best tool ever.", "Uppercase line.", "no full stop", "unlimited clips.",
    "better than opus.", "free forever.", "100 clips a day.", "wow!", "a line that is far far too long to fit.",
])
def test_guard_rejects_risky_lines(line):
    assert not content.line_ok(line)[0]


def test_pick_rotates_motifs_and_never_repeats_lines():
    ledger, seen = [], set()
    for i in range(len(content.BANK) * 3):
        c = content.pick(ledger, seed=i, allow_generate=False)
        assert tuple(c["lines"]) not in seen
        seen.add(tuple(c["lines"]))
        ledger.append({"motif": c["motif"], "lines": c["lines"]})
    # every motif got used evenly
    counts = {m: sum(1 for p in ledger if p["motif"] == m) for m in content.BANK}
    assert set(counts.values()) == {3}
    assert any(p for i, p in enumerate(ledger) if i % 3 == 2)  # light every third


def test_post_text_has_cta_and_hashtags():
    c = content.pick([], motif="computer", seed=1, allow_generate=False)
    for platform in ("tiktok", "instagram"):
        text = content.post_text(c, platform)
        assert "link in bio" in text and len(text) < 2200
    assert "#" in content.post_text(c, "tiktok")


def test_generated_lines_are_guarded(monkeypatch, tmp_path):
    monkeypatch.setattr(content, "GENERATED", tmp_path / "gen.json")

    class Resp:
        def raise_for_status(self): pass
        def json(self):
            pairs = [["your clips, finished.", "every sentence whole."], ["this goes viral!", "trust us."]]
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(pairs)}]}}]}

    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    fresh = content.generate_lines("sentence", api_key="x")
    assert fresh == [["your clips, finished.", "every sentence whole."]]
    assert content.load_generated()["sentence"] == fresh


def test_buffer_post_falls_back_through_input_shapes(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_INSTAGRAM_CHANNEL_ID", "chan")
    calls = []

    class R:
        status_code = 200
        def __init__(self, ok): self.ok = ok
        def json(self):
            if self.ok:
                return {"data": {"createPost": {"post": {"id": "p1"}}}}
            return {"data": {"createPost": {"message": "unknown field"}}}

    def fake_post(url, **kw):
        inp = kw["json"]["variables"]["input"]
        calls.append(inp)
        # pretend the schema rejects the optional extras, accepting the minimal reel shape
        return R(ok="isAiGenerated" not in inp["metadata"]["instagram"])

    monkeypatch.setattr(publish.requests, "post", fake_post)
    assert publish.post_video("instagram", "https://x/v.mp4", "text", "title", first_comment="#tags") == "p1"
    assert calls[0]["metadata"]["instagram"] == {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": False,
                                                 "firstComment": "#tags"}
    assert calls[-1]["metadata"]["instagram"] == {"type": "reel", "shouldShareToFeed": True}
    assert calls[-1]["channelId"] == "chan" and calls[-1]["assets"][0]["video"]["url"] == "https://x/v.mp4"
    assert all(c["schedulingType"] == "automatic" for c in calls)


def test_channel_discovery_and_trend_handoff(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.delenv("BUFFER_TIKTOK_CHANNEL_ID", raising=False)
    publish._channel_cache.clear()
    posted = []

    class R:
        status_code = 200
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data

    def fake_post(url, **kw):
        q = kw["json"]["query"]
        if "organizations" in q:
            return R({"data": {"account": {"organizations": [{"id": "org1"}]}}})
        if "channels(" in q:
            return R({"data": {"channels": [{"id": "ig1", "service": "instagram"}, {"id": "tt1", "service": "tiktok"}]}})
        posted.append(kw["json"]["variables"]["input"])
        return R({"data": {"createPost": {"post": {"id": "p9"}}}})

    monkeypatch.setattr(publish.requests, "post", fake_post)
    assert publish.post_video("tiktok", "u", "t", "t", handoff=True) == "p9"
    assert posted[0]["channelId"] == "tt1" and posted[0]["schedulingType"] == "notification"


def test_post_requires_secrets(monkeypatch):
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BUFFER_API_KEY"):
        publish.post_video("tiktok", "u", "t", "t")


def test_run_posts_each_platform_independently(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "LEDGER", tmp_path / "ledger.json")
    def fake_render(spec, mp4, work, **kw):
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"v")
        return {"duration": 7.5, "cues": []}

    monkeypatch.setattr(video, "render", fake_render)
    monkeypatch.setattr(video, "contact_sheet", lambda *a: None)
    monkeypatch.setattr(publish, "host_on_github_release", lambda path, name: "https://host/" + name)

    def fake_post(platform, *a, **k):
        if platform == "instagram":
            raise RuntimeError("channel disconnected")
        return "tt1"

    monkeypatch.setattr(publish, "post_video", fake_post)
    code = run.main(["--out", str(tmp_path / "out")])
    assert code == 2  # partial success
    ledger = json.loads((tmp_path / "ledger.json").read_text())["posts"]
    assert ledger[0]["platforms"]["tiktok"]["ok"] and not ledger[0]["platforms"]["instagram"]["ok"]
    # a second run right away is skipped (no double posting)
    assert run.main(["--out", str(tmp_path / "out")]) == 0
    assert len(json.loads((tmp_path / "ledger.json").read_text())["posts"]) == 1


def _can_render():
    if not (shutil.which("node") and shutil.which("ffmpeg")):
        return False
    probe = "try{require('playwright')}catch(e){require(require('child_process').execSync('npm root -g').toString().trim()+'/playwright')}"
    return subprocess.run(["node", "-e", probe], capture_output=True, cwd=str(video.HERE)).returncode == 0


@pytest.mark.skipif(not _can_render(), reason="needs node + playwright + ffmpeg")
def test_render_real_reel(tmp_path):
    mp4 = tmp_path / "r.mp4"
    meta = video.render({"motif": "credits", "lines": ["no credits.", "no expiry dates."]}, mp4, tmp_path / "w")
    assert meta["frames"] == round(meta["duration"] * 30)
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(mp4)],
                         capture_output=True, text=True).stdout
    streams = json.loads(out)["streams"]
    assert {"codec_type": "video", "width": 1080, "height": 1920} in streams
    assert any(s["codec_type"] == "audio" for s in streams)


def _click_track(path, bpm=120.0, first=0.25, seconds=30, sr=22050):
    import wave
    import numpy as np
    y = np.zeros(int(sr * seconds))
    period = 60 / bpm
    for b in np.arange(first, seconds, period):
        i = int(b * sr)
        n = int(0.12 * sr)
        tt = np.arange(n) / sr
        y[i:i + n] += np.sin(2 * np.pi * 60 * tt) * np.exp(-tt * 25)
        h = int((b + period / 2) * sr)
        if h + 400 < len(y):
            y[h:h + 400] += 0.2 * np.random.default_rng(int(b * 100)).standard_normal(400)
    y /= np.abs(y).max() * 1.2
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((y * 32767).astype("<i2").tobytes())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_beat_detection_and_warp(tmp_path):
    import numpy as np
    from autopilot import music
    track = tmp_path / "t.wav"
    _click_track(track, bpm=120.0, first=0.25)
    info = music.analyze(track)
    assert abs(info["bpm"] - 120) < 1.5
    true = np.arange(0.25, 30, 0.5)
    err = np.mean([np.min(np.abs(true - b)) for b in info["beats"]])
    assert err < 0.04, err  # within ~one video frame
    anchors = music.plan_warp([0.3, 1.75, 2.2, 3.15, 6.0], 7.4, info["period"])
    assert anchors[0] == [0.0, 0.0] and len(anchors) >= 3
    for v, s in anchors[1:]:
        assert abs(v / info["period"] - round(v / info["period"])) < 1e-3  # every anchor on a beat
    # the warp is monotonic and invertible
    assert music.video_time(anchors, 6.0) > music.video_time(anchors, 3.15)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.parametrize("style", ["phonk", "house", "lofi"])
def test_generated_beats_have_an_exact_grid(tmp_path, style):
    import numpy as np
    from autopilot import beats, music
    r = beats.generate(style, 12, tmp_path / f"{style}.wav", seed=1)
    info = music.analyze(tmp_path / f"{style}.wav")
    assert info["bpm"] == r["bpm"]
    assert np.allclose(np.diff(info["beats"]), 60 / r["bpm"])
    assert beats.pick_style(["phonk", "house"]) == "lofi"
