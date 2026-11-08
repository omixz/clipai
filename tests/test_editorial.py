import json

import pytest

from autopilot import editorial, run, scripts, video, voice


def test_script_bank_is_valid_and_honest():
    assert len(scripts.SCRIPTS) >= 10
    ids = [s["id"] for s in scripts.SCRIPTS]
    assert len(ids) == len(set(ids))
    for s in scripts.SCRIPTS:
        assert scripts.script_problems(s) == [], (s["id"], scripts.script_problems(s))


@pytest.mark.parametrize("say", ["Get rich with Peakcut.", "Unlimited clips, guaranteed.", "Better than Opus."])
def test_guard_rejects_risky_scripts(say):
    bad = {"id": "x", "title": "x", "beats": [{"say": say, "key": say.split()[0], "style": "bold", "scene": "dark"}] * 3}
    assert scripts.script_problems(bad)


def fake_words(text, t0=0.0, step=0.3):
    out, t = [], t0
    for w in text.replace(",", "").replace(".", "").split():
        out.append({"w": w, "start": round(t, 2), "end": round(t + step * .8, 2)})
        t += step
    return out


def test_align_and_roles():
    s = scripts.SCRIPTS[0]
    words = fake_words(" ".join(b["say"] for b in s["beats"]))
    per_beat = voice.align(s["beats"], words)
    assert [len(p) for p in per_beat] == [len(b["say"].replace(",", "").replace(".", "").split()) for b in s["beats"]]
    tl = editorial.build_timeline(s, per_beat, words[-1]["end"])
    first = tl["beats"][0]
    roles = [(t["text"], t["role"]) for t in first["tokens"]]
    assert roles == [("Nobody", "lead"), ("watches", "lead"), ("your", "lead"), ("whole", "key"), ("podcast", "key")]
    starts = [b["start"] for b in tl["beats"]]
    assert starts == sorted(starts) and tl["beats"][-1]["end"] == tl["end"]
    assert tl["duration"] == pytest.approx(tl["end"] + editorial.END_CARD)


def test_align_tolerates_voice_merging_words():
    beats = [{"say": "It's twenty twenty six now."}, {"say": "Next line here."}]
    words = [{"w": w, "start": i * .3, "end": i * .3 + .2} for i, w in enumerate(["It's", "2026", "now", "Next", "line", "here"])]
    per = voice.align(beats, words)
    assert [w["w"] for w in per[1]] == ["next", "line", "here"]


def test_pick_rotates_scripts():
    ledger = []
    seen = []
    for _ in range(len(scripts.SCRIPTS)):
        s = scripts.pick(ledger)
        seen.append(s["id"])
        ledger.append({"script": s["id"]})
    assert len(set(seen)) == len(scripts.SCRIPTS)


def _stub_minimal(monkeypatch):
    def fake_render(spec, mp4, work, **kw):
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"v")
        return {"duration": 7.5, "cues": []}
    monkeypatch.setattr(video, "render", fake_render)
    monkeypatch.setattr(video, "contact_sheet", lambda *a: None)


def test_editorial_failure_falls_back_to_minimal(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setenv("AUTOPILOT_BEATS", "0")
    _stub_minimal(monkeypatch)
    monkeypatch.setattr(editorial, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("voice down")))
    assert run.main(["--dry-run", "--out", str(tmp_path / "out")]) == 0
    assert list((tmp_path / "out").glob("*/reel.mp4"))


def test_editorial_post_records_script(monkeypatch, tmp_path):
    from autopilot import publish
    monkeypatch.setattr(run, "LEDGER", tmp_path / "ledger.json")
    monkeypatch.setenv("AUTOPILOT_BEATS", "0")
    _stub_minimal(monkeypatch)

    def fake_editorial(script, mp4, work, **kw):
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"v")
        return {"duration": 16.0, "voice": "v", "footage_ids": [1, 2], "timeline": {}, "track": None}

    monkeypatch.setattr(editorial, "render", fake_editorial)
    monkeypatch.setattr(publish, "host_on_github_release", lambda p, n: "https://h/" + n)
    texts = {}
    monkeypatch.setattr(publish, "post_video", lambda platform, url, text, **k: texts.setdefault(platform, text) and "id")
    assert run.main(["--out", str(tmp_path / "out")]) == 0
    rec = json.loads((tmp_path / "ledger.json").read_text())["posts"][-1]
    assert rec["motif"] == "editorial" and rec["script"] == scripts.SCRIPTS[0]["id"] and rec["footage_ids"] == [1, 2]
    assert scripts.SCRIPTS[0]["title"] in texts["tiktok"] and "link in bio" in texts["tiktok"]


def test_due_check_catches_missed_slots():
    import datetime as dt
    from autopilot import due
    U = dt.timezone.utc
    at = lambda h, m=0, d=8: dt.datetime(2026, 11, d, h, m, tzinfo=U)  # noqa: E731
    slots = [15, 23]
    assert not due.is_due(at(14, 17), [], slots)                      # before the slot
    assert due.is_due(at(15, 17), [], slots)                          # on time
    assert due.is_due(at(17, 17), [], slots)                          # the 15:17 run was skipped -> catch up
    assert not due.is_due(at(18, 17), [], slots)                      # window over
    assert not due.is_due(at(16, 17), [{"at": at(15, 20).isoformat()}], slots)   # already posted this slot
    assert due.is_due(at(1, 17, d=9), [{"at": at(15, 20).isoformat()}], slots)   # 23:00 window crosses midnight


def test_photo_and_video_backgrounds_composite(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    img, vid = tmp_path / "p.jpg", tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1200x1800", "-frames:v", "1", str(img)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=720x1280:r=30:d=4", "-pix_fmt", "yuv420p", str(vid)], check=True)
    tl = {"beats": [{"start": 0, "end": 1.0}, {"start": 1.0, "end": 2.0}, {"start": 2.0, "end": 2.6}], "end": 2.6}
    out = tmp_path / "bg.mp4"
    editorial.background(tl, {0: img, 2: vid}, out)
    dur = editorial._probe(out)
    assert dur == pytest.approx(2.6 + editorial.END_CARD, abs=0.15)


def test_photo_scene_falls_back_without_key(monkeypatch):
    from autopilot import footage
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    assert footage.find_any("anything", set(), None) is None
    for s in scripts.SCRIPTS:
        for b in s["beats"]:
            if b["scene"].startswith(("footage:", "photo:")):
                assert b.get("alt") in scripts.SCENES, (s["id"], b)
