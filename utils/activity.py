# utils/activity.py
"""A short, persistent log of the batches this app has run.

Home used to show a "Recent Activity" panel fed by a method nothing ever
called, so the panel was permanently empty -- a dashboard whose only content
is an empty-state message. This is the missing half: the tool tabs record a
line when a batch finishes, and Home reads it back.

Kept deliberately small. It is a JSON list beside presets.json, capped at
`LIMIT` entries, holding a timestamp and a sentence. It is not an audit log
and it does not record file paths: the point is "did that batch run, and
when", answerable at a glance.

Qt-free (CLAUDE.md §8), so tests cover it without a widget.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from utils.presets import app_dir

ACTIVITY_FILE = os.path.join(app_dir(), "activity.json")

# Five rows fit Home without scrolling; a few more are kept so the list
# survives a couple of throwaway batches without losing the interesting one.
LIMIT = 20


def load(path: str = "") -> List[Dict[str, Any]]:
    """Return the log, newest first. A missing or corrupt file reads empty.

    A damaged file is not worth an error dialog on startup -- the log is a
    convenience, and refusing to open the app because a nicety failed to
    parse is worse than starting with an empty list.
    """
    target = path or ACTIVITY_FILE
    if not os.path.exists(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def record(text: str, kind: str = "", path: str = "", when: datetime | None = None) -> List[Dict[str, Any]]:
    """Prepend one entry and return the trimmed log.

    `kind` is the icon name Home draws beside the row ("image", "files",
    "hard-drive"), so the log stays readable at a glance rather than being a
    wall of identical sentences.
    """
    target = path or ACTIVITY_FILE
    stamp = (when or datetime.now()).isoformat(timespec="seconds")
    entries = [{"at": stamp, "kind": kind, "text": text}] + load(target)
    entries = entries[:LIMIT]
    try:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2)
    except OSError:
        # Losing a history line must never take the batch down with it: the
        # work the user asked for has already succeeded by this point.
        pass
    return entries


def clear(path: str = "") -> None:
    target = path or ACTIVITY_FILE
    try:
        os.remove(target)
    except OSError:
        pass


def relative_time(stamp: str, now: datetime | None = None) -> str:
    """Render an ISO timestamp as "just now" / "12 min ago" / a date.

    A person reading a dashboard wants elapsed time, not a timestamp they
    have to subtract from the clock in their head; anything older than a week
    becomes a date, where the exact day matters more than the gap.
    """
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    reference = now or datetime.now()
    seconds = (reference - moment).total_seconds()
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
    if seconds < 604800:
        days = int(seconds // 86400)
        return "yesterday" if days == 1 else f"{days} days ago"
    return moment.strftime("%d %b %Y")
