"""Completion emails via Resend's REST API (no SDK needed — httpx is already
a dependency). Soft-required, same pattern as Stripe/AdSense/Google Sign-In:
while RESEND_API_KEY is still the placeholder, callers just skip this
entirely and nothing changes. This also doubles as the start of an email
list — a real monetization lever (re-marketing free users into Pro) that
costs nothing to build alongside the notification itself.
"""
import logging

import httpx

import config

log = logging.getLogger("clipai.email")

RESEND_URL = "https://api.resend.com/emails"


def _send(to_email: str, subject: str, html: str):
    if not config.EMAIL_CONFIGURED:
        return
    try:
        resp = httpx.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={"from": config.EMAIL_FROM, "to": [to_email], "subject": subject, "html": html},
            timeout=10,
        )
        if resp.status_code >= 400:
            log.warning("Resend send failed (%s): %s", resp.status_code, resp.text[:300])
    except Exception:
        log.exception("failed to send completion email to %s", to_email)


def send_done_email(to_email: str, clip_urls: list[str], duration: float, is_pro: bool):
    links = "".join(f'<li><a href="{config.SITE_URL}{u}">Clip {i+1}</a></li>' for i, u in enumerate(clip_urls))
    upsell = "" if is_pro else (
        '<p style="margin-top:24px;padding:16px;background:#f6f5fb;border-radius:10px;">'
        'Want no watermark and unlimited videos? '
        f'<a href="{config.SITE_URL}/create-checkout-session">Upgrade to Pro — $15/mo</a></p>'
    )
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;">
      <h2>Your clips are ready 🎬</h2>
      <p>Peakcut turned your {round(duration)}s video into {len(clip_urls)} clip(s):</p>
      <ul>{links}</ul>
      <p style="color:#6b6478;font-size:0.85rem;">Links expire in 24 hours — download them soon.</p>
      {upsell}
    </div>
    """
    _send(to_email, "Your Peakcut clips are ready", html)


def send_failed_email(to_email: str, error: str):
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;">
      <h2>Processing failed</h2>
      <p>{error}</p>
      <p><a href="{config.SITE_URL}">Try again</a></p>
    </div>
    """
    _send(to_email, "Peakcut — video processing failed", html)
