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
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from utils.copying import BackupAborted, SourceUnavailable, TargetWriteFailed, copy_tree
from utils.fs_utils import _is_link_like, long_path, plain_error
from utils.jobs_store import DEFAULT_RETENTION, VERIFY_MODES, job_hold_released


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
        with os.scandir(long_path(job_root(job))) as entries:
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
        with open(long_path(path), "rb") as handle:
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
    return (os.path.isdir(long_path(version_path(job, stamp)))
            and summary is not None and summary["verified"] is True)


def verified_versions(job: Dict) -> List[str]:
    """The verified subset of `version_names(job)`, oldest first."""
    return [name for name in version_names(job) if is_verified(job, name)]


def version_when(stamp: Optional[str]) -> Optional[str]:
    """A version stamp as the "2026-11-06 14:30" text the Restore row shows
    (the raw stamp if it does not parse), or None for no stamp."""
    if stamp is None:
        return None
    moment = version_datetime(stamp)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else stamp


def restore_candidates(job: Dict) -> Dict:
    """Which version a restore should use, and what it would be passing over.

    A tool that promises "a backup that says OK is restorable" should not
    restore an unchecked copy while a checked one exists, so:

    * `preferred` -- the newest VERIFIED version ("full" or "quick"; "off",
      legacy and damaged-manifest versions are not), else the newest of any
      kind when none is verified.
    * `latest` -- the newest version of any kind.
    * `fallback` -- True only when NO version is verified, so `preferred` is
      unverified data and the user must be told so.
    * `newer_unverified` -- versions newer than `preferred`, all unverified
      by construction; what the user is passing over, and may still choose.

    No versions: preferred and latest are None. Reads each manifest's tail
    only, so it is cheap enough to call when a button is pressed.
    """
    names = version_names(job)
    if not names:
        return {"preferred": None, "latest": None, "fallback": False,
                "newer_unverified": []}
    verified = [name for name in names if is_verified(job, name)]
    preferred = verified[-1] if verified else names[-1]
    return {"preferred": preferred, "latest": names[-1],
            "fallback": not verified,
            "newer_unverified": [name for name in names if name > preferred]}


# ---------------------------------------------------------------------------
# Source sanity guard (card 51)
# ---------------------------------------------------------------------------
# A run is compared with the versions already kept. If it has lost more than
# this share of the files the best of them had, the source was probably
# emptied by mistake, by ransomware or by a half-finished move, so that best
# version -- the "baseline" -- is kept aside while the others prune as usual,
# and a complete copy cannot be rolled out of retention one run at a time.
# There is deliberately no minimum file count: a tiny source going from 2
# files to 1 is a 50% drop and holds like any other.
SANITY_DROP_FRACTION = 0.30


def sanity_baseline(job: Dict, exclude: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """`(files, stamp)` of the retained version of `job` holding the most
    files (ties: the newest of them); `(0, None)` if there is none.

    Any version with a readable, complete manifest trailer counts, verified
    or not: the file count is a property of the source, not of how carefully
    the copy was checked, so a job with verification "off" is guarded too.
    Legacy versions (no manifest), damaged or truncated trailers and a junk
    `files` value do not count. `exclude` is the stamp of the run being judged.

    **The owner's release (card QnFM2GMZ).** When the job carries a valid
    `hold_released` acknowledgement (`jobs_store.job_hold_released`), versions
    OLDER than its `since` stamp are not considered at all: the owner said the
    larger backups from before then are not what the source should be
    compared with any more. `since` is the newest version at the moment of
    release, so it is the first one compared against again -- which makes the
    new reference the level the source was at then, and a later drop of more
    than 30% below THAT holds again. Versions made after the release count as
    usual, so a source that grows back and then shrinks is still guarded
    against its new, larger self. The old big version stays on disk until
    retention prunes it; it simply stops being the baseline.
    """
    release = job_hold_released(job)
    since = release["since"] if release else ""
    best, stamp = 0, None
    for name in version_names(job):             # oldest first, so >= keeps the newest tie
        if name == exclude or name < since:     # stamps sort chronologically
            continue
        files = (read_manifest_summary(job, name) or {}).get("files")
        if isinstance(files, int) and not isinstance(files, bool) and files > 0 and files >= best:
            best, stamp = files, name
    return best, stamp


def baseline_files(job: Dict, exclude: Optional[str] = None) -> int:
    """The file count of `sanity_baseline`; 0 if there is no baseline."""
    return sanity_baseline(job, exclude)[0]


def is_sanity_drop(baseline: int, files: int) -> bool:
    """True if `files` is more than `SANITY_DROP_FRACTION` below `baseline`
    (never with no baseline, e.g. a first run)."""
    return bool(baseline) and (baseline - files) / baseline > SANITY_DROP_FRACTION


def sanity_drop_baseline(job: Dict, files: int, exclude: Optional[str] = None) -> int:
    """The baseline file count if `files` is a sanity drop against it (the
    baseline must be kept aside), else 0.

    The baseline is the MAX over retained versions, not the previous one, so
    the guard keeps holding on every later run while the source stays low.
    Holding protects only that one version (`prune_versions(protect=...)`),
    so a source that stays low costs one extra complete version, never
    unbounded growth. It ends when the count comes back within the
    threshold, or when the owner releases it (`runner.release_hold`).
    """
    baseline = baseline_files(job, exclude)
    return baseline if is_sanity_drop(baseline, files) else 0


def pruning_held_sentence(files: int, baseline: int) -> str:
    """The plain-English warning for a run that kept its baseline aside."""
    fewer = round(100 * (baseline - files) / baseline)
    def count(n: int) -> str:
        return f"{n} file" if n == 1 else f"{n} files"
    return (f"Pruning was held because this backup has {count(files)} and an "
            f"earlier backup has {count(baseline)} ({fewer}% fewer). That "
            "earlier, larger backup is being kept; other old backups are pruned "
            "as usual. If that is not expected, check your files before the "
            "next run.")


def release_hold_question(files: int, baseline: int) -> str:
    """The question the "This is expected" confirmation asks, naming both
    counts: "Your folder now has 6 files; an earlier backup has 10. Stop
    keeping the earlier one?" (the second count carries no unit, as the card
    words it, and so reads right for 1 as well)."""
    noun = "file" if files == 1 else "files"
    return (f"Your folder now has {files} {noun}; an earlier backup has "
            f"{baseline}. Stop keeping the earlier one?")


def prune_versions(job: Dict, protect: Iterable[str] = (),
                   newest: Optional[str] = None) -> List[str]:
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

    Versions named in `protect` (the sanity guard's baseline) are never
    deleted and do not count toward retention, so a held job keeps at most
    `retention` ordinary versions plus the protected ones. An empty `protect`
    is the plain retention prune.

    `newest` is the version the calling run has just made: it is treated as
    the newest whatever its name says. Names are local clock time, so in the
    hour the clocks go back a later run can carry an EARLIER name; ordered by
    name alone, a retention-1 job deleted the backup it had just made and
    kept the older one.
    """
    keep = max(1, int(job.get("retention", DEFAULT_RETENTION)))
    names = version_names(job)
    if newest in names:
        names.remove(newest)
        names.append(newest)
    protected = next((name for name in reversed(names)
                      if is_verified(job, name)), None)
    protect = set(protect)
    candidates = [name for name in names if name not in protect]
    removed = []
    for name in candidates[:max(0, len(candidates) - keep)]:
        if name == protected:
            continue
        # Manifest first: a version whose folder deletion fails halfway must
        # not keep claiming to be verified.
        try:
            os.remove(long_path(manifest_path(job, name)))
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
    """`shutil.rmtree` that also removes read-only files. Raises OSError.

    Takes a plain path; the tree is walked in `long_path`'s form, so a file
    nested past MAX_PATH is deleted too instead of failing the prune.
    """
    path = long_path(path)
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
        os.remove(long_path(manifest_tmp))
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
        with os.scandir(long_path(job_root(job))) as entries:
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
        result["errors"].append(f"{job_root(job)}: {plain_error(exc)}")
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
                    os.unlink(long_path(path))
                except PermissionError:  # read-only bit: clear it, retry once
                    os.chmod(long_path(path), stat.S_IWRITE)
                    os.unlink(long_path(path))
        except OSError as exc:
            result["errors"].append(f"{name}: {plain_error(exc)}")
        else:
            result["removed"].append(name)
    return result


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

# How many file names a restore message spells out before saying "and N more".
RESTORE_NAMED = 5


def _read_manifest_hashes(job: Dict, stamp: str) -> Tuple[Optional[Dict[str, bytes]], Optional[str]]:
    """`({relative path: sha256 digest}, None)` for version `stamp`, or
    `(None, reason)` when its manifest cannot be trusted.

    The reason is "no manifest" (none on disk: made before manifests existed,
    or deleted) or "manifest damaged". A manifest is trusted only if it ends
    in a valid trailer (a run that died mid-write has none), every line
    parses, every file entry has a path and a 64-hex-digit sha256, no path
    repeats, and the entries number what the trailer says. Anything less
    reads as damaged -- never as a pass.

    Loaded into one dict, because the restore walks the folder in directory
    order, not manifest order. Digests are kept as 32 raw bytes, not 64-char
    hex, to keep a 100,000-file manifest to tens of megabytes. Paths are in
    the manifest's own form (relative, forward slashes, case kept), which is
    the form `copy_tree` reports them in.
    """
    path = manifest_path(job, stamp)
    if not os.path.exists(long_path(path)):
        return None, "no manifest"
    summary = read_manifest_summary(job, stamp)
    if summary is None:
        return None, "manifest damaged"
    hashes: Dict[str, bytes] = {}
    try:
        with open(long_path(path), "r", encoding="utf-8") as handle:
            for line in handle:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    return None, "manifest damaged"
                if entry.get("type") != "file":
                    continue
                rel, sha = entry.get("path"), entry.get("sha256")
                digest = bytes.fromhex(sha) if isinstance(sha, str) else b""
                if not isinstance(rel, str) or len(digest) != 32 or rel in hashes:
                    return None, "manifest damaged"
                hashes[rel] = digest
    except (OSError, ValueError):   # unreadable, bad JSON, bad UTF-8, bad hex
        return None, "manifest damaged"
    if len(hashes) != summary.get("files"):
        return None, "manifest damaged"
    return hashes, None


def _files(count: int) -> str:
    """"1 file" / "5 files"."""
    return f"{count} file" if count == 1 else f"{count} files"


def _named(paths: List[str]) -> str:
    """The first `RESTORE_NAMED` paths (sorted), then "and N more"."""
    shown = ", ".join(sorted(paths)[:RESTORE_NAMED])
    rest = len(paths) - RESTORE_NAMED
    return f"{shown} and {rest} more" if rest > 0 else shown


def _restore_check_sentence(check: Dict) -> str:
    """What a restore says about how far its files were checked.

    Returns " and checked each against the backup" (a clause to append to
    "Restored N files to <folder>") when every file matched, "" when nothing
    needs saying, else one or more ". ..."-led sentences.
    """
    if check["unchecked_reason"] == "no manifest":
        return (". This backup has no checksum list, so the files could not "
                "be checked")
    if check["unchecked_reason"] == "manifest damaged":
        return (". This backup's checksum list is damaged, so the files "
                "could not be checked")
    parts = []
    if check["mismatched"]:
        parts.append(f"{_files(len(check['mismatched']))} did not match the "
                     f"backup and may be damaged: {_named(check['mismatched'])}")
    if check["not_in_manifest"]:
        parts.append(f"{_files(len(check['not_in_manifest']))} not on the "
                     "backup's checksum list could not be checked: "
                     f"{_named(check['not_in_manifest'])}")
    if check["missing_from_version"]:
        parts.append(f"{_files(len(check['missing_from_version']))} the "
                     "checksum list records could not be found in the backup: "
                     f"{_named(check['missing_from_version'])}")
    if parts:
        return ". " + ". ".join(parts)
    return " and checked each against the backup" if check["checked"] else ""


def restore_version(job: Dict, version: Optional[str], destination: str,
                    on_progress: Optional[Callable[[int, int], None]] = None,
                    should_abort: Optional[Callable[[], bool]] = None) -> Dict:
    """Copy one stored version into a NEW folder under `destination`.

    `version=None` means the default: `restore_candidates(job)["preferred"]`,
    the newest verified version. A caller that wants a different one (the
    newest unverified, say) names its stamp.

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

    Every file is also checked against the version's manifest (card 6ajl8vjD),
    using the SHA-256 `_hash_copy` already computes while it copies -- no
    second read. That hash is of the bytes READ from the backup, so a match
    proves the stored file still has the bytes recorded when it was backed up
    (it catches bit rot or tampering in the backup); it does NOT re-read what
    was written to the destination, so it says nothing about the write. A
    mismatch never stops the restore and nothing is deleted: the file is kept
    and named in the result. The result gains:

    * `checked` -- restored files whose hash matched the manifest;
    * `mismatched` -- paths whose hash did not (a restored copy of a damaged
      stored file);
    * `not_in_manifest` -- restored files the manifest does not list;
    * `missing_from_version` -- files the manifest lists that the restore did
      not produce and no unreadable-file error accounts for;
    * `unchecked_reason` -- None, "no manifest" or "manifest damaged" (then
      nothing was checked: a manifest that cannot be trusted is never a pass);
    * `verified` -- True only when the manifest was trusted and every restored
      file matched, with none unlisted or missing. A file skipped as
      unreadable is not restored, so it is in `errors` (as before) and not
      counted against `verified`.

    `ok` keeps its meaning (the files were copied); a mismatch is
    `ok=True, verified=False`, so callers that only look at `ok` behave as
    before and callers that care read `verified`. A cancelled restore
    (`ok=False`, `cancelled=True`) reports what had been checked so far and
    is never `verified`.
    """
    if version is None:
        version = restore_candidates(job)["preferred"]
        if version is None:
            return {"ok": False, "message": "There is no backup to restore yet"}
    source = version_path(job, version)
    if not os.path.isdir(long_path(source)):
        return {"ok": False, "message": f"Backup version {version} not found"}

    safe_name = re.sub(r"[^\w.-]+", "_", job.get("name") or job["id"]).strip("_") or job["id"]
    folder = os.path.join(destination, f"{safe_name}-restored-{version}")
    existed = os.path.exists(long_path(folder))
    stats: Dict[str, int] = {}
    expected, unchecked_reason = _read_manifest_hashes(job, version)
    check: Dict = {"checked": 0, "mismatched": [], "not_in_manifest": [],
                   "missing_from_version": [], "unchecked_reason": unchecked_reason,
                   "verified": False}

    def compare(rel: str, _size: int, _mtime: float, sha: str) -> None:
        """Compare one restored file's copy-time hash with its manifest entry.

        Pops the entry so what is left in `expected` at the end is what the
        restore did not produce. Never raises: `copy_tree` would delete the
        file just copied if its callback failed.
        """
        want = expected.pop(rel, None)
        if want is None:
            check["not_in_manifest"].append(rel)
        elif want == bytes.fromhex(sha):
            check["checked"] += 1
        else:
            check["mismatched"].append(rel)

    try:
        files, restored_bytes, errors = copy_tree(
            source, folder, on_progress=on_progress, should_abort=should_abort,
            on_copied=compare if expected is not None else None, stats=stats)
    except BackupAborted:
        # Only the files restored before the stop were compared; nothing here
        # claims the rest were. Never `verified`.
        message = "Restore cancelled"
        if check["checked"] or check["mismatched"] or check["not_in_manifest"]:
            message += (f". {_files(check['checked'])} had been copied and matched "
                        "the backup before it stopped")
            if check["mismatched"]:
                message += (f"; {_files(len(check['mismatched']))} did not match: "
                            f"{_named(check['mismatched'])}")
        return dict(check, ok=False, message=message, cancelled=True)
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
        return {"ok": False, "message": f"Restore failed: {plain_error(exc)}"}

    if expected is not None:
        # What the manifest lists but the restore did not produce. A file that
        # was skipped as unreadable is already reported in `errors` (as its own
        # path, or as a folder holding it), so it is not "missing" as well.
        failed = {err.split(": ", 1)[0] for err in errors}
        for rel in expected:
            parts = rel.split("/")
            if not any("/".join(parts[:i]) in failed for i in range(1, len(parts) + 1)):
                check["missing_from_version"].append(rel)
        # `checked == files` also rules out any unlisted file: files restored
        # = matched + mismatched + unlisted.
        check["verified"] = (not check["mismatched"]
                             and not check["missing_from_version"]
                             and check["checked"] == files)

    message = f"Restored {_files(files)} to {folder}"
    sentence = _restore_check_sentence(check)
    # A clean check is a clause of the first sentence; problems and "could not
    # be checked" are sentences of their own, after the skipped-files note.
    if sentence.startswith(" and "):
        message += sentence
    if errors:
        message += (f". {max(len(errors), stats.get('not_copied', 0))} file(s) "
                    "could not be read from the backup and were skipped")
    if sentence and not sentence.startswith(" and "):
        message += sentence
    return dict(check, ok=True, message=message, path=folder, files=files,
                bytes=restored_bytes, errors=errors)
