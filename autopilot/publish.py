"""Hosting + posting. Same route as the YT-AUTO repo, which is proven against a
live Buffer account:

* Buffer's API needs the video at a public URL (no direct uploads), so the
  file is attached to a rolling "marketing-media" GitHub Release of this
  public repo (free; assets pruned after 14 days).
* Buffer's GraphQL createPost publishes it to the connected TikTok and
  Instagram channels. Field names below were confirmed by introspecting the
  live schema (Nov 2026): Instagram needs metadata.instagram.type ("reel")
  and shouldShareToFeed; TikTok takes title + isAiGenerated. Each post still
  falls back through a couple of shapes so one rejected optional field can't
  block it.
* schedulingType "notification" = Buffer pings your phone to publish by hand,
  which is how a trending in-app sound gets added (TIKTOK_TREND_HANDOFF=1).
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


def metadata_variants(platform: str, title: str, ai_generated: bool, first_comment: str = "") -> list[dict | None]:
    if platform == "tiktok":
        return [{"tiktok": {"title": title[:90], "isAiGenerated": ai_generated}}, None]
    if platform == "instagram":
        full = {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": ai_generated}
        if first_comment:
            full["firstComment"] = first_comment[:2000]
        return [{"instagram": full}, {"instagram": {"type": "reel", "shouldShareToFeed": True}}]
    return [None]


_channel_cache: dict = {}


def discover_channel(platform: str, api_key: str) -> str | None:
    """Finds the channel id for `platform` from the account itself, so the
    *_CHANNEL_ID secrets are optional (they win if set)."""
    if platform in _channel_cache:
        return _channel_cache[platform]

    def gql(q):
        r = requests.post(BUFFER_API, timeout=60, json={"query": q},
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        r.raise_for_status()
        return r.json().get("data") or {}

    try:
        orgs = (gql("{ account { organizations { id } } }").get("account") or {}).get("organizations") or []
        for org in orgs:
            chans = gql('{ channels(input: {organizationId: "%s"}) { id service } }' % org["id"]).get("channels") or []
            for ch in chans:
                _channel_cache.setdefault(ch["service"], ch["id"])
    except Exception as e:  # noqa: BLE001
        print(f"[autopilot] channel discovery failed: {e}")
    return _channel_cache.get(platform)


def post_video(platform: str, video_url: str, text: str, title: str, ai_generated: bool = False,
               draft: bool = False, handoff: bool = False, first_comment: str = "") -> str:
    """Posts to the Buffer channel for `platform`; returns Buffer's post id.
    handoff=True uses Buffer's notification publishing: you get a push, open
    the app, add a trending sound and post."""
    api_key = os.environ.get("BUFFER_API_KEY")
    if not api_key:
        raise RuntimeError(f"BUFFER_API_KEY must be set to post to {platform}.")
    channel = os.environ.get(f"BUFFER_{platform.upper()}_CHANNEL_ID") or discover_channel(platform, api_key)
    if not channel:
        raise RuntimeError(f"No {platform} channel is connected to this Buffer account (or set BUFFER_{platform.upper()}_CHANNEL_ID).")
    errors = []
    for mode in ("shareNow", "addToQueue"):
        for meta in metadata_variants(platform, title, ai_generated, first_comment):
            post_input = {"text": text[:2200], "channelId": channel,
                          "schedulingType": "notification" if handoff else "automatic",
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
