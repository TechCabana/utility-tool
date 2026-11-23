# utils/precheck.py
"""Pre-check (card 104): list every file a job's target cannot hold, before
the job runs, with a plain-language fix for each.

A backup that fails at night because of something the user could have fixed
(a path too long for the backup drive, a file over FAT32's 4 GB limit, a name
the drive cannot store) should be found before the first night, not the next
morning. `precheck(job)` walks the source once -- scandir and stat, plus a
zero-byte open of each file -- and compares every entry with the limits of the
target's filesystem. It hashes nothing and NEVER writes: not to the target
(which need not exist yet), not to the source.

The limits come from the target volume (`volume_info`, GetVolumeInformationW
on Windows, read from the target's nearest existing ancestor like the volume
check in utils/preflight.py). Off Windows the type is "unknown" and no
filesystem limits are applied, so nothing is reported falsely.

Qt-free, like the rest of the engine. One-way dependencies: this module
imports fs_utils, preflight and versions; only runner (and the UI / CLI) import
it. Patch `utils.precheck.volume_info` / `._list_dir` / `._can_open` in tests.
"""
import functools
import os
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from utils.fs_utils import _is_link_like, long_path, plain_error
from utils.preflight import nearest_existing_ancestor
from utils.versions import VERSION_FORMAT

# The largest file FAT can hold: its directory entry stores the size in 32
# bits, so 4 GiB minus one byte. exFAT, NTFS and ReFS have no limit a backup
# source could reach.
FAT_MAX_FILE = 4 * 1024 ** 3 - 1

# Filesystem name (as GetVolumeInformationW reports it, upper-cased) ->
# (largest file in bytes or None, FAT-style naming rules). Every one of these
# is case-insensitive as Windows uses it. Anything else is "unknown": no size
# limit, no naming rules, no case check -- the check must never invent a
# problem it cannot know about.
_FILESYSTEMS = {
    "NTFS": (None, False),
    "REFS": (None, False),
    "EXFAT": (None, True),
    "FAT": (FAT_MAX_FILE, True),
    "FAT12": (FAT_MAX_FILE, True),
    "FAT16": (FAT_MAX_FILE, True),
    "FAT32": (FAT_MAX_FILE, True),
}

# The longest single name every filesystem here allows, in UTF-16 units.
MAX_COMPONENT = 255

# The longest copy path the engine can write on Windows, in UTF-16 units.
#
# Since card 56 every engine OS call goes through `fs_utils.long_path`, so the
# old 260-character MAX_PATH limit no longer applies to the engine on NTFS,
# exFAT or FAT32: the real limit is Windows' extended-length one, 32,767
# units INCLUDING the `\\?\` prefix (`\\?\UNC\` on a share, 6 more than the
# plain `\\` it replaces), and card 56 measured Windows refusing a FOLDER from
# about 32,762. A file's folder is always at least 2 units shorter than the
# file, so a plain copy path of at most 32,750 keeps the file and every folder
# above it inside both limits with a few units to spare.
#
# `precheck(max_path=...)` takes a different value for a target or setup with
# a lower limit (e.g. 260 where extended paths are unavailable); the run
# failure path passes the length the drive actually refused. None means no
# path-length check (off Windows: macOS parity is a later card).
MAX_COPY_PATH = 32_750 if os.name == "nt" else None

# What FAT and exFAT cannot store in a name, beyond the length: these
# characters, control characters, and a trailing dot or space.
FAT_ILLEGAL = frozenset('\\/:*?"<>|') | frozenset(chr(code) for code in range(32))

# Windows device names. With extended-length paths NTFS stores a file called
# CON (measured), so these are reported as a warning, not a failure: most
# programs, Explorer included, cannot open, rename or delete one.
RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)] + [f"LPT{n}" for n in range(1, 10)])

# How many problems a report lists in full; `counts` and `problems_more`
# still count every one, so a tree full of offenders cannot fill memory.
PROBLEMS_MAX = 500

# How many offender paths `last_precheck` and a failure message keep.
SAMPLE_MAX = 5

# A version folder's name is a VERSION_FORMAT stamp plus ".partial" while it
# is being filled, and that is where every copied file is first written.
_PARTIAL_NAME = datetime(2000, 1, 1).strftime(VERSION_FORMAT) + ".partial"

# One plain-language label per kind, for the UI and the CLI.
KIND_LABELS = {
    "path_too_long": "Path too long",
    "file_too_large": "File too large for this drive",
    "bad_name": "Name the drive cannot store",
    "case_collision": "Names differ only by capital letters",
    "unreadable": "Cannot be read right now",
    "source_unavailable": "Source folder not found",
    "target_unavailable": "Backup drive not available",
}

SUGGESTIONS = {
    "path_too_long": ("Shorten a folder or file name, or move the folder nearer "
                      "the drive root."),
    "file_too_large": "Leave this file out, or back up to an NTFS or exFAT drive.",
    "case_collision": ("Rename one of them so the names differ by more than "
                       "capital letters."),
    "unreadable": ("Close the program using it, or check its permissions, "
                   "then back up again."),
    "source_unavailable": "Connect the drive the source folder is on, or edit the job.",
    "target_unavailable": "Connect the backup drive, or edit the job.",
}


@functools.lru_cache(maxsize=None)
def _upcase(char: str) -> str:
    """`char` upper-cased the way Windows compares file names.

    Windows: ntdll's RtlUpcaseUnicodeChar, one UTF-16 unit to one, which is
    what NTFS and FAT use. NOT `str.casefold()`/`upper()`: those map "ß" to
    "ss" and fold "ς" with "σ", names NTFS keeps side by side (measured), so
    "Straße.txt" beside "Strasse.txt" was reported as a collision that would
    fail the backup. A character outside the BMP is two UTF-16 units Windows
    leaves alone. Elsewhere (never reached today: no target filesystem is known
    off Windows) a one-to-one `upper()`. Cached: names reuse few characters.
    """
    if ord(char) > 0xFFFF:
        return char
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.RtlUpcaseUnicodeChar.restype = wintypes.WCHAR
        ntdll.RtlUpcaseUnicodeChar.argtypes = (wintypes.WCHAR,)
        return ntdll.RtlUpcaseUnicodeChar(char)
    upper = char.upper()
    return upper if len(upper) == 1 else char


def _fold(name: str) -> str:
    """The key under which a case-insensitive target files `name`."""
    return "".join(_upcase(char) for char in name)


def _units(text: str) -> int:
    """Length of `text` in UTF-16 code units, the unit Windows' limits use."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def volume_info(path: str) -> Dict:
    """`{"fs": name, "max_component": n}` for the volume holding existing `path`.

    Windows: GetVolumePathNameW finds the volume root (a drive, a mounted
    folder or a share), then GetVolumeInformationW names its filesystem
    ("NTFS", "FAT32", "exFAT"...) and its longest name. Any failure, and any
    other OS, gives `{"fs": "unknown", "max_component": None}`: no limits are
    applied then, so the check reports nothing it cannot know. Never raises.

    ponytail: off Windows this is always "unknown" -- macOS parity is a later
    card (statfs's f_fstypename would be the upgrade).
    """
    unknown = {"fs": "unknown", "max_component": None}
    if os.name != "nt":
        return unknown
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        root = ctypes.create_unicode_buffer(32768)
        # The extended form works here too, and returns a `\\?\` root.
        if not kernel32.GetVolumePathNameW(long_path(path), root, len(root)):
            return unknown
        fs_name = ctypes.create_unicode_buffer(261)
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()
        if not kernel32.GetVolumeInformationW(root.value, None, 0, None,
                                              ctypes.byref(max_component),
                                              ctypes.byref(flags),
                                              fs_name, len(fs_name)):
            return unknown
    except (OSError, AttributeError, ValueError):
        return unknown
    return {"fs": fs_name.value or "unknown", "max_component": max_component.value or None}


def _target_limits(target: str) -> Optional[Dict]:
    """The limits a job's target imposes, or None if its drive is unavailable.

    Read from the target's nearest existing ancestor, so a target folder that
    has not been created yet is checked against the drive it will live on.
    """
    anchor = nearest_existing_ancestor(target) if target else None
    if anchor is None:
        return None
    info = volume_info(anchor)
    fs = str(info.get("fs") or "unknown").upper()
    max_file, fat_names = _FILESYSTEMS.get(fs, (None, False))
    reported = info.get("max_component")
    return {
        "fs": fs if fs in _FILESYSTEMS else "unknown",
        "max_file": max_file,
        "fat_names": fat_names,
        "case_insensitive": fs in _FILESYSTEMS,
        "max_component": reported if isinstance(reported, int) and reported > 0
        else MAX_COMPONENT,
    }


def _list_dir(path: str) -> List[tuple]:
    """`[(name, is_dir, size)]` for plain folder `path`, sorted by name.

    Symlinks and junctions are left out, as the copy leaves them out
    (`_is_link_like`). `size` is None for an entry that cannot be stat'ed
    (it will be skipped by the copy). Raises OSError if `path` cannot be
    listed. Tests patch this to inject sizes and listings.
    """
    listed = []
    with os.scandir(long_path(path)) as entries:
        for entry in entries:
            try:
                if _is_link_like(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    listed.append((entry.name, True, 0))
                else:
                    listed.append((entry.name, False,
                                   entry.stat(follow_symlinks=False).st_size))
            except OSError:
                listed.append((entry.name, False, None))
    return sorted(listed)


def _can_open(path: str) -> Optional[OSError]:
    """None if plain file `path` opens for reading, else the OSError.

    The same open the copy makes (`long_path`, "rb"), reading nothing: a file
    another program holds with an exclusive lock, or one this user may not
    read, fails here exactly as it would fail the copy.
    """
    try:
        with open(long_path(path), "rb"):
            return None
    except OSError as exc:
        return plain_error(exc)


def _name_problem(name: str, limits: Dict) -> Optional[tuple]:
    """`(detail, suggestion, blocking)` if the target cannot store `name`."""
    length = _units(name)
    if length > limits["max_component"]:
        return (f"Its name is {length} characters long; the backup drive allows "
                f"{limits['max_component']}.",
                f"Shorten the name to {limits['max_component']} characters or fewer.",
                True)
    if limits["fat_names"]:
        bad = sorted(set(name) & FAT_ILLEGAL)
        if bad:
            shown = " ".join(c if c.isprintable() else repr(c)[1:-1] for c in bad)
            return (f"Its name contains {shown}, which a {limits['fs']} drive "
                    "cannot store.",
                    f"Rename it without {shown}.", True)
        if name[-1] in ". ":
            ending = "a dot" if name[-1] == "." else "a space"
            return (f"Its name ends in {ending}, which a {limits['fs']} drive "
                    "cannot store.",
                    f"Rename it so it does not end in {ending}.", True)
    if os.name == "nt" and name.split(".")[0].rstrip(" ").upper() in RESERVED_NAMES:
        return (f"Windows reserves the name {name.split('.')[0]} for a device; it "
                "can be backed up, but most programs cannot open, rename or "
                "delete it there.",
                "Rename it, for example by adding a word to the name.", False)
    return None


def precheck(job: Dict,
             progress: Optional[Callable[[int], None]] = None,
             should_abort: Optional[Callable[[], bool]] = None,
             check_open: bool = True,
             max_path: Optional[int] = MAX_COPY_PATH) -> Dict:
    """Walk `job`'s source once and list everything its target cannot hold.

    Returns `{ok, scanned, problems, took, aborted, fs, counts, blocking,
    problems_more}`. `problems` holds up to `PROBLEMS_MAX` entries of
    `{path, rel, kind, detail, suggestion, blocking}`: `path` is the plain
    source path of the offending file or folder, `rel` that path from the
    source root (forward slashes, as in the manifest). Kinds:

    * `path_too_long` -- its copy path, `<target>\\<job id>\\<stamp>.partial
      \\<relative path>`, is longer than `max_path` (see `MAX_COPY_PATH`). A
      folder that is too long is reported once, not again for its contents.
    * `file_too_large` -- larger than the target filesystem's largest file
      (FAT32: 4 GiB - 1 byte).
    * `bad_name` -- a name over the volume's name limit (255), or one a FAT
      or exFAT drive cannot store (`\\ / : * ? " < > |`, control characters, a
      trailing dot or space); a Windows device name (CON, NUL, COM1...) is a
      non-blocking warning, since extended paths can store it.
    * `case_collision` -- two names in one folder that differ only by case,
      on a case-insensitive target.
    * `unreadable` -- a file that cannot be opened (locked, no permission) or
      a folder that cannot be listed: the copy skips it, so it is reported
      as "will be skipped" and is NOT blocking.
    * `source_unavailable` / `target_unavailable` -- the source folder is
      missing, or the target's drive is not there at all.

    `ok` is True only when nothing BLOCKING was found (something that would
    fail the run) and the walk was not aborted; non-blocking problems leave it
    True. `scanned` counts files; `counts` is problems per kind (complete
    even past `PROBLEMS_MAX`); `blocking` the number of blocking ones; `fs`
    the target filesystem ("unknown" when it could not be read); `took`
    seconds.

    Read-only: scandir, stat and a zero-byte open per file; nothing is hashed,
    created or written, on the target or anywhere else. `progress(scanned)` is
    called per file and `should_abort()` checked per entry, like the other
    long operations; an abort returns at once with `aborted: True`, `ok:
    False`. `check_open=False` skips the per-file open (the run-failure path
    uses it: it only wants the path or size offenders).
    """
    start = time.monotonic()
    problems: List[Dict] = []
    counts: Dict[str, int] = {}
    state = {"scanned": 0, "blocking": 0, "aborted": False}
    source = job.get("source", "") or ""
    target = job.get("target", "") or ""

    def add(path: str, rel: str, kind: str, detail: str,
            suggestion: Optional[str] = None, blocking: bool = True) -> None:
        """Count one problem, and list it while there is room."""
        counts[kind] = counts.get(kind, 0) + 1
        state["blocking"] += bool(blocking)
        if len(problems) < PROBLEMS_MAX:
            problems.append({"path": path, "rel": rel, "kind": kind, "detail": detail,
                             "suggestion": suggestion or SUGGESTIONS[kind],
                             "blocking": blocking})

    def report() -> Dict:
        """The finished report."""
        return {"ok": not state["aborted"] and state["blocking"] == 0,
                "scanned": state["scanned"], "problems": problems,
                "took": round(time.monotonic() - start, 3),
                "aborted": state["aborted"], "fs": limits["fs"] if limits else "unknown",
                "counts": counts, "blocking": state["blocking"],
                "problems_more": sum(counts.values()) - len(problems)}

    def aborted() -> bool:
        """True (and remembered) once the caller asks to stop."""
        if should_abort is not None and should_abort():
            state["aborted"] = True
        return state["aborted"]

    limits = _target_limits(target)
    if limits is None:
        add(target, "", "target_unavailable",
            f"The backup drive for {target or '(not set)'} is not connected, so "
            "the backup would fail.")
    if not source or not os.path.isdir(long_path(source)):
        add(source, "", "source_unavailable",
            f"The source folder {source or '(not set)'} cannot be found.")
        return report()
    limits = limits or {"fs": "unknown", "max_file": None, "fat_names": False,
                        "case_insensitive": False, "max_component": MAX_COMPONENT}

    # The copy path the engine would write for a file at relative path `rel`.
    copy_base = os.path.join(os.path.abspath(target), str(job.get("id", "")),
                             _PARTIAL_NAME)
    stack = [(source, "", False)]  # (plain folder, relative path, ancestor too long)
    while stack:
        if aborted():
            return report()
        current, rel_dir, long_above = stack.pop()
        try:
            entries = _list_dir(current)
        except OSError as exc:
            if not rel_dir:  # the source root itself: the run would fail
                add(source, "", "source_unavailable",
                    f"The source folder cannot be read ({plain_error(exc).strerror or exc}).")
                continue
            add(current, rel_dir, "unreadable",
                f"This folder cannot be read right now ({plain_error(exc).strerror or exc}), "
                "so it will be skipped.", blocking=False)
            continue
        seen: Dict[str, str] = {}
        for name, is_dir, size in entries:
            if aborted():
                return report()
            path = os.path.join(current, name)
            rel = f"{rel_dir}/{name}" if rel_dir else name
            folded = _fold(name)
            if limits["case_insensitive"] and folded in seen:
                add(path, rel, "case_collision",
                    f'"{name}" and "{seen[folded]}" are in the same folder and '
                    "differ only by capital letters; the backup drive treats them "
                    "as the same name.")
            seen.setdefault(folded, name)
            named = _name_problem(name, limits)
            if named:
                add(path, rel, "bad_name", named[0], named[1], named[2])
            too_long = False
            if max_path is not None and not long_above:
                length = _units(os.path.join(copy_base, rel.replace("/", os.sep)))
                if length > max_path:
                    too_long = True
                    add(path, rel, "path_too_long",
                        f"Its path on the backup drive would be {length} characters "
                        f"long; the limit is {max_path}.")
            if is_dir:
                stack.append((path, rel, long_above or too_long))
                continue
            state["scanned"] += 1
            if progress is not None:
                progress(state["scanned"])
            if size is None:
                add(path, rel, "unreadable",
                    "Its size cannot be read right now, so it will be skipped.",
                    blocking=False)
                continue
            if limits["max_file"] is not None and size > limits["max_file"]:
                add(path, rel, "file_too_large",
                    f"It is {size:,} bytes; a {limits['fs']} drive holds files of at "
                    f"most {limits['max_file']:,} bytes (4 GB).")
            if check_open:
                error = _can_open(path)
                if error is not None:
                    add(path, rel, "unreadable",
                        f"It cannot be opened for reading right now "
                        f"({error.strerror or error}), so it will be skipped.",
                        blocking=False)
    return report()


def summary(report: Dict, now: Optional[datetime] = None) -> Dict:
    """What a job keeps of its last pre-check (`last_precheck`).

    Time, counts and the first `SAMPLE_MAX` offender paths (plain paths from
    the source root, no OS error text), so the UI can show it without
    re-walking the source.
    """
    return {
        "time": (now or datetime.now()).isoformat(timespec="seconds"),
        "ok": bool(report.get("ok")),
        "aborted": bool(report.get("aborted")),
        "fs": str(report.get("fs", "unknown")),
        "scanned": int(report.get("scanned", 0)),
        "problems": sum((report.get("counts") or {}).values()),
        "blocking": int(report.get("blocking", 0)),
        "counts": dict(report.get("counts") or {}),
        "sample": [p.get("rel") or p.get("path", "")
                   for p in (report.get("problems") or [])[:SAMPLE_MAX]],
    }


def offenders(job: Dict, cause: str, failed_path: str = "",
              should_abort: Optional[Callable[[], bool]] = None) -> Dict:
    """The files behind a run that failed with `cause` "path_too_long" or
    "file_too_large": `{"paths": [rel...], "more": n}`.

    For a path too long, the path the drive actually refused (`failed_path`)
    is the measured limit: every file whose copy path is at least that long
    is listed, whatever `MAX_COPY_PATH` says. No open check is made. Never
    raises: an unexpected error gives an empty list, since this only adds
    detail to a failure message.
    """
    try:
        max_path = MAX_COPY_PATH
        if cause == "path_too_long" and failed_path:
            max_path = _units(os.path.abspath(failed_path)) - 1
        found = precheck(job, should_abort=should_abort, check_open=False,
                         max_path=max_path)
    except Exception:  # ponytail: detail for a message; the run already failed
        return {"paths": [], "more": 0}
    paths = [p["rel"] for p in found["problems"] if p["kind"] == cause]
    total = found["counts"].get(cause, 0)
    return {"paths": paths[:SAMPLE_MAX], "more": max(0, total - SAMPLE_MAX)}
