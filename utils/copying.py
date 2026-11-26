# utils/copying.py
"""Walking a tree and copying it: the hashing per-file copy, `copy_tree`,
and the exceptions that tell a target failure, a vanished source and a
cancellation apart from a skipped file.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.
"""
import contextlib
import errno
import hashlib
import os
import shutil
import stat
import time
from typing import Callable, Dict, Iterator, List, Optional, Set, Tuple

from utils.fs_utils import (_is_link_like, is_legacy_profile_link, long_path,
                            plain_error, relative_to, remove_file)


class BackupAborted(Exception):
    """Raised inside the copy to stop it when the user quits mid-run.

    Not an OSError on purpose: `copy_tree` swallows OSError per entry and
    carries on, which is exactly the behaviour wanted for a locked file and
    exactly wrong for a cancellation.
    """


# Plain-language reasons for a failure to write the drive being copied TO,
# keyed by the machine-readable `cause` a failed result carries. `{drive}` is
# "backup drive" for a backup and "destination drive" for a restore.
TARGET_CAUSES = {
    "no_space": "The {drive} is full",
    "path_too_long": "A file path is too long for the {drive}",
    "file_too_large": "A file is too large for this drive (FAT32 limits files to 4 GB)",
    "target_unavailable": "The {drive} was disconnected or became unavailable",
    "no_permission": "No permission to write to the {drive}",
}
_WINERROR_CAUSES = {112: "no_space", 39: "no_space", 206: "path_too_long",
                    223: "file_too_large", 21: "target_unavailable",
                    3: "target_unavailable", 53: "target_unavailable",
                    64: "target_unavailable", 67: "target_unavailable",
                    5: "no_permission"}
_ERRNO_CAUSES = {errno.ENOSPC: "no_space", errno.ENAMETOOLONG: "path_too_long",
                 errno.EFBIG: "file_too_large", errno.ENOENT: "target_unavailable",
                 getattr(errno, "ESTALE", -1): "target_unavailable",
                 errno.EACCES: "no_permission", errno.EPERM: "no_permission"}


class TargetWriteFailed(Exception):
    """Writing the destination of a copy failed: a full drive, a path too long
    for it, a file too large for FAT32, a drive that went away.

    Not an OSError on purpose, like BackupAborted: `copy_tree` counts a
    per-file OSError as one skipped SOURCE file and carries on, and a run
    that carried on past a full drive went on to verify, promote and PRUNE a
    copy holding a fraction of the files -- deleting the last complete
    backup. Only a failure to open or read the source may be a skip.

    `cause` is a `TARGET_CAUSES` key, or "target_write_failed" when the error
    is not one of those; `path` is the destination that failed.
    """

    def __init__(self, path: str, error: OSError):
        """Classify `error`, raised while writing destination `path`."""
        self.path, self.error = path, error
        cause = (_WINERROR_CAUSES.get(getattr(error, "winerror", None))
                 or _ERRNO_CAUSES.get(error.errno) or "target_write_failed")
        # ponytail: Windows reports a path it cannot hold as a plain ENOENT
        # near 32,767 characters even in the extended-length form every engine
        # call now uses (measured: a folder from ~32,762, a file at 32,767), so
        # length decides there. Not at MAX_PATH any more: a 300-character path
        # is writable, so ENOENT on one means the drive went away, and calling
        # it "too long" listed innocent files as offenders. Mislabels a drive
        # that vanished at a >=32,000 path; the run still fails either way.
        if cause == "target_unavailable" and os.name == "nt" and len(path) >= 32_000:
            cause = "path_too_long"
        self.cause = cause
        super().__init__(self.reason("backup drive"))

    def reason(self, drive: str) -> str:
        """The plain-language cause, naming the file, e.g. "The backup drive
        is full (failed on report.docx)"."""
        text = TARGET_CAUSES.get(self.cause)
        text = (text.format(drive=drive) if text else
                f"Could not write to the {drive}: {self.error.strerror or self.error}")
        return f"{text} (failed on {os.path.basename(self.path)})"


@contextlib.contextmanager
def _writing(path: str) -> Iterator[None]:
    """Re-raise any OSError inside the block as TargetWriteFailed for `path`.

    Wraps exactly the calls that touch the destination, so where an error is
    classified is where it happened, never guessed later from its text.
    """
    try:
        yield
    except OSError as exc:
        raise TargetWriteFailed(path, exc) from exc


# A copy that ends with fewer files than the source held when it started is
# only a failure past BOTH of these: a locked file or two is normal and is
# reported as a skip, but a run that copied a fraction of the tree (a folder
# went away, a drive dropped mid-run) must not become the newest verified
# backup and prune a complete one. The floor stops one locked file in a tiny
# tree from being "more than 30%". A vanished source ROOT always fails,
# whatever the counts, and so does a copy that holds no files at all.
SHORTFALL_TOLERANCE = 0.30
SHORTFALL_MIN_FILES = 3


class SourceUnavailable(Exception):
    """The folder being copied FROM went away or is far from complete.

    Not an OSError on purpose, like TargetWriteFailed: `copy_tree` counts a
    per-file OSError as one skipped file and carries on, and a run that
    carried on past a source that had vanished verified, promoted and PRUNED
    a copy holding one file. `cause` is "source_unavailable" (the root is
    gone or unreadable) or "source_incomplete" (the root is there but the
    shortfall passed `SHORTFALL_TOLERANCE`); `expected` is how many files
    the source held when the copy started and `copied` how many landed.
    """

    def __init__(self, source: str, copied: int, expected: int, cause: str):
        """Record how far the copy of `source` got and why it stopped."""
        self.source, self.copied, self.expected, self.cause = (
            source, copied, expected, cause)
        super().__init__(self.reason("source folder", "backup"))

    def reason(self, what: str, during: str) -> str:
        """The plain-language cause for `what` ("source folder" for a backup,
        "backup version" for a restore) `during` the "backup" or "restore",
        with how much was copied."""
        counts = f"only {self.copied} of {self.expected} file(s) were copied"
        if self.cause == "source_unavailable":
            return (f"The {what} became unavailable during the {during} (it was "
                    f"disconnected, moved or can no longer be read), so {counts}")
        return (f"Too many files in the {what} could not be read during the "
                f"{during}, so {counts}")


def _source_gone(source: str) -> bool:
    """True if `source` can no longer be listed (gone, moved, unplugged)."""
    try:
        with os.scandir(long_path(source)):
            return False
    except OSError:
        return True


# ---------------------------------------------------------------------------
# Walking and copying
# ---------------------------------------------------------------------------

def _iter_tree(root: str) -> Iterator[Optional[Tuple[str, int, float]]]:
    """Yield `(relative path, size, mtime)` per file, or None per unreadable
    entry. Relative paths use forward slashes and keep their case.

    A generator so a caller that only needs a count or a total (pre-flight,
    the copy's progress total) never holds the whole tree in memory.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as entries:
                for entry in entries:
                    try:
                        # entry.path is in long_path's form; the walk keeps
                        # its own plain path so relative paths are unchanged.
                        path = os.path.join(current, entry.name)
                        if _is_link_like(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                        rel = relative_to(path, root).replace("\\", "/")
                        yield rel, stat.st_size, stat.st_mtime
                    except OSError:
                        yield None
        except OSError:
            yield None


def walk_tree(root: str) -> Tuple[Dict[str, Tuple[int, float]], int]:
    """`({relative path: (size, mtime)}, unreadable_count)` for a whole tree.

    Relative paths are normalised with forward slashes and lowercased case
    only for the *key*, so a source and its backup compare correctly on
    Windows' case-insensitive filesystem. Unreadable entries are counted, not
    raised.
    """
    files: Dict[str, Tuple[int, float]] = {}
    skipped = 0
    for item in _iter_tree(root):
        if item is None:
            skipped += 1
        else:
            files[item[0].lower()] = (item[1], item[2])
    return files, skipped


def tree_totals(root: str) -> Tuple[int, int]:
    """`(file count, total bytes)` of a tree, in constant memory."""
    count = size = 0
    for item in _iter_tree(root):
        if item is not None:
            count += 1
            size += item[1]
    return count, size


# Read/write chunk for the hashing copy and the verify pass. 1 MiB is
# shutil's own Windows buffer size.
CHUNK = 1024 * 1024


# Windows file attributes `_hash_copy` carries over. copy2 (CopyFile2) kept
# these for free; copystat only carries read-only (through chmod), so without
# this a backed-up hidden or system file came back as an ordinary one.
# Compressed/encrypted/sparse are not settable through SetFileAttributesW and
# are deliberately absent. Alternate data streams are NOT copied either.
_KEPT_ATTRIBUTES = (0x1 | 0x2 | 0x4 | 0x20 | 0x2000)  # RO, HIDDEN, SYSTEM, ARCHIVE, NOT_CONTENT_INDEXED
_FILE_ATTRIBUTE_NORMAL = 0x80


def _copy_windows_attributes(src: str, dst: str) -> None:
    """Give `dst` the hidden/system/archive/read-only bits `src` has.

    Windows only; a no-op elsewhere. Best effort, like copy2's own metadata
    copy: a failure here leaves a correct copy with plainer attributes, which
    is not a reason to fail the file.
    """
    if os.name != "nt":
        return
    try:
        wanted = os.stat(src).st_file_attributes & _KEPT_ATTRIBUTES
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(dst, wanted or _FILE_ATTRIBUTE_NORMAL)
    except (OSError, AttributeError):
        pass


def _open_for_write(dst: str):
    """Open `dst` for writing, replacing a read-only or hidden file there.

    Windows refuses to truncate a file that is read-only, hidden or system
    (PermissionError). A fresh `.partial` never holds one, but a restore into
    a folder an earlier restore filled does: that restore carried those
    attributes over from the source, so without this a second restore of any
    folder holding a git repo or a desktop.ini failed outright. Clear them
    and try once more -- `_hash_copy` puts the source's attributes back when
    the copy finishes. Any other failure, or a second one, propagates.
    """
    try:
        return open(dst, "wb")
    except PermissionError:
        if os.path.islink(dst) or not os.path.isfile(dst):
            raise
        os.chmod(dst, stat.S_IWRITE)
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(dst, _FILE_ATTRIBUTE_NORMAL)
        return open(dst, "wb")


class CopyResult(tuple):
    """What `_hash_copy` returns: the `(bytes read, source mtime, sha256 hex)`
    tuple it always was, plus `.changed` (card RnuRsA2V).

    A tuple subclass rather than a fourth element so every caller and test
    that unpacks three values keeps working; only the code that cares reads
    the attribute, with `getattr(result, "changed", False)` so a plain tuple
    (a test's stand-in for `_hash_copy`) simply reads as "not changed".
    """

    def __new__(cls, size: int, mtime: float, sha: str, changed: bool = False,
                read_only: bool = False):
        """The three values, whether the source changed under the read, and
        whether the SOURCE file was read-only (card fEC32w6P: recorded in the
        manifest, because the stored copy's own bit now means "protected")."""
        self = super().__new__(cls, (size, mtime, sha))
        self.changed = changed
        self.read_only = read_only
        return self


def _signature(info: os.stat_result) -> Tuple[int, int]:
    """`(size, mtime_ns)`: what "the file is the same" means. Nanoseconds, not
    the float `st_mtime`, which cannot tell two NTFS timestamps 100 ns apart."""
    return info.st_size, info.st_mtime_ns


def _after_read(src: str) -> None:
    """Called once the last chunk is read and the source handle is closed,
    just before the post-copy look at the source. Does nothing; it exists so a
    test can change the file at exactly the moment the check is for."""


def _hash_copy(src: str, dst: str) -> CopyResult:
    """Copy `src` to `dst`, hashing the bytes as they pass through.

    Returns `(bytes read, source mtime, sha256 hex)` (a `CopyResult`). Metadata
    is then copied with `copystat`, as `copy2` does, so the mtime the
    changes-since comparison relies on is preserved. If anything fails once
    `dst` exists, the half-written file is deleted before the error
    propagates, so it can never sit in a version looking like a real copy.

    Errors are classified where they happen: failing to open or read `src`
    raises the OSError (the caller skips that file); failing to create,
    write, close or stamp `dst` raises TargetWriteFailed (the caller fails
    the whole copy). Both are plain paths; the OS calls use `long_path`.

    **Changed while copied (card RnuRsA2V).** The bytes hashed here are what
    the check later re-reads, so verification proves the copy matches what was
    read, never that what was read was ONE state of the file. So the source's
    `(size, mtime_ns)` is taken from the open handle before the first read and
    compared with two later looks: the same handle once the last chunk is in
    (the file as the reader saw it end) and the PATH after the copy (a file
    replaced by rename is a different file; a handle can report stale values
    on some filesystems). Either differing sets `.changed`. Two `fstat`/`stat`
    calls per file. A source that cannot be looked at afterwards is an OSError
    like any read failure: that file is skipped, as the first pass would.
    """
    digest = hashlib.sha256()
    size = 0
    read_error = None
    changed = False
    lsrc = long_path(src)
    ldst = None  # set once the destination path is built; the cleanup below needs it
    try:
        with open(lsrc, "rb") as fsrc:
            before = os.fstat(fsrc.fileno())
            mtime = before.st_mtime
            # No write bit at all = read-only. Windows reports the read-only
            # attribute as mode 0o444, so this is the same test there.
            read_only = not before.st_mode & 0o222
            with _writing(dst):
                ldst = long_path(dst)  # past Windows' limit: a target failure
            with _writing(dst):
                with _open_for_write(ldst) as fdst:
                    while True:
                        try:
                            chunk = fsrc.read(CHUNK)
                        except OSError as exc:
                            read_error = exc  # the source's, re-raised below as a skip
                            break
                        if not chunk:
                            break
                        digest.update(chunk)
                        fdst.write(chunk)
                        size += len(chunk)
            if read_error is not None:
                raise read_error
            after_handle = os.fstat(fsrc.fileno())
        # The source handle is closed from here, so a program (or a test) can
        # replace the file now without being refused for the open handle.
        _after_read(src)
        try:
            shutil.copystat(lsrc, ldst)
        except OSError as exc:
            if exc.filename == lsrc:
                raise  # stat of the source failed: a skip, not the target's
            raise TargetWriteFailed(dst, exc) from exc
        _copy_windows_attributes(lsrc, ldst)
        # Last, so the window the check covers runs to the end of the copy.
        after_path = os.stat(lsrc)
        changed = (_signature(before) != _signature(after_handle)
                   or _signature(before) != _signature(after_path))
    except BaseException:
        # Only a destination that was opened for writing can be half-written;
        # a failure to open the source or build the path leaves nothing of ours.
        if ldst is not None:
            try:
                # remove_file, not os.remove: copystat may already have made
                # the half-copy read-only (a read-only source), and Windows
                # will not delete that.
                remove_file(ldst)
            except OSError:
                pass
        raise
    return CopyResult(size, mtime, digest.hexdigest(), changed, read_only)


# One short pause before the retry pass, and only when a file was skipped.
# Most locks are brief (a document being saved, an antivirus scan, a sync
# client), and the rest of the tree having been copied is usually delay enough;
# a small tree finishes in milliseconds, so half a second more is cheap and
# makes the second attempt a real second chance. A named constant behind
# `_retry_pause` so a test sets it to 0 or replaces the function outright.
RETRY_PAUSE_SECONDS = 0.5


def _retry_pause() -> None:
    """Wait `RETRY_PAUSE_SECONDS` before the retry pass (patched by tests)."""
    if RETRY_PAUSE_SECONDS > 0:
        time.sleep(RETRY_PAUSE_SECONDS)


def copy_tree(source: str, destination: str,
              on_progress: Optional[Callable[[int, int], None]] = None,
              should_abort: Optional[Callable[[], bool]] = None,
              on_copied: Optional[Callable[[str, int, float, str], None]] = None,
              stats: Optional[Dict[str, int]] = None,
              retry_skipped: bool = False,
              recopy_changed: bool = False,
              only: Optional[Set[str]] = None
              ) -> Tuple[int, int, List[str]]:
    """Copy `source` into `destination`, reporting `(done, total)` per file.

    Returns `(files_copied, bytes_copied, errors)`, where `errors` names the
    SOURCE files and folders that could not be opened or read. A locked file
    is normal (a running app holds it open), so it is skipped and reported
    and the copy carries on. A failure to write the DESTINATION -- creating
    a folder, writing or closing a file -- is never a skip: it raises
    TargetWriteFailed and the copy stops, because carrying on past a full or
    vanished drive only produces a copy missing everything after that point.
    An unreadable `source` itself raises OSError.

    A hand-rolled walk, not `shutil.copytree`: copytree catches every
    per-entry OSError, including its own folder creation on the destination,
    into one list, so a target failure could not be told from a skip.
    Symlinks and junctions are skipped (`_is_link_like`, the one link
    predicate: copying a junction that points at an ancestor of the source
    would recurse until the disk fills), but not silently (card tiha5I8F):
    `stats["links"]` counts them, `stats["links_folders"]` says how many point
    at a folder, and `stats["link_names"]` names them all (relative paths,
    sorted). Windows' own access-denied profile junctions (`My Documents`...)
    are left out of all three, see `fs_utils.is_legacy_profile_link`. Existing
    destination folders are reused, as copytree's `dirs_exist_ok=True` did.

    Every file is copied by `_hash_copy`. With `on_copied`,
    `on_copied(relative path, size, mtime, sha256)` (plus `changed=True` and/or
    `ro=True`, only when they apply, see `copy_one`) is called once it has
    landed -- `run_job` streams that into the version's manifest. If
    `on_copied` fails the file just copied is deleted, so the tree never holds
    a file its manifest does not list.

    Completeness: the file count taken when the copy starts is what the copy
    must be measured against. If `source` can no longer be listed -- checked
    as soon as a source error shows up, so a vanished drive stops the copy
    instead of grinding through one error per remaining file, and again when
    the walk ends -- or if the files copied fall short of that count by
    `SHORTFALL_MIN_FILES` or more AND more than `SHORTFALL_TOLERANCE`, or if
    not one file was copied at all, it raises SourceUnavailable. A smaller
    shortfall is just the locked files in `errors`. `stats`, if given, gets "expected" (that starting count) and
    "not_copied" (how many of them did not land): `errors` has one entry for
    a whole unreadable folder, so its length under-reports the files missed.

    With `retry_skipped` (a backup; not a restore), every SOURCE FILE skipped
    during the walk gets exactly one more attempt after the whole tree has
    been walked, in the order it was skipped and through the same `copy_one`
    as any other file, so its manifest line, counters and progress are those
    of a file that copied first time. A recovered file leaves `errors`; one
    that fails again is reported by name with its newest error. An unlistable
    FOLDER is not retried. A retry that hits a target failure, a vanished
    source or a cancellation ends the copy exactly as it would have on the
    first pass: only a source-read failure is ever skipped. `stats["retried"]`
    counts the files recovered.

    With `recopy_changed` (a backup; not a restore, whose source is a stored
    version that nothing is writing), a file whose size or mtime differs after
    the copy from before it (`_hash_copy`'s `.changed`) was being written while
    it was read, so the copy may mix two states. It is copied once more, into
    the same destination, through the same `copy_one`. If that second copy is
    stable the file is an ordinary copied file (`stats["recopied"]` counts it);
    if it changed again the second copy is kept and the file is flagged: it
    reaches `on_copied` with `changed=True` (only then: a callback that takes
    four arguments never sees the keyword), and `stats["changed"]` and
    `stats["changed_names"]` count and name it. **One extra attempt per file,
    in total**: the unreadable-file retry above already is that attempt, so a
    file recovered by it is flagged on a first look at a change instead of
    being copied a third time, and a file whose re-copy then could not be read
    is skipped without the retry pass trying it again. The count, the manifest
    line, the progress step and the byte total are made once per file, after
    its last copy, never per attempt.

    With `only` (the test restore, card 0Bv7mvB3: a SAMPLE of a version), just
    the files whose relative path (forward slashes, the manifest's form) is in
    the set are copied, through the same `copy_one`, and only the folders on
    the way to them are entered. The whole-tree completeness rule below does
    not apply: the expected count is `len(only)`, and a named file the source
    does not hold is simply not copied -- the caller, which knows what it
    asked for, reports it. A source that can no longer be listed still raises
    SourceUnavailable.
    """
    total = len(only) if only is not None else tree_totals(source)[0]
    # Every folder that holds a wanted file, so the walk enters only those.
    wanted_dirs: Set[str] = set()
    for wanted in only or ():
        parts = wanted.split("/")[:-1]
        wanted_dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    state = {"done": 0, "bytes": 0, "recopied": 0}
    errors: List[str] = []
    # (source file, its destination, its index in `errors`) per skipped file.
    skipped_files: List[Tuple[str, str, int]] = []
    changed_names: List[str] = []
    # Links left unfollowed (card tiha5I8F): relative path -> "folder" | "file".
    links: Dict[str, str] = {}
    # Source files whose one extra attempt (a re-copy) is already used.
    spent: set = set()

    def check_source_alive() -> None:
        """Stop the copy if the source root has gone away."""
        if _source_gone(source):
            raise SourceUnavailable(source, state["done"], total,
                                    "source_unavailable")

    def copy_one(src: str, dst: str, second_look: bool = True) -> None:
        """Abort check, copy (and re-copy once if it changed), count, report
        one file. `second_look=False` is the retry pass: that file already
        had its extra attempt."""
        if should_abort is not None and should_abort():
            raise BackupAborted()
        result = _hash_copy(src, dst)
        flagged = False
        if recopy_changed and getattr(result, "changed", False):
            if second_look:
                spent.add(src)
                if should_abort is not None and should_abort():
                    raise BackupAborted()
                try:
                    result = _hash_copy(src, dst)  # overwrites the first copy
                except BaseException:
                    # The first copy is on disk with no manifest line and no
                    # count; a re-copy that fails must not leave it behind as a
                    # stray file in the version (`_hash_copy` only cleans up
                    # after a destination IT opened).
                    try:
                        remove_file(dst)
                    except OSError:
                        pass
                    raise
                flagged = bool(getattr(result, "changed", False))
                state["recopied"] += not flagged
            else:
                flagged = True
        size, mtime, sha = result
        rel = relative_to(src, source).replace("\\", "/")
        if on_copied is not None:
            # Keyword arguments only when they say something, so a callback
            # that takes four arguments never sees them: `changed` (card
            # RnuRsA2V) and `ro` (card fEC32w6P: the SOURCE file was
            # read-only, which the manifest records because the stored copy's
            # own bit now means "protected").
            extra = {}
            if flagged:
                extra["changed"] = True
            if getattr(result, "read_only", False):
                extra["ro"] = True
            try:
                on_copied(rel, size, mtime, sha, **extra)
            except BaseException:
                try:
                    remove_file(dst)
                except OSError:
                    pass
                raise
        if flagged:
            changed_names.append(rel)
        state["bytes"] += size
        state["done"] += 1
        if on_progress is not None:
            on_progress(state["done"], total)

    def skip(src: str, dst: str, exc: OSError) -> str:
        """The `errors` entry for a failed entry, or the run-ending exception.

        Source-side failures are skips; the destination's are
        TargetWriteFailed and pass straight up. A full drive or an over-size
        file can only come from a write, so one arriving unclassified is
        still the target's (belt and braces). Shared by the walk and the
        retry pass so a retried file is classified exactly as on the first.
        """
        failed = TargetWriteFailed(dst, exc)
        if failed.cause in ("no_space", "file_too_large"):
            raise failed from exc
        check_source_alive()  # a vanished root is not one more skip
        # Path from the source root (forward slashes, as in the manifest) so
        # the run log says WHICH file; the "<path>: <error>" shape is relied
        # on by record_run and runlog.
        rel = relative_to(src, source).replace("\\", "/")
        return f"{rel}: {plain_error(exc)}"

    def note_link(entry: os.DirEntry, src: str) -> None:
        """Record a symlink/junction the walk is declining to follow (card
        tiha5I8F), unless it is one of Windows' own legacy profile junctions.

        Called from the one walk the copy already makes, so a link is seen once
        (a dict keyed on its relative path, so even a repeat could not count it
        twice) and a link inside a folder that is never visited is never seen.
        Looks at the link itself and, for kind, stats its target (`is_dir`);
        nothing is listed or opened, so nothing is followed.
        """
        if is_legacy_profile_link(entry, src):
            return
        try:
            kind = "folder" if entry.is_dir() else "file"
        except OSError:
            kind = "file"  # a dead link: nothing to say it points at a folder
        links[relative_to(src, source).replace("\\", "/")] = kind

    def copy_dir(src_dir: str, dst_dir: str) -> None:
        """Copy one folder, depth first. OSError if `src_dir` is unreadable."""
        with os.scandir(long_path(src_dir)) as listing:
            entries = list(listing)
        with _writing(dst_dir):
            os.makedirs(long_path(dst_dir), exist_ok=True)
        for entry in entries:
            # Plain paths throughout; entry.path is in long_path's form.
            src = os.path.join(src_dir, entry.name)
            dst = os.path.join(dst_dir, entry.name)
            is_file = False
            try:
                if _is_link_like(entry):
                    if only is None:
                        note_link(entry, src)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if only is None or relative_to(src, source).replace("\\", "/") in wanted_dirs:
                        copy_dir(src, dst)
                else:
                    if only is not None and relative_to(src, source).replace("\\", "/") not in only:
                        continue
                    is_file = True
                    copy_one(src, dst)
            except OSError as exc:
                errors.append(skip(src, dst, exc))
                if is_file:
                    skipped_files.append((src, dst, len(errors) - 1))
        try:
            shutil.copystat(long_path(src_dir), long_path(dst_dir))
        except OSError:
            pass  # folder times are cosmetic: changes-since compares files only

    copy_dir(source, destination)
    retried = 0
    if retry_skipped and skipped_files:
        _retry_pause()
        for src, dst, index in skipped_files:
            if src in spent:
                continue  # its extra attempt was the re-copy that failed to read
            try:
                copy_one(src, dst, second_look=False)
            except OSError as exc:
                errors[index] = skip(src, dst, exc)
            else:
                errors[index] = None
                retried += 1
        errors[:] = [entry for entry in errors if entry is not None]
    check_source_alive()  # gone by the end: whatever copied is not the whole
    missing = max(0, total - state["done"])
    if stats is not None:
        stats["expected"], stats["not_copied"] = total, missing
        stats["retried"] = retried
        stats["recopied"] = state["recopied"]
        stats["changed"] = len(changed_names)
        stats["changed_names"] = changed_names
        # Sorted by name (case-folded, then exact) so the capped sample every
        # surface shows is the same on every run, whatever order the OS listed.
        stats["links"] = len(links)
        stats["links_folders"] = sum(kind == "folder" for kind in links.values())
        stats["link_names"] = sorted(links, key=lambda name: (name.casefold(), name))
    # A copy holding NOTHING is never a backup, whatever the floor says: a
    # two-file source with both files locked is under SHORTFALL_MIN_FILES,
    # and promoting its empty copy as "verified" pruned the last real one.
    if only is None and (not state["done"] or (missing >= SHORTFALL_MIN_FILES
                                               and missing > SHORTFALL_TOLERANCE * total)):
        raise SourceUnavailable(source, state["done"], total, "source_incomplete")
    return state["done"], state["bytes"], errors
