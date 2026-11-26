# utils/jobs_store.py
"""Backup job storage: the jobs file, its `.bak`, the jobs-file lock and job
CRUD, plus the job-field constants (schedules, retention, verify modes).

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.
"""
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
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
        "last_changed": 0,
        "last_changed_sample": [],
        "last_links": 0,
        "last_links_sample": [],
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


def job_changed(job: Dict) -> int:
    """How many files `job`'s last run kept although they changed while being
    copied (card RnuRsA2V); 0 when the field is missing (an older jobs file) or
    is not a positive whole number."""
    value = job.get("last_changed")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def job_changed_sample(job: Dict) -> List[str]:
    """The (at most `SKIPPED_SAMPLE_MAX`) relative paths stored with `last_changed`."""
    sample = job.get("last_changed_sample")
    if not isinstance(sample, list):
        return []
    return [name for name in sample if isinstance(name, str)][:SKIPPED_SAMPLE_MAX]


def job_links(job: Dict) -> int:
    """How many shortcuts (symlinks, junctions) `job`'s last run did not follow
    (card tiha5I8F); 0 when the field is missing (an older jobs file) or is not a
    positive whole number."""
    value = job.get("last_links")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def job_links_sample(job: Dict) -> List[str]:
    """The (at most `SKIPPED_SAMPLE_MAX`) relative paths stored with `last_links`."""
    sample = job.get("last_links_sample")
    if not isinstance(sample, list):
        return []
    return [name for name in sample if isinstance(name, str)][:SKIPPED_SAMPLE_MAX]


# A version stamp (`versions.VERSION_FORMAT`); `[0-9]`, not `\d`: no Unicode digits.
_STAMP_RE = re.compile(r"[0-9]{8}-[0-9]{6}")


def _count(value) -> int:
    """`value` if it is a positive whole number (not a bool), else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# ---------------------------------------------------------------------------
# The outcome word (card C5HuzNVK)
# ---------------------------------------------------------------------------
#
# `last_result` carries the run's real outcome as ONE word: `ok`,
# `ok_pruning_held`, `ok_skipped`, `ok_changed`, `ok_links`, or the text forms
# `failed: <message>` and `interrupted: <message>`. The ranking below is the ONLY
# place that decides it: the run log (`runlog._outcome`) and `record_run` both
# call it, so the log line and the stored word cannot disagree.
#
# THE WORD IS THE HEADLINE, THE SIBLING FIELDS ARE THE DETAIL. A run can skip a
# file, see another change and not follow a shortcut at once; the word names
# the highest-ranked one only, and `last_skipped`, `last_changed`, `last_links`
# and `last_pruning_held` still carry every count and name. The Backup tab builds
# "OK, 1 skipped, 1 changed during backup, 2 shortcuts not followed" from those
# siblings and never from the word, which is what keeps a job saved by an older
# build (plain `ok` beside a skipped count) displaying exactly like a new one.


def run_counts(result: Dict) -> Tuple[int, int, int]:
    """`(skipped, changed, links)` for a `run_job` result: what a finished run
    left behind. All zero for a failed run, which finished nothing. The one
    place these three counts are derived (`record_run` and the run log share it).
    """
    if not result.get("ok"):
        return 0, 0, 0
    errors = result.get("errors") or []
    return (max(len(errors), _count(result.get("not_copied"))),
            _count(result.get("changed")), _count(result.get("links_skipped")))


def ok_word(result: Dict) -> str:
    """The word for a run that finished ok: the highest-ranked thing it left
    behind. Ranking, first match wins: pruning held, skipped, changed, links,
    then plain `ok`. Callers have checked `result["ok"]`. `pruning_held` is read
    as the run log always read it (exactly True); `run_job` sets it only with a
    positive `baseline_files`, which `record_run` also requires of the sibling
    flag, so the word and `last_pruning_held` agree for every real run."""
    skipped, changed, links = run_counts(result)
    if result.get("pruning_held") is True:
        return "ok_pruning_held"
    if skipped:
        return "ok_skipped"
    if changed:
        return "ok_changed"
    return "ok_links" if links else "ok"


def outcome_word(result: Dict) -> str:
    """Map a `run_job` result onto one outcome word (the run log's `outcome`).

    `busy`, `interrupted` and `hold_released` are not runs that finished and
    are decided first, in that order; then ok runs get `ok_word`; a failed run
    is `cancelled` when its message says the owner stopped it, else `failed`.
    `record_run` stores only the ok words from here: a failed run keeps its
    `failed: <message>` text (a stopped run is stored as `failed: Backup
    cancelled` until card 111 changes that, and then this function already
    returns the word it will store).
    """
    if result.get("busy"):
        return "busy"
    if result.get("interrupted") is True:
        return "interrupted"
    if result.get("hold_released") is True:
        return "hold_released"
    if result.get("ok"):
        return ok_word(result)
    if str(result.get("message", "")).startswith("Backup cancelled"):
        return "cancelled"
    return "failed"


def result_is_ok(value) -> bool:
    """True when a stored `last_result` reads as success: any word that starts
    with "ok" (`ok`, `ok_skipped`, ...). The one `startswith("ok")` readers
    share. Anything that is not a string (a hand-edited 42 or null) is not
    success, and never raises."""
    return isinstance(value, str) and value.startswith("ok")


def job_result(job: Dict) -> str:
    """`job`'s stored `last_result` as a string; "" for a missing or non-string
    value, so a reader can call string methods on it without a guard."""
    value = job.get("last_result")
    return value if isinstance(value, str) else ""


# The UTC time a backup finished, as recorded in its manifest trailer
# (`finished_utc`) and on the job (`last_finished_utc`): fixed-width ISO-8601
# with a literal "Z", e.g. 2026-10-25T01:45:12Z. Fixed width is what lets two
# of them be compared as plain strings (card ved3wVOQ).
FINISHED_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FINISHED_UTC_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def valid_finished_utc(value) -> Optional[str]:
    """`value` if it is a well-formed `FINISHED_UTC_FORMAT` string naming a
    real moment, else None. Junk (wrong type, wrong shape, month 13, a local
    time with an offset, padding) is never trusted for ordering."""
    if not isinstance(value, str) or not _FINISHED_UTC_RE.fullmatch(value):
        return None
    try:
        datetime.strptime(value, FINISHED_UTC_FORMAT)
    except ValueError:
        return None
    return value


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


def job_hold_released(job: Dict) -> Optional[Dict]:
    """The owner's "this is expected" acknowledgement stored on `job` (card
    QnFM2GMZ), or None when there is none or it is junk.

    A valid one is `{"stamp", "files", "level", "since", "time"}`: the baseline
    version that was let go (`stamp`, holding `files` files), the file count
    the source had when it was released (`level`), the newest version at that
    moment (`since`, the first version the guard compares against again) and
    when it happened. Anything else -- a missing field, a wrong type, a stamp
    that is not a version name -- reads as no acknowledgement, so the guard
    falls back to guarding as before rather than trusting a hand-edited value.
    """
    release = job.get("hold_released")
    if not isinstance(release, dict):
        return None
    stamp, since = release.get("stamp"), release.get("since")
    # `stamp` may be "" (the baseline was gone from the disk when released);
    # `since` is what the guard acts on, so it must be a real version name.
    if not (isinstance(stamp, str) and (stamp == "" or _STAMP_RE.fullmatch(stamp))
            and isinstance(since, str) and _STAMP_RE.fullmatch(since)):
        return None
    if not _count(release.get("files")) or not _count(release.get("level")):
        return None
    return release


# ---------------------------------------------------------------------------
# The "a run has started" marker (card PQPfznG1)
# ---------------------------------------------------------------------------
#
# A run used to write nothing to its job until it finished, so a run that was
# killed (Task Scheduler stopping it on battery, a shutdown, a crash) left the
# PREVIOUS run's "ok" standing: the owner was told a backup succeeded that
# never completed. Now the run records, just before it copies, a `running`
# field on its job:
#
#     running = {"since": "<local ISO time>", "pid": N, "start_time": "<opaque>"|None,
#                "stamp": "<version stamp>"|None, "token": "<random>"}
#
# `record_run` removes it in the same atomic write that records the outcome of
# ANY finished run. A marker that is still there while its owner process is
# gone is an interrupted run (`runner.job_interrupted`). The file is the one
# thing the Backup tab and the headless run already read, which is why the
# marker lives on the job rather than in the run log.
#
# `pid` + `start_time` are the same owner identity the per-job lock records
# (`locks.process_start_time`), so "is the owner alive" has one source of truth
# (`locks._judge`) and a reused pid is told from the real owner.

def job_running(job: Dict) -> Optional[Dict]:
    """The start marker stored on `job` as `{since, pid, start_time, stamp}`, or
    None when there is none or it is junk.

    A hand-edited or half-written value must read as "no marker", never crash
    the Backup tab: `since` must parse as a time, `pid` must be a positive
    whole number (not a bool), `start_time` text or null. A `stamp` that is not
    a version name is dropped (the marker stays valid without it).
    """
    marker = job.get("running")
    if not isinstance(marker, dict):
        return None
    since, pid, start_time = marker.get("since"), marker.get("pid"), marker.get("start_time")
    if not isinstance(since, str):
        return None
    try:
        datetime.fromisoformat(since)
    except ValueError:
        return None
    if not _count(pid) or not (start_time is None or isinstance(start_time, str)):
        return None
    stamp = marker.get("stamp")
    if not (isinstance(stamp, str) and _STAMP_RE.fullmatch(stamp)):
        stamp = None
    return {"since": since, "pid": pid, "start_time": start_time, "stamp": stamp}


def new_running_marker(stamp: Optional[str]) -> Dict:
    """A marker for a run of THIS process starting now (`stamp` is the version
    it claimed). The clock is read here, not in the runner, so a test that
    freezes the runner's clock does not change what a marker holds."""
    pid = os.getpid()
    return {"since": datetime.now().isoformat(timespec="seconds"), "pid": pid,
            "start_time": locks.process_start_time(pid), "stamp": stamp,
            "token": uuid.uuid4().hex}


def running_owner_gone(marker: Dict) -> bool:
    """True when the process that wrote `marker` is no longer running it.

    Asks `locks._judge`, the question the per-job lock asks of its holder: a
    dead pid, or a live pid whose start time differs (the pid was reused), is
    gone; a live pid with the SAME start time is the owner however long it has
    run (a backup of more than a day stays "running"); an owner whose start
    time was not recorded or cannot be read now is unverified and gets the
    lock's age rule instead.
    """
    try:
        age = (datetime.now() - datetime.fromisoformat(marker["since"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = 0.0
    stale, _reused = locks._judge(
        {"pid": marker["pid"], "start_time": marker.get("start_time")}, max(age, 0.0))
    return stale


def set_running(job_id: str, marker: Dict) -> bool:
    """Store `marker` as `job_id`'s `running` field; False if there is no such job.

    Re-reads the job under the jobs-file lock and sets ONLY this field, as
    `record_run` and `record_precheck` do: the caller's job dict can be older
    than a hold released, a pre-check recorded or a schedule edited meanwhile,
    and writing it back would undo them. Raises JobsFileCorrupt or OSError
    like `save_job`; the runner treats both as "no marker", never as a failed
    backup.
    """
    with _jobs_file_lock():
        job = get_job(job_id)
        if job is None:
            return False
        job["running"] = marker
        _save_job(job)
    return True


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


# Windows refuses to replace a file another process has open (WinError 5),
# and readers (`load_jobs` in the Backup screen, a headless run's `get_job`,
# an antivirus scan) open the jobs file without taking the jobs-file lock. A
# read lasts milliseconds, so a few short retries ride it out; without them a
# run's start marker was silently dropped (a killed run then still read "ok")
# and record_run failed outright.
REPLACE_ATTEMPTS = 20
REPLACE_PAUSE_SECONDS = 0.05


def _replace(temp: str, path: str) -> None:
    """`os.replace`, retried briefly while another process has `path` open."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_PAUSE_SECONDS)


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
        _replace(temp, path)
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


class QuarantineRefused(Exception):
    """`quarantine_jobs_file` declined because doing it could destroy good data
    (the jobs file or its `.bak` is usable after all) or there is nothing to
    move. Nothing was changed."""


def _rename_aside(path: str, stamp: str) -> str:
    """Rename `path` to ``<path>.corrupt-<stamp>`` (``-2``, ``-3``... if that
    name is taken) and return the new name. Never overwrites and never copies.

    os.rename refuses an existing destination on Windows; the exists() check
    covers POSIX, where it would silently replace it. ponytail: the
    check-then-rename gap on POSIX is closed in practice by the jobs-file lock
    (the only writer of these names); use os.link + os.unlink if that changes.
    """
    base = f"{path}.corrupt-{stamp}"
    for number in range(1, 1000):
        kept = base if number == 1 else f"{base}-{number}"
        if os.path.lexists(kept):
            continue
        try:
            os.rename(path, kept)
        except FileExistsError:
            continue
        return kept
    raise OSError(f"Could not find a free name to keep '{path}' under.")


def quarantine_jobs_file() -> str:
    """Move a damaged jobs file aside so the user can start with an empty list.

    For the one state with no other way out: the file is damaged and there is
    no usable `.bak`. Renames (never copies, never deletes) the file to
    ``backup_jobs.json.corrupt-<YYYYMMDD-HHMMSS>`` and returns that path; a
    stale, unusable `.bak` goes aside the same way. Afterwards `load_jobs()`
    returns `[]` and `save_job` works again.

    Everything is decided under the jobs-file lock from a fresh read, because
    the screen's idea of the state may be stale: raises `QuarantineRefused`,
    having changed nothing, if the jobs file is now valid or missing or a
    usable `.bak` exists -- moving either would hide good data. OSError (the
    jobs file is left where it was) if a rename fails.
    """
    with _jobs_file_lock():
        try:
            _read_valid(JOBS_FILE)
        except FileNotFoundError:
            raise QuarantineRefused(
                f"There is no jobs file ('{JOBS_FILE}') to set aside.") from None
        except ValueError:
            pass  # damaged: the case this exists for
        else:
            raise QuarantineRefused(
                "The jobs file can be read again, so it was left alone.")
        if _has_usable_backup():
            raise QuarantineRefused(
                f"A usable backup copy ('{_backup_path()}') exists; restore it "
                "instead of starting with an empty list.")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        moved_bak = None
        if os.path.lexists(_backup_path()):
            # Unusable (checked above). Moved first: if it fails nothing has
            # changed, and if the jobs file then fails this is put back.
            try:
                moved_bak = _rename_aside(_backup_path(), stamp)
            except OSError as exc:
                raise OSError(f"Could not set aside '{_backup_path()}': {exc}") from exc
        try:
            return _rename_aside(JOBS_FILE, stamp)
        except OSError as exc:
            if moved_bak:
                try:
                    os.rename(moved_bak, _backup_path())
                except OSError:
                    pass  # it was unusable anyway; it stays under its new name
            raise OSError(f"Could not set aside '{JOBS_FILE}': {exc}") from exc


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


# What the job form edits. Everything else on a stored job is written by runs,
# pre-checks and releases (`record_run`, `record_precheck`, `set_running`,
# `runner.release_hold`) and must not be rolled back by an edit.
SETTINGS_KEYS = ("name", "source", "target", "schedule", "at", "retention",
                 "target_volume", "verify")


def save_job_settings(job: Dict) -> None:
    """Save what the job form edits: a new job whole, an existing one by
    updating only `SETTINGS_KEYS` on a fresh read under the jobs-file lock.

    The Backup screen loads its jobs once and edits that copy. Saved whole, a
    copy loaded while a run was going wrote back that run's start marker after
    it had finished (the job then read "Interrupted" and the run log gained a
    false line), and a copy older than a release undid it. Raises like
    `save_job`.
    """
    with _jobs_file_lock():
        stored = get_job(job["id"])
        if stored is not None:
            stored.update({key: job[key] for key in SETTINGS_KEYS if key in job})
            job = stored
        _save_job(job)


def remove_job(job_id: str) -> None:
    """Drop the job with this id. Raises JobsFileCorrupt if the file is damaged."""
    with _jobs_file_lock():
        save_jobs([job for job in load_jobs() if job.get("id") != job_id])


def record_precheck(job_id: str, summary: Dict) -> Optional[Dict]:
    """Store a pre-check's summary (`utils.precheck.summary`) on the job as
    `last_precheck`, and return the job; None if there is no such job.

    Same re-read-under-the-lock as `record_run`, so it cannot undo a run
    recorded meanwhile. Every other field, known or not, is kept as it was.
    Raises JobsFileCorrupt or OSError like `save_job`.
    """
    with _jobs_file_lock():
        job = get_job(job_id)
        if job is None:
            return None
        job["last_precheck"] = summary
        _save_job(job)
    return job


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
        # The real outcome, not a plain "ok" (card C5HuzNVK); see the block
        # above `run_counts` for why the word is only the headline.
        job["last_result"] = (ok_word(result) if result.get("ok")
                              else f"failed: {result.get('message', '')}")
        # The run is over, whatever the outcome: the start marker goes in the
        # SAME atomic write as the result, never before it. Cleared first and
        # recorded after, a crash between the two would leave the previous
        # "ok" standing with no sign a run ever began -- the lie this marker
        # exists to prevent. See `job_running`.
        job.pop("running", None)
        # A run that finished but left files behind has its word in last_result
        # (readers that test startswith("ok") keep working); the counts and a few
        # names ride beside it so the screen can say all of it. The count is the
        # one the run's own message uses, so the screen and the message agree.
        errors = result.get("errors") or []
        skipped, changed, links = run_counts(result)
        job["last_skipped"] = skipped
        # File names only: not the OS error text (it can carry a full path) and
        # not the folder path the run log keeps (`copy_tree` errors read
        # "<path from the source root>: <error>").
        job["last_skipped_sample"] = [
            str(error).split(": ", 1)[0].rsplit("/", 1)[-1]
            for error in errors[:SKIPPED_SAMPLE_MAX]
        ] if skipped else []
        # Card RnuRsA2V: files kept although they were still changing while
        # copied. Same shape as the skipped pair, and a sibling of it: the screen
        # builds "OK, N changed during backup" from this count. Relative paths
        # (from the source root, forward slashes), never an absolute one, and
        # only the successful run's: a failed run kept nothing.
        job["last_changed"] = changed
        job["last_changed_sample"] = [
            name for name in (result.get("changed_sample") or [])
            if isinstance(name, str)][:SKIPPED_SAMPLE_MAX] if changed else []
        # Card tiha5I8F: shortcuts the run did not follow. Same shape again (the
        # screen builds "OK, N shortcuts not followed"): relative paths only,
        # never the target a link points at, and only a finished run's, since a
        # failed run made no statement about the tree.
        job["last_links"] = links
        job["last_links_sample"] = [
            name for name in (result.get("links_sample") or [])
            if isinstance(name, str)][:SKIPPED_SAMPLE_MAX] if links else []
        # Card 51: a run that kept its baseline aside says so on the job. Only a
        # successful run recomputes the three fields: a failed run (often the
        # very low-space failure a hold can lead to) says nothing about the
        # source's file count, so it must not hide that older backups are
        # still being kept aside.
        if result.get("ok"):
            held = bool(result.get("pruning_held") is True
                        and _count(result.get("baseline_files")) > 0)
            job["last_pruning_held"] = held
            job["last_baseline_files"] = _count(result.get("baseline_files")) if held else 0
            job["last_files"] = _count(result.get("files"))
            # Card ved3wVOQ: when the backup finished, in UTC (the same value
            # as its manifest trailer). Only a valid one is written, so a
            # missing or junk value leaves any earlier one alone. It is not in
            # SETTINGS_KEYS, so a job-form save can never roll it back.
            finished = valid_finished_utc(result.get("finished_utc"))
            if finished:
                job["last_finished_utc"] = finished
        _save_job(job)
    return job
