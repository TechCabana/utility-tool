# tests/helpers.py
# Shared test helpers (card fEC32w6P). Finished versions and their manifests are
# marked read-only, so a test that edits, corrupts or deletes a file INSIDE a
# finished version has to clear the mark first, the way a person would. These
# are written without calling utils, so they mean the same thing whatever the
# code under test does.
import os
import pathlib
import stat
import sys

_EXT = "\\\\?\\"


def app_data_dir(root) -> pathlib.Path:
    """The Safekeep app-data folder a child process will use when it is launched
    with `APPDATA=<root>` and `HOME=<root>` (every headless-subprocess test does).

    Mirrors `utils.fs_utils.app_dir_for` without calling it: `<root>/Safekeep`
    where APPDATA rules (Windows), `<root>/Library/Application Support/Safekeep`
    on macOS and `<root>/.config/Safekeep` elsewhere, both derived from HOME.
    A test that put the jobs file in `<root>/Safekeep` on every OS made the child
    look in a folder it never reads (exit 2 for a job that was there).
    """
    base = pathlib.Path(root)
    if sys.platform == "darwin":
        return base / "Library" / "Application Support" / "Safekeep"
    if os.name == "nt":
        return base / "Safekeep"
    return base / ".config" / "Safekeep"


def _long(path) -> str:
    """`path` in extended-length form on Windows (names with trailing dots and
    paths past 260 characters), unchanged elsewhere."""
    path = os.fspath(path)
    if os.name != "nt" or path.startswith(_EXT):
        return path
    # normpath(join(cwd, ...)), not abspath: abspath strips trailing dots and
    # spaces on Windows, so "notes." would be looked up as "notes".
    return _EXT + os.path.normpath(os.path.join(os.getcwd(), path))


def clear_ro(path) -> None:
    """Make one file writable again (read-only bit off, owner write bit on,
    everything else kept). Silent for a path that is not there."""
    target = _long(path)
    try:
        os.chmod(target, stat.S_IMODE(os.stat(target).st_mode) | stat.S_IWUSR)
    except FileNotFoundError:
        pass


def clear_ro_tree(path) -> None:
    """`clear_ro` for every file under `path` (a folder), or for the file."""
    target = _long(path)
    if os.path.isfile(target):
        clear_ro(path)
        return
    for current, _dirs, files in os.walk(target):
        for name in files:
            clear_ro(os.path.join(current, name))


def is_read_only(path) -> bool:
    """True when `path` has no write permission at all (the read-only
    attribute on Windows, no write bit elsewhere)."""
    return not os.stat(_long(path)).st_mode & 0o222


def file_attributes(path) -> int:
    """Windows file attribute bits of `path` (hidden 0x2, system 0x4, ...)."""
    return os.stat(_long(path)).st_file_attributes


def set_attributes(path, attributes: int) -> None:
    """Set a file's Windows attribute bits to exactly `attributes` (Windows)."""
    import ctypes
    assert ctypes.windll.kernel32.SetFileAttributesW(_long(path), attributes), path
