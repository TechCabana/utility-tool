# utils/runlog.py
"""The per-job run log: one JSON line per run, kept in `<app data>/logs`.

Qt-free, like the rest of the engine. Before this, the only memory of a run was
the `last_result` string the next run overwrote, and a headless run that hit a
damaged jobs file left nothing at all. Silence is never success.

**Format.** `<app data>/logs/<job id>.jsonl`, one object per line, UTF-8, oldest
first, newest `RUNLOG_MAX_RUNS` kept. A run that cannot be tied to a job (an
unknown id, a damaged or unreadable jobs file, a job with no usable id) goes to
the one app-level log, `logs/app.jsonl`. Every line carries exactly `LINE_KEYS`:

    v, start, end, trigger, job, name, outcome, files, bytes, skipped,
    skipped_paths, skipped_more, pruned, version, verify_mode, cause, message,
    pruning_held, offenders, baseline_files, baseline_stamp, retried

`retried` (card b5CfTkL8) counts the files that could not be read at first and
were copied by the one retry after the walk; they are in `files`, not `skipped`.

`offenders` (card 104) lists, from the source root, the files a run that failed
as "path_too_long" or "file_too_large" found the drive cannot hold (at most
`SKIPPED_PATHS_MAX`); it is empty otherwise.

`baseline_files` and `baseline_stamp` (card QnFM2GMZ) name the older, larger
backup a run held aside, or that the owner released with "This is expected":
its file count and its version stamp (0 and null when there is none). The
release itself is logged as one line with `outcome` "hold_released", `files`
the count the source had and `trigger` "manual" (the Backup screen) or "cli".

A run that started and never recorded an outcome (the process was killed, the
machine lost power, it crashed) is logged ONCE, when the next run, the headless
start or the Backup screen first finds its start marker with no owner left
(`runner.settle_interrupted`): `outcome` "interrupted", `trigger` "recovery",
`start` and `end` both the time the lost run began (the moment it died is not
known), `version` the version stamp it had claimed. No path, no file count.

`outcome` is one of `OUTCOMES`. PATHS AND COUNTS ONLY, never file contents or
OS error text beyond what a result's own message already carries. The line is
built from that explicit field list (`build_line`), never from the whole result
dict, so a key a future result grows cannot leak into the log.

**A log failure never changes a run.** `append_run` swallows every error and
returns a bool: a full disk, a read-only folder or a lock timeout must not turn
a good backup into a failed one.

**Locking.** The append and the trim happen under one short lock
(`runlog-<id>.lock` in the locks folder, via utils/locks.py). It is always
taken LAST -- after the per-job run lock is released and never while the
jobs-file lock is held -- and released at once, so it cannot deadlock with them.
"""
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from utils import jobs_store, locks
from utils.fs_utils import app_dir

RUNLOG_VERSION = 1
# Newest runs kept per log. ponytail: a count, not a byte cap -- a line is
# ~400 B plus at most 50 paths, so 200 lines stay well under a megabyte.
RUNLOG_MAX_RUNS = 200
SKIPPED_PATHS_MAX = 50
# A real message is a few hundred characters. The cap is what keeps "200 lines
# stay well under a megabyte" true whatever an exception's text turns out to be.
MESSAGE_MAX = 2000
APP_LOG_NAME = "app"
OUTCOMES = ("ok", "ok_skipped", "ok_pruning_held", "failed", "cancelled", "busy",
            "hold_released", "interrupted")
LINE_KEYS = ("v", "start", "end", "trigger", "job", "name", "outcome", "files",
             "bytes", "skipped", "skipped_paths", "skipped_more", "pruned",
             "version", "verify_mode", "cause", "message", "pruning_held",
             "offenders", "baseline_files", "baseline_stamp", "retried")
LOCK_TIMEOUT = 2.0


def safe_id(job_id: str) -> str:
    """`job_id` as a file-name stem: plain word characters pass through, anything
    else (a hand-edited id with a separator or `..`) is hashed, so no id can
    point outside its folder. The run lock names its file the same way."""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id):
        return job_id
    return "id-" + hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:16]


def _logs_dir() -> str:
    """`<app data>/logs`. Not created here: readers must not make folders.
    Tests redirect this ONE name (tests/conftest.py)."""
    return os.path.join(app_dir(), "logs")


def log_path(job_id: Optional[str] = None) -> str:
    """Where `job_id`'s log lives; the app-level log for None or a non-id."""
    if not isinstance(job_id, str) or not job_id:
        return os.path.join(_logs_dir(), APP_LOG_NAME + ".jsonl")
    return os.path.join(_logs_dir(), safe_id(job_id) + ".jsonl")


def _count(value) -> int:
    """`value` if it is a non-negative whole number (not a bool), else 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _text(value) -> Optional[str]:
    """`value` if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _outcome(result: Dict, skipped: int) -> str:
    """Map a `run_job` result onto one of `OUTCOMES`."""
    if result.get("busy"):
        return "busy"
    if result.get("interrupted") is True:
        return "interrupted"
    if result.get("hold_released") is True:
        return "hold_released"
    if result.get("ok"):
        if result.get("pruning_held") is True:
            return "ok_pruning_held"
        return "ok_skipped" if skipped else "ok"
    if str(result.get("message", "")).startswith("Backup cancelled"):
        return "cancelled"
    return "failed"


def build_line(job: Optional[Dict], result: Dict, trigger: str,
               start: datetime, end: datetime) -> Dict:
    """The log line for one run, from an explicit whitelist of result fields."""
    job = job if isinstance(job, dict) else {}
    errors = result.get("errors") or []
    # Same count record_run stores as `last_skipped` (card 50): a failed run
    # skipped nothing, it did not finish.
    skipped = (max(len(errors), _count(result.get("not_copied")))
               if result.get("ok") else 0)
    # The part before the first ": " is the path; the OS error text after it is
    # left out, as in `last_skipped_sample`.
    paths = [str(error).split(": ", 1)[0]
             for error in errors[:SKIPPED_PATHS_MAX]] if skipped else []
    pruned = result.get("pruned")
    mode = _text(result.get("verify_mode"))
    if mode is None and job:
        mode = jobs_store.job_verify_mode(job)
    return {
        "v": RUNLOG_VERSION,
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "trigger": trigger,
        "job": _text(job.get("id")),
        "name": _text(job.get("name")),
        "outcome": _outcome(result, skipped),
        "files": _count(result.get("files")),
        "bytes": _count(result.get("bytes")),
        "skipped": skipped,
        "skipped_paths": paths,
        "skipped_more": skipped - len(paths),
        "pruned": len(pruned) if isinstance(pruned, (list, tuple)) else _count(pruned),
        "version": _text(result.get("version")),
        "verify_mode": mode,
        "cause": _text(result.get("cause")),
        "message": str(result.get("message", ""))[:MESSAGE_MAX],
        "pruning_held": result.get("pruning_held") is True,
        # Paths only, from the source root; anything that is not a string is
        # dropped rather than logged.
        "offenders": [p for p in (result.get("offenders") or [])
                      if isinstance(p, str)][:SKIPPED_PATHS_MAX]
        if isinstance(result.get("offenders"), list) else [],
        "baseline_files": _count(result.get("baseline_files")),
        # A version name or nothing: never a path, whatever the result carries.
        "baseline_stamp": (result.get("baseline_stamp")
                           if isinstance(result.get("baseline_stamp"), str)
                           and jobs_store._STAMP_RE.fullmatch(result["baseline_stamp"])
                           else None),
        "retried": _count(result.get("retried")),
    }


def _is_line(text: str) -> bool:
    """True if `text` is one parseable run line (a JSON object)."""
    try:
        return isinstance(json.loads(text), dict)
    except ValueError:
        return False


def _trim(path: str) -> None:
    """Keep only the newest `RUNLOG_MAX_RUNS` lines; the caller holds the lock.

    Reads the file once and rewrites it only when it is over the cap. Unparseable
    lines are dropped by a rewrite (never counted as runs) but never stop one.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        lines = [line.rstrip("\r\n") for line in handle if line.strip()]
    if len(lines) <= RUNLOG_MAX_RUNS:
        return
    kept = [line for line in lines if _is_line(line)][-RUNLOG_MAX_RUNS:]
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in kept))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            os.remove(temp)
        except OSError:
            pass
        raise


def _append_line(path: str, line: str) -> None:
    """Append `line` to `path` as exactly one line; the caller holds the lock.

    Two things a plain text-mode append got wrong:

    * A file name can hold an unpaired surrogate (Windows names are UTF-16 and
      need not be well formed) and so can a message quoting it. UTF-8 refuses
      to encode one, so the whole line -- the run -- went unlogged.
      `backslashreplace` writes it as the `\\udXXX` escape, which is valid JSON
      and reads back as the same character.
    * A crash or a full disk mid-append leaves a fragment with no newline. The
      next line used to be glued onto it, so one torn line cost two runs. Start
      on a fresh line whenever the file does not end with one.
    """
    data = line.encode("utf-8", "backslashreplace") + b"\n"
    with open(path, "ab+") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                data = b"\n" + data
        handle.write(data)  # append mode: always lands at the end


def append_run(job: Optional[Dict], result: Dict, trigger: str,
               start: datetime, end: Optional[datetime] = None,
               app_level: bool = False) -> bool:
    """Append one line for a finished run; True if it was written.

    Goes to the job's own log, or to `app.jsonl` when the job is None / has no
    usable id / `app_level` is set (a failure of the jobs file rather than of a
    run). Never raises and never changes a run's outcome: any failure is
    swallowed and reported only as False.
    """
    try:
        line = json.dumps(build_line(job, result, trigger, start, end or datetime.now()),
                          ensure_ascii=False)
        job_id = job.get("id") if isinstance(job, dict) and not app_level else None
        path = log_path(job_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lock_name = f"runlog-{os.path.basename(path)[:-len('.jsonl')]}.lock"
        lock = locks.acquire_wait(os.path.join(jobs_store._locks_dir(), lock_name),
                                  "run log update", timeout=LOCK_TIMEOUT)
        try:
            _append_line(path, line)
            _trim(path)
        finally:
            lock.release()
        return True
    except Exception:  # ponytail: a log must never fail a run; no fallback sink yet
        return False


def read_runs(job_id: Optional[str] = None, limit: int = 50) -> List[Dict]:
    """The newest `limit` runs of `job_id` (the app log for None), newest first.

    Lines that do not parse as a JSON object are skipped; a missing or unreadable
    log is an empty list.
    """
    try:
        with open(log_path(job_id), "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    runs: List[Dict] = []
    for text in reversed(lines):
        try:
            entry = json.loads(text)
        except ValueError:
            continue
        if isinstance(entry, dict):
            runs.append(entry)
            if len(runs) >= limit:
                break
    return runs
