# utils/backup_utils.py
"""Backup jobs: storage, the versioned copy itself, the changes-since
comparison, restore, and the Windows Task Scheduler registration behind the
"schedule" field.

Qt-free by design (CLAUDE.md §3/§8) -- plain paths and plain dicts in, plain
dicts out. `tabs/backup_tab.py` runs the long ones from a QThread worker and
renders what comes back; nothing here knows that. `main.py --run-backup-job`
calls `run_job` directly, with no QApplication at all, which is what the
scheduled task invokes.

Storage is one JSON file in the OS app-data directory, via
`utils/fs_utils.app_dir()`. There is no first-run default set: a machine with no backup jobs has none, and the
Backup screen's EmptyState is the correct rendering of that.

**Scheduling is handed to the OS, and the OS differs.** Configuration lives
in this app; the recurring trigger belongs to the platform. Windows uses
`schtasks.exe` and macOS uses `launchd`, both of which ship with the system,
so neither adds a dependency. Linux has no equivalent implemented and reports
that plainly rather than pretending a job is scheduled.

This is the one place the repo departs from CLAUDE.md §1's "no per-OS
branching" goal, by explicit owner decision, and the branching is confined to
the four functions at the bottom of this file.

**The macOS path is unverified.** It was written on a Windows machine and has
never been run on a Mac. The parts that can be tested without one - the plist
that gets written and the path it is written to - are covered by
tests/test_backup_utils.py; loading it with `launchctl` is not. A failure
there reports the real error rather than claiming success, which is the most
that can honestly be promised until someone runs it.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from utils.fs_utils import _is_link_like, app_dir, human_size

# A path, not a snapshot: nothing is read until load_jobs() opens it, so
# utils/migrate.py copying a pre-rename jobs file into app_dir() any time
# before that first read is enough for it to be found here.
JOBS_FILENAME = "backup_jobs.json"
JOBS_FILE = os.path.join(app_dir(), JOBS_FILENAME)

# A version directory is named for the moment the backup started. Matched by
# this pattern when versions are listed, so a half-written ".partial" copy or
# anything else the user drops in the target is never mistaken for a version.
VERSION_FORMAT = "%Y%m%d-%H%M%S"
_VERSION_RE = re.compile(r"^\d{8}-\d{6}$")

SCHEDULES = ("manual", "daily", "weekly")
DEFAULT_RETENTION = 5

# copy2 preserves mtime, so an untouched file compares equal to the nanosecond
# on NTFS. The tolerance is for filesystems that store a coarser timestamp
# (FAT32 on a USB backup drive rounds to 2 seconds), not for slack.
MTIME_TOLERANCE = 2.0

# Pre-flight wants this much free space over the source's total size: the new
# version is written alongside the old ones (no dedup), and filesystem
# overhead (cluster rounding, metadata) makes a bare sum of sizes optimistic.
SPACE_MARGIN = 1.10

# Windows only: keep schtasks' console window from flashing over the app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BackupAborted(Exception):
    """Raised inside the copy to stop it when the user quits mid-run.

    Not an OSError on purpose: `shutil.copytree` swallows OSError per entry
    and carries on, which is exactly the behaviour wanted for a locked file
    and exactly wrong for a cancellation.
    """


class JobsFileCorrupt(Exception):
    """The jobs file exists but cannot be trusted as a list of jobs.

    Raised instead of returning an empty list: "no jobs" and "a file I could
    not read" are different facts, and treating the second as the first made
    the next save overwrite every job the user had. `has_backup` says whether
    `restore_jobs_from_backup()` has a usable `.bak` to work from.
    """

    def __init__(self, message: str, path: str = "", has_backup: bool = False):
        super().__init__(message)
        self.path = path
        self.has_backup = has_backup


# ---------------------------------------------------------------------------
# Job storage
# ---------------------------------------------------------------------------

def new_job(name: str, source: str, target: str, schedule: str = "daily",
            at: str = "20:00", retention: int = DEFAULT_RETENTION,
            target_volume: str = "") -> Dict:
    """A job dict with a fresh id. Nothing is written to disk here.

    `target_volume` is the identity of the drive `target` was on when the job
    was set up (see `target_volume()`); "" means not recorded yet, and the
    first successful run records it.
    """
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "source": source,
        "target": target,
        "schedule": schedule if schedule in SCHEDULES else "manual",
        "at": at,
        "retention": max(1, int(retention)),
        "target_volume": target_volume,
        "last_run": "",
        "last_result": "",
    }


def _backup_path() -> str:
    """Where the last good copy of the jobs file is kept."""
    return JOBS_FILE + ".bak"


def _read_valid(path: str) -> Tuple[bytes, List[Dict]]:
    """Read `path` and return (raw bytes, parsed jobs).

    A missing file propagates as FileNotFoundError. Anything else wrong with
    it -- unreadable, invalid JSON, not a list of objects -- is a ValueError
    carrying a human reason, so callers decide what that means for them.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ValueError(f"it cannot be read: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise ValueError(f"it is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(j, dict) for j in data):
        raise ValueError("it is not a list of jobs")
    return raw, data


def _has_usable_backup() -> bool:
    """True when the `.bak` copy exists and parses as a jobs list."""
    try:
        _read_valid(_backup_path())
    except (FileNotFoundError, ValueError):
        return False
    return True


def load_jobs() -> List[Dict]:
    """Every configured job; an empty list only when there is no jobs file.

    A file that exists but cannot be read, is not valid JSON, or is not a list
    raises `JobsFileCorrupt` -- never `[]`, because the very next `save_job`
    would then overwrite the damaged file and every job in it with one job.
    """
    try:
        return _read_valid(JOBS_FILE)[1]
    except FileNotFoundError:
        return []
    except ValueError as exc:
        has_backup = _has_usable_backup()
        raise JobsFileCorrupt(
            f"The backup jobs file '{JOBS_FILE}' is damaged: {exc}. "
            + ("A backup copy of it is available to restore."
               if has_backup else "No usable backup copy of it exists."),
            JOBS_FILE, has_backup) from exc


def _write_atomic(path: str, data: bytes) -> None:
    """Replace `path` with `data` so a crash leaves the old file or the new.

    Written to a temp file in the same directory (os.replace is only atomic
    within one volume), flushed and fsynced, then swapped in. The temp file is
    removed on any failure; an OSError comes back naming the file.

    ponytail: a hard kill between mkstemp and os.replace leaves a stray
    ``*.tmp`` in the app-data directory; sweep them on startup if it ever
    matters.
    """
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                prefix=os.path.basename(path) + ".",
                                suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException as exc:
        try:
            os.remove(temp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise OSError(f"Could not write '{path}': {exc}") from exc
        raise


def save_jobs(jobs: List[Dict]) -> None:
    """Atomically store `jobs`, keeping the previous good version as `.bak`.

    Refuses (raises `JobsFileCorrupt`) when the file on disk is damaged, so a
    save can never run on top of one -- and so the `.bak` can never be
    replaced by a corrupt current file.
    """
    load_jobs()  # raises JobsFileCorrupt on a damaged file; [] if missing
    data = json.dumps(jobs, indent=2).encode("utf-8")
    try:
        previous = _read_valid(JOBS_FILE)[0]
    except FileNotFoundError:
        previous = None
    if previous is not None:
        _write_atomic(_backup_path(), previous)
    _write_atomic(JOBS_FILE, data)


def restore_jobs_from_backup() -> int:
    """Put the `.bak` copy back as the jobs file; return how many jobs.

    The damaged file is kept first as ``<jobs file>.corrupt-<timestamp>`` so
    nothing is destroyed. Raises `JobsFileCorrupt` when there is no usable
    `.bak`, having written nothing.
    """
    try:
        raw, jobs = _read_valid(_backup_path())
    except (FileNotFoundError, ValueError) as exc:
        raise JobsFileCorrupt(
            f"There is no usable backup copy ('{_backup_path()}') to restore "
            "the jobs file from.", JOBS_FILE, False) from exc
    if os.path.exists(JOBS_FILE):
        kept = f"{JOBS_FILE}.corrupt-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        shutil.copyfile(JOBS_FILE, kept)
    _write_atomic(JOBS_FILE, raw)
    return len(jobs)


def get_job(job_id: str) -> Optional[Dict]:
    """The stored job with this id, or None. Raises JobsFileCorrupt."""
    for job in load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def save_job(job: Dict) -> None:
    """Insert or replace `job` by id. Does not touch the OS scheduler --
    `sync_task()` is called separately so a storage failure and a scheduler
    failure are reported distinctly. Raises JobsFileCorrupt, writing nothing,
    if the stored file is damaged."""
    jobs = load_jobs()
    for index, existing in enumerate(jobs):
        if existing.get("id") == job["id"]:
            jobs[index] = job
            break
    else:
        jobs.append(job)
    save_jobs(jobs)


def remove_job(job_id: str) -> None:
    """Drop the job with this id. Raises JobsFileCorrupt if the file is damaged."""
    save_jobs([job for job in load_jobs() if job.get("id") != job_id])


# ---------------------------------------------------------------------------
# Versions on disk
# ---------------------------------------------------------------------------

def job_root(job: Dict) -> str:
    """`<target>/<job id>` -- every version of this job lives under here.

    Keyed on the id, not the name, so renaming a job never orphans its own
    history.
    """
    return os.path.join(job["target"], job["id"])


def version_names(job: Dict) -> List[str]:
    """Existing version stamps for `job`, oldest first.

    Only names matching the timestamp format count. A `.partial` directory
    left by an interrupted run, or anything else in the target folder, is
    ignored rather than offered as a restore point.
    """
    try:
        with os.scandir(job_root(job)) as entries:
            names = [e.name for e in entries
                     if e.is_dir(follow_symlinks=False) and _VERSION_RE.match(e.name)]
    except OSError:
        return []
    return sorted(names)


def latest_version(job: Dict) -> Optional[str]:
    names = version_names(job)
    return names[-1] if names else None


def version_path(job: Dict, name: str) -> str:
    return os.path.join(job_root(job), name)


def version_datetime(name: str) -> Optional[datetime]:
    try:
        return datetime.strptime(name, VERSION_FORMAT)
    except ValueError:
        return None


def prune_versions(job: Dict) -> List[str]:
    """Delete the oldest versions beyond the job's retention count.

    Returns the names actually removed. A version that cannot be deleted (a
    file in it is open, the drive went away) is left in place and simply not
    reported as removed -- a failed prune must never fail the backup that
    just succeeded.
    """
    keep = max(1, int(job.get("retention", DEFAULT_RETENTION)))
    names = version_names(job)
    removed = []
    for name in names[:max(0, len(names) - keep)]:
        try:
            shutil.rmtree(version_path(job, name))
            removed.append(name)
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Walking and copying
# ---------------------------------------------------------------------------

def _ignore_links(directory: str, names) -> set:
    """`shutil.copytree`'s ignore hook, dropping symlinks and junctions.

    Reuses `fs_utils._is_link_like` rather than a second link predicate:
    `is_symlink()` is False for a Windows junction, and the reparse-point
    attribute alone wrongly catches cloud-sync roots, so the name-surrogate
    test lives in exactly one place. Copying a junction that points at an
    ancestor of the source would otherwise recurse until the disk fills.
    """
    try:
        with os.scandir(directory) as entries:
            return {entry.name for entry in entries if _is_link_like(entry)}
    except OSError:
        return set()


def walk_tree(root: str) -> Tuple[Dict[str, Tuple[int, float]], int]:
    """`({relative path: (size, mtime)}, unreadable_count)` for a whole tree.

    Relative paths are normalised with forward slashes and lowercased case
    only for the *key*, so a source and its backup compare correctly on
    Windows' case-insensitive filesystem. Unreadable entries are counted, not
    raised.
    """
    files: Dict[str, Tuple[int, float]] = {}
    skipped = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if _is_link_like(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        rel = os.path.relpath(entry.path, root).replace("\\", "/")
                        files[rel.lower()] = (stat.st_size, stat.st_mtime)
                    except OSError:
                        skipped += 1
        except OSError:
            skipped += 1
    return files, skipped


def copy_tree(source: str, destination: str,
              on_progress: Optional[Callable[[int, int], None]] = None,
              should_abort: Optional[Callable[[], bool]] = None
              ) -> Tuple[int, int, List[str]]:
    """Copy `source` into `destination`, reporting `(done, total)` per file.

    Returns `(files_copied, bytes_copied, errors)`. Built on
    `shutil.copytree` with a counting `copy_function` rather than a
    hand-rolled walk: copytree already collects per-file failures and carries
    on, which is exactly the "a permission error must not abandon the batch"
    behaviour the rest of this app has, and it already handles directory
    creation and metadata.
    """
    total = len(walk_tree(source)[0])
    state = {"done": 0, "bytes": 0}

    def copy_one(src: str, dst: str) -> str:
        if should_abort is not None and should_abort():
            raise BackupAborted()
        shutil.copy2(src, dst)
        state["done"] += 1
        try:
            state["bytes"] += os.path.getsize(dst)
        except OSError:
            pass
        if on_progress is not None:
            on_progress(state["done"], total)
        return dst

    errors: List[str] = []
    try:
        shutil.copytree(source, destination, copy_function=copy_one,
                        ignore=_ignore_links, ignore_dangling_symlinks=True,
                        dirs_exist_ok=True)
    except shutil.Error as exc:
        # copytree raises this once, at the end, carrying every entry it could
        # not copy. A locked file is normal (a running app holds it open), so
        # it is reported alongside a successful backup, never as a failure of
        # the whole run.
        for entry in exc.args[0]:
            errors.append(f"{os.path.basename(str(entry[0]))}: {entry[2]}")
    return state["done"], state["bytes"], errors


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
    """
    return str(os.stat(path).st_dev)


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
    present; free space covers the source plus `SPACE_MARGIN`; and, when the
    job recorded one, the target is on the same volume as before (an unplugged
    drive must not cause a backup into a folder on C:). A job with no recorded
    volume is not blocked -- `run_job` reports the current one for
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

    files, _ = walk_tree(source)
    if not files:
        return ("The source folder is empty (or none of its files can be "
                "read), so nothing was backed up. Check that its drive is "
                "connected.")
    if target_inside_source(source, target):
        return "The target cannot be inside the source folder."

    anchor = nearest_existing_ancestor(target)
    if anchor is None:
        return f"The target drive is not available, so nothing was backed up: {target}"

    need = int(sum(size for size, _ in files.values()) * SPACE_MARGIN)
    try:
        free = free_space(anchor)
    except OSError as exc:
        return f"Could not read the free space on the target drive: {exc}"
    if free < need:
        return (f"Not enough free space on the target drive: the backup needs "
                f"about {human_size(need)} but only {human_size(free)} is free.")

    recorded = job.get("target_volume")
    if recorded:
        try:
            current = volume(anchor)
        except OSError as exc:
            return f"Could not identify the target drive: {exc}"
        if current != recorded:
            return ("The backup drive is not the same drive as before. Connect "
                    "the original drive or edit the job.")
    return None


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------

def run_job(job: Dict,
            on_progress: Optional[Callable[[int, int], None]] = None,
            should_abort: Optional[Callable[[], bool]] = None) -> Dict:
    """Take one versioned copy of `job`'s source and prune old versions.

    Returns a plain result dict: `ok`, `message`, and on success `version`,
    `files`, `bytes`, `pruned`, `errors`. Never raises for a filesystem
    problem the user can see and fix -- a missing source, an unwritable
    target and a drive that is not mounted all come back as `ok=False` with a
    message, per CLAUDE.md §5's "a destination failure degrades cleanly".

    The copy lands in `<stamp>.partial` and is renamed to `<stamp>` only once
    it completes. An interrupted run therefore leaves nothing that
    `version_names()` will offer as a restore point, and nothing that the
    changes-since comparison would measure against.

    `preflight()` runs first and nothing is created until it passes. On
    success the result carries `target_volume` for `record_run` to store.
    """
    problem = preflight(job)
    if problem:
        return {"ok": False, "message": problem}
    source, target = job["source"], job["target"]

    stamp = datetime.now().strftime(VERSION_FORMAT)
    root = job_root(job)
    partial = os.path.join(root, stamp + ".partial")
    final = os.path.join(root, stamp)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "message": f"Target folder is not writable: {exc}"}

    try:
        files, copied_bytes, errors = copy_tree(
            source, partial, on_progress=on_progress, should_abort=should_abort)
    except BackupAborted:
        shutil.rmtree(partial, ignore_errors=True)
        return {"ok": False, "message": "Backup cancelled"}
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        return {"ok": False, "message": f"Backup failed: {exc}"}

    try:
        os.replace(partial, final)
    except OSError as exc:
        shutil.rmtree(partial, ignore_errors=True)
        return {"ok": False, "message": f"Could not finalise the backup: {exc}"}

    pruned = prune_versions(job)
    message = f"Backed up {files} file(s) to {final}"
    if errors:
        message += f". {len(errors)} file(s) could not be read"
    if pruned:
        message += f". Removed {len(pruned)} old version(s)"
    return {"ok": True, "message": message, "version": stamp, "files": files,
            "bytes": copied_bytes, "pruned": pruned, "errors": errors,
            "target_volume": target_volume(target)}


def record_run(job_id: str, result: Dict) -> Optional[Dict]:
    """Write a run's outcome back onto the stored job, and return it.

    Re-read from disk rather than mutating the caller's copy: the headless CLI
    and the GUI can both be looking at this file, and the run is the only
    field either of them writes during a run. Raises JobsFileCorrupt if the
    jobs file has been damaged since the run began.
    """
    job = get_job(job_id)
    if job is None:
        return None
    if result.get("ok") and result.get("target_volume") and not job.get("target_volume"):
        # Trust on first use: a job made before volume identity existed learns
        # its drive from its first successful run, and is checked against it
        # from then on.
        job["target_volume"] = result["target_volume"]
    job["last_run"] = datetime.now().isoformat(timespec="seconds")
    job["last_result"] = ("ok" if result.get("ok") else "failed") + \
        (f": {result.get('message', '')}" if not result.get("ok") else "")
    save_job(job)
    return job


# ---------------------------------------------------------------------------
# Changes since the last backup (the Restore table's second column)
# ---------------------------------------------------------------------------

def changes_since(job: Dict,
                  on_progress: Optional[Callable[[int, int], None]] = None,
                  should_abort: Optional[Callable[[], bool]] = None) -> Dict:
    """How much of `job`'s source differs from its most recent version.

    Returns `ok`, `message`, `version`, `when`, plus `added`, `modified`,
    `removed`, `changed` (their sum) and `bytes` (the size of everything
    added or modified, i.e. what the next backup would have to write).

    **Comparison is by size and mtime, not by content hash.** `copy2`
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
        "message": ("No changes since the last backup" if changed == 0 else
                    f"{changed} file(s) changed since the last backup"),
    }


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_version(job: Dict, version: str, destination: str,
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    should_abort: Optional[Callable[[], bool]] = None) -> Dict:
    """Copy one stored version into a NEW folder under `destination`.

    Deliberately not an overwrite of the live source. Restoring on top of the
    working folder is a destructive merge -- it would delete nothing but would
    silently replace edits made since the backup, with no undo -- and this
    card never asked for those semantics. The copy lands in
    `<destination>/<job name>-restored-<version>/`, which is unambiguous about
    what it is and can be moved over the source by hand once the user has
    looked at it.
    """
    source = version_path(job, version)
    if not os.path.isdir(source):
        return {"ok": False, "message": f"Backup version {version} not found"}

    safe_name = re.sub(r"[^\w.-]+", "_", job.get("name") or job["id"]).strip("_") or job["id"]
    folder = os.path.join(destination, f"{safe_name}-restored-{version}")
    try:
        files, restored_bytes, errors = copy_tree(
            source, folder, on_progress=on_progress, should_abort=should_abort)
    except BackupAborted:
        return {"ok": False, "message": "Restore cancelled"}
    except OSError as exc:
        return {"ok": False, "message": f"Restore failed: {exc}"}

    message = f"Restored {files} file(s) to {folder}"
    if errors:
        message += f". {len(errors)} file(s) could not be written"
    return {"ok": True, "message": message, "path": folder, "files": files,
            "bytes": restored_bytes, "errors": errors}


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

# Task Scheduler name prefixes. The legacy one is what tasks were registered
# under before the Safekeep rename; only utils/migrate.py asks for it, to
# remove an old task once its replacement has registered.
TASK_PREFIX = "Safekeep_Backup_"
LEGACY_TASK_PREFIX = "UtilityTool_Backup_"


def task_name(job_id: str, legacy: bool = False) -> str:
    """The scheduled task's name. Prefixed so every task this app owns is
    identifiable in Task Scheduler, and keyed on the job id so a rename never
    strands one. `legacy=True` gives the pre-rename name."""
    return f"{LEGACY_TASK_PREFIX if legacy else TASK_PREFIX}{job_id}"


def job_command(job_id: str) -> str:
    """The command line a scheduled task runs -- the headless CLI in main.py.

    `sys.executable` is the interpreter actually running, so a job created
    from the venv schedules the venv's python. A PyInstaller build sets
    `sys.frozen`, in which case the executable *is* the app and takes the flag
    directly.

    Packaging gap, stated rather than hidden: a task created while running
    from source hardcodes that interpreter and that checkout path, so moving
    or deleting either breaks the schedule silently (the task runs and fails).
    Re-creating the job after packaging fixes it; a packaged build is the real
    answer and is out of this card's scope.
    """
    return " ".join(f'"{part}"' if " " in part else part
                    for part in job_argv(job_id))


def job_argv(job_id: str) -> List[str]:
    """The same command as an argv list, which is what launchd wants.

    `schtasks` takes one quoted string; a launchd plist takes an array of
    arguments and does no shell parsing of its own. Building the list first
    and joining it for Windows keeps one definition of what actually runs.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-backup-job", job_id]
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    return [sys.executable, entry, "--run-backup-job", job_id]


def schtasks_args(job: Dict) -> Optional[List[str]]:
    """The full `schtasks /create` argv for `job`, or None for a manual job.

    Separated from the call that runs it so the argument construction -- the
    part with the interesting edge cases -- is testable without registering a
    real task on the machine running the tests.
    """
    schedule = job.get("schedule", "manual")
    if schedule not in ("daily", "weekly"):
        return None
    args = [
        "schtasks", "/create",
        "/tn", task_name(job["id"]),
        "/tr", job_command(job["id"]),
        "/sc", "WEEKLY" if schedule == "weekly" else "DAILY",
        "/st", job.get("at") or "20:00",
        "/f",  # replace an existing task of the same name rather than failing
    ]
    if schedule == "weekly":
        # schtasks needs an explicit day for a weekly task; Monday is the
        # default rather than a fourth field on the job form nobody asked for.
        args += ["/d", "MON"]
    return args


# ------------------------------
# macOS (launchd)
# ------------------------------
# A launchd label has to be unique per user and is conventionally reverse-DNS.
# The legacy prefix is the pre-rename label, used only by the migration.
LAUNCHD_PREFIX = "com.techcabana.safekeep.backup"
LEGACY_LAUNCHD_PREFIX = "com.techcabana.utilitytool.backup"

# launchd's own weekday numbering: 0 and 7 are both Sunday, 1 is Monday. The
# Windows path pins a weekly job to Monday, so this does too -- the two
# platforms must not disagree about which day "weekly" means.
LAUNCHD_MONDAY = 1


def launchd_label(job_id: str, legacy: bool = False) -> str:
    """The launchd label for `job_id`; `legacy=True` gives the pre-rename one."""
    return f"{LEGACY_LAUNCHD_PREFIX if legacy else LAUNCHD_PREFIX}.{job_id}"


def launchd_plist_path(job_id: str, legacy: bool = False) -> str:
    """Where the agent's plist lives. Per-user, so no admin rights needed."""
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                        f"{launchd_label(job_id, legacy)}.plist")


def launchd_plist(job: Dict) -> Optional[str]:
    """The plist XML for `job`, or None when the job has no schedule.

    Separated from the call that installs it for the same reason
    `schtasks_args` is: the interesting edge cases are in what gets written,
    and that part can be tested on any machine. Installing it cannot.
    """
    schedule = job.get("schedule", "manual")
    if schedule not in ("daily", "weekly"):
        return None

    at = job.get("at") or "20:00"
    try:
        hour, minute = (int(part) for part in at.split(":", 1))
    except ValueError:
        hour, minute = 20, 0

    interval = [("Hour", hour), ("Minute", minute)]
    if schedule == "weekly":
        interval.append(("Weekday", LAUNCHD_MONDAY))

    arguments = "".join(f"        <string>{_xml_escape(part)}</string>\n"
                        for part in job_argv(job["id"]))
    calendar = "".join(f"        <key>{key}</key><integer>{value}</integer>\n"
                       for key, value in interval)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"    <key>Label</key>\n    <string>{launchd_label(job['id'])}</string>\n"
        f"    <key>ProgramArguments</key>\n    <array>\n{arguments}    </array>\n"
        f"    <key>StartCalendarInterval</key>\n    <dict>\n{calendar}    </dict>\n"
        # The app must not be held open by the scheduler, and a backup that
        # fails should not be respawned in a loop.
        "    <key>RunAtLoad</key>\n    <false/>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


def _launchd_register(job: Dict) -> Tuple[bool, str]:
    """Write the plist and ask launchd to load it."""
    path = launchd_plist_path(job["id"])
    body = launchd_plist(job)
    if body is None:
        _launchd_unregister(job["id"])
        return True, "No schedule: this job runs only when you press Run Now."
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        return False, f"Could not write the launchd job at {path}: {exc}"

    # bootout first so a changed schedule replaces the running agent rather
    # than failing with "service already loaded".
    _launchctl(["bootout", _launchd_domain(), launchd_label(job["id"])])
    ok, output = _launchctl(["bootstrap", _launchd_domain(), path])
    if ok:
        return True, f"Scheduled with launchd ({launchd_label(job['id'])})."
    return False, output or "launchctl refused to load the job."


def _launchd_domain() -> str:
    return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/501"


def _launchd_unregister(job_id: str, legacy: bool = False) -> Tuple[bool, str]:
    ok, output = _launchctl(["bootout", _launchd_domain(), launchd_label(job_id, legacy)])
    try:
        os.remove(launchd_plist_path(job_id, legacy))
    except OSError:
        pass  # never registered, or already gone
    return ok, output


def _launchd_exists(job_id: str, legacy: bool = False) -> bool:
    return os.path.isfile(launchd_plist_path(job_id, legacy))


def _launchctl(args: List[str]) -> Tuple[bool, str]:
    """Run launchctl, never raising. Same contract as `_run` for schtasks."""
    try:
        completed = subprocess.run(["launchctl"] + args,
                                   capture_output=True, text=True)
    except (OSError, ValueError) as exc:
        return False, f"Could not call launchctl: {exc}"
    output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return completed.returncode == 0, output


def _run(args: List[str]) -> Tuple[bool, str]:
    """Run a schtasks command, returning `(ok, output)`.

    Never raises: a machine without schtasks (macOS, Linux) or a policy that
    blocks it must leave the job configured and runnable by hand, not break
    the screen.
    """
    if os.name != "nt":
        return False, ("Windows Task Scheduler is not available on this "
                       "platform. Run this job manually with Run Now.")
    try:
        completed = subprocess.run(args, capture_output=True, text=True,
                                   creationflags=_NO_WINDOW)
    except (OSError, ValueError) as exc:
        return False, f"Could not call schtasks: {exc}"
    output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
    return completed.returncode == 0, output


def register_task(job: Dict) -> Tuple[bool, str]:
    """Create or replace the OS scheduled task for `job`.

    A job whose schedule is "manual" has no task, so any existing one is
    removed -- switching a job to manual has to actually stop it running.
    """
    if sys.platform == "darwin":
        return _launchd_register(job)
    if os.name != "nt":
        return False, ("Scheduling is implemented for Windows and macOS only. "
                       "This job still runs when you press Run Now.")
    args = schtasks_args(job)
    if args is None:
        unregister_task(job["id"])
        return True, "No schedule: this job runs only when you press Run Now."
    return _run(args)


def unregister_task(job_id: str, legacy: bool = False) -> Tuple[bool, str]:
    """Delete the OS scheduled task for `job_id`. A job that never had one
    reports failure from schtasks, which the caller treats as "nothing to
    remove" rather than an error. `legacy=True` targets the pre-rename task."""
    if sys.platform == "darwin":
        return _launchd_unregister(job_id, legacy)
    return _run(["schtasks", "/delete", "/tn", task_name(job_id, legacy), "/f"])


def task_exists(job_id: str, legacy: bool = False) -> bool:
    """Whether the OS scheduler holds a task for `job_id` (or, with
    `legacy=True`, one under the pre-rename name)."""
    if sys.platform == "darwin":
        return _launchd_exists(job_id, legacy)
    ok, _ = _run(["schtasks", "/query", "/tn", task_name(job_id, legacy)])
    return ok


def scheduler_name() -> str:
    """What the OS calls its scheduler, for user-facing copy.

    The Backup screen used to say "Windows Task Scheduler" on every platform,
    including the one where that is not what happens.
    """
    if sys.platform == "darwin":
        return "launchd"
    if os.name == "nt":
        return "Windows Task Scheduler"
    return "the system scheduler"


def schedule_label(job: Dict) -> str:
    """How a job's schedule reads in the jobs table."""
    schedule = job.get("schedule", "manual")
    if schedule == "daily":
        return f"Daily at {job.get('at', '20:00')}"
    if schedule == "weekly":
        return f"Weekly, Mon at {job.get('at', '20:00')}"
    return "Manual only"


def sync_task(job: Dict) -> Tuple[bool, str]:
    """Bring the OS scheduler in line with the job as stored. The one entry
    point the UI calls after any add or edit."""
    return register_task(job)
