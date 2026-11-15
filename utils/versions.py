# utils/versions.py
"""Versions on disk: names and paths, manifests and what counts as verified,
pruning by retention, sweeping abandoned leftovers, and restoring a version.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.
"""
import json
import os
import re
import shutil
import stat
import sys
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional

from utils.copying import BackupAborted, SourceUnavailable, TargetWriteFailed, copy_tree
from utils.fs_utils import _is_link_like
from utils.jobs_store import DEFAULT_RETENTION, VERIFY_MODES


# A version directory is named for the moment the backup started. Matched by
# this pattern when versions are listed, so a half-written ".partial" copy or
# anything else the user drops in the target is never mistaken for a version.
VERSION_FORMAT = "%Y%m%d-%H%M%S"
_VERSION_RE = re.compile(r"^\d{8}-\d{6}$")


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
    """The newest version stamp of `job` (verified or not), or None."""
    names = version_names(job)
    return names[-1] if names else None


def version_path(job: Dict, name: str) -> str:
    """The folder holding version `name` of `job`."""
    return os.path.join(job_root(job), name)


def version_datetime(name: str) -> Optional[datetime]:
    """The local time a version stamp names, or None if it is not one."""
    try:
        return datetime.strptime(name, VERSION_FORMAT)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------
# Each version has a JSON Lines manifest BESIDE its folder, never inside it:
# `<job root>/<stamp>.manifest.jsonl`. Inside, it would be a file the source
# does not have -- changes_since would count it as removed and a restore would
# bring it back as a stray file -- and the version folder must stay an
# ordinary copy of the source. version_names() only lists directories, so the
# sibling file is never mistaken for a version.
#
# Line 1 is a header, then one line per copied file, then -- only once the
# job's verification mode has passed -- an end trailer
# `{"type": "end", "files", "bytes", "verify": mode, "verified": bool}`.
# "verified" is true for "full" and "quick" and false for "off". A trailer
# written before modes existed has no "verify" key and reads as "full".
# A version is verified iff its folder exists and its manifest ends in a
# verified trailer. Legacy versions (made before manifests) have none and are
# simply unverified, as is an "off" version.

MANIFEST_SUFFIX = ".manifest.jsonl"
MANIFEST_VERSION = 1


def manifest_path(job: Dict, stamp: str) -> str:
    """Where version `stamp`'s manifest lives: a sibling of its folder."""
    return os.path.join(job_root(job), stamp + MANIFEST_SUFFIX)


def _last_line(path: str, limit: int = 4096) -> Optional[bytes]:
    """The last non-empty line of `path`, reading only its tail.

    None if the file cannot be read or the last line is longer than `limit`
    (a real trailer is under 100 bytes, so that is garbage, not a trailer).
    """
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            end = handle.tell()
            start = max(0, end - limit)
            handle.seek(start)
            tail = handle.read().rstrip(b"\r\n")
    except OSError:
        return None
    cut = tail.rfind(b"\n")
    if cut < 0 and start > 0:
        return None
    return tail[cut + 1:]


def read_manifest_summary(job: Dict, stamp: str) -> Optional[Dict]:
    """Version `stamp`'s end trailer, or None if it has no valid one.

    The returned trailer always carries "verify" (one of `VERIFY_MODES`;
    "full" for a trailer written before modes existed) and a boolean
    "verified". Missing, truncated (a run that died mid-write) or garbage
    manifests all come back None -- "not verified", never an exception -- and
    so does a trailer whose mode is unknown or disagrees with its "verified"
    flag, since only a run of this code writes one and it never writes that.
    """
    line = _last_line(manifest_path(job, stamp))
    if not line:
        return None
    try:
        trailer = json.loads(line.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(trailer, dict) or trailer.get("type") != "end":
        return None
    mode = trailer.get("verify", "full")
    if mode not in VERIFY_MODES or trailer.get("verified") is not (mode != "off"):
        return None
    return dict(trailer, verify=mode)


def verify_mode(job: Dict, stamp: str) -> Optional[str]:
    """How version `stamp` was checked ("full", "quick" or "off"), or None
    when it has no readable, complete manifest (legacy, crashed, damaged)."""
    summary = read_manifest_summary(job, stamp)
    return summary["verify"] if summary else None


def is_verified(job: Dict, stamp: str) -> bool:
    """True iff version `stamp` exists and its manifest ends verified -- a
    "full" or "quick" run. An "off" version is complete but never verified."""
    summary = read_manifest_summary(job, stamp)
    return (os.path.isdir(version_path(job, stamp))
            and summary is not None and summary["verified"] is True)


def verified_versions(job: Dict) -> List[str]:
    """The verified subset of `version_names(job)`, oldest first."""
    return [name for name in version_names(job) if is_verified(job, name)]


def prune_versions(job: Dict) -> List[str]:
    """Delete the oldest versions beyond the job's retention count.

    Returns the names actually removed. A version that cannot be deleted (a
    file in it is open, the drive went away) is left in place and simply not
    reported as removed -- a failed prune must never fail the backup that
    just succeeded.

    The newest VERIFIED version is never deleted, even when retention says
    it should be: if every newer version is a legacy (manifest-less) copy or
    one made with verification "off", it is still the last one known to match
    what was copied. Legacy and "off" versions otherwise prune by retention
    like any other. A pruned version's manifest goes with it.
    """
    keep = max(1, int(job.get("retention", DEFAULT_RETENTION)))
    names = version_names(job)
    protected = next((name for name in reversed(names)
                      if is_verified(job, name)), None)
    removed = []
    for name in names[:max(0, len(names) - keep)]:
        if name == protected:
            continue
        # Manifest first: a version whose folder deletion fails halfway must
        # not keep claiming to be verified.
        try:
            os.remove(manifest_path(job, name))
        except FileNotFoundError:
            pass  # a legacy version has none
        except OSError:
            continue
        try:
            _rmtree(version_path(job, name))
        except OSError:
            continue
        removed.append(name)
    return removed


def _make_writable_and_retry(func, path, _error) -> None:
    """`shutil.rmtree` error hook: clear read-only, then retry once.

    Windows refuses to delete a read-only file, and a backup keeps the
    source's read-only bit, so without this one read-only file in the source
    made every older version of the job undeletable and retention silently
    stopped working. A second failure propagates to the caller.
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path: str) -> None:
    """`shutil.rmtree` that also removes read-only files. Raises OSError."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable_and_retry)
    else:  # onexc does not exist before 3.12; onerror is deprecated from it
        shutil.rmtree(path, onerror=_make_writable_and_retry)


def _discard(partial: str, manifest_tmp: str) -> None:
    """Remove an abandoned run's `.partial` folder and its temp manifest."""
    try:
        _rmtree(partial)
    except OSError:
        pass  # best effort: a leftover .partial is never listed as a version
    try:
        os.remove(manifest_tmp)
    except OSError:
        pass


# Leftovers of failed or crashed runs, matched by exact name so a user's own
# files in the target are never candidates. `[0-9]`, not `\d`: no Unicode digits.
SWEEP_MAX_AGE_DAYS = 7
_STAMP = r"[0-9]{8}-[0-9]{6}"
_PARTIAL_RE = re.compile(rf"({_STAMP})\.partial")
_MANIFEST_TMP_RE = re.compile(rf"({_STAMP}){re.escape(MANIFEST_SUFFIX)}\.tmp")
_MANIFEST_RE = re.compile(rf"({_STAMP}){re.escape(MANIFEST_SUFFIX)}")


def sweep_stale(job: Dict, now: Optional[datetime] = None,
                max_age_days: int = SWEEP_MAX_AGE_DAYS) -> Dict:
    """Delete abandoned run leftovers in `job`'s root older than `max_age_days`.

    A failed verification deliberately keeps its `<stamp>.partial` folder and
    `<stamp>.manifest.jsonl.tmp`, and a crash can leave a `<stamp>.manifest.jsonl`
    with no `<stamp>` folder (an orphan). Nothing else removes them, so they
    pile up and eat the free space the next run's pre-flight needs.

    Only three exact name shapes are candidates (see the `_*_RE` patterns);
    anything else, every promoted version `<stamp>`, and any manifest whose
    `<stamp>` entry exists are never touched. Links and junctions are never
    followed or removed. Age comes from the stamp in the name (strictly older
    than `now - max_age_days`), not from file times, so it is deterministic.

    The newest `.partial` is always kept, with its manifests, so one failed
    attempt stays inspectable; it is reported as `kept_for_inspection` when it
    is old enough that it would otherwise have been swept (else None).

    Returns `{"removed": [names], "errors": [str], "kept_for_inspection": ...}`.
    Never raises: one item that cannot be deleted goes in `errors` and the
    sweep carries on. The caller must hold the job's run lock.
    """
    result: Dict = {"removed": [], "errors": [], "kept_for_inspection": None}
    cutoff = (now or datetime.now()) - timedelta(days=max_age_days)
    partials: Dict[str, str] = {}
    tmps: Dict[str, str] = {}
    manifests: Dict[str, str] = {}
    taken = set()
    try:
        with os.scandir(job_root(job)) as entries:
            for entry in entries:
                taken.add(entry.name)
                if _is_link_like(entry):
                    continue
                for pattern, found, is_dir in ((_PARTIAL_RE, partials, True),
                                               (_MANIFEST_TMP_RE, tmps, False),
                                               (_MANIFEST_RE, manifests, False)):
                    match = pattern.fullmatch(entry.name)
                    if match and version_datetime(match.group(1)) is not None:
                        shape_ok = (entry.is_dir(follow_symlinks=False) if is_dir
                                    else entry.is_file(follow_symlinks=False))
                        if shape_ok:
                            found[match.group(1)] = entry.name
    except FileNotFoundError:
        return result  # no job root yet: nothing to sweep
    except OSError as exc:
        result["errors"].append(f"{job_root(job)}: {exc}")
        return result

    def old(stamp: str) -> bool:
        """True when the stamp is strictly older than the cutoff."""
        return version_datetime(stamp) < cutoff

    keep = max(partials, default=None)  # stamps sort chronologically
    if keep is not None and old(keep):
        result["kept_for_inspection"] = partials[keep]
    else:
        keep = None
    doomed = [(partials[s], True) for s in partials if s != keep and old(s)]
    doomed += [(tmps[s], False) for s in tmps if s != keep and old(s)]
    doomed += [(manifests[s], False) for s in manifests
               if s != keep and s not in taken and old(s)]
    for name, is_dir in sorted(doomed):
        path = os.path.join(job_root(job), name)
        try:
            if is_dir:
                _rmtree(path)
            else:
                try:
                    os.unlink(path)
                except PermissionError:  # read-only bit: clear it, retry once
                    os.chmod(path, stat.S_IWRITE)
                    os.unlink(path)
        except OSError as exc:
            result["errors"].append(f"{name}: {exc}")
        else:
            result["removed"].append(name)
    return result


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

    If the destination cannot be written (full, too-long path, unplugged) the
    result is `ok=False` with a `cause`, and a restore folder this call
    created is removed so a partial restore is never mistaken for a whole
    one. A folder that already existed (an earlier restore of the same
    version) is left alone, since it may hold the user's own changes, and
    the message says it may be incomplete. Files in the version that cannot
    be read are skipped and reported in `errors`, as before -- unless the
    stored version itself vanishes mid-restore or most of it cannot be read
    (`copy_tree`'s completeness rule): that fails with cause
    "source_unavailable" / "source_incomplete" and the same folder handling.
    """
    source = version_path(job, version)
    if not os.path.isdir(source):
        return {"ok": False, "message": f"Backup version {version} not found"}

    safe_name = re.sub(r"[^\w.-]+", "_", job.get("name") or job["id"]).strip("_") or job["id"]
    folder = os.path.join(destination, f"{safe_name}-restored-{version}")
    existed = os.path.exists(folder)
    stats: Dict[str, int] = {}
    try:
        files, restored_bytes, errors = copy_tree(
            source, folder, on_progress=on_progress, should_abort=should_abort,
            stats=stats)
    except BackupAborted:
        return {"ok": False, "message": "Restore cancelled"}
    except (TargetWriteFailed, SourceUnavailable) as exc:
        if existed:
            after = f"{folder} already existed and may now be incomplete."
        else:
            try:
                _rmtree(folder)
                after = "The incomplete restore folder was removed."
            except FileNotFoundError:
                after = "Nothing was restored."
            except OSError:
                after = f"The incomplete restore folder could not be removed: {folder}"
        reason = (exc.reason("backup version", "restore") if isinstance(exc, SourceUnavailable)
                  else exc.reason("destination drive"))
        return {"ok": False, "cause": exc.cause,
                "message": f"Restore failed: {reason}. {after}"}
    except OSError as exc:
        return {"ok": False, "message": f"Restore failed: {exc}"}

    message = f"Restored {files} file(s) to {folder}"
    if errors:
        message += (f". {max(len(errors), stats.get('not_copied', 0))} file(s) "
                    "could not be read from the backup and were skipped")
    return {"ok": True, "message": message, "path": folder, "files": files,
            "bytes": restored_bytes, "errors": errors}
