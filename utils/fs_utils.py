# utils/fs_utils.py
"""The few filesystem helpers the backup engine shares with the UI.

What is left of the old `utils/presets.py` and `utils/disk_utils.py` once the
image, rename and disk-cleanup tools were removed: the OS app-data directory
every stored file lives in, the one symlink/junction predicate the backup walk
relies on, the extended-length path form every engine filesystem call goes
through (`long_path`), and byte formatting for the Restore table.

Qt-free by design (CLAUDE.md §3/§8) -- plain paths in, plain values out.
"""
import datetime
import errno
import os
import sys

APP_NAME = "Safekeep"

# The product's name before the Safekeep rename. Read only by utils/migrate.py
# (and main.py's one-time QSettings copy) to find data written under it.
LEGACY_APP_NAME = "UtilityTool"

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")

# Windows' name-surrogate bit. A reparse tag with this bit set stands in for
# another path (junctions 0xA0000003, symlinks 0xA000000C); one without it is
# a filter that leaves the directory a real place on disk. Written out rather
# than imported so this module stays import-safe off Windows.
_NAME_SURROGATE = 0x20000000


def app_dir() -> str:
    """The OS-specific app-data directory, created if it does not exist yet.

    Every file the app stores (backup jobs, the activity log, the icon cache)
    lives here rather than beside the code, so it survives a reinstall.
    """
    base = app_dir_for(APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base


def app_dir_for(name: str) -> str:
    """Where the app-data directory for product `name` is, without creating it.

    Separate from `app_dir()` so the migration can look for the pre-rename
    directory without conjuring an empty one into existence.
    """
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", name)
    if os.name == "nt":
        return os.path.join(os.getenv("APPDATA") or os.path.expanduser("~\\AppData\\Roaming"), name)
    return os.path.join(os.path.expanduser("~/.config"), name)


# Windows' extended-length path limit, in characters, prefix included. Python
# itself refuses a longer path with a ValueError, which is not an OSError and
# would escape every per-file handler in the engine.
_MAX_EXTENDED = 32767
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def long_path(path: str) -> str:
    """`path` in Windows' extended-length form, for the OS call itself only.

    A version lives at `<target>\\<job id>\\<stamp>.partial\\...`, so every
    copied file's path is ~35 characters longer than its source's. Without
    the `\\\\?\\` prefix, whether a path past 260 characters (MAX_PATH) can be
    opened depends on the machine's LongPathsEnabled policy, which is off by
    default -- so a deep file was silently impossible to back up on most PCs.

    The prefix also switches OFF Win32's normalisation, so the path is made
    absolute and normalised first (`..`, forward slashes, a trailing
    separator); a UNC path `\\\\server\\share` becomes `\\\\?\\UNC\\server\\share`.
    An already-prefixed path is returned unchanged. A no-op off Windows.

    Use the result only as the argument of the filesystem call; anything
    shown, stored or logged keeps the plain path (`plain_path` undoes this).
    Raises OSError(ENAMETOOLONG) past Windows' 32,767-character limit, so a
    caller's OSError handling classifies it rather than crashing.
    """
    if os.name != "nt" or not path:
        return path
    path = os.fspath(path)
    slashed = path.replace("/", "\\")
    if slashed.startswith((_EXTENDED, "\\\\.\\")):
        # Already extended (or a device path): left as is, bar the slashes,
        # which this form would read as part of a name.
        extended = slashed
    else:
        # normpath(join(cwd, path)), not abspath: on Windows abspath calls
        # GetFullPathName, which silently strips trailing dots and spaces, so
        # a file named "notes." would be opened as "notes" and never found.
        full = os.path.normpath(os.path.join(os.getcwd(), path))
        extended = (_EXTENDED_UNC + full[2:] if full.startswith("\\\\")
                    else _EXTENDED + full)
    if len(extended) > _MAX_EXTENDED:
        raise OSError(errno.ENAMETOOLONG,
                      f"Path is longer than Windows' limit of {_MAX_EXTENDED} characters",
                      path)
    return extended


def relative_to(path: str, root: str) -> str:
    """`path` relative to `root`, by string prefix, never touching the filesystem.

    `os.path.relpath` calls abspath, which on Windows strips trailing dots and
    spaces from every component, so a file named "notes." came out as "notes"
    in the manifest and the run log. The engine builds every path it measures
    as `os.path.join(root, ...)`, so a prefix cut is exact; a path that is not
    under `root` falls back to `relpath`.

    A drive or share root keeps its separator when normalised ("E:\\",
    "\\\\srv\\share\\", "/"), so the prefix gets one only if it lacks it.
    """
    norm_path, norm_root = os.path.normpath(path), os.path.normpath(root)
    prefix = norm_root if norm_root.endswith(os.sep) else norm_root + os.sep
    if os.path.normcase(norm_path).startswith(os.path.normcase(prefix)):
        return norm_path[len(prefix):]
    return os.path.relpath(path, root)


def plain_path(path):
    """Undo `long_path`: the ordinary form of an extended-length path.

    Anything else (a plain path, None, bytes) is returned unchanged.
    """
    if not isinstance(path, str):
        return path
    if path.startswith(_EXTENDED_UNC):
        return "\\\\" + path[len(_EXTENDED_UNC):]
    if path.startswith(_EXTENDED):
        return path[len(_EXTENDED):]
    return path


def plain_error(exc: OSError) -> OSError:
    """`exc` with any extended-length file names put back in plain form.

    An OSError names the path it was called with, so one raised by a call
    made through `long_path` would show `\\\\?\\C:\\...` in a message the user
    reads. Edits `exc` in place and returns it, for use inside an f-string.
    """
    exc.filename = plain_path(exc.filename)
    exc.filename2 = plain_path(exc.filename2)
    return exc


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


# Folder links Windows itself puts in a user profile for programs written
# before Vista ("My Documents" -> Documents, "Application Data" -> AppData\Roaming,
# ...). They point back into the same profile, so they hold nothing a backup of
# that profile is missing, and they deny listing by design. Lowercase: Windows
# names are case-insensitive.
LEGACY_PROFILE_LINKS = frozenset({
    "my documents", "application data", "local settings", "cookies", "nethood",
    "printhood", "recent", "sendto", "start menu", "templates", "my music",
    "my pictures", "my videos", "history", "temporary internet files",
})


def profile_root(path: str):
    """The user-profile folder `path` is inside (or is): the nearest ancestor
    whose parent is called `Users`, as `C:\\Users\\alice`; None if there is none.

    Lexical only (never touches the filesystem), so it works on a path that is
    not there, and tests can stand a fake profile up in a temp folder.
    """
    current = os.path.normpath(path)
    while True:
        parent, name = os.path.split(current)
        if not name or parent == current:
            return None
        if os.path.basename(parent).lower() == "users":
            return current
        current = parent


def is_legacy_profile_link(entry: os.DirEntry, path: str) -> bool:
    """True for one of Windows' own compatibility junctions in a user profile,
    which the "shortcuts not followed" report must not ask the user to act on.

    ALL of: Windows; a junction (not a symlink) to a folder; named in
    `LEGACY_PROFILE_LINKS`; inside a user profile (`profile_root`); and its
    target resolves inside that same profile. A junction with one of these
    names that points anywhere else (another drive, another user) is somebody's
    real shortcut and is still reported.

    Not decided by listing the junction: these are access-denied by design, and
    listing would FOLLOW the link, which the copy deliberately never does. The
    target is read with `os.path.realpath` (reads the link, lists nothing). If
    that cannot resolve it returns the link's own path, which is inside the
    profile, so the name and place decide -- the legacy set is the evidence.
    `path` is the entry's plain path.
    """
    if os.name != "nt" or entry.name.lower() not in LEGACY_PROFILE_LINKS:
        return False
    try:
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
            return False
    except OSError:
        return False
    profile = profile_root(os.path.dirname(path))
    if profile is None:
        return False
    inside = os.path.normcase(os.path.realpath(profile)) + os.sep
    return (os.path.normcase(os.path.realpath(path)) + os.sep).startswith(inside)


def human_size(num_bytes: int) -> str:
    """Format a byte count as a short human-readable string, e.g. "4.2 GB".

    Binary units (1 KB = 1024 B). Negative input is clamped to zero -- a size
    is never negative, and a stat quirk should not render as "-1.0 KB".
    """
    size = float(max(0, num_bytes))
    for unit in _UNITS[:-1]:
        if size < 1024:
            # Whole bytes are exact; there is no fractional byte to show.
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} {_UNITS[-1]}"


def short_when(stamp: str) -> str:
    """An ISO timestamp as "09 Sep 08:49", the form the job table shows.

    The year is dropped for a time in the current year (it is almost never what
    is being read, and the long form set the column's minimum width) and kept,
    without the time, for an earlier year, where the year IS the point. "-" for
    an empty value; an unparseable one is shown as stored, trimmed, never raised.
    """
    if not stamp:
        return "-"
    try:
        moment = datetime.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return str(stamp).replace("T", " ")[:16]
    if moment.year != datetime.datetime.now().year:
        return moment.strftime("%d %b %Y")
    return moment.strftime("%d %b %H:%M")
