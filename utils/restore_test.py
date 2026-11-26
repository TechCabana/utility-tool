# utils/restore_test.py
"""Test restore (card 0Bv7mvB3, spec safety item 8): restore a random SAMPLE of
a version into a temporary folder, read what was written back from disk, and
compare it with the manifest.

Goal 1 is "a backup that says OK is actually restorable". Verification re-reads
the copy IN PLACE, and a restore (card 6ajl8vjD) compares the hash it computes
while copying with the manifest -- but that hash is of the bytes READ from the
backup. Neither looks at what a restore WRITES. This does: the sampled files go
through the real restore path (`versions.restore_version(only=...)`, so the same
walk, the same long-path and attribute handling and the same read-only rules),
into a folder made under the OS temp directory (never under the job's target or
the user's folders), and each restored file is then opened FRESH from disk and
its size and SHA-256 compared with the manifest's. The folder is removed in a
`finally`, whatever happens.

**What it proves, and what it does not.** A sample (not every file) restores and
matches the recorded checksums when read back. It does not prove the source was
complete, that the files NOT sampled are intact, or that the backup drive has no
later bit rot (the OS cache can serve the read from memory).

**The name.** The card calls it `test_restore`; a function whose name starts
with `test_` is collected as a test by pytest the moment it is imported into a
test module, so it is `restore_test` here (the product's wording, "test
restore", is kept for every message).

**Only a trusted manifest is sampled.** The manifest is read with the same
reader the restore check uses (`versions._read_manifest_hashes`): missing, or
damaged (no trailer, a line that does not parse, a repeated path, a count that
disagrees with the trailer) is `cannot_test` with that reason -- never a pass --
and so is a version whose trailer says it was not verified (verification "off").

**Locking.** A test reads a version, and a backup prunes versions, so the test
holds the job's per-job run lock for its whole duration (`runner._job_lock_path`,
the lock `run_job` takes). Run on demand and the lock is busy: `status` "busy",
nothing touched. Run automatically (`automatic`) it is called from INSIDE
`run_job`'s already-held lock and takes none.

**The automatic run** (`automatic`, called by `runner._with_restore_test`): after
a job's first successful backup, and again after each later one until a test has
PASSED once. Bounded so a first backup is not doubled in time: no file over
`AUTO_MAX_FILE` is sampled and the sample's bytes stop at `AUTO_MAX_BYTES`. On
demand the largest file is always included. Qt-free. One-way dependencies:
imports jobs_store, runlog, verify, versions, copying, locks, fs_utils; only
runner, `main.py` and the tests import this.
"""
import math
import os
import random
import tempfile
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from utils import jobs_store, locks, runlog, verify
from utils.copying import TARGET_CAUSES
from utils.fs_utils import long_path
from utils.versions import (_files, _named, _read_manifest_hashes, _rmtree,
                            read_manifest_summary, restore_candidates,
                            restore_version, version_path)

# The folder every test makes under the OS temp directory, so a leftover (a
# killed process) is recognisable by name and by nothing else.
TEMP_PREFIX = "safekeep-restore-test-"

# Random entries per test: about this share of the files, at least
# SAMPLE_MIN, at most SAMPLE_MAX, never more than there are files. The largest
# file is added on top of these, so a sample is that many plus one.
SAMPLE_FRACTION = 0.05
SAMPLE_MIN = 5
SAMPLE_MAX = 50

# The automatic run's bounds (the on-demand test has none): no file larger than
# AUTO_MAX_FILE is sampled, so the largest file is skipped when it is over 1 GiB,
# and the sample stops adding files once their sizes reach AUTO_MAX_BYTES.
# ponytail: a time bound by size, not a stopwatch; add a deadline if a slow
# drive makes 2 GiB too long.
AUTO_MAX_FILE = 1024 ** 3
AUTO_MAX_BYTES = 2 * 1024 ** 3

# Names a result carries; the total stays in `failed`.
FAILED_NAMED = jobs_store.SKIPPED_SAMPLE_MAX

# What a failing file's `failed_why` says.
WHY_MISSING = "missing"
WHY_UNREADABLE = "unreadable"
WHY_SIZE = "size differs"
WHY_HASH = "checksum differs"
WHY_BACKUP = "the backup's copy does not match its manifest"
WHY_NOT_RESTORED = "not restored"


def default_sample_size(count: int) -> int:
    """How many RANDOM entries a test of `count` files takes:
    `min(count, max(SAMPLE_MIN, ceil(5% of count)), SAMPLE_MAX)`."""
    return min(count, max(SAMPLE_MIN, math.ceil(SAMPLE_FRACTION * count)), SAMPLE_MAX)


def choose_sample(sizes: Dict[str, int], sample: Optional[int] = None,
                  rng: Optional[random.Random] = None, include_largest: bool = True,
                  max_file: Optional[int] = None, max_bytes: Optional[int] = None
                  ) -> List[str]:
    """The relative paths to test: the largest file (if `include_largest`),
    then `sample` random others (`default_sample_size` of ALL the files when
    None), never a path twice.

    `max_file` leaves out every file larger than it before anything is chosen
    (so "the largest" is then the largest of the rest), and `max_bytes` stops
    adding files once their sizes would pass it: the automatic run's bounds.
    `rng` is injectable so a test can fix the draw; the pool is sorted first so
    the same seed gives the same sample whatever order the manifest listed.
    """
    rng = rng if rng is not None else random.Random()
    pool = sorted(rel for rel, size in sizes.items() if max_file is None or size <= max_file)
    largest = max(pool, key=lambda rel: (sizes[rel], rel)) if pool and include_largest else None
    wanted = default_sample_size(len(sizes)) if sample is None else max(0, int(sample))
    rest = [rel for rel in pool if rel != largest]
    chosen = ([largest] if largest is not None else []) + rng.sample(rest, min(wanted, len(rest)))
    if max_bytes is None:
        return chosen
    kept, used = [], 0
    for rel in chosen:
        if used + sizes[rel] <= max_bytes:
            kept.append(rel)
            used += sizes[rel]
    return kept


def _trusted_entries(job: Dict, version: str
                     ) -> Tuple[Optional[Dict[str, Tuple[int, bytes]]], Optional[str]]:
    """`({path: (size, sha256 digest)}, None)` for a version whose manifest can
    be trusted, else `(None, reason)`: "no manifest", "manifest damaged" or "not
    verified" (the trailer says verification was off). Same reader as restore."""
    sizes: Dict[str, int] = {}
    hashes, reason = _read_manifest_hashes(job, version, None, sizes)
    if hashes is None:
        return None, reason
    summary = read_manifest_summary(job, version)
    if summary is None or summary.get("verified") is not True:
        return None, "not verified"
    if len(sizes) != len(hashes):          # a line with no usable size: not trusted
        return None, "manifest damaged"
    return {rel: (sizes[rel], digest) for rel, digest in hashes.items()}, None


def _count_files(folder: str) -> int:
    """How many files are left under `folder` (links are not followed), at least
    1 when the folder itself is still there: the "could not remove" count."""
    total = 0
    for _current, _dirs, files in os.walk(long_path(folder)):
        total += len(files)
    return max(1, total)


def _remove_folder(folder: str) -> int:
    """Remove the temporary folder, clearing read-only marks first (a restored
    copy of a read-only source file comes back read-only). Returns 0 when it is
    gone -- checked, not assumed -- else how many files were left."""
    try:
        _rmtree(folder)
    except OSError:
        pass
    if not os.path.lexists(long_path(folder)):
        return 0
    return _count_files(folder)


def _result(status: str, version: Optional[str], reason: str = "", **fields) -> Dict:
    """A test's result dict with every key present. `message` is built here, and
    never names the temporary folder or any absolute path (it is logged)."""
    failed_names = fields.pop("failed_names", [])
    sampled = fields.pop("sampled", 0)
    failed = len(failed_names)
    result = {"status": status, "version": version or "", "sampled": sampled,
              "checked": sampled - failed if status in ("passed", "failed") else 0,
              "failed": failed, "failed_sample": sorted(failed_names)[:FAILED_NAMED],
              "failed_why": fields.pop("failed_why", {}), "reason": reason,
              "includes_largest": fields.pop("includes_largest", False),
              "largest": fields.pop("largest", None), "cleanup_failed": 0,
              "leftover": None, "stored": False,
              "time": datetime.now().isoformat(timespec="seconds")}
    result.update(fields)
    if status == "passed":
        result["message"] = f"Test restore passed: {_files(sampled)} restored and checked."
    elif status == "failed":
        result["message"] = ("Test restore FOUND A PROBLEM: "
                             f"{_named(failed_names)} did not match.")
    elif status == "cannot_test":
        result["message"] = f"Test restore could not be run: {reason}."
    elif status == "busy":
        result["message"] = ("A backup of this job is running, so the test restore "
                             "did not start. Try again when it finishes.")
    else:
        result["message"] = "Test restore cancelled"
    return result


def _compare(job: Dict, version: str, folder: str, chosen: List[str],
             entries: Dict[str, Tuple[int, bytes]],
             should_abort: Optional[Callable[[], bool]]) -> Optional[Dict[str, str]]:
    """Restore `chosen` into `folder` and compare. `{failing path: why}` (empty
    when all matched), or None when cancelled. Raises `_CannotWrite` when the
    temporary folder could not be written."""
    restored = restore_version(job, version, folder, should_abort=should_abort,
                               only=set(chosen))
    if restored.get("cancelled"):
        return None
    failures: Dict[str, str] = {}
    if not restored.get("ok"):
        cause = restored.get("cause")
        if cause not in ("source_unavailable", "source_incomplete"):
            raise _CannotWrite(TARGET_CAUSES.get(cause, "The temporary folder could not "
                                                 "be written").format(drive="temporary folder"))
        return {rel: WHY_NOT_RESTORED for rel in chosen}    # the version itself went away
    root = restored["path"]
    for rel in chosen:
        if should_abort is not None and should_abort():
            return None
        size, digest = entries[rel]
        try:
            # A fresh open of what was WRITTEN: the authority. Not the hash the
            # copy took while reading the backup.
            got_size, got_sha = verify._rehash(os.path.join(root, *rel.split("/")))
        except FileNotFoundError:
            failures[rel] = WHY_MISSING
        except OSError:
            failures[rel] = WHY_UNREADABLE
        else:
            if got_size != size:
                failures[rel] = WHY_SIZE
            elif got_sha != digest.hex():
                failures[rel] = WHY_HASH
    # What the restore's own check found as well (a stored copy that no longer
    # matches its manifest, a sampled file the version lacks): named once.
    for rel in restored.get("mismatched", []):
        failures.setdefault(rel, WHY_BACKUP)
    for rel in restored.get("missing_from_version", []):
        failures.setdefault(rel, WHY_MISSING)
    return failures


class _CannotWrite(Exception):
    """The temporary folder could not be written (a full or unwritable temp
    drive): the version was not shown to be bad, so the test cannot be run."""


def _run_test(job: Dict, version: Optional[str], sample: Optional[int],
              rng: Optional[random.Random], include_largest: bool,
              should_abort: Optional[Callable[[], bool]], temp_base: Optional[str],
              max_file: Optional[int] = None, max_bytes: Optional[int] = None) -> Dict:
    """The test itself, with no lock, no store and no log (callers add those).

    Never raises for a state of the version (no manifest, damaged, not verified,
    gone): those are `cannot_test`. The temporary folder is removed in a
    `finally` on every path, including a cancel and an unexpected exception.
    """
    if version is None:
        candidates = restore_candidates(job)
        version = candidates["preferred"]
        if version is None or candidates["fallback"]:
            return _result("cannot_test", None, "there is no verified backup to test")
    if not os.path.isdir(long_path(version_path(job, version))):
        return _result("cannot_test", version, "that backup version was not found")
    entries, reason = _trusted_entries(job, version)
    if entries is None:
        return _result("cannot_test", version, reason)
    if not entries:
        return _result("cannot_test", version, "this backup holds no files, so there is nothing to test")
    sizes = {rel: size for rel, (size, _digest) in entries.items()}
    chosen = choose_sample(sizes, sample, rng, include_largest, max_file, max_bytes)
    if not chosen:
        return _result("cannot_test", version,
                       "every file is too large for the automatic test; run a test restore "
                       "from the command line instead")
    overall_largest = max(sizes, key=lambda rel: (sizes[rel], rel))
    facts = {"sampled": len(chosen), "largest": overall_largest,
             "includes_largest": overall_largest in chosen}
    try:
        folder = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=temp_base)
    except OSError as exc:
        return _result("cannot_test", version,
                       f"a temporary folder could not be made ({exc.strerror or 'unknown error'})")
    try:
        try:
            failures = _compare(job, version, folder, chosen, entries, should_abort)
        except _CannotWrite as exc:
            outcome = _result("cannot_test", version, str(exc))
        else:
            if failures is None:
                outcome = _result("cancelled", version)
            else:
                outcome = _result("failed" if failures else "passed", version,
                                  failed_names=sorted(failures),
                                  failed_why={rel: failures[rel]
                                              for rel in sorted(failures)[:FAILED_NAMED]},
                                  **facts)
    finally:
        left = _remove_folder(folder)
    if left:
        outcome["cleanup_failed"] = left
        outcome["leftover"] = folder
        outcome["message"] += (f" The temporary folder could not be removed; {_files(left)} "
                               "may be left in your temp folder.")
    return outcome


def _store(job: Dict, result: Dict) -> None:
    """Store `result` on the job when it is a result worth keeping, and say in
    `result["stored"]` whether it landed. A busy or cancelled test says nothing
    about the version and is never stored; a failure to write is reported
    (`store_error`), never raised -- the test happened."""
    if result["status"] not in jobs_store.RESTORE_TEST_STATUSES:
        return
    try:
        result["stored"] = jobs_store.record_restore_test(job.get("id"), result) is not None
    except (jobs_store.JobsFileCorrupt, OSError) as exc:
        result["store_error"] = str(exc)


def restore_test(job: Dict, version: Optional[str] = None, sample: Optional[int] = None,
                 *, rng: Optional[random.Random] = None,
                 should_abort: Optional[Callable[[], bool]] = None,
                 temp_base: Optional[str] = None, trigger: str = "manual",
                 store: bool = True, log: bool = True) -> Dict:
    """Test-restore a sample of `version` (default: the newest verified one).

    `sample` is how many random files to take besides the largest (default
    `default_sample_size`); the largest file is ALWAYS included here.

    Returns a dict: `status` ("passed", "failed", "cannot_test", "busy" or
    "cancelled"), `version`, `sampled` (files tested, the largest included),
    `checked` (those that matched), `failed` (the count), `failed_sample` (at
    most 5 relative paths, forward slashes), `failed_why` (path -> what differed),
    `largest` / `includes_largest`, `reason`, `message`, `cleanup_failed` (files
    left behind when the temporary folder could not be removed, else 0),
    `leftover` (that folder, for the person; never logged), `stored`, `time`.

    The job's run lock is held throughout; a busy job returns "busy" and
    touches nothing. A passed/failed/cannot_test result is stored on the job
    (`last_restore_test`, `store=False` to skip) and logged as one line
    (`log=False` to skip; `trigger` "manual" or "auto"); a busy or cancelled one
    is neither. Raises nothing for the state of the version.
    """
    from utils.runner import _job_lock_path   # runner imports this module: resolved at call time
    began = datetime.now()
    lock_path = _job_lock_path(job)
    if lock_path is None:
        return _result("cannot_test", None, "this backup job has no usable id")
    try:
        lock = locks.acquire(lock_path, f"test restore {job['id']}")
    except locks.LockBusy:
        return _result("busy", version)
    except OSError as exc:
        return _result("cannot_test", version, f"the backup lock could not be taken ({exc})")
    try:
        result = _run_test(job, version, sample, rng, True, should_abort, temp_base)
    finally:
        lock.release()
    if store:
        _store(job, result)
    if log and result["status"] in runlog.RESTORE_OUTCOMES:
        runlog.append_run(job, {"restore_test_line": result}, trigger, began)
    return result


def automatic(job: Dict, version: str, should_abort: Optional[Callable[[], bool]] = None,
              temp_base: Optional[str] = None, rng: Optional[random.Random] = None
              ) -> Optional[Dict]:
    """The test `run_job` makes after a successful backup that made `version`,
    or None when none is due. The CALLER HOLDS THE JOB'S RUN LOCK.

    Due when the job is in the jobs file (a dict that is not stored has nowhere
    to keep a result), its verification is not "off" (nothing was verified to
    compare against), and its stored `last_restore_test` is absent or not
    "passed": the first successful backup tests, and each later one retries
    until a test has passed once -- a "failed" or "cannot_test" result must not
    be the last word forever, a "passed" one is not repeated. Bounded by
    `AUTO_MAX_FILE` / `AUTO_MAX_BYTES`. The result is stored here; the log line
    is left to the caller (`result["status"]` in `runlog.RESTORE_OUTCOMES`),
    which writes it after the run's own line and once the lock is released.
    Raises what the jobs file raises (the caller turns that into a sentence).
    """
    stored = jobs_store.get_job(job.get("id"))
    if stored is None or jobs_store.job_verify_mode(stored) == "off":
        return None
    previous = jobs_store.job_restore_test(stored)
    if previous is not None and previous["status"] == "passed":
        return None
    result = _run_test(job, version, None, rng, True, should_abort, temp_base,
                       AUTO_MAX_FILE, AUTO_MAX_BYTES)
    _store(job, result)
    return result
