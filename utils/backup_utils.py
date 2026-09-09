# utils/backup_utils.py
"""Backup jobs: storage, the versioned copy itself, the changes-since
comparison, restore, and the Windows Task Scheduler registration behind the
"schedule" field.

Qt-free by design (CLAUDE.md §3/§8) -- plain paths and plain dicts in, plain
dicts out. `tabs/disk_tab.py` runs the long ones from a QThread worker and
renders what comes back; nothing here knows that. `main.py --run-backup-job`
calls `run_job` directly, with no QApplication at all, which is what the
scheduled task invokes.

Storage mirrors `utils/presets.py` exactly -- one JSON file in the OS
app-data directory, via that module's own `app_dir()`. Unlike presets there
is no first-run default set: a machine with no backup jobs has none, and the
Backup screen's EmptyState is the correct rendering of that.

**Scheduling is Windows-only in this version.** The owner's decision for this
card was to hand scheduling to the OS while keeping configuration in the app,
and this machine is Windows, so the real path implemented here is
`schtasks.exe` (ships with Windows, no new dependency). macOS `launchd` is a
deliberate, named gap -- `register_task` reports "not supported on this
platform" there rather than pretending a job is scheduled, and the job still
runs fine from Run Now. That is the one place this repo departs from
CLAUDE.md §1's "no per-OS branching" goal, by explicit owner decision.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from utils.disk_utils import _is_link_like
from utils.presets import app_dir

JOBS_FILE = os.path.join(app_dir(), "backup_jobs.json")

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

# Windows only: keep schtasks' console window from flashing over the app.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BackupAborted(Exception):
    """Raised inside the copy to stop it when the user quits mid-run.

    Not an OSError on purpose: `shutil.copytree` swallows OSError per entry
    and carries on, which is exactly the behaviour wanted for a locked file
    and exactly wrong for a cancellation.
    """


# ---------------------------------------------------------------------------
# Job storage (same shape as utils/presets.py)
# ---------------------------------------------------------------------------

def new_job(name: str, source: str, target: str, schedule: str = "daily",
            at: str = "20:00", retention: int = DEFAULT_RETENTION) -> Dict:
    """A job dict with a fresh id. Nothing is written to disk here."""
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "source": source,
        "target": target,
        "schedule": schedule if schedule in SCHEDULES else "manual",
        "at": at,
        "retention": max(1, int(retention)),
        "last_run": "",
        "last_result": "",
    }


def load_jobs() -> List[Dict]:
    """Every configured job, or an empty list.

    A missing or corrupt file reads as "no jobs" rather than raising: the
    Backup screen must still open on a machine whose jobs.json was hand-edited
    into invalid JSON, and an EmptyState is a truthful rendering of a file
    that cannot be read.
    """
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_jobs(jobs: List[Dict]) -> None:
    with open(JOBS_FILE, "w", encoding="utf-8") as handle:
        json.dump(jobs, handle, indent=2)


def get_job(job_id: str) -> Optional[Dict]:
    for job in load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def save_job(job: Dict) -> None:
    """Insert or replace `job` by id. Does not touch the OS scheduler --
    `sync_task()` is called separately so a storage failure and a scheduler
    failure are reported distinctly."""
    jobs = load_jobs()
    for index, existing in enumerate(jobs):
        if existing.get("id") == job["id"]:
            jobs[index] = job
            break
    else:
        jobs.append(job)
    save_jobs(jobs)


def remove_job(job_id: str) -> None:
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

    Reuses `disk_utils._is_link_like` rather than a second link predicate:
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
    raised -- same contract as `disk_utils.folder_size`.
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
    """
    source = job.get("source", "")
    target = job.get("target", "")
    if not source or not os.path.isdir(source):
        return {"ok": False, "message": f"Source folder not found: {source or '(not set)'}"}
    if not target:
        return {"ok": False, "message": "No target folder is set for this job"}

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
            "bytes": copied_bytes, "pruned": pruned, "errors": errors}


def record_run(job_id: str, result: Dict) -> Optional[Dict]:
    """Write a run's outcome back onto the stored job, and return it.

    Re-read from disk rather than mutating the caller's copy: the headless CLI
    and the GUI can both be looking at this file, and the run is the only
    field either of them writes during a run.
    """
    job = get_job(job_id)
    if job is None:
        return None
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
        return {"ok": False, "message": "Source folder not found", "changed": 0}

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

def task_name(job_id: str) -> str:
    """The scheduled task's name. Prefixed so every task this app owns is
    identifiable in Task Scheduler, and keyed on the job id so a rename never
    strands one."""
    return f"UtilityTool_Backup_{job_id}"


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
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --run-backup-job {job_id}'
    entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    return f'"{sys.executable}" "{entry}" --run-backup-job {job_id}'


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


def _run(args: List[str]) -> Tuple[bool, str]:
    """Run a schtasks command, returning `(ok, output)`.

    Never raises: a machine without schtasks (macOS, Linux) or a policy that
    blocks it must leave the job configured and runnable by hand, not break
    the screen.
    """
    if os.name != "nt":
        return False, ("OS scheduling is only implemented for Windows "
                       "(schtasks). Run this job manually with Run Now.")
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
    args = schtasks_args(job)
    if args is None:
        unregister_task(job["id"])
        return True, "No schedule: this job runs only when you press Run Now."
    return _run(args)


def unregister_task(job_id: str) -> Tuple[bool, str]:
    """Delete the OS scheduled task for `job_id`. A job that never had one
    reports failure from schtasks, which the caller treats as "nothing to
    remove" rather than an error."""
    return _run(["schtasks", "/delete", "/tn", task_name(job_id), "/f"])


def task_exists(job_id: str) -> bool:
    ok, _ = _run(["schtasks", "/query", "/tn", task_name(job_id)])
    return ok


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
