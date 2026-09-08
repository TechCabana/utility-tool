# utils/disk_utils.py
"""Disk inspection helpers for the Disk tab: byte formatting and a
per-top-level-folder size breakdown of a directory.

Qt-free by design (CLAUDE.md §3/§8) -- these take plain paths in and return
plain values out. `tabs/disk_tab.py` runs `top_level_breakdown` from a
QThread worker and renders the result; nothing here knows that.

Drive totals are not wrapped here: `shutil.disk_usage()` already answers that
question in one stdlib call, so the tab calls it directly.
"""
import os
from typing import Callable, List, Optional, Tuple

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

# Windows' name-surrogate bit. A reparse tag with this bit set stands in for
# another path (junctions 0xA0000003, symlinks 0xA000000C); one without it is
# a filter that leaves the directory a real place on disk. Written out rather
# than imported so this module stays import-safe off Windows.
_NAME_SURROGATE = 0x20000000


def _is_link_like(entry: os.DirEntry) -> bool:
    """True for symlinks and, on Windows, junctions / mount points.

    `DirEntry.is_symlink()` is False for a junction -- Python counts only
    IO_REPARSE_TAG_SYMLINK as a link -- so a Windows home directory's legacy
    junctions ("My Documents", "Application Data", "Cookies", "Recent", ...)
    would be walked as if they were real folders. They redirect into folders
    already being counted, so following them means double counting, and they
    are access-denied by design, so they also produce rows of pure noise
    carrying a spurious permission warning.

    Testing the reparse-point *attribute* alone is wrong and was tried first:
    a cloud-sync root (Dropbox, iCloudDrive) is also a reparse point, tagged
    0x9000xxxx by the cloud filter driver, and dropping those hid 70 GB of
    real folders from the breakdown. Only the name-surrogate bit separates
    "this is really somewhere else" from "this is a real directory with a
    filter attached".
    """
    try:
        if entry.is_symlink():
            return True
        tag = entry.stat(follow_symlinks=False).st_reparse_tag
    except AttributeError:
        return False  # not Windows: st_reparse_tag doesn't exist
    except OSError:
        return False  # unreadable; the caller counts it as skipped
    return bool(tag & _NAME_SURROGATE)


def human_size(num_bytes: int) -> str:
    """Format a byte count as a short human-readable string, e.g. "4.2 GB".

    Binary units (1 KB = 1024 B), matching the KB figures the Image Tools
    batch already reports. Negative input is clamped to zero -- a size is
    never negative, and a stat quirk should not render as "-1.0 KB".
    """
    size = float(max(0, num_bytes))
    for unit in _UNITS[:-1]:
        if size < 1024:
            # Whole bytes are exact; there is no fractional byte to show.
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def folder_size(path: str,
                should_abort: Optional[Callable[[], bool]] = None) -> Tuple[int, int]:
    """Total bytes under `path`, plus a count of entries that could not be read.

    Walks iteratively with `os.scandir` (no recursion limit, and it reuses the
    stat data the directory listing already returned rather than re-statting).

    Symlinks and Windows junctions are never followed. A Windows home
    directory is full of legacy junctions ("My Documents", AppData's
    "Application Data") that would otherwise be counted twice or loop.

    Permission errors are counted, never raised: AppData in particular is
    partially unreadable on Windows, and one denied subfolder must not lose
    the whole scan. The returned count is what the UI surfaces as "some items
    were skipped", so a partial total is visibly partial.

    `should_abort()`, when given, is polled per directory so a long scan can
    be stopped; an aborted walk returns whatever it had summed so far.
    """
    total = 0
    skipped = 0
    stack = [path]
    while stack:
        if should_abort is not None and should_abort():
            break
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if _is_link_like(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        skipped += 1
        except OSError:
            skipped += 1
    return total, skipped


def top_level_breakdown(root: str,
                        should_abort: Optional[Callable[[], bool]] = None,
                        on_folder: Optional[Callable[[str], None]] = None
                        ) -> List[Tuple[str, int, int]]:
    """Size every immediate subdirectory of `root`, largest first.

    Returns a list of `(name, size_bytes, skipped_count)` sorted descending by
    size. Loose files directly in `root` are not included -- this is a folder
    breakdown, and a per-file listing is a different screen.

    `on_folder(name)` is called before each folder is walked, so a caller can
    show which one is being measured. `should_abort()` is polled inside the
    walk, so an in-flight scan can be cancelled promptly.
    """
    names = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    if _is_link_like(entry) or not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                names.append(entry.name)
    except OSError:
        # An unreadable root has no breakdown to report; the caller renders
        # this the same as "no folders here".
        return []

    results = []
    for name in sorted(names):
        if should_abort is not None and should_abort():
            break
        if on_folder is not None:
            on_folder(name)
        size, skipped = folder_size(os.path.join(root, name), should_abort)
        results.append((name, size, skipped))

    results.sort(key=lambda row: row[1], reverse=True)
    return results
