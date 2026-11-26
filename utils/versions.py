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
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from utils.copying import BackupAborted, SourceUnavailable, TargetWriteFailed, copy_tree
from utils.fs_utils import (_is_link_like, clear_read_only, is_read_only, long_path,
                            plain_error, remark, remove_file, set_read_only)
from utils.jobs_store import (DEFAULT_RETENTION, FINISHED_UTC_FORMAT, VERIFY_MODES,
                              job_hold_released, valid_finished_utc)


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
    """Existing version stamps for `job`, sorted BY NAME.

    This is the raw listing, not the chronological order: names are local
    clock time, so across an autumn clock change name order can disagree with
    the order the backups were made in. Anything that means "older/newer"
    uses `ordered_versions`.

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
    """The newest version stamp of `job` (verified or not), or None. Newest
    in `ordered_versions` order -- by recorded UTC finish time, not by name."""
    names = ordered_versions(job)
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
# `{"type": "end", "files", "bytes", "verify": mode, "verified": bool,
# "finished_utc": "2026-10-25T01:45:12Z"}`.
# "verified" is true for "full" and "quick" and false for "off". A trailer
# written before modes existed has no "verify" key and reads as "full". A
# trailer written before card ved3wVOQ has no "finished_utc" and orders by
# name (see "Ordering" below); readers ignore keys they do not know.
# A file line carries `"changed": true` (card RnuRsA2V) only when the file was
# still changing while its second copy was read: kept, flagged, hash of what
# was read. Older manifests lack it; every reader ignores it.
# A file line carries `"ro": true` (card fEC32w6P) only when the SOURCE file was
# read-only. A finished version's files are marked read-only (see "Protecting a
# finished version" below), so the stored copy's own bit can no longer say what
# the source's was; this key does. The trailer carries `"protected": true` when
# the run was made by a build that does both (records `ro`, marks the version):
# restore reads `ro` only for such versions. Older manifests lack both keys and
# readers ignore keys they do not know.
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

    "finished_utc" is present only when it is a well-formed UTC time; a junk
    value is dropped (the version then orders by name), never trusted and
    never an exception.
    """
    line = _last_line(manifest_path(job, stamp))
    if not line:
        return None
    try:
        trailer = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):    # RecursionError: a 4 KB run of "["
        return None
    if not isinstance(trailer, dict) or trailer.get("type") != "end":
        return None
    mode = trailer.get("verify", "full")
    if mode not in VERIFY_MODES or trailer.get("verified") is not (mode != "off"):
        return None
    summary = dict(trailer, verify=mode)
    finished = valid_finished_utc(summary.pop("finished_utc", None))
    if finished:
        summary["finished_utc"] = finished
    # Only a real `true` counts; anything else is the same as absent.
    if summary.pop("protected", None) is True:
        summary["protected"] = True
    return summary


def verify_mode(job: Dict, stamp: str) -> Optional[str]:
    """How version `stamp` was checked ("full", "quick" or "off"), or None
    when it has no readable, complete manifest (legacy, crashed, damaged)."""
    summary = read_manifest_summary(job, stamp)
    return summary["verify"] if summary else None


def is_verified(job: Dict, stamp: str) -> bool:
    """True iff version `stamp` exists and its manifest ends verified -- a
    "full" or "quick" run. An "off" version is complete but never verified.

    This is deliberately only "the manifest says so": it does NOT re-read the
    stored files, so a file edited after the run (the read-only mark makes
    that an accident to be refused, not a thing prevented) still reads as
    verified. Catching that is the deep-verify card's job, not this one's."""
    summary = read_manifest_summary(job, stamp)
    return (os.path.isdir(long_path(version_path(job, stamp)))
            and summary is not None and summary["verified"] is True)


# ---------------------------------------------------------------------------
# Ordering (card ved3wVOQ)
# ---------------------------------------------------------------------------
# Version NAMES are local clock time and never change. In the hour the clocks
# go back, a run made later can carry an EARLIER name, so "older/newer" is not
# decided by name. It is decided by `finished_utc`, the UTC time recorded in
# the manifest trailer when the run finalised it. Runs of one job never
# overlap (the per-job lock), so finish order is run order.
#
# The rule, in one place (`_order_key`):
#   * a version that carries a valid `finished_utc` orders by it;
#   * a version without one (made before this change, or a damaged/missing
#     trailer or a junk value) cannot be compared by UTC, so these order by
#     NAME among themselves, and ALL of them come BEFORE every version that
#     carries a UTC time -- a version with the field was necessarily made by a
#     build newer than any version without it;
#   * ties (same UTC second, or both without) break by name.
# Known limit: a version whose trailer later becomes damaged loses its UTC
# time and so sorts among the oldest; it is unverified at that point anyway.

def utc_stamp(moment: datetime) -> str:
    """`moment` as the `finished_utc` text. A naive `moment` is taken to be
    UTC already; an aware one is converted to UTC."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime(FINISHED_UTC_FORMAT)


def _order_key(stamp: str, summary: Optional[Dict]) -> Tuple[int, str, str]:
    """Sort key of version `stamp` whose trailer summary is `summary`.

    `(1, finished_utc, stamp)` with a UTC time, `(0, "", stamp)` without.
    UTC strings are fixed-width, so string order is time order.
    """
    finished = (summary or {}).get("finished_utc")
    return (1, finished, stamp) if finished else (0, "", stamp)


def _ordered(job: Dict) -> List[Tuple[str, Optional[Dict]]]:
    """`(stamp, trailer summary or None)` for every version, oldest first.

    Reads each manifest's last line once (a few hundred bytes) and returns the
    summaries so a caller that needs "is it verified" or the file count does
    not read them again. Never raises: an unreadable manifest is a None
    summary and the version orders by name.
    """
    entries = [(name, read_manifest_summary(job, name)) for name in version_names(job)]
    entries.sort(key=lambda entry: _order_key(*entry))
    return entries


def ordered_versions(job: Dict) -> List[str]:
    """Version stamps of `job`, oldest first by recorded UTC finish time, with
    the fallback rule above. THE chronological order: latest_version, restore,
    prune, the sanity baseline and changes-since all use it."""
    return [name for name, _ in _ordered(job)]


def version_order_key(job: Dict, stamp: str) -> Tuple[int, str, str]:
    """Where `stamp` sits in the order, for comparing a stamp that is not part
    of a listing (the hold release's `since`) with the listed versions. A
    version no longer on disk has no trailer to read, so it keys by name like
    a version made before this change."""
    return _order_key(stamp, read_manifest_summary(job, stamp))


def _summary_verified(summary: Optional[Dict]) -> bool:
    """True when a trailer summary says verified ("full" or "quick")."""
    return summary is not None and summary["verified"] is True


def verified_versions(job: Dict) -> List[str]:
    """The verified subset of `ordered_versions(job)`, oldest first."""
    return [name for name, summary in _ordered(job) if _summary_verified(summary)]


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
    entries = _ordered(job)                      # oldest first, by recorded UTC time
    names = [name for name, _ in entries]
    if not names:
        return {"preferred": None, "latest": None, "fallback": False,
                "newer_unverified": []}
    verified = [name for name, summary in entries if _summary_verified(summary)]
    preferred = verified[-1] if verified else names[-1]
    return {"preferred": preferred, "latest": names[-1],
            "fallback": not verified,
            "newer_unverified": names[names.index(preferred) + 1:]}


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
    "Older" and "newest" are in `ordered_versions` order (recorded UTC finish
    time), not name order.

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
    # `since` is a stamp; compare in the order the versions are listed in, by
    # its own key (its recorded UTC time if it is still on disk).
    since_key = version_order_key(job, release["since"]) if release else None
    best, stamp = 0, None
    for name, summary in _ordered(job):         # oldest first, so >= keeps the newest tie
        if name == exclude or (since_key and _order_key(name, summary) < since_key):
            continue
        files = (summary or {}).get("files")
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


# ---------------------------------------------------------------------------
# Protecting a finished version (card fEC32w6P, spec safety item 9)
# ---------------------------------------------------------------------------
# A version is an ordinary folder the user is invited to open, so an accidental
# save, a drag in Explorer or a careless script could change a backup with no
# sign. Once a version is final, every FILE in it and its manifest get the
# read-only mark; a write is then refused until someone clears the mark on
# purpose. This is protection against accidents, NOT against a determined
# writer (clearing the mark is one click): tamper detection is the deep-verify
# and encrypted-manifest cards' job, and `is_verified` still means only "the
# manifest ends in a verified trailer".
#
# WHEN: `run_job` calls this right after the manifest and the folder are
# promoted (`os.replace` of both) and BEFORE the prune, i.e. after everything
# that writes into the version has finished (the copy, card 55's re-copy, card
# 54's retry, verification all ran against the `.partial`), and the manifest's
# trailer is final. Nothing writes a version afterwards.
#
# WHAT: files and the manifest only. Directories are not marked: on Windows a
# folder's read-only bit protects nothing inside it and has Explorer side
# effects, and on POSIX dropping a directory's write bit would stop retention
# deleting its files. Only the read-only bit changes (read-modify-write, see
# `fs_utils.set_read_only`): hidden, system and archive bits are kept.
#
# FAILURE IS NOT FATAL: a share that ignores attributes or a permission error
# leaves that file writable and counted in `failed`; the version is complete
# and valid either way, and restore treats a still-writable file in a
# protected version as an ordinary writable one (see `restore_version`).
#
# Versions made by earlier builds are not marked and are not migrated.

def protect_version(job: Dict, stamp: str) -> Dict[str, int]:
    """Mark version `stamp`'s files and its manifest read-only.

    Returns `{"marked": n, "failed": n}`. Never raises for a filesystem
    problem: one file that cannot be marked (or a folder that cannot be listed)
    counts once in `failed` and the walk carries on. Links and junctions are
    never followed or touched.
    """
    marked = failed = 0
    stack = [version_path(job, stamp)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as entries:
                listing = list(entries)
        except OSError:
            failed += 1
            continue
        for entry in listing:
            path = os.path.join(current, entry.name)
            try:
                if _is_link_like(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                set_read_only(path, True)
                marked += 1
            except OSError:
                failed += 1
    try:
        set_read_only(manifest_path(job, stamp), True)
        marked += 1
    except OSError:
        failed += 1
    return {"marked": marked, "failed": failed}


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

    Oldest and newest are in `ordered_versions` order (recorded UTC finish
    time; a version without one orders by name, before all that have one).

    `newest` is the version the calling run has just made: it is treated as
    the newest whatever its recorded time or name says. Names are local clock
    time, so in the hour the clocks go back a later run can carry an EARLIER
    name; ordered by name alone, a retention-1 job deleted the backup it had
    just made and kept the older one (PR #17). The UTC order now gets that
    right by itself; this stays as the belt to its braces, for a clock set
    back by hand (UTC then goes backwards too) or an unreadable trailer.
    """
    keep = max(1, int(job.get("retention", DEFAULT_RETENTION)))
    entries = _ordered(job)
    names = [name for name, _ in entries]
    if newest in names:
        names.remove(newest)
        names.append(newest)
    verified = {name for name, summary in entries if _summary_verified(summary)}
    protected = next((name for name in reversed(names) if name in verified), None)
    protect = set(protect)
    candidates = [name for name in names if name not in protect]
    removed = []
    for name in candidates[:max(0, len(candidates) - keep)]:
        if name == protected:
            continue
        # Manifest first: a version whose folder deletion fails halfway must
        # not keep claiming to be verified.
        # `remove_file` because the manifest is read-only once the version is
        # protected (card fEC32w6P); a failure here leaves the version whole.
        try:
            remove_file(manifest_path(job, name))
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

    Windows refuses to delete a read-only file. A backup keeps the source's
    read-only bit, and since card fEC32w6P every finished version's files are
    read-only, so without this retention could never delete a version. A
    second failure propagates to the caller.
    """
    was_read_only = is_read_only(path) and os.path.isfile(long_path(path))
    clear_read_only(path)
    try:
        func(path)
    except OSError:
        # The delete still failed (a file another program holds open): the
        # file keeps its mark rather than being left writable in a version
        # that stays on disk (card fEC32w6P).
        if was_read_only:
            remark(path)
        raise


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
        remove_file(manifest_tmp)
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
    That is deliberately still the NAME, not `ordered_versions`' UTC order
    (card ved3wVOQ): a leftover has no finished trailer, and an autumn clock
    change moves a name by one hour against a seven-day cutoff.

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
                except PermissionError:  # read-only mark: clear it, retry once
                    clear_read_only(path)
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


def _read_manifest_hashes(job: Dict, stamp: str, ro_out: Optional[set] = None
                          ) -> Tuple[Optional[Dict[str, bytes]], Optional[str]]:
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

    `ro_out`, if given, receives the path of every file line marked
    `"ro": true` (the source file was read-only). It is only meaningful when
    the manifest is trusted (the second value is None).
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
                if ro_out is not None and entry.get("ro") is True:
                    ro_out.add(rel)
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

    **Permissions (card fEC32w6P).** The stored files of a version marked by
    `protect_version` are read-only, and `_hash_copy` carries that over, so for
    such a version (trailer `protected: true`, manifest trusted) each restored
    file is made writable unless its manifest line says `ro: true` (the SOURCE
    file was read-only), and then made read-only. A file that is still
    writable in a protected version (its marking failed) is restored writable
    like any other. A version without `protected` (made by an earlier build) or
    with a manifest that cannot be trusted keeps the old rule: the copy keeps
    whatever read-only bit the stored file has. A restore into a folder that
    already holds a read-only file of the same name replaces it
    (`copying._open_for_write`); one that cannot is a `no_permission` failure
    naming the file.

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
    # Card fEC32w6P. Which restored files should be read-only. The stored files
    # of a PROTECTED version (trailer `protected: true`) are read-only because
    # of the protective mark, so their own bit says nothing about the source:
    # a restored file is writable unless its manifest line says `ro: true`.
    # A version made before the mark (no `protected`) keeps the old rule: the
    # stored file's own bit IS the source's, and the copy carries it over. A
    # manifest that cannot be trusted (`expected is None`) says nothing about
    # `ro`, so the copy's attributes are left exactly as the copy made them.
    summary = read_manifest_summary(job, version)
    protected = bool(summary and summary.get("protected"))
    read_only_paths: set = set()
    expected, unchecked_reason = _read_manifest_hashes(
        job, version, read_only_paths if protected else None)
    fix_modes = protected and expected is not None
    check: Dict = {"checked": 0, "mismatched": [], "not_in_manifest": [],
                   "missing_from_version": [], "unchecked_reason": unchecked_reason,
                   "verified": False}

    def compare(rel: str, _size: int, _mtime: float, sha: str, **_flags) -> None:
        """Compare one restored file's copy-time hash with its manifest entry,
        and (protected version) give the restored file its source's mode.

        Pops the entry so what is left in `expected` at the end is what the
        restore did not produce. Never raises: `copy_tree` would delete the
        file just copied if its callback failed. `_flags` swallows `ro=True`,
        which `copy_tree` passes because the STORED file is read-only.
        """
        want = expected.pop(rel, None)
        if want is None:
            check["not_in_manifest"].append(rel)
        elif want == bytes.fromhex(sha):
            check["checked"] += 1
        else:
            check["mismatched"].append(rel)
        if fix_modes:
            try:
                set_read_only(os.path.join(folder, *rel.split("/")),
                              rel in read_only_paths)
            except OSError:
                # Best effort: the file keeps the stored copy's read-only bit,
                # which is the safe side; the restore itself is unaffected.
                check["unrestored_modes"] = check.get("unrestored_modes", 0) + 1

    def count_read_only(_rel: str, _size: int, _mtime: float, _sha: str,
                        ro: bool = False, **_flags) -> None:
        """No trusted manifest: count the restored files that kept the stored
        copy's read-only mark (`ro=True` from `copy_tree`), so the message can
        say why they cannot be edited. Since card fEC32w6P a protected
        version's every file is read-only, and only its manifest says which
        were read-only in the source. Never raises."""
        if ro:
            check["read_only_kept"] = check.get("read_only_kept", 0) + 1

    try:
        files, restored_bytes, errors = copy_tree(
            source, folder, on_progress=on_progress, should_abort=should_abort,
            on_copied=compare if expected is not None else count_read_only, stats=stats)
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
    if check.get("unrestored_modes"):
        message += (f". {_files(check['unrestored_modes'])} could not be made "
                    "writable and stayed read-only (clear the read-only mark to edit)")
    if check.get("read_only_kept"):
        message += (f". {_files(check['read_only_kept'])} stayed read-only as they are "
                    "stored in the backup, since its checksum list cannot say which were "
                    "read-only originally (clear the read-only mark to edit)")
    return dict(check, ok=True, message=message, path=folder, files=files,
                bytes=restored_bytes, errors=errors)
