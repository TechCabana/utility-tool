# utils/undo.py
"""Reverse the last Move, Copy or Rename batch, or refuse and say why.

Delete needs nothing here: `file_utils.delete_file` uses the Recycle Bin, the
confirmation says so, and the OS already provides the undo. The three
operations that write in place are the ones with no way back.

Two decisions shape this module, both the owner's:

**Only the last batch.** There is no journal on disk and no history to walk.
The mistake this protects against - wrong pattern, wrong destination - is
noticed within seconds, and a durable multi-step history buys a scenario this
tool does not have. The record lives in memory and is dropped when the next
batch starts.

**All or nothing.** If any file in the batch cannot be put back, none of them
are, and the reason is named. The alternative - reverse what you can, report
the rest - leaves a folder half in one state and half in another, which the
owner rejected outright: "with batch jobs it would be annoying to get half
fixed". A refusal is recoverable by hand; a half-reverted folder of two
hundred files is not.

Qt-free (CLAUDE.md §8), so the interesting part is tested without a widget.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import List, Tuple

# What a reversal does per operation:
#   rename / move -> put the file back where it came from
#   copy          -> remove the copy that was made, leaving the original alone
REVERSIBLE = ("rename", "move", "copy")


@dataclass
class BatchRecord:
    """What one completed batch did, in enough detail to undo it."""
    operation: str
    # (original_path, final_path) for every file that actually succeeded.
    # A file that failed or was skipped never moved, so it is not in here.
    pairs: List[Tuple[str, str]] = field(default_factory=list)
    # Size and mtime of each final path as this app left it, so a file that
    # someone else has touched since can be recognised.
    fingerprints: List[Tuple[int, float]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.pairs)


def fingerprint(path: str) -> Tuple[int, float]:
    """Size and mtime, the same pair `backup_utils.changes_since` compares on.

    Not a content hash: this runs on every file of every batch, and the
    question is only "has anything touched this since we wrote it", where a
    cheap check that can produce a false *positive* is the right trade. A
    false positive refuses a reversal that would have been safe, which is
    recoverable; a false negative overwrites someone's edit, which is not.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return (-1, -1.0)
    return (stat.st_size, stat.st_mtime)


def record_batch(operation: str, pairs: List[Tuple[str, str]]) -> BatchRecord | None:
    """Capture a finished batch, or None if it is not something we can undo."""
    if operation not in REVERSIBLE or not pairs:
        return None
    return BatchRecord(operation=operation, pairs=list(pairs),
                       fingerprints=[fingerprint(final) for _, final in pairs])


def blockers(record: BatchRecord) -> List[str]:
    """Everything standing between this record and a clean reversal.

    Checked for the whole batch before a single file is touched, because the
    decision is all-or-nothing: finding the third problem after two files have
    already moved back is exactly the half-reverted state this avoids.
    """
    problems: List[str] = []
    for (original, final), expected in zip(record.pairs, record.fingerprints):
        name = os.path.basename(final)

        if not os.path.exists(final):
            problems.append(f"{name} is no longer there")
            continue

        if fingerprint(final) != expected:
            problems.append(f"{name} has been changed since the batch ran")
            continue

        if record.operation == "copy":
            continue  # reversing a copy only removes the copy

        # A rename or a move puts the file back, so the place it came from
        # has to still be free. Something else living there now means undoing
        # would overwrite a file this app never touched.
        if os.path.exists(original) and not _same_path(original, final):
            problems.append(f"something else now occupies {os.path.basename(original)}")

    return problems


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def reverse(record: BatchRecord) -> dict:
    """Put the batch back, or change nothing at all.

    Returns `{"ok", "message", "reversed"}`. When `ok` is False nothing on
    disk has been touched: every check runs first.
    """
    if record is None or not len(record):
        return {"ok": False, "message": "There is nothing to undo.", "reversed": 0}

    problems = blockers(record)
    if problems:
        shown = "; ".join(problems[:3])
        if len(problems) > 3:
            shown += f"; and {len(problems) - 3} more"
        return {
            "ok": False,
            "reversed": 0,
            "message": (f"Nothing was undone - {shown}. The whole batch is left "
                        f"as it is rather than putting only some of it back."),
        }

    done = 0
    for original, final in record.pairs:
        if record.operation == "copy":
            # The copy is this app's own artefact and the original is
            # untouched, so removing it loses nothing the user had before.
            from utils.file_utils import delete_file

            delete_file(final)
        else:
            os.makedirs(os.path.dirname(original) or ".", exist_ok=True)
            shutil.move(final, original)
        done += 1

    return {"ok": True, "reversed": done,
            "message": _describe(record.operation, done)}


def _describe(operation: str, count: int) -> str:
    if operation == "copy":
        return f"Removed {count} copy(s). The originals were never touched."
    verb = "rename" if operation == "rename" else "move"
    return f"Undid the {verb} of {count} file(s)."
