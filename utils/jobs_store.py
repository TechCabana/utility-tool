# utils/jobs_store.py
"""Backup job storage: the jobs file, its `.bak`, the jobs-file lock and job
CRUD, plus the job-field constants (schedules, retention, verify modes).

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.
"""
import contextlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Tuple

from utils import locks
from utils.fs_utils import app_dir


# A path, not a snapshot: nothing is read until load_jobs() opens it, so
# utils/migrate.py copying a pre-rename jobs file into app_dir() any time
# before that first read is enough for it to be found here.
JOBS_FILENAME = "backup_jobs.json"
JOBS_FILE = os.path.join(app_dir(), JOBS_FILENAME)


SCHEDULES = ("manual", "daily", "weekly")
DEFAULT_RETENTION = 5


# How hard a run checks its own copy before the version counts (card 42).
# "full" re-reads every copied file; "quick" checks every file's size and
# re-reads a sample; "off" skips the re-read and the version is never counted
# as verified. A missing or unknown value is "full": the safe default, and an
# old or hand-edited jobs file must never weaken or crash a run.
VERIFY_MODES = ("full", "quick", "off")
DEFAULT_VERIFY = "full"


def job_verify_mode(job: Dict) -> str:
    """`job`'s verification mode; "full" when it is missing or unknown."""
    mode = job.get("verify")
    return mode if mode in VERIFY_MODES else DEFAULT_VERIFY


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
            target_volume: str = "", verify: str = DEFAULT_VERIFY) -> Dict:
    """A job dict with a fresh id. Nothing is written to disk here.

    `target_volume` is the identity of the drive `target` was on when the job
    was set up (see `target_volume()`); "" means not recorded yet, and the
    first successful run records it. `verify` is one of `VERIFY_MODES`;
    anything else is stored as "full".
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
        "verify": verify if verify in VERIFY_MODES else DEFAULT_VERIFY,
        "last_run": "",
        "last_result": "",
        "last_skipped": 0,
        "last_skipped_sample": [],
        "last_pruning_held": False,
        "last_baseline_files": 0,
        "last_files": 0,
    }


# How many unreadable-file names a job keeps from its last run (card 50).
SKIPPED_SAMPLE_MAX = 5


def job_skipped(job: Dict) -> int:
    """How many files `job`'s last run did not copy; 0 when the field is
    missing (an older jobs file) or is not a positive whole number."""
    value = job.get("last_skipped")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def job_skipped_sample(job: Dict) -> List[str]:
    """The (at most `SKIPPED_SAMPLE_MAX`) names stored with `last_skipped`."""
    sample = job.get("last_skipped_sample")
    if not isinstance(sample, list):
        return []
    return [name for name in sample if isinstance(name, str)][:SKIPPED_SAMPLE_MAX]


def _count(value) -> int:
    """`value` if it is a positive whole number (not a bool), else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def job_pruning_held(job: Dict) -> bool:
    """True when `job`'s last run held pruning (the source sanity guard, card 51).

    False for a job from an older jobs file (no fields) and for any junk: the
    flag must be exactly True and both counts the warning quotes must be
    positive whole numbers.
    """
    return (job.get("last_pruning_held") is True
            and _count(job.get("last_baseline_files")) > 0
            and _count(job.get("last_files")) > 0)


def job_baseline_files(job: Dict) -> int:
    """The file count the held run was compared with; 0 when not held."""
    return _count(job.get("last_baseline_files")) if job_pruning_held(job) else 0


def job_last_files(job: Dict) -> int:
    """How many files the last successful run copied; 0 if unknown."""
    return _count(job.get("last_files"))


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


def _locks_dir() -> str:
    """`<app data>/locks`, created. Lives in the app-data folder rather than
    the backup target so a lock works with the target drive absent, never
    writes to the target before pre-flight passes, and is shared by the GUI
    and the headless run (both use `app_dir()`)."""
    path = os.path.join(app_dir(), "locks")
    os.makedirs(path, exist_ok=True)
    return path


@contextlib.contextmanager
def _jobs_file_lock() -> Iterator[None]:
    """Hold the jobs-file lock for one load -> modify -> save.

    Without it two processes (the GUI and a scheduled headless run) can both
    load the file, each change a different job, and the later save silently
    erases the earlier one. Short-lived: waits up to ~5 s for the holder, then
    raises OSError. Taken once per top-level operation by `save_job`,
    `remove_job`, `record_run` and `restore_jobs_from_backup`; they call the
    unlocked `_save_job` internally, so nothing ever waits on itself.
    """
    lock = locks.acquire_wait(os.path.join(_locks_dir(), "jobs-file.lock"),
                              "jobs file update")
    try:
        yield
    finally:
        lock.release()


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
    with _jobs_file_lock():
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


def _save_job(job: Dict) -> None:
    """`save_job` without taking the jobs-file lock; the caller holds it."""
    jobs = load_jobs()
    for index, existing in enumerate(jobs):
        if existing.get("id") == job["id"]:
            jobs[index] = job
            break
    else:
        jobs.append(job)
    save_jobs(jobs)


def save_job(job: Dict) -> None:
    """Insert or replace `job` by id. Does not touch the OS scheduler --
    `sync_task()` is called separately so a storage failure and a scheduler
    failure are reported distinctly. Raises JobsFileCorrupt, writing nothing,
    if the stored file is damaged; OSError if the jobs-file lock times out."""
    with _jobs_file_lock():
        _save_job(job)


def remove_job(job_id: str) -> None:
    """Drop the job with this id. Raises JobsFileCorrupt if the file is damaged."""
    with _jobs_file_lock():
        save_jobs([job for job in load_jobs() if job.get("id") != job_id])


def record_run(job_id: str, result: Dict) -> Optional[Dict]:
    """Write a run's outcome back onto the stored job, and return it.

    Re-read from disk rather than mutating the caller's copy: the headless CLI
    and the GUI can both be looking at this file, and the run is the only
    field either of them writes during a run. Raises JobsFileCorrupt if the
    jobs file has been damaged since the run began, OSError if the jobs-file
    lock cannot be taken. Do not call this for a `busy` result from `run_job`.
    """
    with _jobs_file_lock():
        job = get_job(job_id)
        if job is None:
            return None
        if result.get("ok") and result.get("target_volume") and not job.get("target_volume"):
            # Trust on first use: a job made before volume identity existed learns
            # its drive from its first successful run, and is checked against it
            # from then on.
            job["target_volume"] = result["target_volume"]
        job["last_run"] = datetime.now().isoformat(timespec="seconds")
        job["last_result"] = ("ok" if result.get("ok") else "failed") +             (f": {result.get('message', '')}" if not result.get("ok") else "")
        # A run that finished but left files behind is still "ok" (readers of
        # last_result keep working); the count and a few names ride beside it
        # so the screen can say so. The count is the one the run's own message
        # uses, so the screen and the message agree.
        errors = result.get("errors") or []
        skipped = (max(len(errors), result.get("not_copied", 0))
                   if result.get("ok") else 0)
        job["last_skipped"] = skipped
        # Names only, never the OS error text (it can carry a full path).
        job["last_skipped_sample"] = [
            str(error).split(": ", 1)[0] for error in errors[:SKIPPED_SAMPLE_MAX]
        ] if skipped else []
        # Card 51: a run that held pruning says so on the job. The flag is
        # recomputed per run, so a normal run (or a failed one) clears it.
        held = bool(result.get("ok") and result.get("pruning_held") is True
                    and _count(result.get("baseline_files")) > 0)
        job["last_pruning_held"] = held
        job["last_baseline_files"] = _count(result.get("baseline_files")) if held else 0
        job["last_files"] = _count(result.get("files")) if result.get("ok") else 0
        _save_job(job)
    return job
