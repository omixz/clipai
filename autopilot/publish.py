"""Hosting + posting. Same route as the YT-AUTO repo, which is proven against a
live Buffer account:

* Buffer's API needs the video at a public URL (no direct uploads), so the
  file is attached to a rolling "marketing-media" GitHub Release of this
  public repo (free; assets pruned after 14 days).
* Buffer's GraphQL createPost publishes it to the connected TikTok and
  Instagram channels. Like YT-AUTO, each post tries a few input shapes (with
  and without platform metadata, shareNow then addToQueue) so one rejected
  optional field can't block the post. Run autopilot/buffer_check.py to see
  exactly which fields your account's schema accepts.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import requests

BUFFER_API = "https://api.buffer.com"
RELEASE_TAG = "marketing-media"
KEEP_DAYS = 14

CREATE_POST = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id } }
    ... on MutationError { message }
  }
}
"""


def host_on_github_release(path: Path, name: str) -> str:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY are needed to host the video (set automatically in GitHub Actions).")
    api = f"https://api.github.com/repos/{repo}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.get(f"{api}/releases/tags/{RELEASE_TAG}", headers=h, timeout=30)
    if r.status_code == 404:
        r = requests.post(f"{api}/releases", headers=h, timeout=30, json={
            "tag_name": RELEASE_TAG, "name": "Marketing media (auto-managed)", "prerelease": True,
            "body": "Rolling storage for the marketing autopilot's videos; assets older than 14 days are pruned."})
    r.raise_for_status()
    release = r.json()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KEEP_DAYS)
    for a in release.get("assets", []):
        if a["name"] == name or dt.datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")) < cutoff:
            requests.delete(f"{api}/releases/assets/{a['id']}", headers=h, timeout=30)
    up = requests.post(f"https://uploads.github.com/repos/{repo}/releases/{release['id']}/assets",
                       params={"name": name}, headers={**h, "Content-Type": "video/mp4"},
                       data=path.read_bytes(), timeout=600)
    up.raise_for_status()
    return up.json()["browser_download_url"]


def _create_post(post_input: dict, api_key: str) -> tuple[str | None, str]:
    r = requests.post(BUFFER_API, timeout=60,
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                      json={"query": CREATE_POST, "variables": {"input": post_input}})
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    data = r.json()
    result = (data.get("data") or {}).get("createPost") or {}
    post = result.get("post")
    if post and post.get("id"):
        return post["id"], ""
    return None, str(data.get("errors") or result.get("message") or data)[:400]


def metadata_variants(platform: str, title: str, ai_generated: bool) -> list[dict | None]:
    if platform == "tiktok":
        return [{"tiktok": {"title": title[:90], "isAiGenerated": ai_generated}}, None]
    if platform == "instagram":
        return [{"instagram": {"type": "reel", "shouldShareToFeed": True}}, {"instagram": {"type": "reel"}}, None]
    return [None]


def post_video(platform: str, video_url: str, text: str, title: str, ai_generated: bool = False,
               draft: bool = False) -> str:
    """Posts to the Buffer channel for `platform`; returns Buffer's post id."""
    api_key = os.environ.get("BUFFER_API_KEY")
    channel = os.environ.get(f"BUFFER_{platform.upper()}_CHANNEL_ID")
    if not (api_key and channel):
        raise RuntimeError(f"BUFFER_API_KEY and BUFFER_{platform.upper()}_CHANNEL_ID must be set to post to {platform}.")
    errors = []
    for mode in ("shareNow", "addToQueue"):
        for meta in metadata_variants(platform, title, ai_generated):
            post_input = {"text": text[:2200], "channelId": channel, "schedulingType": "automatic",
                          "mode": mode, "needsApproval": False, "assets": [{"video": {"url": video_url}}]}
            if meta:
                post_input["metadata"] = meta
            if draft:
                post_input["saveToDraft"] = True
            post_id, err = _create_post(post_input, api_key)
            if post_id:
                print(f"[autopilot] {platform}: posted (mode={mode}, metadata={'yes' if meta else 'no'}) id={post_id}")
                return post_id
            errors.append(f"{mode}/{'meta' if meta else 'plain'}: {err}")
    raise RuntimeError(f"Buffer rejected the {platform} post: " + " | ".join(errors[-3:]))
