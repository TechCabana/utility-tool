# utils/runner.py
"""Running a job (lock, pre-flight, copy, verify, promote, prune) and the
changes-since comparison shown in the Restore table.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.

Two calls go through another module's attribute rather than an imported
name, so a test that patches that module's attribute reaches this code too:
`jobs_store._locks_dir()` and `verify._verify_copy(...)`.
"""
import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from utils import jobs_store, locks, precheck, runlog, verify
from utils.copying import (BackupAborted, SourceUnavailable, TargetWriteFailed,
                           _writing, copy_tree, walk_tree)
from utils.fs_utils import long_path, plain_error
from utils.jobs_store import job_verify_mode
from utils.preflight import _preflight_and_sweep, target_volume
from utils.verify import _quick_verify
from utils.versions import (MANIFEST_VERSION, VERSION_FORMAT, _discard, is_sanity_drop,
                            is_verified, job_root, latest_version, manifest_path,
                            prune_versions, pruning_held_sentence, restore_candidates,
                            sanity_baseline, utc_stamp, version_path,
                            version_when)


# The copy preserves mtime (copystat, as copy2 does), so an untouched file compares equal to the nanosecond
# on NTFS. The tolerance is for filesystems that store a coarser timestamp
# (FAT32 on a USB backup drive rounds to 2 seconds), not for slack.
MTIME_TOLERANCE = 2.0


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------


# How many one-second stamps `_claim_stamp` tries before giving up.
CLAIM_ATTEMPTS = 3

# The real `datetime`, captured before any test replaces `runner.datetime` to
# fake the LOCAL clock (version names), so the UTC clock below is a separate
# thing that can be faked separately -- the autumn clock change needs both.
_REAL_DATETIME = datetime


def _utc_now() -> datetime:
    """The current time in UTC (timezone-aware). The run's `finished_utc`
    comes from here; tests patch THIS to set UTC independently of the local
    clock that names versions (`datetime.now()` in `_claim_stamp`)."""
    return _REAL_DATETIME.now(timezone.utc)


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
            os.mkdir(long_path(partial))
        except FileExistsError:
            continue
        if (os.path.exists(long_path(os.path.join(root, stamp)))
                or os.path.exists(long_path(manifest_path(job, stamp)))):
            os.rmdir(long_path(partial))
            continue
        return stamp
    return None


def run_job(job: Dict,
            on_progress: Optional[Callable[[int, int], None]] = None,
            should_abort: Optional[Callable[[], bool]] = None,
            on_phase: Optional[Callable[[str], None]] = None,
            trigger: str = "manual",
            after_lock: Optional[Callable[[], None]] = None) -> Dict:
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
    3. Append the end trailer (recording the mode, `verified` false for "off",
       and `finished_utc`, the UTC finish time versions are ordered by),
       fsync, rename the manifest into place, and only then rename
       `.partial` to `<stamp>` -- so a version folder never exists without
       its finished manifest. An "off" version is promoted like any other.
    4. Prune, which never removes the newest verified version, nor the
       baseline the source sanity guard protects (see below).

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
    `SANITY_DROP_FRACTION` fewer files than the largest retained version
    with a complete manifest (verified or not) holds, the run still succeeds
    and keeps its version, and that largest version is protected from
    pruning while the others prune by retention as usual (it does not count
    toward retention): `pruning_held` is True, `baseline_files` is its count
    and the message says so (`baseline_stamp` names it). No baseline (a first
    run) never holds. See `versions.sanity_baseline` and
    `prune_versions(protect=...)`. The owner can end a hold with
    `release_hold`; the guard then ignores versions older than the release
    (the acknowledgement is read from the jobs file, so a stale `job` dict
    cannot undo it).

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
    `after_lock`, if given, is called once the lock is held and before
    pre-flight: work that walks the source (the first unattended run's
    pre-check) belongs there, so a run refused as busy never starts it. It must
    not raise; if it does, the lock is released and the exception propagates.

    **Run log (card 52).** Every call appends exactly one line to the job's log
    (`utils/runlog.py`) once the lock is released -- success, failure, cancelled,
    busy and pre-flight refusals alike, and a run that raises is logged as
    "failed" before the exception propagates unchanged. `trigger` says how it
    started: "manual" (the GUI's Run button) or "scheduled" (the headless
    `--run-backup-job` path). A log failure never changes the result.
    """
    start = datetime.now()
    try:
        result = _locked_run(job, on_progress, should_abort, on_phase, after_lock)
    except BaseException as exc:
        runlog.append_run(job, {"ok": False, "message": f"{type(exc).__name__}: {exc}"},
                          trigger, start)
        raise
    runlog.append_run(job, result, trigger, start)
    return result


def _locked_run(job: Dict,
                on_progress: Optional[Callable[[int, int], None]],
                should_abort: Optional[Callable[[], bool]],
                on_phase: Optional[Callable[[str], None]],
                after_lock: Optional[Callable[[], None]] = None) -> Dict:
    """`run_job` without the log: take the per-job lock, run, release it."""
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
        # A previous run of this job that died leaves its start marker behind;
        # record that fact before this run replaces the marker. Nothing is
        # written unless there is a stale marker, and a busy run (above) never
        # gets here, so a refusal leaves the jobs file as it was.
        settle_interrupted(job["id"], check_lock=False)
        if after_lock is not None:
            after_lock()
        result = _run_job(job, on_progress, should_abort, on_phase)
    finally:
        lock.release()
    if lock.recovered is not None:
        who = (f"process {lock.recovered}" if lock.recovered > 0
               else "an earlier run")
        if lock.recovered_reused:
            who += ", whose id has since been reused by another process"
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
    return os.path.join(jobs_store._locks_dir(), runlog.safe_id(job_id) + ".lock")


# ---------------------------------------------------------------------------
# Interrupted runs (card PQPfznG1)
# ---------------------------------------------------------------------------
#
# The marker itself (`jobs_store.job_running`) is written by `_mark_running`
# just before copying and removed by `record_run`. What reads it, and what it
# means, lives here because the answer needs the per-job lock path.

# `last_result` of a job whose run was found interrupted. It does not start
# with "ok", so every reader that treats `startswith("ok")` as success (the
# Backup tab's status and warning code) already treats it as a failure.
INTERRUPTED_PREFIX = "interrupted"


def interrupted_result(since: str) -> str:
    """The `last_result` text for a run that began at `since` and never ended."""
    return f"{INTERRUPTED_PREFIX}: started {since} and never finished"


def _lock_live(job: Dict) -> bool:
    """True when `job`'s per-job lock is held by a live owner: a run is under
    way right now, whatever an older start marker says."""
    path = _job_lock_path(job)
    return path is not None and locks.is_held(path)


def _stale_marker(job: Dict, check_lock: bool = True) -> Optional[Dict]:
    """`job`'s start marker when nothing is running it any more, else None.

    None for: no marker (or a junk one); an owner that is alive (running, however
    long -- `jobs_store.running_owner_gone`); and, with `check_lock`, a per-job
    lock held by a live owner, because then ANOTHER run is in progress (it took
    over after the marked one died and has not marked itself yet). A caller
    that holds the lock itself passes `check_lock=False`.
    """
    marker = jobs_store.job_running(job)
    if marker is None or not jobs_store.running_owner_gone(marker):
        return None
    if check_lock and _lock_live(job):
        return None
    return marker


def job_interrupted(job: Dict) -> Optional[Dict]:
    """`{"since", "stamp", "settled"}` when `job`'s latest run began and never
    finished, else None. Pure: reads the dict and the process table, writes
    nothing, no Qt.

    Two forms of the same fact. A start marker whose owner is gone is
    `settled=False`; the same fact after `settle_interrupted` has recorded it
    (`last_result` starts with "interrupted", `last_run` is when it began) is
    `settled=True` with no `stamp`. A marker whose owner is alive, or whose job
    is locked by a live run, is a run in progress, never "interrupted".
    """
    if jobs_store.job_running(job) is not None:
        marker = _stale_marker(job)
        return ({"since": marker["since"], "stamp": marker["stamp"], "settled": False}
                if marker else None)
    result = job.get("last_result")
    if isinstance(result, str) and result.startswith(INTERRUPTED_PREFIX) and job.get("last_run"):
        return {"since": job["last_run"], "stamp": None, "settled": True}
    return None


def settle_interrupted(job_id: str, check_lock: bool = True) -> Optional[Dict]:
    """Turn a start marker with no owner into the recorded fact, exactly once.

    Under the jobs-file lock, from a fresh read: if the job's marker is stale,
    the job gets `last_run` = when that run began, `last_result` =
    "interrupted: started ... and never finished" and loses the marker, in one
    atomic write; then ONE line (outcome "interrupted") is appended to the run
    log. The second call finds no marker, so it neither writes nor logs again,
    however many processes look.

    Why here and not in the renderer: every reader of `last_result` (not only
    the Backup tab) sees a failure from then on, and the run log is complete
    even if nobody ever runs the job again. It is called when a run starts
    (`_locked_run`, which holds the lock: `check_lock=False`), when the headless
    run starts, and when the Backup tab loads the jobs. The log line follows the
    write, so a crash in between loses a line rather than duplicating one.

    Returns the marker settled (`since`, `stamp`), or None when there was
    nothing to settle or the jobs file could not be read or written (a damaged
    file is reported by its usual paths, not here).
    """
    try:
        with jobs_store._jobs_file_lock():
            stored = jobs_store.get_job(job_id)
            marker = _stale_marker(stored, check_lock) if stored else None
            if marker is None:
                return None
            stored["last_run"] = marker["since"]
            stored["last_result"] = interrupted_result(marker["since"])
            stored.pop("running", None)
            jobs_store._save_job(stored)
    except (jobs_store.JobsFileCorrupt, OSError):
        return None
    began = datetime.fromisoformat(marker["since"])
    runlog.append_run(
        stored, {"ok": False, "interrupted": True, "cause": "interrupted",
                 "version": marker["stamp"],
                 "message": f"The run that started at {marker['since']} never "
                            "finished: its process stopped before it recorded a result."},
        "recovery", began, began)
    return marker


def settle_all_interrupted(jobs: list) -> bool:
    """`settle_interrupted` for every job in `jobs` that reads as interrupted by
    an unsettled marker; True if any was settled (the caller re-reads the file)."""
    changed = False
    for job in jobs:
        info = job_interrupted(job)
        if info is not None and not info["settled"] and settle_interrupted(job.get("id")):
            changed = True
    return changed


def _mark_running(job: Dict, stamp: str, began: Optional[Dict]) -> None:
    """Record that this run is about to copy, on the stored job.

    Never fails the backup: a lock timeout, a damaged jobs file (its usual
    handling still happens when `record_run` runs) or a job that is not in the
    file just means no marker, and the run reads as it always did. On success
    the marker is copied into `began` so `_abandon_run` knows this run owns one.
    """
    try:
        marker = jobs_store.new_running_marker(stamp)
        if jobs_store.set_running(job.get("id"), marker) and began is not None:
            began.update(marker)
    except Exception:  # ponytail: a marker is a courtesy; no retry, no log sink
        pass


def _abandon_run(job: Dict, exc: BaseException) -> None:
    """A run that was marked is leaving by an exception no handler recorded:
    record it as failed now, which also removes the marker.

    Without this the job would keep a marker whose owner (this still-living
    process) reads as "running" for as long as the process lives, and the
    earlier "ok" would stand. Same write the caller's own `record_run` makes
    (and a second one overwrites it harmlessly). Never raises.
    """
    try:
        jobs_store.record_run(job.get("id"), {
            "ok": False, "message": f"{type(exc).__name__}: {exc}"})
    except Exception:
        pass


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


RELEASE_NOTHING = "Nothing to release: this backup is not holding any older version."
RELEASE_BUSY = "A backup is running; try again when it finishes."


def _released_message(baseline: int, retention) -> str:
    """What a release tells the owner: the older backup is not kept aside any
    more, and (honestly) it is still on disk until the normal clean-up."""
    keep = max(1, int(retention)) if isinstance(retention, int) else jobs_store.DEFAULT_RETENTION
    return (f"Done: the earlier backup ({baseline} files) is no longer kept "
            "aside. Nothing was deleted now; it is removed in the normal "
            f"clean-up once it is older than the {keep} backups you keep.")


def _release_locked(job_id: str) -> Dict:
    """The release itself; the caller holds the job's run lock.

    Everything is decided from a fresh read under the jobs-file lock, because
    the screen's idea of the job may be stale. Raises JobsFileCorrupt or
    OSError (lock timeout, write failure) like `save_job`.
    """
    with jobs_store._jobs_file_lock():
        job = jobs_store.get_job(job_id)
        if job is None:
            return {"ok": False, "released": False, "cause": "unknown_job",
                    "message": f"There is no backup job with id '{job_id}'."}
        baseline = jobs_store.job_baseline_files(job)
        current = jobs_store.job_last_files(job)
        if not jobs_store.job_pruning_held(job):
            return {"ok": True, "released": False,
                    "message": RELEASE_NOTHING, "baseline_files": 0,
                    "current_files": current, "baseline_stamp": None}
        newest = latest_version(job)
        _, stamp = sanity_baseline(job, exclude=newest)
        if newest is not None:
            job["hold_released"] = {
                "stamp": stamp or "", "files": baseline, "level": current,
                "since": newest, "time": datetime.now().isoformat(timespec="seconds")}
        job["last_pruning_held"] = False
        job["last_baseline_files"] = 0
        jobs_store._save_job(job)
    return {"ok": True, "released": True,
            "message": _released_message(baseline, job.get("retention")),
            "baseline_files": baseline, "current_files": current,
            "baseline_stamp": stamp}


def release_hold(job_id: str, trigger: str = "manual") -> Dict:
    """Let go of a pruning hold the owner says is expected (card QnFM2GMZ).

    The source sanity guard keeps one older, larger backup (the baseline) aside
    while the source stays more than 30% below it. A person who really did
    delete folders on purpose has no other way to say so, so this records
    that on the job -- `hold_released = {stamp, files, level, since, time}` --
    and clears the hold markers so the card stops saying "pruning held".

    **The rule it sets up** (applied by `versions.sanity_baseline`): versions
    older than `since` -- the newest version at the moment of release, i.e.
    the one holding the `level` the source was at -- are no longer compared
    with. The next run prunes by plain retention, so the old large version
    goes as soon as it is outside retention, and cannot re-hold in the
    meantime just because it is still on disk. `level` becomes the reference:
    a later run more than 30% below it holds again (protecting the then
    largest version from `since` on), and a source that grows back is
    guarded against its new size like any other. A second release simply
    replaces the acknowledgement.

    **It never deletes or moves anything.** Pruning happens only in a later
    run, through `prune_versions`. With no active hold it is a no-op:
    `ok=True, released=False` and a plain message, and nothing is written.

    The job's run lock is taken first, so releasing while that job is running
    is refused (`busy=True`, `ok=False`): the run's own `record_run` would
    otherwise overwrite the acknowledgement. Then the jobs-file lock and a
    fresh read, as `record_run` does.

    Returns `{ok, released, message, baseline_files, current_files,
    baseline_stamp}`; an unknown id is `ok=False, cause="unknown_job"`. Raises
    JobsFileCorrupt for a damaged jobs file. A release is logged as one line
    in the job's run log (`trigger` "manual" or "cli"); that never fails it.
    """
    start = datetime.now()
    job = jobs_store.get_job(job_id)             # JobsFileCorrupt propagates
    if job is None:
        return {"ok": False, "released": False, "cause": "unknown_job",
                "message": f"There is no backup job with id '{job_id}'."}
    lock_path = _job_lock_path(job)
    if lock_path is None:
        return {"ok": False, "released": False,
                "message": "This backup job has no usable id. Nothing was changed."}
    try:
        lock = locks.acquire(lock_path, f"release hold {job_id}")
    except locks.LockBusy:
        return {"ok": False, "released": False, "busy": True, "message": RELEASE_BUSY}
    except OSError as exc:
        return {"ok": False, "released": False,
                "message": f"Could not take the backup lock: {exc}. Nothing was changed."}
    try:
        result = _release_locked(job_id)
    finally:
        lock.release()
    if result.get("released"):
        # After both locks are gone, as the run log requires.
        runlog.append_run(job, dict(result, hold_released=True, files=result["current_files"]),
                          trigger, start)
    return result


def _run_job(job: Dict,
             on_progress: Optional[Callable[[int, int], None]],
             should_abort: Optional[Callable[[], bool]],
             on_phase: Optional[Callable[[str], None]]) -> Dict:
    """Pre-flight, sweep leftovers, then the backup; `run_job` holds the lock."""
    problem, sweep = _preflight_and_sweep(job)
    if problem:
        result = {"ok": False, "message": problem}
    else:
        began: Dict = {}   # filled by `_backup` once it has marked the job
        try:
            result = _backup(job, on_progress, should_abort, on_phase, began)
        except BaseException as exc:
            # Includes types no handler below expects (and KeyboardInterrupt):
            # the marker must not outlive a run that is leaving by a raise.
            if began:
                _abandon_run(job, exc)
            raise
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


def _with_stored_release(job: Dict) -> Dict:
    """`job`, carrying the "this is expected" acknowledgement the jobs FILE has.

    A caller's job dict can be older than a release made meanwhile (the GUI
    holds the jobs it loaded; `main.py --release-hold` writes the file), and
    a run that judged the source with the stale dict would hold again at once.
    So the stored acknowledgement wins over the dict's own. An unreadable jobs
    file or a job not in it (a test's unsaved dict) leaves `job` as it was.
    """
    try:
        stored = jobs_store.get_job(job.get("id"))
    except (jobs_store.JobsFileCorrupt, OSError):
        return job
    release = jobs_store.job_hold_released(stored) if stored else None
    return dict(job, hold_released=release) if release else job


def _backup(job: Dict,
            on_progress: Optional[Callable[[int, int], None]],
            should_abort: Optional[Callable[[], bool]],
            on_phase: Optional[Callable[[str], None]],
            began: Optional[Dict] = None) -> Dict:
    """The copy, verify, promote and prune steps, after pre-flight has passed.

    Once the version stamp is claimed -- every refusal that can happen before
    any copying (a guard, an unwritable target, a stamp clash) is behind us --
    the job is marked as started (`_mark_running`; `began` receives the marker),
    so a run killed from here on reads as interrupted instead of leaving the
    previous result standing. The caller's `record_run` removes the marker.
    """
    source, target = job["source"], job["target"]

    root = job_root(job)
    try:
        os.makedirs(long_path(root), exist_ok=True)
        stamp = _claim_stamp(root, job)
    except OSError as exc:
        return {"ok": False, "message": f"Target folder is not writable: {plain_error(exc)}"}
    if stamp is None:
        return {"ok": False, "message": (
            "Another backup of this job is starting at the same moment, so this "
            "one did not start. Nothing was written; try again.")}
    partial = os.path.join(root, stamp + ".partial")
    final = os.path.join(root, stamp)
    manifest = manifest_path(job, stamp)
    manifest_tmp = manifest + ".tmp"

    _mark_running(job, stamp, began)
    if on_phase is not None:
        on_phase("copying")
    stats: Dict = {}
    try:
        # The manifest lives on the target, so failing to create or write it
        # is a target write failure like any other (a full drive refuses a
        # manifest line as readily as a data file) and is reported the same.
        with _writing(manifest_tmp):
            out = open(long_path(manifest_tmp), "w", encoding="utf-8", newline="\n")
        with out:
            with _writing(manifest_tmp):
                out.write(json.dumps({"type": "header", "version": MANIFEST_VERSION,
                                      "stamp": stamp, "source": source}) + "\n")

            def record(rel: str, size: int, mtime: float, sha: str,
                       changed: bool = False) -> None:
                """Stream one file's manifest line straight to disk.

                Raises TargetWriteFailed, not OSError: copy_tree would count
                an OSError as one skipped file and carry on, leaving a copied
                file the manifest omits. `changed` (card RnuRsA2V) adds
                `"changed": true` to the line of a file that was still being
                written on its second copy too; the key is absent otherwise,
                and every reader ignores keys it does not know.
                """
                line = {"type": "file", "path": rel, "size": size,
                        "mtime": mtime, "sha256": sha}
                if changed:
                    line["changed"] = True
                with _writing(manifest_tmp):
                    out.write(json.dumps(line) + "\n")
                    out.flush()

            files, copied_bytes, errors = copy_tree(
                source, partial, on_progress=on_progress,
                should_abort=should_abort, on_copied=record, stats=stats,
                retry_skipped=True, recopy_changed=True)
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
        leftover = [p for p in (partial, manifest_tmp) if os.path.exists(long_path(p))]
        if not os.path.isdir(long_path(root)):
            # The drive itself went away: "does not exist" here means "cannot
            # be seen", and the half-written copy is still on it.
            cleanup = ("the incomplete copy could not be removed because the "
                       "backup drive is unavailable")
        elif leftover:
            cleanup = "the incomplete copy could not be removed: " + ", ".join(leftover)
        else:
            cleanup = "the incomplete copy was removed"
        result = {"ok": False, "cause": exc.cause,
                  "message": (f"Backup failed: {exc}. Nothing was saved or pruned "
                              f"and earlier backups are untouched; {cleanup}.")}
        if exc.cause in ("path_too_long", "file_too_large"):
            # Card 104: name every file the drive cannot hold, not just the
            # first one the copy tripped over, so the fix is one pass.
            found = precheck.offenders(job, exc.cause,
                                       getattr(exc, "path", ""), should_abort)
            if found["paths"]:
                more = f" and {found['more']} more" if found["more"] else ""
                result["offenders"] = found["paths"]
                result["message"] += (" Files the backup drive cannot hold: "
                                      f"{', '.join(found['paths'])}{more}.")
        return result
    except OSError as exc:
        _discard(partial, manifest_tmp)
        return {"ok": False, "message": f"Backup failed: {plain_error(exc)}"}

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
            problem = f"its manifest could not be read back ({plain_error(exc)})"
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

    # Card ved3wVOQ: the UTC moment the run finalises the manifest. It is what
    # orders this version against the others (`versions.ordered_versions`),
    # because the version NAME is local time and repeats in an autumn hour.
    finished_utc = utc_stamp(_utc_now())
    try:
        with open(long_path(manifest_tmp), "a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps({"type": "end", "files": files,
                                  "bytes": copied_bytes, "verify": mode,
                                  "verified": mode != "off",
                                  "finished_utc": finished_utc}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(long_path(manifest_tmp), long_path(manifest))
        os.replace(long_path(partial), long_path(final))
    except OSError as exc:
        _discard(partial, manifest_tmp)
        try:
            os.remove(long_path(manifest))
        except OSError:
            pass
        return {"ok": False, "message": f"Could not finalise the backup: {plain_error(exc)}"}

    # Source sanity guard (card 51): a run that lost far more files than the
    # best retained version had still succeeds and its version is kept, and
    # that best version (the baseline) is protected from pruning while the
    # rest prune as usual -- so a mostly-emptied source cannot roll every
    # complete copy out of retention, and a source that stays low cannot grow
    # the target without bound. The new version is excluded from the baseline.
    baseline, baseline_stamp = sanity_baseline(_with_stored_release(job), exclude=stamp)
    held = is_sanity_drop(baseline, files)
    if not held:
        baseline = 0
    pruned = prune_versions(job, protect={baseline_stamp} if held else (), newest=stamp)
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
    # Card b5CfTkL8: files that could not be read at first and were copied by
    # the one retry after the walk. They are in the backup, so they are in
    # `files`, the manifest and the verification, and not in the skipped count.
    retried = stats.get("retried", 0)
    if retried:
        message += (f". {retried} file(s) could not be read at first and were "
                    "copied on the second try")
    if errors:
        message += (f". {not_copied} file(s) could not be read and were "
                    "skipped, so they are not in this backup")
    # Card RnuRsA2V: a file that changed under the read is copied once more.
    # One that settled is an ordinary file; one that changed again is kept and
    # named, because its copy may mix old and new parts of the file. Either
    # way verification passed against the bytes that were read.
    recopied = stats.get("recopied", 0)
    changed = stats.get("changed", 0)
    changed_sample = list(stats.get("changed_names", []))[:jobs_store.SKIPPED_SAMPLE_MAX]
    if recopied:
        message += (f". {recopied} file(s) changed while they were being copied "
                    "and were copied again")
    if changed:
        message += (f". {changed} file(s) were still changing while they were "
                    "copied, even on a second try, so each may mix old and new "
                    "parts (changed during backup)")
    if held:
        message += ". " + pruning_held_sentence(files, baseline)
    if pruned:
        # A held run prunes too now, after a sentence that ends in its own ".".
        message += ("" if message.endswith(".") else ".") + f" Removed {len(pruned)} old version(s)"
    return {"ok": True, "message": message, "version": stamp, "files": files,
            "bytes": copied_bytes, "pruned": pruned, "errors": errors,
            "not_copied": not_copied, "retried": retried,
            "recopied": recopied, "changed": changed,
            "changed_sample": changed_sample,
            "pruning_held": held, "baseline_files": baseline,
            "baseline_stamp": baseline_stamp if held else None,
            "verified": mode != "off", "verify_mode": mode, "manifest": manifest,
            "finished_utc": finished_utc, "target_volume": target_volume(target)}


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
    if not source or not os.path.isdir(long_path(source)):
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
    candidates = restore_candidates(job)
    return {
        "ok": True,
        "version": version,
        "when": version_when(version),
        "added": added, "modified": modified, "removed": removed,
        "changed": changed, "bytes": changed_bytes,
        # Whether the LATEST version (the one compared above) was verified;
        # False for an "off" or legacy one.
        "verified": is_verified(job, version),
        # What Restore will actually use (card 45). Measured here, in the
        # worker, so the row never reads manifests on the UI thread. Drift
        # above is still against the LATEST version, not this one.
        "restore": candidates,
        "preferred_when": version_when(candidates["preferred"]),
        "message": ("No changes since the last backup" if changed == 0 else
                    f"{changed} file(s) changed since the last backup"),
    }
