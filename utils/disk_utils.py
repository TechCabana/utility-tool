# utils/disk_utils.py
"""Disk inspection helpers for the Disk tab: byte formatting, a
per-top-level-folder size breakdown, the Cleanup quick scan, and the
duplicate-file / duplicate-image scans that share Cleanup's screen.

Qt-free by design (CLAUDE.md §3/§8) -- these take plain paths in and return
plain values out. `tabs/disk_tab.py` runs `top_level_breakdown` and
`cleanup_scan` from QThread workers and renders the results; nothing here
knows that.

Drive totals are not wrapped here: `shutil.disk_usage()` already answers that
question in one stdlib call, so the tab calls it directly. Deletion is not
wrapped here either: `utils/file_utils.delete_file()` already sends a path to
the Recycle Bin via send2trash, and Cleanup reuses it.
"""
import ctypes
import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from utils.image_utils import SUPPORTED_SUFFIXES

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


# ===========================================================================
# Cleanup quick scan
# ===========================================================================
#
# Eight categories, per DESIGN.md's Cleanup comp. Seven of them are measured
# for real off this machine; "Application leftovers" is reported as not yet
# detected rather than faked -- see `cleanup_scan` for why.
#
# Everything here is a plain function over plain paths. The category table at
# the bottom (`cleanup_scan`) is the only place that knows which root feeds
# which category, so adding a category is one entry there plus, at most, one
# new finder.

_DAY = 86400
OLD_INSTALLER_DAYS = 30      # a setup file untouched for a month is spent
OLD_INSTALLER_SUFFIXES = (".exe", ".msi", ".msix", ".appx")
LARGE_FILE_BYTES = 100 * 1024 ** 2
LARGE_FILE_DAYS = 90


@dataclass
class CleanupCategory:
    """One row of the Cleanup quick scan.

    `items` are the individual (path, size_bytes) pairs the category found --
    the drill-down list, and also exactly what gets sent to the Recycle Bin
    when the category is checked. `size` is the reclaimable total, which is
    the sum of `items` for every category except Recycle Bin, where the size
    comes from Windows itself and there are no listable items.

    `removable` is False for a category that can be measured but not cleaned
    by this app (Recycle Bin: its contents are already in the bin, and
    emptying it would be a permanent delete, which this app never does).
    """
    key: str
    name: str
    hint: str
    items: List[Tuple[str, int]] = field(default_factory=list)
    size: int = 0
    skipped: int = 0
    removable: bool = True
    note: str = ""


def find_files(roots: Sequence[str],
               suffixes: Optional[Sequence[str]] = None,
               min_size: int = 0,
               older_than_days: Optional[float] = None,
               max_depth: Optional[int] = None,
               exclude: Optional[Sequence[str]] = None,
               should_abort: Optional[Callable[[], bool]] = None
               ) -> Tuple[List[Tuple[str, int]], int]:
    """Files under `roots` matching every filter given, plus a skipped count.

    One generic finder rather than one function per category: system cache,
    log files, old installers and large-and-old files differ only in root,
    suffix, size and age, so they are four calls to this, not four walks.

    `max_depth` counts directory levels below a root: 0 stays in the root
    itself, 1 also visits its immediate subfolders. Depth is capped for the
    AppData scans because a full AppData walk takes long enough to make the
    Cleanup tab feel broken.

    Age is `st_mtime`, not `st_atime`: Windows disables last-access-time
    updates by default, so atime is frequently just a copy of mtime and
    filtering on it would quietly mean the same thing while reading as if it
    meant something better.

    `exclude` names directories not to descend into. Two Cleanup categories
    would otherwise overlap: %TEMP% lives inside %LOCALAPPDATA%, so every
    *.log in the temp folder would be reported both as system cache and as a
    log file -- counted twice in the totals, and offered twice to the delete
    worker, where the second attempt fails on a file that is already gone.

    Symlinks and junctions are skipped (`_is_link_like`) and unreadable
    entries are counted, never raised -- same contract as `folder_size`.
    """
    items: List[Tuple[str, int]] = []
    skipped = 0
    cutoff = time.time() - older_than_days * _DAY if older_than_days else None
    wanted = tuple(s.lower() for s in suffixes) if suffixes else None
    blocked = {os.path.normcase(os.path.abspath(p)) for p in (exclude or ())}

    for root in roots:
        stack = [(root, 0)]
        while stack:
            if should_abort is not None and should_abort():
                return items, skipped
            current, depth = stack.pop()
            if blocked and os.path.normcase(os.path.abspath(current)) in blocked:
                continue
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if _is_link_like(entry):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if max_depth is None or depth + 1 <= max_depth:
                                    stack.append((entry.path, depth + 1))
                                continue
                            if wanted and not entry.name.lower().endswith(wanted):
                                continue
                            stat = entry.stat(follow_symlinks=False)
                            if stat.st_size < min_size:
                                continue
                            if cutoff is not None and stat.st_mtime > cutoff:
                                continue
                            items.append((entry.path, stat.st_size))
                        except OSError:
                            skipped += 1
            except OSError:
                skipped += 1
    return items, skipped


def empty_folders(roots: Sequence[str],
                  should_abort: Optional[Callable[[], bool]] = None
                  ) -> Tuple[List[Tuple[str, int]], int]:
    """Directories under `roots` that hold nothing at all, as (path, 0) pairs.

    "Empty" here means the directory has no entries whatsoever -- not "no
    files, but some empty subdirectories". The stricter definition is the
    safe one: it can only ever propose a leaf, so confirming the whole list
    can never remove a directory the user is still looking at in the parent
    row above it. A `roots` entry is never itself proposed, however empty it
    is; offering to bin Documents is not a cleanup.

    ponytail: a tree of nothing but empty folders is reported one leaf per
    pass, so it takes N rescans to clear N levels. Roll the parents up only
    if that turns out to bite -- it needs a post-order walk, which is a
    bigger change than the problem so far justifies.
    """
    found: List[Tuple[str, int]] = []
    skipped = 0

    for root in roots:
        stack = [root]
        while stack:
            if should_abort is not None and should_abort():
                return found, skipped
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    children = list(entries)
            except OSError:
                skipped += 1
                continue
            if not children:
                if os.path.normcase(os.path.abspath(current)) != \
                        os.path.normcase(os.path.abspath(root)):
                    found.append((current, 0))
                continue
            for entry in children:
                try:
                    if _is_link_like(entry):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                except OSError:
                    skipped += 1
    return found, skipped


class _SHQUERYRBINFO(ctypes.Structure):
    """Windows' SHQUERYRBINFO: DWORD cbSize; __int64 i64Size, i64NumItems."""
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_uint64),
        ("i64NumItems", ctypes.c_uint64),
    ]


def recycle_bin_stats() -> Tuple[int, int]:
    """(total_bytes, item_count) across every drive's Recycle Bin, or (0, 0).

    Uses shell32's SHQueryRecycleBinW through ctypes: it is the API Explorer
    itself answers this with, it needs no elevation, and it costs no new
    dependency. The alternative -- walking $Recycle.Bin -- is access-denied
    for other users' bins and would need admin to be complete.

    A NULL root path means "sum every drive". Any failure (non-Windows, a
    shell call that refuses) reads as (0, 0): the Cleanup row then shows an
    empty bin, which is the honest rendering of "nothing measured".
    """
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        result = ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info))
    except (AttributeError, OSError):
        return 0, 0  # not Windows, or shell32 refused the call
    if result != 0:  # anything but S_OK
        return 0, 0
    return int(info.i64Size), int(info.i64NumItems)


def browser_cache_dirs(local_appdata: str) -> List[str]:
    """Cache directories of the Chromium browsers and Firefox that exist here.

    Only paths that are actually present are returned, so a machine without
    Firefox contributes nothing rather than a zero-byte row. Firefox keys its
    cache by profile directory, so its profiles are enumerated rather than
    hardcoded.
    """
    dirs = []
    chromium = [
        ("Google", "Chrome"),
        ("Microsoft", "Edge"),
        ("BraveSoftware", "Brave-Browser"),
        ("Chromium", None),
    ]
    for vendor, product in chromium:
        base = os.path.join(local_appdata, vendor)
        if product:
            base = os.path.join(base, product)
        default = os.path.join(base, "User Data", "Default")
        for name in ("Cache", "Code Cache", "GPUCache"):
            path = os.path.join(default, name)
            if os.path.isdir(path):
                dirs.append(path)

    firefox = os.path.join(local_appdata, "Mozilla", "Firefox", "Profiles")
    try:
        with os.scandir(firefox) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    path = os.path.join(entry.path, "cache2")
                    if os.path.isdir(path):
                        dirs.append(path)
    except OSError:
        pass  # Firefox not installed, or its profile folder is unreadable
    return dirs


def cleanup_roots(home: Optional[str] = None) -> Dict[str, str]:
    """The directories the quick scan looks in, by role, existing ones only.

    Keys: local, roaming, temp, downloads, documents, desktop. A caller that
    asks for a missing key gets nothing scanned for that category rather than
    an error -- an OS without %APPDATA% simply has no roaming logs.
    """
    home = home or os.path.expanduser("~")
    candidates = {
        "local": os.environ.get("LOCALAPPDATA", ""),
        "roaming": os.environ.get("APPDATA", ""),
        "temp": os.environ.get("TEMP", "") or os.environ.get("TMP", ""),
        "downloads": os.path.join(home, "Downloads"),
        "documents": os.path.join(home, "Documents"),
        "desktop": os.path.join(home, "Desktop"),
    }
    return {k: v for k, v in candidates.items() if v and os.path.isdir(v)}


def _present(roots: Dict[str, str], *keys: str) -> List[str]:
    """The roots for `keys` that exist on this machine, de-duplicated."""
    seen, out = set(), []
    for key in keys:
        path = roots.get(key)
        if not path:
            continue
        norm = os.path.normcase(os.path.abspath(path))
        if norm not in seen:
            seen.add(norm)
            out.append(path)
    return out


def cleanup_scan(home: Optional[str] = None,
                 should_abort: Optional[Callable[[], bool]] = None,
                 on_category: Optional[Callable[[str], None]] = None
                 ) -> List[CleanupCategory]:
    """Measure all eight Cleanup categories, in DESIGN.md's comp order.

    `on_category(name)` fires before each category is measured so the tab can
    say which one is running; `should_abort()` is polled inside every walk, so
    a scan cancels promptly and returns the categories finished so far.

    Application leftovers is the one category with no detection behind it. A
    generic "orphaned folder with no matching install" test means
    cross-referencing every AppData folder against the uninstall registry and
    the Store package list, and getting that wrong proposes deleting the data
    of an app the user still runs. It reports as not yet detected instead of
    guessing -- an empty row that says so is worth more than a plausible one.
    """
    roots = cleanup_roots(home)
    results: List[CleanupCategory] = []

    def add(cat: CleanupCategory) -> None:
        cat.size = cat.size or sum(size for _, size in cat.items)
        results.append(cat)

    def aborted() -> bool:
        return should_abort is not None and should_abort()

    def announce(name: str) -> None:
        if on_category is not None:
            on_category(name)

    # --- 1. System cache -------------------------------------------------
    announce("System cache")
    # %TEMP% is normally %LOCALAPPDATA%\Temp. Scan that one directory, never
    # LOCALAPPDATA itself -- the rest of it is application data, not cache.
    if roots.get("temp"):
        temp_roots = [roots["temp"]]
    elif roots.get("local"):
        temp_roots = [os.path.join(roots["local"], "Temp")]
    else:
        temp_roots = []
    items, skipped = find_files(
        [p for p in temp_roots if os.path.isdir(p)], should_abort=should_abort)
    add(CleanupCategory(
        key="system_cache",
        name="System cache",
        hint="Temporary files under " + (temp_roots[0] if temp_roots else "%TEMP%"),
        items=items, skipped=skipped,
        note="Files an app has open right now cannot be moved; those are "
             "reported as errors and left alone.",
    ))
    if aborted():
        return results

    # --- 2. Log files ----------------------------------------------------
    announce("Log files")
    items, skipped = find_files(
        _present(roots, "local", "roaming"),
        suffixes=(".log",),
        max_depth=4,   # a full AppData walk is minutes; four levels is seconds
        exclude=temp_roots,  # those files are already System cache, above
        should_abort=should_abort,
    )
    add(CleanupCategory(
        key="logs",
        name="Log files",
        hint="*.log in AppData, four levels deep, outside the temp folder",
        items=items, skipped=skipped,
    ))
    if aborted():
        return results

    # --- 3. Application leftovers ---------------------------------------
    announce("Application leftovers")
    add(CleanupCategory(
        key="leftovers",
        name="Application leftovers",
        hint="Not yet detected",
        removable=False,
        note="Telling an orphaned AppData folder from a running app's data "
             "needs an uninstall-registry cross-reference this app does not "
             "do yet. Nothing is guessed here.",
    ))
    if aborted():
        return results

    # --- 4. Browser cache ------------------------------------------------
    announce("Browser cache")
    cache_dirs = browser_cache_dirs(roots["local"]) if roots.get("local") else []
    items, skipped = find_files(cache_dirs, should_abort=should_abort)
    add(CleanupCategory(
        key="browser_cache",
        name="Browser cache",
        hint=(f"{len(cache_dirs)} cache folder(s) across the browsers installed here"
              if cache_dirs else "No Chromium or Firefox cache folders found"),
        items=items, skipped=skipped,
        note="Close the browser first: a running browser holds its cache open "
             "and those files will report as errors.",
    ))
    if aborted():
        return results

    # --- 5. Old installers -----------------------------------------------
    announce("Old installers")
    items, skipped = find_files(
        _present(roots, "downloads"),
        suffixes=OLD_INSTALLER_SUFFIXES,
        older_than_days=OLD_INSTALLER_DAYS,
        max_depth=1,
        should_abort=should_abort,
    )
    add(CleanupCategory(
        key="installers",
        name="Old installers",
        hint=f"Setup files in Downloads not touched for "
             f"{OLD_INSTALLER_DAYS} days",
        items=items, skipped=skipped,
    ))
    if aborted():
        return results

    # --- 6. Recycle Bin ---------------------------------------------------
    announce("Recycle Bin")
    bin_size, bin_count = recycle_bin_stats()
    add(CleanupCategory(
        key="recycle_bin",
        name="Recycle Bin",
        hint=f"{bin_count} item(s) already in the bin",
        size=bin_size,
        removable=False,
        note="Already in the Recycle Bin. Emptying it is a permanent delete, "
             "which this app never does -- empty it from Explorer to reclaim "
             "the space.",
    ))
    if aborted():
        return results

    # --- 7. Large & old files ---------------------------------------------
    announce("Large & old files")
    items, skipped = find_files(
        _present(roots, "downloads", "documents", "desktop"),
        min_size=LARGE_FILE_BYTES,
        older_than_days=LARGE_FILE_DAYS,
        should_abort=should_abort,
    )
    items.sort(key=lambda row: row[1], reverse=True)
    add(CleanupCategory(
        key="large_old",
        name="Large & old files",
        hint=f"Over {human_size(LARGE_FILE_BYTES)} and untouched for "
             f"{LARGE_FILE_DAYS} days, in Downloads, Documents and Desktop",
        items=items, skipped=skipped,
    ))
    if aborted():
        return results

    # --- 8. Empty folders --------------------------------------------------
    announce("Empty folders")
    items, skipped = empty_folders(
        _present(roots, "downloads", "documents", "desktop"),
        should_abort=should_abort,
    )
    add(CleanupCategory(
        key="empty_folders",
        name="Empty folders",
        hint="Folders holding nothing at all, in Downloads, Documents and Desktop",
        items=items, skipped=skipped,
        note="These reclaim no space; they are listed because they are clutter.",
    ))
    return results


# ===========================================================================
# Duplicates
# ===========================================================================
#
# Two modes, per PRODUCT.md's "files: exact match; images: visual-similarity
# match with a confidence badge". Both return the same `DuplicateGroup` shape
# so the Disk tab renders them with one widget:
#
#   Files  -- identical bytes, decided by SHA-256. One file per group is kept
#             (the oldest), the rest are offered for the Recycle Bin.
#   Images -- visually similar, decided by a difference hash. The highest
#             resolution file is kept, and the group carries a similarity %.
#
# Everything here is plain functions over plain paths (CLAUDE.md §3/§8);
# `tabs/disk_tab.py` runs them from a QThread and renders what comes back.

HASH_CHUNK_BYTES = 1024 * 1024   # read big files a MB at a time, never whole
IMAGE_HASH_SIDE = 8              # 8x8 comparisons -> 64 bits
IMAGE_HASH_BITS = IMAGE_HASH_SIDE * IMAGE_HASH_SIDE
# 5 differing bits out of 64 is ~92% similar. Tight enough that two different
# photographs of the same scene stay apart, loose enough that a resave, a
# resize or a recompression of one picture still lands in its own group.
IMAGE_HASH_MAX_DISTANCE = 5


@dataclass
class DuplicateGroup:
    """One set of duplicate files: the one being kept, and the copies.

    `keep` and each entry of `duplicates` are the same `(path, size_bytes)`
    pair the Cleanup categories use, so the Recycle Bin worker takes either
    without a second code path.

    `similarity` is the percentage the *least* similar copy matches `keep` --
    always 100 in Files mode, where the match is byte-exact. Which file is
    kept is not user-configurable yet; that is a deliberate follow-up, not an
    oversight (see `_exact_group` / `_image_group` for the rules applied).
    """
    keep: Tuple[str, int]
    duplicates: List[Tuple[str, int]] = field(default_factory=list)
    similarity: int = 100

    @property
    def size(self) -> int:
        """Bytes reclaimed if every copy in this group is removed."""
        return sum(size for _, size in self.duplicates)


def file_sha256(path: str, chunk_bytes: int = HASH_CHUNK_BYTES) -> str:
    """The SHA-256 of a file's contents, read in chunks.

    Chunked because "large & old files" in this app means 100 MB and up, and
    a duplicate scan that read a 4 GB video into memory to hash it would take
    the app down instead of finding the duplicate.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_bytes), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mtime(path: str) -> float:
    """Modification time, or 0.0 for a file that cannot be stat'd.

    A file that vanished between the walk and the sort must not abort the
    scan; treating it as maximally old only affects which copy is proposed
    for keeping, and the user confirms that anyway.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _exact_group(paths: Sequence[str], size: int) -> DuplicateGroup:
    """Build a group from byte-identical paths, keeping the oldest.

    Oldest by `st_mtime`: the copies are identical, so the only thing telling
    them apart is which one has been sitting there since the original was
    made. Ties break on the normalised path so the same folder always yields
    the same answer -- a scan that proposed a different keeper each run would
    be impossible to trust.
    """
    ordered = sorted(paths, key=lambda p: (_mtime(p), os.path.normcase(p)))
    return DuplicateGroup(
        keep=(ordered[0], size),
        duplicates=[(p, size) for p in ordered[1:]],
    )


def find_duplicate_files(roots: Sequence[str],
                         should_abort: Optional[Callable[[], bool]] = None,
                         on_progress: Optional[Callable[[int, int], None]] = None
                         ) -> Tuple[List[DuplicateGroup], int]:
    """Exact-duplicate groups under `roots`, largest reclaimable first.

    Two passes, because hashing everything would be the slow way round: files
    are grouped by size first (free -- the walk already stat'd them), and only
    sizes shared by more than one file are hashed. Files of a unique size
    cannot possibly be byte-identical to anything, so they are never read.

    Zero-byte files are skipped entirely. Every empty file is byte-identical
    to every other one, so including them would produce a single enormous
    "duplicate" group that reclaims nothing.

    `on_progress(done, total)` counts hashed files, not walked ones -- that is
    the part that takes the time. `should_abort()` is polled between files and
    returns the groups completed so far, same contract as `cleanup_scan`.
    """
    files, skipped = find_files(roots, min_size=1, should_abort=should_abort)

    by_size: Dict[int, List[str]] = {}
    for path, size in files:
        by_size.setdefault(size, []).append(path)
    candidates = [(size, paths) for size, paths in by_size.items() if len(paths) > 1]

    total = sum(len(paths) for _, paths in candidates)
    done = 0
    groups: List[DuplicateGroup] = []
    for size, paths in candidates:
        by_hash: Dict[str, List[str]] = {}
        for path in paths:
            if should_abort is not None and should_abort():
                return groups, skipped
            done += 1
            if on_progress is not None:
                on_progress(done, total)
            try:
                by_hash.setdefault(file_sha256(path), []).append(path)
            except OSError:
                # Locked or vanished between the walk and the read. Counted,
                # never raised -- one unreadable file must not lose the scan.
                skipped += 1
        for same in by_hash.values():
            if len(same) > 1:
                groups.append(_exact_group(same, size))

    groups.sort(key=lambda group: group.size, reverse=True)
    return groups, skipped


def image_dhash(path: str, side: int = IMAGE_HASH_SIDE
                ) -> Optional[Tuple[int, int, int]]:
    """`(hash_bits, width, height)` for an image, or None if it can't be
    fingerprinted (unreadable, or genuinely flat -- see below).

    Difference hash: reduce to `(side + 1) x side` greyscale pixels and set one
    bit per pixel for "brighter than the pixel to its right". The result
    describes the picture's gradient structure, so a resave, a resize or a
    recompression of the same photograph produces nearly the same bits while
    its actual bytes are completely different.

    dHash rather than the average hash (aHash) the brief also allowed, at the
    same code size: aHash compares each pixel to the frame's mean, so a
    near-flat image (a photo of an overcast sky, a mostly-white document scan)
    hashes to all, or nearly all, zeros regardless of its actual content --
    dHash's neighbour comparison is far less prone to that. It is not immune
    to it, though: a *perfectly* flat image (every pixel the same value, no
    gradient at all -- solid-colour icon art, a blank scan, a corrupt export)
    has no "brighter than" edges either way, so it still hashes to all zeros
    under dHash too. Verified directly: a solid white PNG and a solid black
    PNG produce the identical zero hash here, exactly the collision aHash was
    rejected for. That case is guarded below rather than left to the grouping
    step, since a false "100% match" between two unrelated flat images is
    exactly the kind of thing this scan must not propose for deletion.

    Pillow only, deliberately: no `imagehash`, no numpy, no OpenCV. The whole
    technique is the twelve lines below.
    """
    try:
        with Image.open(path) as image:
            width, height = image.size
            small = image.convert("L").resize(
                (side + 1, side), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return None  # not an image, truncated, or an unsupported variant

    low, high = small.getextrema()
    if low == high:
        # Perfectly flat: no gradient for dHash to describe, so any hash it
        # produced would collide with every other flat image regardless of
        # colour. Treated the same as an unreadable image -- the caller
        # counts it as skipped rather than folding it into a group it cannot
        # meaningfully belong to.
        return None

    pixels = list(small.getdata())
    bits = 0
    index = 0
    for row in range(side):
        base = row * (side + 1)
        for column in range(side):
            if pixels[base + column] > pixels[base + column + 1]:
                bits |= 1 << index
            index += 1
    return bits, width, height


def hamming_distance(left: int, right: int) -> int:
    """How many bits differ between two hashes."""
    return bin(left ^ right).count("1")


def similarity_percent(distance: int, bits: int = IMAGE_HASH_BITS) -> int:
    """A Hamming distance rendered as the similarity badge's percentage."""
    return int(round(100 * (1 - distance / bits)))


def _image_group(entries: Sequence[Tuple[str, int, int, int]]) -> DuplicateGroup:
    """Build a group from similar images, keeping the highest resolution one.

    `entries` are `(path, size_bytes, hash_bits, pixel_count)`.

    Resolution, not a sharpness measure. Between two copies of one picture the
    larger one is the one that has not been downscaled, and pixel count comes
    free from the header Pillow already read. A real sharpness score (the
    variance of a Laplacian, say) means a per-pixel convolution over full-size
    images in pure Python, or OpenCV/numpy as a new dependency -- a lot of
    cost to re-rank copies that resolution has usually already ordered
    correctly. Ties break on file size (less recompression), then on age, then
    on path, so the proposed keeper is stable across runs.

    The similarity carried by the group is the *worst* match against the kept
    file, so the badge never overstates how alike the set is.
    """
    ordered = sorted(entries,
                     key=lambda e: (-e[3], -e[1], _mtime(e[0]), os.path.normcase(e[0])))
    keep = ordered[0]
    copies = ordered[1:]
    worst = min(similarity_percent(hamming_distance(keep[2], other[2]))
                for other in copies)
    return DuplicateGroup(
        keep=(keep[0], keep[1]),
        duplicates=[(other[0], other[1]) for other in copies],
        similarity=worst,
    )


def find_duplicate_images(roots: Sequence[str],
                          max_distance: int = IMAGE_HASH_MAX_DISTANCE,
                          should_abort: Optional[Callable[[], bool]] = None,
                          on_progress: Optional[Callable[[int, int], None]] = None
                          ) -> Tuple[List[DuplicateGroup], int]:
    """Visually-similar image groups under `roots`, largest reclaimable first.

    Only files with an extension in `image_utils.SUPPORTED_SUFFIXES` are
    fingerprinted -- the same set Image Tools accepts, so the two screens
    agree on what an image is.

    Unlike the exact scan there is no size prefilter to lean on: two versions
    of one picture are different sizes, which is the point, so every image has
    to be opened. That is what `on_progress` is for.

    ponytail: grouping is an O(n^2) pairwise compare of the fingerprints, and
    greedy -- the first image of a group seeds it and claims every image
    within `max_distance`. Fine for the thousands of images a Downloads /
    Documents / Desktop scan turns up (the hashes are plain ints and the
    compare is a popcount). If it ever has to handle a whole photo library,
    the upgrade is BK-tree or multi-index hashing over the same hashes, not a
    different fingerprint.
    """
    files, skipped = find_files(roots, suffixes=SUPPORTED_SUFFIXES, min_size=1,
                                should_abort=should_abort)

    fingerprints: List[Tuple[str, int, int, int]] = []
    total = len(files)
    for done, (path, size) in enumerate(files, 1):
        if should_abort is not None and should_abort():
            return [], skipped
        if on_progress is not None:
            on_progress(done, total)
        hashed = image_dhash(path)
        if hashed is None:
            skipped += 1
            continue
        bits, width, height = hashed
        fingerprints.append((path, size, bits, width * height))

    groups: List[DuplicateGroup] = []
    claimed = set()
    for index, seed in enumerate(fingerprints):
        if index in claimed:
            continue
        members = [seed]
        for other_index in range(index + 1, len(fingerprints)):
            if other_index in claimed:
                continue
            other = fingerprints[other_index]
            if hamming_distance(seed[2], other[2]) <= max_distance:
                claimed.add(other_index)
                members.append(other)
        if len(members) > 1:
            claimed.add(index)
            groups.append(_image_group(members))

    groups.sort(key=lambda group: group.size, reverse=True)
    return groups, skipped


def duplicate_roots(home: Optional[str] = None) -> List[str]:
    """The folders both duplicate scans look in: Downloads, Documents, Desktop.

    Deliberately the same three roots as the "Large & old files" category
    rather than a new set. They are the user's own working folders, so a
    duplicate found there is a file they actually put there twice -- whereas
    scanning AppData or %TEMP% would report caches that are meant to hold
    identical copies and are not the user's to reason about.
    """
    return _present(cleanup_roots(home), "downloads", "documents", "desktop")
