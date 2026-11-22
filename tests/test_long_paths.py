# tests/test_long_paths.py
# Card EQKCcWJe: a deep file must back up and restore whatever the machine's
# LongPathsEnabled policy says. A version lives at
# `<target>\<job id>\<stamp>.partial\...`, so a copy's path is ~35 characters
# longer than its source's; with plain paths a 250-character source file was
# dropped as "could not be read" on any PC with the policy off (the default).
#
# The Done-when test switches the policy off FOR THIS PROCESS ONLY, for real:
# Windows decides per process from the PEB's IsLongPathAwareProcess bit
# (BitField, offset 3, bit 7), which `RtlAreLongPathsEnabled` reads. The bit is
# flipped, read back through that call, checked with a positive control (a
# plain path past 260 characters really fails to be created) and restored in a
# `finally`. Nothing outside this process -- no registry value -- is touched.
#
# Test fixtures and checks build their own `\\?\` paths (`_x`) rather than
# calling the helper under test, so they mean the same thing against code
# without it.
import errno
import hashlib
import json
import os
import shutil
import time
from contextlib import contextmanager

import pytest

from utils import backup_utils, jobs_store

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows path limits")

MAX_PATH = 260


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep this file's jobs off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


def _x(path) -> str:
    """`path` in extended-length form, built independently of utils."""
    return "\\\\?\\" + os.path.abspath(str(path))


def _write(path, data: bytes) -> None:
    """Create `path` (any length) and its folders, holding `data`."""
    os.makedirs(_x(os.path.dirname(str(path))), exist_ok=True)
    with open(_x(path), "wb") as handle:
        handle.write(data)


def _sha(path) -> str:
    """SHA-256 of `path` (any length)."""
    with open(_x(path), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _deep_file(root, length: int, leaf: str = "f.bin") -> str:
    """A path under `root` exactly `length` characters long, ending in `leaf`.

    Folders of at most 100 characters, so no single name nears Windows'
    255-character limit.
    """
    path = str(root)
    rest = length - len(path) - 1 - len(leaf)  # characters left for "\folder" parts
    while rest > 0:
        name = min(100, rest - 1)
        if rest - (name + 1) == 1:   # a single leftover character cannot be a folder
            name -= 1
        assert name >= 1, "length too close to the root"
        path = os.path.join(path, "d" * name)
        rest -= name + 1
    path = os.path.join(path, leaf)
    assert len(path) == length, (len(path), length)
    return path


# ---------------------------------------------------------------------------
# Switching the long-path policy off for this process
# ---------------------------------------------------------------------------

def _peb_bitfield():
    """The PEB BitField byte of this process (ctypes c_ubyte), Windows only."""
    import ctypes

    class _BasicInformation(ctypes.Structure):
        _fields_ = [("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                    ("AffinityMask", ctypes.c_void_p), ("BasePriority", ctypes.c_void_p),
                    ("UniqueProcessId", ctypes.c_void_p),
                    ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

    ntdll = ctypes.WinDLL("ntdll")
    info = _BasicInformation()
    status = ntdll.NtQueryInformationProcess(ctypes.c_void_p(-1), 0, ctypes.byref(info),
                                             ctypes.sizeof(info), None)
    assert status == 0 and info.PebBaseAddress, f"NtQueryInformationProcess: {status:#x}"
    return ctypes.c_ubyte.from_address(info.PebBaseAddress + 3)


def _long_paths_enabled() -> bool:
    """What Windows itself says for this process (`RtlAreLongPathsEnabled`)."""
    import ctypes
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.RtlAreLongPathsEnabled.restype = ctypes.c_ubyte
    return bool(ntdll.RtlAreLongPathsEnabled())


_LONG_PATH_AWARE = 0x80  # IsLongPathAwareProcess


@contextmanager
def long_paths_disabled(probe_root):
    """Run the block as if LongPathsEnabled were off, then put it back.

    Asserts the switch took (read back from Windows) and that a plain path
    past MAX_PATH really cannot be created, so a pass inside the block is
    evidence, not an artefact of a policy that was on anyway.
    """
    bits = _peb_bitfield()
    original, was_enabled = bits.value, _long_paths_enabled()
    bits.value = original & ~_LONG_PATH_AWARE
    try:
        assert not _long_paths_enabled(), "the flag flip did not take"
        control = _deep_file(probe_root, MAX_PATH + 20, "control.txt")
        with pytest.raises(OSError):  # positive control: plain long paths DO fail
            os.makedirs(os.path.dirname(control), exist_ok=True)
            open(control, "wb").close()
        yield
    finally:
        bits.value = original
    assert _long_paths_enabled() == was_enabled  # restored for every later test


def _manifest_paths(job, stamp):
    """The relative paths a version's manifest lists, in order."""
    with open(backup_utils.manifest_path(job, stamp), encoding="utf-8") as handle:
        return [entry["path"] for entry in map(json.loads, handle)
                if entry.get("type") == "file"]


# ---------------------------------------------------------------------------
# The Done-when
# ---------------------------------------------------------------------------

@windows_only
def test_a_deep_file_backs_up_and_restores_with_long_paths_off(tmp_path):
    source, target, restore_to = tmp_path / "s", tmp_path / "t", tmp_path / "r"
    target.mkdir()
    restore_to.mkdir()
    # 250 characters at the source, ~285 in the version: the card's case.
    near = _deep_file(source / "near", 250)
    # Already past MAX_PATH at the source.
    beyond = _deep_file(source / "beyond", 300)
    short = str(source / "short.txt")
    for path in (near, beyond, short):
        _write(path, os.urandom(3000))
    rel = {path: os.path.relpath(path, source).replace("\\", "/")
           for path in (near, beyond, short)}

    job = backup_utils.new_job("Deep", str(source), str(target),
                               schedule="manual", retention=3)
    backup_utils.save_job(job)
    with long_paths_disabled(tmp_path / "probe"):
        result = backup_utils.run_job(job)
        stored = backup_utils.record_run(job["id"], result)
        version = backup_utils.version_path(job, result.get("version") or "?")
        copy_of_near = os.path.join(version, *rel[near].split("/"))
        assert len(copy_of_near) > MAX_PATH  # the copy path really is too long
        restored = backup_utils.restore_version(job, None, str(restore_to))

    assert result["ok"], result["message"]
    assert result["files"] == 3 and result["errors"] == [] and result["not_copied"] == 0
    assert "skipped" not in result["message"]
    assert result["verified"] is True
    assert stored["last_result"] == "ok" and stored["last_skipped"] == 0
    assert "?" not in result["message"], result["message"]
    for path in (near, beyond, short):
        assert _sha(os.path.join(version, *rel[path].split("/"))) == _sha(path)
    assert sorted(_manifest_paths(job, result["version"])) == sorted(rel.values())

    assert restored["ok"], restored["message"]
    assert restored["files"] == 3 and restored["errors"] == []
    assert "skipped" not in restored["message"]
    for path in (near, beyond, short):
        copy = os.path.join(restored["path"], *rel[path].split("/"))
        assert len(copy) > len(path)
        assert _sha(copy) == _sha(path)


@windows_only
def test_pruning_and_quick_verify_reach_a_deep_version_with_long_paths_off(tmp_path):
    # Prune (`_rmtree`) and quick verify's stat walk a version too; a deep file
    # that could not be deleted used to leave the version standing forever.
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    deep = _deep_file(source, 250)
    _write(deep, b"x" * 100)
    job = backup_utils.new_job("Deep", str(source), str(target), schedule="manual",
                               retention=1, verify="quick")
    with long_paths_disabled(tmp_path / "probe"):
        first = backup_utils.run_job(job)
        time.sleep(1.1)  # a distinct version stamp
        second = backup_utils.run_job(job)

    assert first["ok"] and first["files"] == 1, first["message"]
    assert second["ok"] and second["files"] == 1, second["message"]
    assert second["pruned"] == [first["version"]]
    assert backup_utils.version_names(job) == [second["version"]]
    assert not os.path.exists(_x(backup_utils.version_path(job, first["version"])))


@windows_only
@pytest.mark.parametrize("jump", [True, False], ids=["one-name-jumps-past", "folders-reach-it"])
def test_a_copy_path_past_the_windows_limit_fails_the_run_not_skips(tmp_path, jump):
    # Windows cannot store a path of 32,767 characters or more. Measured here:
    # it refuses a folder from ~32,762 and a file at 32,767 with a plain
    # ENOENT ("folders-reach-it"), and Python refuses a longer string outright
    # with ValueError, which no `except OSError` catches, so a single long
    # name that jumps straight past the limit crashed the run
    # ("one-name-jumps-past"). Both must fail cleanly as "path too long",
    # like any other target failure, and leave nothing behind.
    source = tmp_path / "s"
    target = tmp_path / ("t" * 200)
    target.mkdir()
    job = backup_utils.new_job("Huge", str(source), str(target), schedule="manual")
    partial = len(backup_utils.job_root(job)) + len("\\20260101-000000.partial")
    if jump:
        # The copy's folder stops at 32,590 (storable); its 200-character
        # file name lands at 32,791.
        leaf = "n" * 200
        deep = _deep_file(source, len(str(source)) + (32590 - partial) + 1 + len(leaf), leaf)
        assert len(deep) - len(str(source)) + partial > 32767
    else:
        deep = _deep_file(source, 32767 - 100)   # storable at the source only
    _write(deep, b"y")
    try:
        result = backup_utils.run_job(job)
        assert result["ok"] is False, result["message"]
        assert result["cause"] == "path_too_long", result["message"]
        assert "A file path is too long for the backup drive" in result["message"]
        assert os.listdir(backup_utils.job_root(job)) == []
    finally:
        shutil.rmtree(_x(source), ignore_errors=True)


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------

@windows_only
@pytest.mark.parametrize("given, expected", [
    ("C:\\Data\\file.txt", "\\\\?\\C:\\Data\\file.txt"),
    ("C:/Data/sub/file.txt", "\\\\?\\C:\\Data\\sub\\file.txt"),
    ("C:\\Data\\sub\\..\\file.txt", "\\\\?\\C:\\Data\\file.txt"),
    ("C:\\Data\\sub\\", "\\\\?\\C:\\Data\\sub"),
    ("C:\\", "\\\\?\\C:\\"),
    ("\\\\server\\share\\dir\\f.txt", "\\\\?\\UNC\\server\\share\\dir\\f.txt"),
    ("//server/share/dir", "\\\\?\\UNC\\server\\share\\dir"),
    ("\\\\?\\C:\\Data\\f.txt", "\\\\?\\C:\\Data\\f.txt"),
    ("//?/C:/Data/f.txt", "\\\\?\\C:\\Data\\f.txt"),
    ("\\\\?\\UNC\\server\\share\\f", "\\\\?\\UNC\\server\\share\\f"),
])
def test_long_path_forms(given, expected):
    from utils.fs_utils import long_path, plain_path
    assert long_path(given) == expected
    assert long_path(expected) == expected            # idempotent
    assert plain_path(expected) == os.path.abspath(plain_path(expected))


@windows_only
def test_long_path_makes_a_relative_path_absolute(tmp_path, monkeypatch):
    from utils.fs_utils import long_path
    monkeypatch.chdir(tmp_path)
    assert long_path("a\\b.txt") == "\\\\?\\" + str(tmp_path / "a" / "b.txt")


@windows_only
def test_plain_path_undoes_long_path():
    from utils.fs_utils import long_path, plain_path
    assert plain_path(long_path("C:\\Data\\f.txt")) == "C:\\Data\\f.txt"
    assert plain_path(long_path("\\\\server\\share\\f")) == "\\\\server\\share\\f"
    assert plain_path("C:\\plain") == "C:\\plain"
    assert plain_path(None) is None


@windows_only
def test_long_path_past_the_windows_limit_is_an_oserror():
    from utils.fs_utils import long_path
    with pytest.raises(OSError) as raised:
        long_path("C:\\" + "\\".join(["n" * 200] * 170))
    assert raised.value.errno == errno.ENAMETOOLONG
    assert long_path("C:\\" + "n" * 32000).startswith("\\\\?\\")  # under it: fine


def test_long_path_is_a_no_op_off_windows(monkeypatch):
    from utils import fs_utils
    monkeypatch.setattr(fs_utils.os, "name", "posix")
    assert fs_utils.long_path("/home/me/../data/") == "/home/me/../data/"
    assert fs_utils.long_path("") == ""


@windows_only
def test_an_error_message_names_the_plain_path():
    from utils.fs_utils import long_path, plain_error
    exc = PermissionError(errno.EACCES, "Access is denied",
                          long_path("C:\\Data\\a.txt"), None, long_path("C:\\Data\\b.txt"))
    text = str(plain_error(exc))
    assert "?" not in text, text
    assert exc.filename == "C:\\Data\\a.txt" and exc.filename2 == "C:\\Data\\b.txt"


@windows_only
def test_a_skipped_deep_file_is_reported_by_its_plain_path(tmp_path, monkeypatch):
    # A skip's message ends up on the Backup screen and in the run log; the
    # OS error raised by an extended-length call must not leak `\\?\` there.
    from utils import copying
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    for name in ("a.txt", "b.txt", "c.txt", "locked.txt"):
        _write(source / name, b"z")
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and str(path).endswith("locked.txt"):
            raise PermissionError(errno.EACCES, "Permission denied", path)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)
    job = backup_utils.new_job("Skip", str(source), str(target), schedule="manual")
    result = backup_utils.run_job(job)
    assert result["ok"] and len(result["errors"]) == 1, result["message"]
    assert result["errors"][0].startswith("locked.txt: ")
    assert "?" not in result["errors"][0], result["errors"][0]
    assert str(source) in result["errors"][0].replace("\\\\", "\\")


def _raw_x(path) -> str:
    """Extended-length form WITHOUT normalisation, so a trailing dot or space survives."""
    return "\\\\?\\" + str(path)


@windows_only
@pytest.mark.parametrize("given, expected", [
    ("C:\\Data\\notes.", "\\\\?\\C:\\Data\\notes."),
    ("C:\\Data\\notes ", "\\\\?\\C:\\Data\\notes "),
    ("C:\\Data\\dir.\\f.txt", "\\\\?\\C:\\Data\\dir.\\f.txt"),
    ("C:\\Data\\sub\\..\\notes.", "\\\\?\\C:\\Data\\notes."),
])
def test_long_path_keeps_a_trailing_dot_or_space(given, expected):
    from utils.fs_utils import long_path
    assert long_path(given) == expected


@windows_only
def test_a_file_named_with_a_trailing_dot_or_space_backs_up_and_restores(tmp_path):
    """abspath strips trailing dots/spaces, so these names used to be opened as other names."""
    source, target, restore_to = tmp_path / "s", tmp_path / "t", tmp_path / "r"
    target.mkdir()
    restore_to.mkdir()
    names = ["notes.", "memo ", "dir.\\inside.txt", "plain.txt"]
    payload = {name: os.urandom(2000) for name in names}
    for name, data in payload.items():
        full = _raw_x(source) + "\\" + name
        os.makedirs(full.rsplit("\\", 1)[0], exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(data)

    job = backup_utils.new_job("Dots", str(source), str(target), schedule="manual", retention=3)
    backup_utils.save_job(job)
    result = backup_utils.run_job(job)
    restored = backup_utils.restore_version(job, None, str(restore_to))

    assert result["ok"], result["message"]
    assert result["files"] == len(names) and result["errors"] == [] and result["not_copied"] == 0
    assert restored["ok"] and restored["files"] == len(names) and restored["errors"] == []
    for name, data in payload.items():
        with open(_raw_x(restored["path"]) + "\\" + name, "rb") as handle:
            assert handle.read() == data, name


@windows_only
def test_relative_to_keeps_trailing_dots_and_ignores_case():
    from utils.fs_utils import relative_to
    assert relative_to("C:\\Src\\dir.\\f.txt", "C:\\Src") == "dir.\\f.txt"
    assert relative_to("c:\\src\\a\\notes.", "C:\\SRC\\") == "a\\notes."
    assert relative_to("C:\\other\\f", "C:\\Src") == "..\\other\\f"   # not under root: relpath
