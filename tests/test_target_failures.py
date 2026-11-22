# tests/test_target_failures.py
# Card 48: a failure to WRITE the target (full drive, path too long, file too
# large, drive gone, no permission) fails the run. It must never be counted as
# a skipped source file, because a run that carried on then verified, promoted
# and PRUNED a copy holding a fraction of the files, deleting the only
# complete backup. Only a failure to open or read the SOURCE is a skip.
#
# Faults are injected at the real destination write (the file object
# `_hash_copy` writes through, reached via `backup_utils.open`) or at the real
# `os.makedirs`, never by patching the function under test. Expected hashes
# are computed here with hashlib, independently of the code under test.
import errno
import hashlib
import os

import pytest

from utils import backup_utils, copying, jobs_store

FILES = 30


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def tree(tmp_path):
    """30 source files (20 at the top, 10 in `sub`), retention 1."""
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    for i in range(FILES):
        folder = source if i < 20 else source / "sub"
        (folder / f"file{i:02d}.bin").write_bytes(os.urandom(2000 + i))
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Target job", str(source), str(target),
                               schedule="manual", retention=1)
    return job, source, target


def sha(path) -> str:
    """Independent SHA-256 of a file on disk."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def tree_hashes(root) -> dict:
    """{relative path: sha256} for every file under `root`."""
    found = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, root).replace("\\", "/")] = sha(full)
    return found


def fail_destination_writes(monkeypatch, root, nth, error, at="write"):
    """Make the `nth` destination file opened for writing under `root` fail.

    `at` is "write" (the first write raises), "close" (writes land, closing
    raises -- a buffered small file reports a full disk there) or "open".
    Returns a dict whose "path" is the destination that failed.
    """
    real_open = open
    state = {"n": 0, "path": None}
    root = os.path.normcase(os.path.abspath(str(root)))

    class Failing:
        """A real destination handle with one operation failing."""

        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()
            if at == "close" and exc[0] is None:
                raise error

        def write(self, data):
            if at == "write":
                raise error
            return self.handle.write(data)

    def fake_open(path, mode="r", *args, **kwargs):
        full = os.path.normcase(os.path.abspath(str(path)))
        if mode == "wb" and full.startswith(root):
            state["n"] += 1
            if state["n"] == nth:
                state["path"] = str(path)
                if at == "open":
                    raise error
                return Failing(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)
    return state


def first_complete_run(job, source):
    """Run once for real; return (stamp, {rel: sha} of that version)."""
    result = backup_utils.run_job(job)
    assert result["ok"] and result["files"] == FILES, result["message"]
    version = backup_utils.version_path(job, result["version"])
    hashes = tree_hashes(version)
    assert hashes == tree_hashes(source)  # positive control: it IS complete
    return result["version"], hashes


def assert_failed_cleanly(job, result, first, first_hashes):
    """The card's Done-when: nothing promoted, pruned or left behind."""
    assert result["ok"] is False, result["message"]
    assert "pruned" not in result and "version" not in result
    # Only the first version and its manifest remain: no new version folder,
    # no final or .tmp manifest, no .partial.
    assert sorted(os.listdir(backup_utils.job_root(job))) == sorted(
        [first, first + backup_utils.MANIFEST_SUFFIX])
    assert backup_utils.version_names(job) == [first]
    assert backup_utils.is_verified(job, first)
    assert tree_hashes(backup_utils.version_path(job, first)) == first_hashes


# ---------------------------------------------------------------------------
# The Done-when: a target write fails part-way through a run
# ---------------------------------------------------------------------------

def test_a_full_drive_on_the_4th_of_30_files_fails_and_keeps_the_complete_backup(
        tree, monkeypatch):
    job, source, _ = tree
    first, first_hashes = first_complete_run(job, source)
    failed = fail_destination_writes(monkeypatch, backup_utils.job_root(job), 4,
                                     OSError(errno.ENOSPC, "No space left on device"))

    result = backup_utils.run_job(job)

    assert failed["path"]  # the injection really fired
    assert_failed_cleanly(job, result, first, first_hashes)
    assert result["cause"] == "no_space"
    assert "The backup drive is full" in result["message"]
    assert os.path.basename(failed["path"]) in result["message"]
    assert "skipped" not in result["message"]


@pytest.mark.parametrize("error, at, cause, words", [
    (OSError(errno.ENAMETOOLONG, "File name too long"), "open",
     "path_too_long", "A file path is too long for the backup drive"),
    (OSError(errno.EFBIG, "File too large"), "write",
     "file_too_large", "A file is too large for this drive (FAT32 limits files to 4 GB)"),
    (OSError(errno.EIO, "Input/output error"), "write",
     "target_write_failed", "Could not write to the backup drive: Input/output error"),
    (OSError(errno.ENOENT, "No such file or directory"), "open",
     "target_unavailable", "The backup drive was disconnected or became unavailable"),
    (OSError(errno.ENOSPC, "No space left on device"), "close",
     "no_space", "The backup drive is full"),
])
def test_every_target_cause_fails_the_run_in_plain_words(tree, monkeypatch,
                                                         error, at, cause, words):
    job, source, _ = tree
    first, first_hashes = first_complete_run(job, source)
    failed = fail_destination_writes(monkeypatch, backup_utils.job_root(job), 4,
                                     error, at=at)

    result = backup_utils.run_job(job)

    assert failed["path"]
    assert_failed_cleanly(job, result, first, first_hashes)
    assert result["cause"] == cause
    assert words in result["message"]
    assert os.path.basename(failed["path"]) in result["message"]


def test_a_target_failure_prunes_nothing_even_when_retention_would(tree, monkeypatch):
    # With one version, prune has nothing it may delete (the newest verified
    # one is protected), so "prunes nothing" needs a prunable older version.
    job, source, _ = tree
    job["retention"] = 2
    older = backup_utils.run_job(job)["version"]
    newer, _ = first_complete_run(job, source)
    assert backup_utils.version_names(job) == [older, newer]
    job["retention"] = 1  # a successful run now WOULD prune `older`
    fail_destination_writes(monkeypatch, backup_utils.job_root(job), 4,
                            OSError(errno.ENOSPC, "No space left on device"))
    result = backup_utils.run_job(job)
    assert result["ok"] is False and result["cause"] == "no_space"
    assert backup_utils.version_names(job) == [older, newer]


def test_a_target_failure_on_the_very_first_file_fails_the_run(tree, monkeypatch):
    job, source, _ = tree
    first, first_hashes = first_complete_run(job, source)
    fail_destination_writes(monkeypatch, backup_utils.job_root(job), 1,
                            OSError(errno.ENOSPC, "No space left on device"))
    result = backup_utils.run_job(job)
    assert_failed_cleanly(job, result, first, first_hashes)
    assert result["cause"] == "no_space"


def test_a_target_failure_with_no_earlier_backup_leaves_nothing(tree, monkeypatch):
    job, _, _ = tree
    fail_destination_writes(monkeypatch, backup_utils.job_root(job), 4,
                            OSError(errno.ENOSPC, "No space left on device"))
    result = backup_utils.run_job(job)
    assert result["ok"] is False and result["cause"] == "no_space"
    assert os.listdir(backup_utils.job_root(job)) == []


def test_a_folder_that_cannot_be_created_on_the_target_fails_the_run(tree, monkeypatch):
    # copytree used to create destination folders itself and collect that
    # failure as one more "skipped file", silently dropping the whole folder.
    job, source, _ = tree
    first, first_hashes = first_complete_run(job, source)
    real_makedirs = os.makedirs
    seen = {"hit": False}

    def fake_makedirs(path, *args, **kwargs):
        parts = os.path.normpath(str(path)).split(os.sep)
        if parts[-1] == "sub" and parts[-2].endswith(".partial"):
            seen["hit"] = True
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(os, "makedirs", fake_makedirs)
    result = backup_utils.run_job(job)

    assert seen["hit"]
    assert_failed_cleanly(job, result, first, first_hashes)
    assert result["cause"] == "no_permission"
    assert "No permission to write to the backup drive" in result["message"]
    assert "sub" in result["message"]


def test_an_unclassified_full_drive_error_still_fails_the_run(tree, monkeypatch):
    # Belt and braces in copy_tree: a full drive can only come from a write,
    # so an ENOSPC that reaches the copy loop as a plain OSError (a write
    # path that forgot to classify it) must still fail the run, not skip.
    job, source, _ = tree
    first, first_hashes = first_complete_run(job, source)
    real = backup_utils._hash_copy
    calls = {"n": 0}

    def full_from_the_4th(src, dst):
        calls["n"] += 1
        if calls["n"] >= 4:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst)

    monkeypatch.setattr(copying, "_hash_copy", full_from_the_4th)
    result = backup_utils.run_job(job)
    assert_failed_cleanly(job, result, first, first_hashes)
    assert result["cause"] == "no_space"


# ---------------------------------------------------------------------------
# Positive controls: a SOURCE failure is still a skip
# ---------------------------------------------------------------------------

def test_an_unreadable_source_file_is_still_a_skip_and_prune_still_runs(tree, monkeypatch):
    job, source, _ = tree
    first, _ = first_complete_run(job, source)
    bad = os.path.normcase(os.path.abspath(str(source / "sub" / "file25.bin")))
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.normcase(os.path.abspath(str(path))) == bad:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["files"] == FILES - 1
    assert len(result["errors"]) == 1 and "file25.bin" in result["errors"][0]
    assert "1 file(s) could not be read and were skipped" in result["message"]
    assert result["pruned"] == [first]  # retention 1: the old one went
    assert backup_utils.is_verified(job, result["version"])


def test_an_unreadable_source_folder_is_still_a_skip(tree, monkeypatch):
    job, source, _ = tree
    real_scandir = os.scandir
    bad = os.path.normcase(os.path.abspath(str(source / "sub")))

    def fake_scandir(path="."):
        if os.path.normcase(os.path.abspath(str(path))) == bad:
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["files"] == 20
    assert len(result["errors"]) == 1 and result["errors"][0].startswith("sub:")
    version = backup_utils.version_path(job, result["version"])
    assert not os.path.exists(os.path.join(version, "sub"))


# ---------------------------------------------------------------------------
# What the copy must still do
# ---------------------------------------------------------------------------

def test_progress_counts_every_file_once_in_order(tree):
    job, _, _ = tree
    seen = []
    backup_utils.copy_tree(job["source"], os.path.join(str(job["target"]), "out"),
                           on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(i, FILES) for i in range(1, FILES + 1)]


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_a_junction_to_an_ancestor_is_skipped_not_followed(tree):
    import _winapi
    job, source, _ = tree
    try:
        _winapi.CreateJunction(str(source), str(source / "sub" / "loop"))
    except OSError:
        pytest.skip("could not create a junction here")
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert result["files"] == FILES and result["errors"] == []
    version = backup_utils.version_path(job, result["version"])
    assert not os.path.exists(os.path.join(version, "sub", "loop"))


def test_restore_into_an_existing_folder_reuses_it(tree, tmp_path):
    job, source, _ = tree
    version = backup_utils.run_job(job)["version"]
    out = tmp_path / "out"
    out.mkdir()
    first = backup_utils.restore_version(job, version, str(out))
    again = backup_utils.restore_version(job, version, str(out))
    assert first["ok"] and again["ok"], again["message"]
    assert again["files"] == FILES
    assert tree_hashes(again["path"]) == tree_hashes(source)


# ---------------------------------------------------------------------------
# Restore: an unwritable destination is a failure, not "N errors"
# ---------------------------------------------------------------------------

def test_a_restore_whose_destination_fills_up_fails_and_removes_its_folder(
        tree, tmp_path, monkeypatch):
    job, _, _ = tree
    version = backup_utils.run_job(job)["version"]
    out = tmp_path / "out"
    out.mkdir()
    failed = fail_destination_writes(monkeypatch, out, 2,
                                     OSError(errno.ENOSPC, "No space left on device"))

    result = backup_utils.restore_version(job, version, str(out))

    assert failed["path"]
    assert result["ok"] is False, result["message"]
    assert result["cause"] == "no_space"
    assert "The destination drive is full" in result["message"]
    assert os.path.basename(failed["path"]) in result["message"]
    assert os.listdir(out) == []  # the half-restored folder is gone


def test_a_restore_into_an_earlier_restore_folder_leaves_it_and_says_so(
        tree, tmp_path, monkeypatch):
    job, _, _ = tree
    version = backup_utils.run_job(job)["version"]
    out = tmp_path / "out"
    out.mkdir()
    folder = backup_utils.restore_version(job, version, str(out))["path"]
    (out / os.path.basename(folder) / "my notes.txt").write_text("mine", encoding="utf-8")
    fail_destination_writes(monkeypatch, out, 2,
                            OSError(errno.ENOSPC, "No space left on device"))

    result = backup_utils.restore_version(job, version, str(out))

    assert result["ok"] is False and result["cause"] == "no_space"
    assert "may now be incomplete" in result["message"]
    assert (out / os.path.basename(folder) / "my notes.txt").read_text(
        encoding="utf-8") == "mine"


# ---------------------------------------------------------------------------
# The cause mapping itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("winerror, cause", [
    (112, "no_space"), (39, "no_space"), (206, "path_too_long"),
    (223, "file_too_large"), (21, "target_unavailable"), (3, "target_unavailable"),
    (53, "target_unavailable"), (64, "target_unavailable"),
    (67, "target_unavailable"), (5, "no_permission"),
])
def test_windows_error_codes_map_to_causes(winerror, cause):
    error = OSError(0, "windows says no")
    error.winerror = winerror
    assert backup_utils.TargetWriteFailed("x/f.bin", error).cause == cause


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH is a Windows limit")
def test_a_missing_path_past_max_path_is_called_too_long():
    long_path = "C:\\" + "d" * 300 + "\\f.bin"
    error = OSError(errno.ENOENT, "No such file or directory")
    assert backup_utils.TargetWriteFailed(long_path, error).cause == "path_too_long"
    assert backup_utils.TargetWriteFailed("C:\\d\\f.bin", error).cause == "target_unavailable"
