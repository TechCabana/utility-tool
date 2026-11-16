# utils/preflight.py
"""Pre-flight: refuse to start a run that cannot succeed or could do harm,
and the sweep-then-retry for a target that is only full of leftovers.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.
"""
import os
import shutil
from typing import Callable, Dict, Optional, Tuple

from utils.copying import tree_totals
from utils.fs_utils import human_size
from utils.jobs_store import job_pruning_held
from utils.versions import sweep_stale


# Pre-flight wants this much free space over the source's total size: the new
# version is written alongside the old ones (no dedup), and filesystem
# overhead (cluster rounding, metadata) makes a bare sum of sizes optimistic.
SPACE_MARGIN = 1.10


# ---------------------------------------------------------------------------
# Pre-flight: refuse to start a run that cannot succeed or could do harm
# ---------------------------------------------------------------------------

def target_inside_source(source: str, target: str) -> bool:
    """True if `target` is `source` or lies anywhere beneath it.

    Backing a folder up into itself copies the previous run's copies on every
    run, so the target grows without bound and no error is ever raised. Both
    paths are resolved (`realpath`, so a junction cannot hide the overlap) and
    case-folded (`normcase`; Windows paths differ in case without differing at
    all). The comparison is on a separator boundary, so `data2` is not
    "inside" `data`.
    """
    source_norm = os.path.normcase(os.path.realpath(source))
    target_norm = os.path.normcase(os.path.realpath(target))
    prefix = source_norm if source_norm.endswith(os.sep) else source_norm + os.sep
    return target_norm == source_norm or target_norm.startswith(prefix)


def nearest_existing_ancestor(path: str) -> Optional[str]:
    """`path` itself, or its closest parent that exists; None if none does.

    None means even the root is missing (an unplugged drive letter), which
    ends the walk instead of looping on a root that is its own parent.
    """
    current = os.path.abspath(path)
    while not os.path.exists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current


def volume_id(path: str) -> str:
    """Identity of the volume holding existing `path`, as a string.

    `os.stat().st_dev`. On Windows that is the volume serial number, which
    follows the drive across letter changes. On macOS/Linux it is the device
    number, which is NOT guaranteed stable across remounts or reboots, so
    there the check is best-effort and unverified: it can wrongly block a
    legitimate remount (re-save the job to re-record it) and is not relied on
    to catch every swapped drive. Raises OSError.

    On Windows only the low 32 bits are kept: that is the serial `vol`
    prints, and the only part every Python agrees on. 3.12+ reports NTFS's
    64-bit serial in `st_dev` while earlier versions (README says 3.9+)
    report the 32-bit one, so the raw value recorded under one interpreter
    would read as "a different drive" under the other and block every run.
    """
    dev = os.stat(path).st_dev
    return str(dev & 0xFFFFFFFF if os.name == "nt" else dev)


def _same_volume(recorded: str, current: str) -> bool:
    """True if a recorded volume id names the `current` volume.

    Also accepts a recorded raw 64-bit Windows serial (stored before
    `volume_id` masked it) whose low 32 bits match.
    """
    if recorded == current:
        return True
    return (os.name == "nt" and recorded.isdigit() and current.isdigit()
            and int(recorded) & 0xFFFFFFFF == int(current))


def target_volume(target: str) -> str:
    """`volume_id` of `target`'s nearest existing ancestor, or "" if the
    drive is unavailable. For recording on a job; never raises."""
    anchor = nearest_existing_ancestor(target)
    if anchor is None:
        return ""
    try:
        return volume_id(anchor)
    except OSError:
        return ""


# The start of preflight's insufficient-space message. `_run_job` matches on it
# to tell "the target is full" (which a sweep of abandoned leftovers may cure)
# from every other refusal (which it cannot).
LOW_SPACE_PREFIX = "Not enough free space on the target drive"

# Appended to the low-space refusal when the job's last successful run kept
# an older, larger backup aside (the source sanity guard, card 51).
HELD_LOW_SPACE_SENTENCE = (
    "Older backups are being kept aside because your source folder shrank. "
    "Free some space on the backup drive, or delete the oldest backup folders "
    "you no longer need.")


def _free_bytes(path: str) -> int:
    """Free bytes on the volume holding existing `path`. Raises OSError."""
    return shutil.disk_usage(path).free


def preflight(job: Dict,
              free_space: Optional[Callable[[str], int]] = None,
              volume: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """Check `job` before anything is written. None if safe, else why not.

    Runs before the job root is created, so a refused run leaves the target
    untouched. Guards, in order: source exists, is a directory and holds at
    least one file (a blank or unplugged source must not drive a prune of
    good history); target is not the source or inside it; the target drive is
    present; when the job recorded one, the target is on the same volume as
    before (an unplugged drive must not cause a backup into a folder on C:);
    and free space covers the source plus `SPACE_MARGIN`. A job with no
    recorded volume is not blocked -- `run_job` reports the current one for
    `record_run` to store (trust on first use).

    `free_space(path) -> bytes` and `volume(path) -> str` are injectable for
    tests; they default to the real disk and `volume_id`.
    """
    free_space = free_space or _free_bytes
    volume = volume or volume_id
    source = job.get("source", "")
    target = job.get("target", "")
    if not source or not os.path.isdir(source):
        return f"Source folder not found: {source or '(not set)'}"
    if not target:
        return "No target folder is set for this job"

    file_count, source_bytes = tree_totals(source)
    if not file_count:
        return ("The source folder is empty (or none of its files can be "
                "read), so nothing was backed up. Check that its drive is "
                "connected.")
    if target_inside_source(source, target):
        return "The target cannot be inside the source folder."

    anchor = nearest_existing_ancestor(target)
    if anchor is None:
        return f"The target drive is not available, so nothing was backed up: {target}"

    # Drive identity before free space: a low-space refusal lets `_run_job`
    # sweep old leftovers, and it must never sweep (or report the space of) a
    # drive this job does not recognise.
    recorded = job.get("target_volume")
    if recorded:
        try:
            current = volume(anchor)
        except OSError as exc:
            return f"Could not identify the target drive: {exc}"
        if not _same_volume(str(recorded), current):
            return ("The backup drive is not the same drive as before. Connect "
                    "the original drive or edit the job.")

    need = int(source_bytes * SPACE_MARGIN)
    try:
        free = free_space(anchor)
    except OSError as exc:
        return f"Could not read the free space on the target drive: {exc}"
    if free < need:
        message = (f"{LOW_SPACE_PREFIX}: the backup needs "
                   f"about {human_size(need)} but only {human_size(free)} is free.")
        if job_pruning_held(job):
            # Card 51: the guard's kept-aside baseline can be what fills the
            # drive, and the user cannot see that from "Failed" alone.
            message += " " + HELD_LOW_SPACE_SENTENCE
        return message
    return None


def _preflight_and_sweep(job: Dict) -> Tuple[Optional[str], Optional[Dict]]:
    """Run pre-flight and `sweep_stale`; return `(problem, sweep or None)`.

    The sweep runs once pre-flight passes. It also runs when pre-flight fails
    ONLY on free space -- abandoned partials can be what fills the target --
    after which pre-flight is re-run once. Any other refusal (wrong drive,
    missing source, ...) deletes nothing. Sweeping only ever deletes, so the
    "nothing is created before the guards pass" guarantee holds.
    """
    problem = preflight(job)
    if problem is None:
        return None, sweep_stale(job)
    if not problem.startswith(LOW_SPACE_PREFIX):
        return problem, None
    sweep = sweep_stale(job)
    if sweep["removed"]:
        return preflight(job), sweep
    return problem, sweep
