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


@pytest.mark.parametrize("line", [
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
        assert "link in bio" in text and "#" in text and len(text) < 2200


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
        return R(ok="metadata" not in inp)  # pretend the schema rejects instagram metadata

    monkeypatch.setattr(publish.requests, "post", fake_post)
    assert publish.post_video("instagram", "https://x/v.mp4", "text", "title") == "p1"
    assert calls[0]["metadata"]["instagram"]["type"] == "reel"
    assert calls[-1]["channelId"] == "chan" and calls[-1]["assets"][0]["video"]["url"] == "https://x/v.mp4"


def test_post_requires_secrets(monkeypatch):
    monkeypatch.delenv("BUFFER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="BUFFER_API_KEY"):
        publish.post_video("tiktok", "u", "t", "t")


def test_run_posts_each_platform_independently(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setattr(video, "render", lambda spec, mp4, work: mp4.parent.mkdir(parents=True, exist_ok=True) or mp4.write_bytes(b"v") or {})
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
