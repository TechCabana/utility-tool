# utils/runner.py
"""Running a job (lock, pre-flight, copy, verify, promote, prune) and the
changes-since comparison shown in the Restore table.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.

Two calls go through another module's attribute rather than an imported
name, so a test that patches that module's attribute reaches this code too:
`jobs_store._locks_dir()` and `verify._verify_copy(...)`.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime
from typing import Callable, Dict, Optional

from utils import jobs_store, locks, verify
from utils.copying import (BackupAborted, SourceUnavailable, TargetWriteFailed,
                           _writing, copy_tree, walk_tree)
from utils.jobs_store import job_verify_mode
from utils.preflight import _preflight_and_sweep, target_volume
from utils.verify import _quick_verify
from utils.versions import (MANIFEST_VERSION, VERSION_FORMAT, _discard, is_verified,
                            job_root, latest_version, manifest_path, prune_versions,
                            pruning_held_sentence, sanity_drop_baseline,
                            version_datetime, version_path)


# The copy preserves mtime (copystat, as copy2 does), so an untouched file compares equal to the nanosecond
# on NTFS. The tolerance is for filesystems that store a coarser timestamp
# (FAT32 on a USB backup drive rounds to 2 seconds), not for slack.
MTIME_TOLERANCE = 2.0


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------


# How many one-second stamps `_claim_stamp` tries before giving up.
CLAIM_ATTEMPTS = 3


def _claim_stamp(root: str, job: Dict) -> Optional[str]:
    """Reserve a version stamp no other run of this job holds; None if none.

    Version names have one-second resolution, so two runs in the same second
    (a quick run then Run now again, or the scheduled task firing during a
    manual run) used to share a `.partial` folder and a manifest -- and the
    loser's cleanup deleted the winner's verified manifest, silently demoting
    a good version to unverified. `os.mkdir` of the `.partial` folder is the
    atomic claim; a stamp whose version or manifest already exists is
    released and the next second tried. Raises OSError if the target cannot
    be written.

    This is the second line of defence: `run_job` holds a per-job lock, so two
    runs of one job normally never reach here together; the stamp claim still
    covers a lock that was taken over as stale while its owner was alive.
    """
    for attempt in range(CLAIM_ATTEMPTS):
        if attempt:
            time.sleep(1)
        stamp = datetime.now().strftime(VERSION_FORMAT)
        partial = os.path.join(root, stamp + ".partial")
        try:
            os.mkdir(partial)
        except FileExistsError:
            continue
        if (os.path.exists(os.path.join(root, stamp))
                or os.path.exists(manifest_path(job, stamp))):
            os.rmdir(partial)
            continue
        return stamp
    return None


def run_job(job: Dict,
            on_progress: Optional[Callable[[int, int], None]] = None,
            should_abort: Optional[Callable[[], bool]] = None,
            on_phase: Optional[Callable[[str], None]] = None) -> Dict:
    """Take one versioned, checked copy of `job`'s source; prune old versions.

    Returns a plain result dict: `ok`, `message`, and on success `version`,
    `files`, `bytes`, `pruned`, `pruning_held`, `baseline_files`, `errors`, `verified` (True for the "full" and
    "quick" modes, False for "off"), `verify_mode`, `manifest` and
    `target_volume`. Never raises for a filesystem
    problem the user can see and fix -- a missing source, an unwritable
    target and a drive that is not mounted all come back as `ok=False` with a
    message, per CLAUDE.md §5's "a destination failure degrades cleanly".

    The sequence, and why it is in this order:

    1. Copy into `<stamp>.partial`, hashing each file as it is copied and
       streaming one manifest line per file into `<stamp>.manifest.jsonl.tmp`.
    2. Check the copy, per the job's `verify` mode (`job_verify_mode`):
       "full" re-reads every copied file and compares it to the manifest
       (`_verify_copy`); "quick" checks every file's size and re-reads a
       sample (`_quick_verify`); "off" skips this step. Any mismatch fails
       the run: nothing is promoted, nothing is pruned, and the `.partial`
       folder and `.tmp` manifest are KEPT so the failure can be inspected.
    3. Append the end trailer (recording the mode, and `verified` false for
       "off"), fsync, rename the manifest into place, and only then rename
       `.partial` to `<stamp>` -- so a version folder never exists without
       its finished manifest. An "off" version is promoted like any other.
    4. Prune, which never removes the newest verified version -- unless the
       source sanity guard holds it (see below).

    Source files that could not be read are reported in `errors` and left
    out of the manifest; the run is still ok (`not_copied` counts the files
    that never landed, which can exceed `len(errors)` when a whole folder was
    unreadable). "Verified" means every file that was copied matches, not
    that every source file was copied -- the message says how many were
    skipped. A failure to WRITE the target (full drive, path too long, file
    too large, drive gone) is never a skip: the run fails with a `cause` (a
    `TARGET_CAUSES` key or "target_write_failed"), nothing is promoted or
    pruned, and the `.partial` folder and `.tmp` manifest are removed.

    A SOURCE that goes away mid-run fails the same way, with cause
    "source_unavailable" (the source folder can no longer be listed, noticed
    at the first source error or at the end of the copy) or
    "source_incomplete" (the folder is there but at least
    `SHORTFALL_MIN_FILES` files and more than `SHORTFALL_TOLERANCE` of the
    count taken before the copy did not land, or not one file was copied).
    A failure to write the manifest is a target failure too, and when the
    backup drive itself is gone the message says the incomplete copy could
    not be removed rather than claiming it was. The check is made when the
    COPY ends: a source that disappears afterwards, during verification,
    changes nothing -- verification reads the copy, and every file was
    already copied.

    **Source sanity guard.** If this run copied more than
    `SANITY_DROP_FRACTION` fewer files than the largest retained verified
    version holds, the run still succeeds and keeps its version, but pruning
    is skipped: `pruning_held` is True, `baseline_files` is that largest
    count, `pruned` is empty and the message says so. No baseline (a first
    run) never holds. See `versions.sanity_drop_baseline`.

    `on_phase` is called with "copying" and then, unless the mode is "off",
    "verifying"; `on_progress` restarts from 0 for the verify pass (over the
    sample, for "quick"). Cancelling (`should_abort`) in
    either phase removes the `.partial` folder and the `.tmp` manifest.

    A per-job lock (`<app data>/locks/<job id>.lock`) is taken before
    anything else, including pre-flight, and released in `finally` however the
    run ends. If another live run of this job holds it the result is
    `{"ok": False, "busy": True, ...}` and nothing was touched -- callers must
    NOT `record_run` a busy result, or it would overwrite the real last result.
    A lock left by a dead process (or older than 24 h) is taken over, and the
    result message says so.

    `preflight()` runs after the lock and nothing is created until it passes.
    """
    lock_path = _job_lock_path(job)
    if lock_path is None:
        return {"ok": False, "message": "This backup job has no usable id, so "
                "it cannot run. Nothing was changed."}
    try:
        lock = locks.acquire(lock_path, f"backup job {job['id']}")
    except locks.LockBusy as busy:
        return {"ok": False, "busy": True, "message": _busy_message(busy.holder)}
    except OSError as exc:
        return {"ok": False, "message": f"Could not take the backup lock: {exc}. "
                "Nothing was changed."}
    try:
        result = _run_job(job, on_progress, should_abort, on_phase)
    finally:
        lock.release()
    if lock.recovered is not None:
        who = (f"process {lock.recovered}" if lock.recovered > 0
               else "an earlier run")
        message = result.get("message", "")
        if not message.endswith((".", "!")):
            message += "."
        result["message"] = f"{message} Recovered a stale lock left by {who}."
    return result


def _job_lock_path(job: Dict) -> Optional[str]:
    """Where `job`'s run lock lives, or None if it has no usable id.

    The id is a hex string we generate, but the jobs file is hand-editable:
    an id with a path separator or `..` must not point the lock outside the
    locks folder, so anything but plain word characters is hashed instead.
    """
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        job_id = "id-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]
    return os.path.join(jobs_store._locks_dir(), job_id + ".lock")


def _busy_message(holder: Optional[Dict]) -> str:
    """The plain-English refusal for a run that found its job already running."""
    if not holder:
        return "This backup job is already running. Nothing was changed."
    try:
        started = datetime.fromisoformat(holder["started"]).strftime("%H:%M")
    except (KeyError, TypeError, ValueError):
        started = "earlier"
    return (f"This backup job is already running (started {started}, process "
            f"{holder.get('pid')}). Nothing was changed.")


def _run_job(job: Dict,
             on_progress: Optional[Callable[[int, int], None]],
             should_abort: Optional[Callable[[], bool]],
             on_phase: Optional[Callable[[str], None]]) -> Dict:
    """Pre-flight, sweep leftovers, then the backup; `run_job` holds the lock."""
    problem, sweep = _preflight_and_sweep(job)
    if problem:
        result = {"ok": False, "message": problem}
    else:
        result = _backup(job, on_progress, should_abort, on_phase)
    return _with_sweep(result, sweep)


def _with_sweep(result: Dict, sweep: Optional[Dict]) -> Dict:
    """Add `swept` and a plain-English cleanup note to `result` if a sweep ran."""
    if sweep is None:
        return result
    result["swept"] = list(sweep["removed"])
    notes = []
    if sweep["removed"]:
        notes.append(f"Cleaned up {len(sweep['removed'])} abandoned leftover(s) "
                     "from earlier runs.")
    if sweep["errors"]:
        notes.append(f"{len(sweep['errors'])} leftover(s) could not be removed.")
    if notes:
        message = result.get("message", "")
        if not message.endswith((".", "!")):
            message += "."
        result["message"] = f"{message} {' '.join(notes)}"
    return result


def _backup(job: Dict,
            on_progress: Optional[Callable[[int, int], None]],
            should_abort: Optional[Callable[[], bool]],
            on_phase: Optional[Callable[[str], None]]) -> Dict:
    """The copy, verify, promote and prune steps, after pre-flight has passed."""
    source, target = job["source"], job["target"]

    root = job_root(job)
    try:
        os.makedirs(root, exist_ok=True)
        stamp = _claim_stamp(root, job)
    except OSError as exc:
        return {"ok": False, "message": f"Target folder is not writable: {exc}"}
    if stamp is None:
        return {"ok": False, "message": (
            "Another backup of this job is starting at the same moment, so this "
            "one did not start. Nothing was written; try again.")}
    partial = os.path.join(root, stamp + ".partial")
    final = os.path.join(root, stamp)
    manifest = manifest_path(job, stamp)
    manifest_tmp = manifest + ".tmp"

    if on_phase is not None:
        on_phase("copying")
    stats: Dict[str, int] = {}
    try:
        # The manifest lives on the target, so failing to create or write it
        # is a target write failure like any other (a full drive refuses a
        # manifest line as readily as a data file) and is reported the same.
        with _writing(manifest_tmp):
            out = open(manifest_tmp, "w", encoding="utf-8", newline="\n")
        with out:
            with _writing(manifest_tmp):
                out.write(json.dumps({"type": "header", "version": MANIFEST_VERSION,
                                      "stamp": stamp, "source": source}) + "\n")

            def record(rel: str, size: int, mtime: float, sha: str) -> None:
                """Stream one file's manifest line straight to disk.

                Raises TargetWriteFailed, not OSError: copy_tree would count
                an OSError as one skipped file and carry on, leaving a copied
                file the manifest omits.
                """
                with _writing(manifest_tmp):
                    out.write(json.dumps({"type": "file", "path": rel, "size": size,
                                          "mtime": mtime, "sha256": sha}) + "\n")
                    out.flush()

            files, copied_bytes, errors = copy_tree(
                source, partial, on_progress=on_progress,
                should_abort=should_abort, on_copied=record, stats=stats)
    except BackupAborted:
        _discard(partial, manifest_tmp)
        return {"ok": False, "message": "Backup cancelled"}
    except (TargetWriteFailed, SourceUnavailable) as exc:
        # Unlike a failed verification, nothing is kept for inspection: the
        # copy is incomplete by definition, and on a full drive it is the
        # very thing using up the space. A source that vanished mid-run is
        # the same: what was copied is a fraction of the tree and must never
        # be promoted, or it would prune the last complete backup.
        _discard(partial, manifest_tmp)
        leftover = [p for p in (partial, manifest_tmp) if os.path.exists(p)]
        if not os.path.isdir(root):
            # The drive itself went away: "does not exist" here means "cannot
            # be seen", and the half-written copy is still on it.
            cleanup = ("the incomplete copy could not be removed because the "
                       "backup drive is unavailable")
        elif leftover:
            cleanup = "the incomplete copy could not be removed: " + ", ".join(leftover)
        else:
            cleanup = "the incomplete copy was removed"
        return {"ok": False, "cause": exc.cause,
                "message": (f"Backup failed: {exc}. Nothing was saved or pruned "
                            f"and earlier backups are untouched; {cleanup}.")}
    except OSError as exc:
        _discard(partial, manifest_tmp)
        return {"ok": False, "message": f"Backup failed: {exc}"}

    mode = job_verify_mode(job)
    sampled = 0
    problem = None
    if mode != "off":
        if on_phase is not None:
            on_phase("verifying")
        try:
            if mode == "quick":
                checked, bad, named, sampled = _quick_verify(
                    partial, manifest_tmp, on_progress, should_abort, stamp)
            else:
                checked, bad, named = verify._verify_copy(partial, manifest_tmp,
                                                          on_progress, should_abort,
                                                          files)
        except BackupAborted:
            _discard(partial, manifest_tmp)
            return {"ok": False, "message": "Backup cancelled"}
        except OSError as exc:
            problem = f"its manifest could not be read back ({exc})"
        else:
            if bad:
                more = ", ..." if bad > len(named) else ""
                problem = (f"{bad} file(s) did not match what was copied "
                           f"({', '.join(named)}{more})")
            elif checked != files:
                problem = (f"the manifest lists {checked} file(s) but {files} "
                           "were copied")
    if problem:
        return {"ok": False,
                "message": (f"Backup verification failed: {problem}. Nothing was "
                            "pruned and earlier backups are untouched. The "
                            f"unverified copy was kept for inspection at {partial}"),
                "verified": False, "verify_mode": mode, "partial": partial,
                "manifest": manifest_tmp, "files": files, "errors": errors}

    try:
        with open(manifest_tmp, "a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps({"type": "end", "files": files,
                                  "bytes": copied_bytes, "verify": mode,
                                  "verified": mode != "off"}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(manifest_tmp, manifest)
        os.replace(partial, final)
    except OSError as exc:
        _discard(partial, manifest_tmp)
        try:
            os.remove(manifest)
        except OSError:
            pass
        return {"ok": False, "message": f"Could not finalise the backup: {exc}"}

    # Source sanity guard (card 51): a run that lost far more files than the
    # best retained version had still succeeds and its version is kept, but
    # pruning is held so a mostly-emptied source cannot roll every complete
    # copy out of retention. The new version is excluded from the baseline.
    baseline = sanity_drop_baseline(job, files, exclude=stamp)
    held = baseline > 0
    pruned = [] if held else prune_versions(job)
    if mode == "quick":
        message = (f"Backed up {files} file(s) to {final} and quick-checked "
                   f"{sampled} of {files} file(s) (sizes of all, hashes of a sample)")
    elif mode == "off":
        message = (f"Backed up {files} file(s) to {final}, NOT verified: "
                   "verification is turned off for this job")
    else:
        message = f"Backed up {files} file(s) to {final} and verified every copied file"
    # `errors` has one entry per unreadable FOLDER, so count the files that
    # never landed against the count taken before the copy instead.
    not_copied = max(len(errors), stats.get("not_copied", 0))
    if errors:
        message += (f". {not_copied} file(s) could not be read and were "
                    "skipped, so they are not in this backup")
    if held:
        message += ". " + pruning_held_sentence(files, baseline)
    if pruned:
        message += f". Removed {len(pruned)} old version(s)"
    return {"ok": True, "message": message, "version": stamp, "files": files,
            "bytes": copied_bytes, "pruned": pruned, "errors": errors,
            "not_copied": not_copied,
            "pruning_held": held, "baseline_files": baseline,
            "verified": mode != "off", "verify_mode": mode, "manifest": manifest,
            "target_volume": target_volume(target)}


# ---------------------------------------------------------------------------
# Changes since the last backup (the Restore table's second column)
# ---------------------------------------------------------------------------

def changes_since(job: Dict,
                  on_progress: Optional[Callable[[int, int], None]] = None,
                  should_abort: Optional[Callable[[], bool]] = None) -> Dict:
    """How much of `job`'s source differs from its most recent version.

    Returns `ok`, `message`, `version`, `when`, `verified` (`is_verified` of
    that version), plus `added`, `modified`,
    `removed`, `changed` (their sum) and `bytes` (the size of everything
    added or modified, i.e. what the next backup would have to write).

    **Comparison is by size and mtime, not by content hash.** The copy
    preserves mtime, so an untouched file matches its backup exactly and any
    ordinary edit changes one or both. Hashing both trees would be certain but
    reads every byte of both -- minutes for the kind of folder people back up,
    for a number shown in a table column. The gap it leaves is a file edited
    to exactly the same length whose mtime was then restored deliberately;
    that needs `touch` to produce, and a hash pass is the upgrade if it ever
    matters.
    """
    source = job.get("source", "")
    if not source or not os.path.isdir(source):
        # `fixable` says the failure is one editing the job would resolve,
        # so the UI does not have to infer that from the message text. The
        # other two failures here are not: "never backed up" is a normal
        # state, and a cancelled comparison says nothing about the job.
        return {"ok": False, "message": "Source folder not found",
                "changed": 0, "fixable": True}

    version = latest_version(job)
    if version is None:
        return {"ok": False, "message": "Never backed up", "changed": 0}

    if on_progress is not None:
        on_progress(0, 2)
    current, _ = walk_tree(source)
    if should_abort is not None and should_abort():
        return {"ok": False, "message": "Cancelled", "changed": 0}
    if on_progress is not None:
        on_progress(1, 2)
    backed_up, _ = walk_tree(version_path(job, version))
    if on_progress is not None:
        on_progress(2, 2)

    added = modified = 0
    changed_bytes = 0
    for rel, (size, mtime) in current.items():
        previous = backed_up.get(rel)
        if previous is None:
            added += 1
            changed_bytes += size
        elif previous[0] != size or abs(previous[1] - mtime) > MTIME_TOLERANCE:
            modified += 1
            changed_bytes += size
    removed = sum(1 for rel in backed_up if rel not in current)

    changed = added + modified + removed
    when = version_datetime(version)
    return {
        "ok": True,
        "version": version,
        "when": when.strftime("%Y-%m-%d %H:%M") if when else version,
        "added": added, "modified": modified, "removed": removed,
        "changed": changed, "bytes": changed_bytes,
        # The version a Restore would use; False for an "off" or legacy one,
        # so the Restore table never makes it look checked.
        "verified": is_verified(job, version),
        "message": ("No changes since the last backup" if changed == 0 else
                    f"{changed} file(s) changed since the last backup"),
    }
