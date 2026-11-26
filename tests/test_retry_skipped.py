# tests/test_retry_skipped.py
# Card b5CfTkL8: a source file that cannot be opened or read is tried ONE more
# time after the rest of the tree has been copied, before the verify pass.
# Brief locks (a document being saved, an antivirus scan, a sync client) are
# recovered; a file still locked stays skipped and is reported by name. Only a
# SOURCE read failure is ever retried -- a target write failure, a vanished
# source and a cancellation end the run exactly as on the first pass.
#
# Faults are injected at the source open (`copying.open`, as
# tests/test_skipped_status.py does) and, on Windows, with a REAL exclusive
# lock (CreateFileW with share mode 0) released at a controlled point: the
# `copying._retry_pause` hook, which runs once between the walk and the retry
# pass. Nothing here sleeps or relies on timing.
import ctypes
import datetime
import errno
import hashlib
import json
import os
import shutil

import pytest

from utils import backup_utils, copying, jobs_store, runlog

windows_only = pytest.mark.skipif(os.name != "nt", reason="needs a Windows share-mode lock")

TOP = [f"a{i:02d}.txt" for i in range(10)]
SUB = [f"b{i}.txt" for i in range(4)]
TOTAL = len(TOP) + len(SUB)


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


def make_source(root, top=TOP, sub=SUB):
    """A source folder: `top` at the root, `sub` inside `sub/`, distinct bytes."""
    (root / "sub").mkdir(parents=True)
    for name in top:
        (root / name).write_bytes(f"contents of {name}".encode() * 50)
    for name in sub:
        (root / "sub" / name).write_bytes(f"deep contents of {name}".encode() * 50)
    return root


@pytest.fixture
def job(tmp_path):
    """A saved job over 14 files, retention 3, target empty."""
    source = make_source(tmp_path / "src")
    target = tmp_path / "dst"
    target.mkdir()
    made = backup_utils.new_job("Retry", str(source), str(target),
                                schedule="manual", retention=3)
    backup_utils.save_job(made)
    return made


class FlakyOpen:
    """Fail source opens (`copying.open`, "rb") by file name.

    `names` maps a name to how many of its opens fail: 1 recovers on the
    retry, 99 never does. Records every open of a watched name, in order.
    """

    def __init__(self, monkeypatch, names, error=None):
        self.opens = {}
        self.order = []
        real_open = open

        def fake_open(path, mode="r", *args, **kwargs):
            name = os.path.basename(str(path))
            if mode == "rb" and name in names:
                count = self.opens[name] = self.opens.get(name, 0) + 1
                self.order.append(name)
                if count <= names[name]:
                    raise (error or PermissionError(errno.EACCES, "Permission denied", str(path)))
            return real_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(copying, "open", fake_open, raising=False)


def sha_of(text: bytes) -> str:
    """Independent SHA-256, computed here and not by the code under test."""
    return hashlib.sha256(text).hexdigest()


def manifest_lines(job, result):
    """The parsed lines of the finished version's manifest."""
    with open(result["manifest"], encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def manifest_files(job, result):
    """`{path: entry}` for the manifest's file lines; asserts no path repeats."""
    entries = [e for e in manifest_lines(job, result) if e["type"] == "file"]
    paths = [e["path"] for e in entries]
    assert len(paths) == len(set(paths)), "a file has two manifest lines"
    return {e["path"]: e for e in entries}


def version_hashes(job, result):
    """`{relative path: sha256}` of every file in the promoted version."""
    root = backup_utils.version_path(job, result["version"])
    found = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                found[os.path.relpath(full, root).replace("\\", "/")] = (
                    hashlib.sha256(handle.read()).hexdigest())
    return found


# ---------------------------------------------------------------------------
# The Done-when, injected failure
# ---------------------------------------------------------------------------

def test_a_file_unreadable_once_is_copied_on_the_retry_with_zero_skipped(job, monkeypatch):
    flaky = FlakyOpen(monkeypatch, {"a03.txt": 1})
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["files"] == TOTAL
    assert result["errors"] == [] and result["not_copied"] == 0
    assert result["retried"] == 1
    entry = manifest_files(job, result)["a03.txt"]                  # in the manifest...
    assert entry["sha256"] == sha_of(b"contents of a03.txt" * 50)    # ...with the right hash
    assert version_hashes(job, result)["a03.txt"] == entry["sha256"]
    assert manifest_lines(job, result)[-1]["verified"] is True       # ...and verified
    assert manifest_lines(job, result)[-1]["files"] == TOTAL
    assert flaky.opens["a03.txt"] == 2                                # one retry, not a loop
    assert "skipped" not in result["message"]
    assert "1 file(s) could not be read at first and were copied on the second try" in result["message"]


def test_a_file_unreadable_on_both_attempts_is_still_skipped_by_name(job, monkeypatch):
    flaky = FlakyOpen(monkeypatch, {"a03.txt": 99})
    result = backup_utils.run_job(job)

    assert result["ok"]
    assert result["files"] == TOTAL - 1
    assert len(result["errors"]) == 1 and result["errors"][0].startswith("a03.txt: ")
    assert result["not_copied"] == 1 and result["retried"] == 0
    assert "1 file(s) could not be read and were skipped" in result["message"]
    assert "second try" not in result["message"]
    assert "a03.txt" not in manifest_files(job, result)
    assert flaky.opens["a03.txt"] == 2                                # exactly one retry


@pytest.mark.parametrize("error", [
    PermissionError(errno.EACCES, "Permission denied"),
    OSError(errno.EIO, "Input/output error"),
])
def test_any_source_open_failure_is_retried(job, monkeypatch, error):
    flaky = FlakyOpen(monkeypatch, {"b1.txt": 1}, error=error)
    result = backup_utils.run_job(job)

    assert result["ok"] and result["retried"] == 1 and result["errors"] == []
    assert "sub/b1.txt" in manifest_files(job, result)
    assert flaky.opens["b1.txt"] == 2


def test_a_read_that_fails_half_way_is_retried_and_leaves_no_half_file(tmp_path, monkeypatch):
    source = make_source(tmp_path / "s", top=["big.bin"], sub=[])
    payload = os.urandom(3000)
    (source / "big.bin").write_bytes(payload)
    dest = tmp_path / "d"
    monkeypatch.setattr(copying, "CHUNK", 1000)
    real_open = open
    attempts = []

    class HalfReader:
        """A source handle whose read fails after the first chunk."""

        def __init__(self, handle):
            self.handle, self.reads = handle, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size):
            self.reads += 1
            if len(attempts) == 1 and self.reads == 2:
                raise OSError(errno.EIO, "Input/output error")
            return self.handle.read(size)

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) == "big.bin":
            attempts.append(1)
            return HalfReader(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)
    seen = []
    stats = {}
    files, nbytes, errors = copying.copy_tree(
        str(source), str(dest), stats=stats, retry_skipped=True,
        on_copied=lambda rel, size, mtime, sha: seen.append((rel, size, sha)))

    assert (files, nbytes, errors) == (1, 3000, [])
    assert seen == [("big.bin", 3000, sha_of(payload))]               # one line, whole file
    assert (dest / "big.bin").read_bytes() == payload
    assert stats["retried"] == 1 and len(attempts) == 2


# ---------------------------------------------------------------------------
# The Done-when, a real exclusive lock (Windows)
# ---------------------------------------------------------------------------

def _kernel32():
    """kernel32 with the handle types spelled out (a 64-bit handle is not an int)."""
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


class ExclusiveLock:
    """Hold `path` open with share mode 0, as an app that has it open does."""

    def __init__(self, path):
        self.k32 = _kernel32()
        handle = self.k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
        self.handle = handle

    def release(self):
        """Close the handle; safe to call twice."""
        if self.handle is not None:
            assert self.k32.CloseHandle(self.handle)
            self.handle = None


class WatchedCopy:
    """Wrap the real `copying._hash_copy`: record each attempt by file name,
    and the error the first attempt hit."""

    def __init__(self, monkeypatch):
        self.calls, self.errors = {}, {}
        real = copying._hash_copy

        def watched(src, dst):
            name = os.path.basename(src)
            self.calls[name] = self.calls.get(name, 0) + 1
            try:
                return real(src, dst)
            except OSError as exc:
                self.errors.setdefault(name, exc)
                raise

        monkeypatch.setattr(copying, "_hash_copy", watched)


@windows_only
def test_a_really_locked_file_released_before_the_retry_is_copied_and_verified(job, monkeypatch):
    lock = ExclusiveLock(os.path.join(job["source"], "a05.txt"))
    pauses = []

    def release_between_passes():
        pauses.append(1)
        lock.release()

    monkeypatch.setattr(copying, "_retry_pause", release_between_passes)
    watched = WatchedCopy(monkeypatch)
    try:
        result = backup_utils.run_job(job)
    finally:
        lock.release()

    assert isinstance(watched.errors["a05.txt"], PermissionError)    # it WAS locked first time
    assert watched.errors["a05.txt"].errno == errno.EACCES      # open() reports a sharing violation so
    assert pauses == [1]
    assert result["ok"], result["message"]
    assert result["files"] == TOTAL and result["errors"] == [] and result["not_copied"] == 0
    entry = manifest_files(job, result)["a05.txt"]
    assert entry["sha256"] == sha_of(b"contents of a05.txt" * 50)
    assert manifest_lines(job, result)[-1]["verified"] is True
    assert watched.calls["a05.txt"] == 2
    os.remove(os.path.join(job["source"], "a05.txt"))               # the handle really is gone


@windows_only
def test_a_file_locked_for_the_whole_run_is_skipped_by_name_after_exactly_one_retry(job, monkeypatch):
    lock = ExclusiveLock(os.path.join(job["source"], "sub", "b2.txt"))
    pauses = []
    monkeypatch.setattr(copying, "_retry_pause", lambda: pauses.append(1))
    watched = WatchedCopy(monkeypatch)
    try:
        result = backup_utils.run_job(job)
    finally:
        lock.release()

    assert result["ok"]
    assert watched.calls["b2.txt"] == 2 and pauses == [1]            # never a loop
    assert [e.split(": ")[0] for e in result["errors"]] == ["sub/b2.txt"]
    assert result["files"] == TOTAL - 1 and result["retried"] == 0
    assert "sub/b2.txt" not in manifest_files(job, result)
    os.remove(os.path.join(job["source"], "sub", "b2.txt"))          # the handle really is gone


# ---------------------------------------------------------------------------
# Counts, order, manifest
# ---------------------------------------------------------------------------

def test_some_recovered_some_not_counts_messages_and_order_are_exact(job, monkeypatch):
    flaky = FlakyOpen(monkeypatch, {"a01.txt": 1, "a04.txt": 99, "b0.txt": 1, "b3.txt": 99})
    result = backup_utils.run_job(job)
    backup_utils.record_run(job["id"], result)
    stored = backup_utils.get_job(job["id"])

    first_pass, retry_pass = flaky.order[:4], flaky.order[4:]
    assert sorted(first_pass) == ["a01.txt", "a04.txt", "b0.txt", "b3.txt"]
    assert retry_pass == first_pass                                   # retried in the order skipped
    skipped_in_order = [n for n in first_pass if n in ("a04.txt", "b3.txt")]
    assert [e.split(": ")[0].rsplit("/", 1)[-1] for e in result["errors"]] == skipped_in_order
    assert result["files"] == TOTAL - 2
    assert result["not_copied"] == 2 and result["retried"] == 2
    assert stored["last_skipped"] == 2                                # "OK, 2 skipped": N after retry
    assert sorted(stored["last_skipped_sample"]) == ["a04.txt", "b3.txt"]
    assert "2 file(s) could not be read at first and were copied on the second try" in result["message"]
    assert "2 file(s) could not be read and were skipped" in result["message"]
    assert set(manifest_files(job, result)) == (
        {f"{n}" for n in TOP} | {f"sub/{n}" for n in SUB}) - {"a04.txt", "sub/b3.txt"}


def test_a_retried_run_totals_equal_a_run_with_no_lock(job, tmp_path, monkeypatch):
    clean = backup_utils.new_job("Clean", job["source"], str(tmp_path / "dst2"),
                                 schedule="manual", retention=3)
    os.makedirs(clean["target"])
    clean_result = backup_utils.run_job(clean)
    FlakyOpen(monkeypatch, {"a00.txt": 1, "b1.txt": 1, "a07.txt": 1})
    result = backup_utils.run_job(job)

    assert (result["files"], result["bytes"]) == (clean_result["files"], clean_result["bytes"])
    assert version_hashes(job, result) == version_hashes(clean, clean_result)
    keep = lambda e: {k: e[k] for k in ("path", "size", "sha256")}       # noqa: E731
    assert (sorted(map(keep, manifest_files(job, result).values()), key=lambda e: e["path"])
            == sorted(map(keep, manifest_files(clean, clean_result).values()), key=lambda e: e["path"]))
    assert manifest_lines(job, result)[-1]["bytes"] == clean_result["bytes"]


def test_the_manifest_has_one_line_per_copied_file_and_none_for_a_failed_attempt(job, monkeypatch):
    FlakyOpen(monkeypatch, {"a02.txt": 1, "a06.txt": 99})
    result = backup_utils.run_job(job)

    lines = manifest_lines(job, result)
    paths = [e["path"] for e in lines if e["type"] == "file"]
    assert len(paths) == len(set(paths)) == result["files"] == TOTAL - 1
    assert paths.count("a02.txt") == 1 and "a06.txt" not in paths
    assert [e["type"] for e in lines].count("end") == 1


def test_progress_counts_a_recovered_file_once_and_never_a_failed_attempt(tmp_path, monkeypatch):
    source = make_source(tmp_path / "s")
    FlakyOpen(monkeypatch, {"a03.txt": 1, "a08.txt": 99})
    seen = []
    files, _, errors = copying.copy_tree(
        str(source), str(tmp_path / "d"), retry_skipped=True,
        on_progress=lambda done, total: seen.append((done, total)))

    assert files == TOTAL - 1 and len(errors) == 1
    assert [d for d, _ in seen] == list(range(1, TOTAL))             # 1..13, no repeat, no skip
    assert {t for _, t in seen} == {TOTAL}


@windows_only
def test_a_file_past_260_characters_is_retried_through_the_long_path_form(tmp_path, monkeypatch):
    deep = str(tmp_path / "s")
    for _ in range(3):
        deep = os.path.join(deep, "d" * 100)
    os.makedirs("\\\\?\\" + deep)
    payload = os.urandom(500)
    with open("\\\\?\\" + os.path.join(deep, "f.bin"), "wb") as handle:
        handle.write(payload)
    assert len(os.path.join(deep, "f.bin")) > 300      # Windows' own limit is 260
    flaky = FlakyOpen(monkeypatch, {"f.bin": 1})
    seen = []
    files, _, errors = copying.copy_tree(
        str(tmp_path / "s"), str(tmp_path / "d"), retry_skipped=True,
        on_copied=lambda rel, size, mtime, sha: seen.append((rel, sha)))

    assert (files, errors) == (1, []) and flaky.opens["f.bin"] == 2
    assert seen == [("d" * 100 + "/" + "d" * 100 + "/" + "d" * 100 + "/f.bin", sha_of(payload))]


@pytest.mark.parametrize("mode", ["full", "quick", "off"])
def test_a_recovered_file_goes_through_every_verification_mode(job, monkeypatch, mode):
    job["verify"] = mode
    FlakyOpen(monkeypatch, {"a09.txt": 1})
    result = backup_utils.run_job(job)

    assert result["ok"] and result["retried"] == 1 and result["verify_mode"] == mode
    assert manifest_lines(job, result)[-1]["verify"] == mode
    assert manifest_lines(job, result)[-1]["files"] == TOTAL


def test_a_recovered_file_whose_copy_is_wrong_fails_verification(job, monkeypatch):
    FlakyOpen(monkeypatch, {"a05.txt": 1})
    real = copying._hash_copy

    def corrupting(src, dst):
        size, mtime, sha = real(src, dst)
        if os.path.basename(src) == "a05.txt":
            with open(dst, "ab") as handle:        # the bytes on the target differ
                handle.write(b"x")
        return size, mtime, sha

    monkeypatch.setattr(copying, "_hash_copy", corrupting)
    result = backup_utils.run_job(job)

    assert not result["ok"] and "verification failed" in result["message"]
    assert "a05.txt" in result["message"]


# ---------------------------------------------------------------------------
# What is NOT retried, and what ends the run during a retry
# ---------------------------------------------------------------------------

def test_a_clean_run_never_enters_the_retry_pass_and_a_skip_does(job, monkeypatch):
    pauses = []
    monkeypatch.setattr(copying, "_retry_pause", lambda: pauses.append(1))
    clean = backup_utils.run_job(job)
    assert clean["ok"] and clean["retried"] == 0 and pauses == []

    FlakyOpen(monkeypatch, {"a00.txt": 99})                          # positive control
    backup_utils.run_job(job)
    assert pauses == [1]


def test_restore_style_copies_do_not_retry_unless_asked(tmp_path, monkeypatch):
    source = make_source(tmp_path / "s")
    flaky = FlakyOpen(monkeypatch, {"a03.txt": 1})
    files, _, errors = copying.copy_tree(str(source), str(tmp_path / "d"))

    assert files == TOTAL - 1 and len(errors) == 1
    assert flaky.opens["a03.txt"] == 1                                # the default is no retry


@pytest.mark.parametrize("error,cause", [
    (OSError(errno.ENOSPC, "No space left on device"), "no_space"),
    (PermissionError(errno.EACCES, "Access is denied"), "no_permission"),
])
def test_a_target_failure_during_the_retry_fails_the_run_and_keeps_the_last_backup(
        job, tmp_path, monkeypatch, error, cause):
    good = backup_utils.run_job(job)
    assert good["ok"]
    FlakyOpen(monkeypatch, {"a03.txt": 1})
    real = copying._open_for_write

    def failing(dst):
        if os.path.basename(dst) == "a03.txt":   # only the retry gets this far
            raise error
        return real(dst)

    monkeypatch.setattr(copying, "_open_for_write", failing)
    result = backup_utils.run_job(job)

    assert not result["ok"] and result["cause"] == cause
    assert "could not be read" not in result["message"]               # not reported as a skip
    assert backup_utils.latest_version(job) == good["version"]       # the earlier backup stands
    assert backup_utils.is_verified(job, good["version"])
    root = backup_utils.job_root(job)
    assert not [n for n in os.listdir(root) if n.endswith((".partial", ".tmp"))]


def test_cancelling_during_the_retry_pass_ends_cancelled_and_copies_nothing_more(job, monkeypatch):
    good = backup_utils.run_job(job)
    flaky = FlakyOpen(monkeypatch, {"a03.txt": 1, "a04.txt": 1})
    state = {"cancel": False}

    def cancel_at_the_pause():
        state["cancel"] = True

    monkeypatch.setattr(copying, "_retry_pause", cancel_at_the_pause)
    result = backup_utils.run_job(job, should_abort=lambda: state["cancel"])

    assert not result["ok"] and result["message"] == "Backup cancelled"
    assert flaky.opens == {"a03.txt": 1, "a04.txt": 1}                # no retry attempt was made
    assert backup_utils.latest_version(job) == good["version"]
    root = backup_utils.job_root(job)
    assert not [n for n in os.listdir(root) if n.endswith((".partial", ".tmp"))]


def test_cancelling_between_two_retries_stops_before_the_second(job, monkeypatch):
    flaky = FlakyOpen(monkeypatch, {"a03.txt": 1, "a04.txt": 1})
    state = {"cancel": False}
    real = copying._hash_copy

    def cancel_after_first_retry(src, dst):
        out = real(src, dst)
        # `real` raises for a first-pass open, so reaching here means a RETRY of one of
        # the two files just succeeded: cancel before the other one's retry. Which of
        # the two goes first is the order the folder was listed in (sorted on NTFS,
        # arbitrary on POSIX), so the test must not name it.
        if os.path.basename(src) in ("a03.txt", "a04.txt"):
            state["cancel"] = True
        return out

    monkeypatch.setattr(copying, "_hash_copy", cancel_after_first_retry)
    result = backup_utils.run_job(job, should_abort=lambda: state["cancel"])

    assert not result["ok"] and result["message"] == "Backup cancelled"
    # One file was retried (opened twice), the other's retry never started (opened once).
    assert sorted(flaky.opens.values()) == [1, 2], flaky.opens


def test_a_file_that_vanished_between_the_passes_is_skipped_by_name_not_a_crash(job, monkeypatch):
    FlakyOpen(monkeypatch, {"a03.txt": 1})
    monkeypatch.setattr(copying, "_retry_pause",
                        lambda: os.remove(os.path.join(job["source"], "a03.txt")))
    result = backup_utils.run_job(job)

    assert result["ok"]
    assert [e.split(": ")[0] for e in result["errors"]] == ["a03.txt"]
    assert "denied" not in result["errors"][0].lower()                # the newest error, not the first
    assert result["files"] == TOTAL - 1 and result["not_copied"] == 1 and result["retried"] == 0


def test_a_source_that_vanished_between_the_passes_fails_the_run_as_it_would_first_time(job, monkeypatch):
    good = backup_utils.run_job(job)
    FlakyOpen(monkeypatch, {"a03.txt": 1})
    monkeypatch.setattr(copying, "_retry_pause", lambda: shutil.rmtree(job["source"]))
    result = backup_utils.run_job(job)

    assert not result["ok"] and result["cause"] == "source_unavailable"
    assert backup_utils.latest_version(job) == good["version"]


def test_an_unlistable_folder_is_not_retried(tmp_path, monkeypatch):
    source = make_source(tmp_path / "s")
    real_scandir = os.scandir
    listings = []

    def fake_scandir(path="."):
        if os.path.basename(str(path).rstrip("\\/")) == "sub":
            listings.append(path)
            if len(listings) > 1:                    # the count passes saw it; the copy cannot
                raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    copied_from = []
    real = copying._hash_copy
    monkeypatch.setattr(copying, "_hash_copy",
                        lambda src, dst: copied_from.append(os.path.basename(src)) or real(src, dst))
    stats = {}
    files, _, errors = copying.copy_tree(str(source), str(tmp_path / "d"),
                                         stats=stats, retry_skipped=True)

    assert "sub" not in copied_from and len(copied_from) == len(TOP)  # never tried as a file
    assert files == len(TOP) and [e.split(": ")[0] for e in errors] == ["sub"]
    assert stats["retried"] == 0 and len(listings) == 2           # the folder was listed once to copy


# ---------------------------------------------------------------------------
# The refusals and the guards count files that actually landed
# ---------------------------------------------------------------------------

def test_a_source_whose_every_file_was_locked_at_first_is_not_refused_if_they_recover(tmp_path, monkeypatch):
    source = make_source(tmp_path / "s", top=["only1.txt", "only2.txt"], sub=[])
    target = tmp_path / "d"
    target.mkdir()
    one = backup_utils.new_job("Two", str(source), str(target), schedule="manual")
    FlakyOpen(monkeypatch, {"only1.txt": 1, "only2.txt": 1})
    result = backup_utils.run_job(one)

    assert result["ok"] and result["files"] == 2 and result["retried"] == 2


def test_a_source_whose_every_file_stays_locked_is_still_refused(tmp_path, monkeypatch):
    source = make_source(tmp_path / "s", top=["only1.txt", "only2.txt"], sub=[])
    target = tmp_path / "d"
    target.mkdir()
    one = backup_utils.new_job("Two", str(source), str(target), schedule="manual")
    FlakyOpen(monkeypatch, {"only1.txt": 99, "only2.txt": 99})
    result = backup_utils.run_job(one)

    assert not result["ok"] and result["cause"] == "source_incomplete"


def test_the_shortfall_rule_is_judged_after_the_retry(job, monkeypatch):
    # 5 of 14 locked is past both the floor (3) and 30%: refused without a
    # retry. Recovered, the copy is complete and the guard sees all 14 files.
    names = {"a00.txt": 1, "a01.txt": 1, "a02.txt": 1, "a03.txt": 1, "a04.txt": 1}
    with monkeypatch.context() as patch:
        FlakyOpen(patch, {n: 99 for n in names})
        refused = backup_utils.run_job(job)
    assert not refused["ok"] and refused["cause"] == "source_incomplete"

    FlakyOpen(monkeypatch, names)
    result = backup_utils.run_job(job)
    assert result["ok"] and result["files"] == TOTAL and result["retried"] == 5
    assert result["pruning_held"] is False and result["baseline_files"] == 0


def test_the_sanity_guard_sees_the_file_count_after_the_retry(tmp_path, monkeypatch):
    # A 20-file baseline, then a source cut to 14 files (exactly 30% down, which
    # is not a drop). Three are locked first time: if they stayed locked the
    # backup would hold 11 (45% down, pruning held); recovered, it holds 14.
    source = make_source(tmp_path / "s", top=[f"f{i:02d}.txt" for i in range(20)], sub=[])
    target = tmp_path / "d"
    target.mkdir()
    one = backup_utils.new_job("Guard", str(source), str(target), schedule="manual", retention=5)
    assert backup_utils.run_job(one)["files"] == 20
    for i in range(6, 20):
        (source / f"f{i:02d}.txt").unlink()
    for i in range(8):
        (source / f"keep{i}.txt").write_text(f"k{i}")
    names = ["f00.txt", "f01.txt", "keep0.txt"]

    with monkeypatch.context() as patch:
        FlakyOpen(patch, {n: 99 for n in names})
        stuck = backup_utils.run_job(one)
    assert stuck["ok"] and stuck["files"] == 11 and stuck["pruning_held"] is True

    FlakyOpen(monkeypatch, {n: 1 for n in names})
    result = backup_utils.run_job(one)
    assert result["ok"] and result["files"] == 14 and result["retried"] == 3
    assert result["pruning_held"] is False


# ---------------------------------------------------------------------------
# Reporting: run log, jobs file
# ---------------------------------------------------------------------------

def test_the_run_log_line_carries_the_retried_count_and_no_skip(job, monkeypatch):
    FlakyOpen(monkeypatch, {"a03.txt": 1})
    backup_utils.run_job(job)
    line = runlog.read_runs(job["id"])[0]

    assert set(line) == set(runlog.LINE_KEYS)
    assert line["outcome"] == "ok" and line["retried"] == 1
    assert line["skipped"] == 0 and line["skipped_paths"] == [] and line["files"] == TOTAL


def test_the_run_log_line_for_a_file_that_stays_locked(job, monkeypatch):
    FlakyOpen(monkeypatch, {"a03.txt": 99, "a04.txt": 1})
    backup_utils.run_job(job)
    line = runlog.read_runs(job["id"])[0]

    assert line["outcome"] == "ok_skipped" and line["retried"] == 1
    assert line["skipped"] == 1 and line["skipped_paths"] == ["a03.txt"]


def test_a_clean_run_logs_zero_retried(job):
    backup_utils.run_job(job)
    assert runlog.read_runs(job["id"])[0]["retried"] == 0


def test_the_run_log_keeps_counts_only(job):
    line = runlog.build_line(job, {"ok": True, "retried": "C:/secret/file.txt"}, "manual",
                             datetime.datetime.now(),
                             datetime.datetime.now())
    assert line["retried"] == 0                                       # not a count: dropped
    line = runlog.build_line(job, {"ok": True, "retried": 3, "secret": "x"}, "manual",
                             datetime.datetime.now(),
                             datetime.datetime.now())
    assert line["retried"] == 3 and "secret" not in line and set(line) == set(runlog.LINE_KEYS)


def test_the_jobs_file_stores_only_the_still_skipped_files(job, monkeypatch):
    FlakyOpen(monkeypatch, {"a00.txt": 1, "a01.txt": 1})
    result = backup_utils.run_job(job)
    stored = backup_utils.record_run(job["id"], result)

    assert stored["last_result"] == "ok"
    assert stored["last_skipped"] == 0 and stored["last_skipped_sample"] == []
