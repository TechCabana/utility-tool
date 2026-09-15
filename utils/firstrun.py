# utils/firstrun.py
"""Whether this profile has seen the welcome panel yet.

A marker file in the app-data directory rather than a `QSettings` key, which
would have been less code. Two reasons it is worth the file:

* The card's condition is "a profile with no app data gets an orientation".
  On Windows `QSettings` lives in the registry, so deleting the app-data
  folder - the obvious way to reset an app - would leave the flag behind and
  the welcome would never come back.
* It keeps the whole of this app's per-user state in one directory the user
  can see, back up and delete, which is the same promise `presets.json` and
  `backup_jobs.json` already make.

Qt-free (CLAUDE.md §8), so it is tested without a widget.
"""
from __future__ import annotations

import os

from utils.presets import app_dir

MARKER_NAME = "welcome-seen"


def marker_path(directory: str = "") -> str:
    return os.path.join(directory or app_dir(), MARKER_NAME)


def seen(directory: str = "") -> bool:
    """True once this profile has dismissed the welcome."""
    return os.path.exists(marker_path(directory))


def mark_seen(directory: str = "") -> None:
    """Record that the welcome has been dismissed.

    A failure here is deliberately silent. The worst case is that the panel
    appears once more on the next launch, which is a mild annoyance; refusing
    to dismiss it, or raising out of a click handler, would be worse than the
    thing it is protecting against.
    """
    try:
        with open(marker_path(directory), "w", encoding="utf-8") as handle:
            handle.write("The welcome panel is not shown again once this file exists.\n")
    except OSError:
        pass


def reset(directory: str = "") -> None:
    """Forget that it was seen. Exists for tests and for a support answer."""
    try:
        os.remove(marker_path(directory))
    except OSError:
        pass
