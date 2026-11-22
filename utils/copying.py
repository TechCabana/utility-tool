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
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from utils.fs_utils import _is_link_like, long_path, plain_error


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
        # ponytail: Windows reports a path it cannot hold as a plain ENOENT --
        # past MAX_PATH without the extended-length form, and near 32,767
        # characters even with it (measured: a folder from ~32,762, a file
        # at 32,767) -- so length decides. Mislabels a drive that vanished at
        # a >=260 path; the run still fails either way.
        if cause == "target_unavailable" and os.name == "nt" and len(path) >= 260:
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
                        rel = os.path.relpath(path, root).replace("\\", "/")
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


def _hash_copy(src: str, dst: str) -> Tuple[int, float, str]:
    """Copy `src` to `dst`, hashing the bytes as they pass through.

    Returns `(bytes read, source mtime, sha256 hex)`. Metadata is then copied
    with `copystat`, as `copy2` does, so the mtime the changes-since
    comparison relies on is preserved. If anything fails once `dst` exists,
    the half-written file is deleted before the error propagates, so it can
    never sit in a version looking like a real copy.

    Errors are classified where they happen: failing to open or read `src`
    raises the OSError (the caller skips that file); failing to create,
    write, close or stamp `dst` raises TargetWriteFailed (the caller fails
    the whole copy). Both are plain paths; the OS calls use `long_path`.
    """
    digest = hashlib.sha256()
    size = 0
    read_error = None
    lsrc = long_path(src)
    with open(lsrc, "rb") as fsrc:
        mtime = os.fstat(fsrc.fileno()).st_mtime
        with _writing(dst):
            ldst = long_path(dst)  # past Windows' limit: a target failure
        try:
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
            try:
                shutil.copystat(lsrc, ldst)
            except OSError as exc:
                if exc.filename == lsrc:
                    raise  # stat of the source failed: a skip, not the target's
                raise TargetWriteFailed(dst, exc) from exc
            _copy_windows_attributes(lsrc, ldst)
        except BaseException:
            try:
                os.remove(ldst)
            except OSError:
                pass
            raise
    return size, mtime, digest.hexdigest()


def copy_tree(source: str, destination: str,
              on_progress: Optional[Callable[[int, int], None]] = None,
              should_abort: Optional[Callable[[], bool]] = None,
              on_copied: Optional[Callable[[str, int, float, str], None]] = None,
              stats: Optional[Dict[str, int]] = None
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
    would recurse until the disk fills). Existing destination folders are
    reused, as copytree's `dirs_exist_ok=True` did.

    Every file is copied by `_hash_copy`. With `on_copied`,
    `on_copied(relative path, size, mtime, sha256)` is called once it has
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
    """
    total = tree_totals(source)[0]
    state = {"done": 0, "bytes": 0}
    errors: List[str] = []

    def check_source_alive() -> None:
        """Stop the copy if the source root has gone away."""
        if _source_gone(source):
            raise SourceUnavailable(source, state["done"], total,
                                    "source_unavailable")

    def copy_one(src: str, dst: str) -> None:
        """Abort check, copy, count, report one file."""
        if should_abort is not None and should_abort():
            raise BackupAborted()
        size, mtime, sha = _hash_copy(src, dst)
        if on_copied is not None:
            try:
                on_copied(os.path.relpath(src, source).replace("\\", "/"),
                          size, mtime, sha)
            except BaseException:
                try:
                    os.remove(long_path(dst))
                except OSError:
                    pass
                raise
        state["bytes"] += size
        state["done"] += 1
        if on_progress is not None:
            on_progress(state["done"], total)

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
            try:
                if _is_link_like(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    copy_dir(src, dst)
                else:
                    copy_one(src, dst)
            except OSError as exc:
                # Source-side failures land here; the destination's are
                # TargetWriteFailed and pass straight up. A full drive or an
                # over-size file can only come from a write, so one arriving
                # unclassified is still the target's (belt and braces).
                failed = TargetWriteFailed(dst, exc)
                if failed.cause in ("no_space", "file_too_large"):
                    raise failed from exc
                check_source_alive()  # a vanished root is not one more skip
                # Path from the source root (forward slashes, as in the
                # manifest) so the run log says WHICH file; the "<path>: <error>"
                # shape is relied on by record_run and runlog.
                rel = os.path.relpath(src, source).replace("\\", "/")
                errors.append(f"{rel}: {plain_error(exc)}")
        try:
            shutil.copystat(long_path(src_dir), long_path(dst_dir))
        except OSError:
            pass  # folder times are cosmetic: changes-since compares files only

    copy_dir(source, destination)
    check_source_alive()  # gone by the end: whatever copied is not the whole
    missing = max(0, total - state["done"])
    if stats is not None:
        stats["expected"], stats["not_copied"] = total, missing
    # A copy holding NOTHING is never a backup, whatever the floor says: a
    # two-file source with both files locked is under SHORTFALL_MIN_FILES,
    # and promoting its empty copy as "verified" pruned the last real one.
    if not state["done"] or (missing >= SHORTFALL_MIN_FILES
                             and missing > SHORTFALL_TOLERANCE * total):
        raise SourceUnavailable(source, state["done"], total, "source_incomplete")
    return state["done"], state["bytes"], errors
