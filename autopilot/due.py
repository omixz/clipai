#!/usr/bin/env python3
"""Is a post due right now? GitHub's cron is best-effort — scheduled runs
are often late and sometimes skipped entirely — so the workflow wakes every
hour and this decides: a post is due when we're within WINDOW hours after a
posting slot and nothing has been posted since that slot began. A skipped
slot is simply picked up by the next hourly run.

Slots are UTC hours in AUTOPILOT_SLOTS (default "15,23" ≈ 11am and 7pm
US Eastern). Prints "true"/"false"; with GITHUB_OUTPUT set, also writes
`run=<value>` for later workflow steps. Standard library only (runs before
any installs).
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

LEDGER = Path(__file__).resolve().parent / "state" / "ledger.json"
WINDOW = 3


def slot_start(now: dt.datetime, slots: list[int]) -> dt.datetime | None:
    """Start of the slot window `now` falls in, or None."""
    for h in slots:
        for day in (0, -1):  # a window can cross midnight (e.g. 23:00 + 3h)
            start = (now + dt.timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            if start <= now < start + dt.timedelta(hours=WINDOW):
                return start
    return None


def is_due(now: dt.datetime, posts: list[dict], slots: list[int]) -> bool:
    start = slot_start(now, slots)
    if start is None:
        return False
    for p in reversed(posts):
        try:
            if dt.datetime.fromisoformat(p["at"]) >= start:
                return False
        except Exception:
            continue
    return True


def main() -> int:
    slots = [int(x) % 24 for x in (os.environ.get("AUTOPILOT_SLOTS") or "15,23").split(",") if x.strip().isdigit()]
    try:
        posts = json.loads(LEDGER.read_text()).get("posts", [])
    except Exception:
        posts = []
    due = is_due(dt.datetime.now(dt.timezone.utc), posts, slots)
    print("true" if due else "false")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"run={'true' if due else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
