#!/usr/bin/env python3
"""Setup helper (run by the "Marketing setup check" workflow):
1. lists the channels your BUFFER_API_KEY can see, so you can copy the TikTok
   and Instagram channel ids into repo secrets;
2. shows which TikTok / Instagram metadata fields Buffer's createPost accepts;
3. with --probe, renders a 3-second clip and saves it as a DRAFT on each
   configured channel (nothing is published) to prove the whole chain.
Writes buffer_report.md."""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autopilot import publish  # noqa: E402

KEY = os.environ.get("BUFFER_API_KEY", "")


def gql(query):
    r = requests.post(publish.BUFFER_API, headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                      json={"query": query}, timeout=60)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:500]}


def main():
    out = ["## Buffer setup check", ""]
    if not KEY:
        out.append("**BUFFER_API_KEY is not set.** Create a Buffer account, connect TikTok and Instagram (a Business or "
                   "Creator account), create an API key and add it as the repo secret `BUFFER_API_KEY`.")
    else:
        for q in ("{ account { organizations { id name channels { id name service } } } }", "{ channels { id name service } }"):
            code, data = gql(q)
            out += [f"### Channels (`{q[:40]}…`) → HTTP {code}", "```json", json.dumps(data, indent=1)[:3000], "```"]
            if code == 200 and not data.get("errors"):
                break
        out.append("Copy the TikTok channel id into `BUFFER_TIKTOK_CHANNEL_ID` and the Instagram one into `BUFFER_INSTAGRAM_CHANNEL_ID`.")
        for t in ("PostInputMetaData", "TiktokPostMetadataInput", "InstagramPostMetadataInput"):
            code, data = gql(f'{{ __type(name: "{t}") {{ inputFields {{ name type {{ name kind ofType {{ name }} }} }} }} }}')
            out += [f"### `{t}` fields", "```json", json.dumps(data, indent=1)[:1500], "```"]
        if "--probe" in sys.argv:
            out.append("### Draft probe (nothing is published)")
            try:
                clip = Path("probe.mp4")
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:r=30:d=3",
                                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-shortest", "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)], check=True)
                url = publish.host_on_github_release(clip, "probe.mp4")
                out.append(f"- hosted at {url}")
                for platform in ("tiktok", "instagram"):
                    if not os.environ.get(f"BUFFER_{platform.upper()}_CHANNEL_ID"):
                        out.append(f"- {platform}: channel id secret not set, skipped")
                        continue
                    try:
                        pid = publish.post_video(platform, url, "probe — safe to delete", "probe", draft=True)
                        out.append(f"- ✅ {platform}: draft `{pid}` saved — delete it in Buffer")
                    except Exception as e:  # noqa: BLE001
                        out.append(f"- ❌ {platform}: {e}")
            except Exception:
                out += ["```", traceback.format_exc()[-1500:], "```"]
    Path("buffer_report.md").write_text("\n".join(out))
    print("\n".join(out)[:4000])


if __name__ == "__main__":
    main()
