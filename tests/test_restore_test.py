# tests/test_restore_test.py
# Card 0Bv7mvB3: a test restore restores a random SAMPLE of a version into a
# temporary folder through the real restore path, reads what was written back
# from disk, compares size and SHA-256 with the manifest, and removes the folder.
#
# Versions are made by real `run_job` runs (so the manifest is the one the engine
# writes) and damaged on disk the way a real fault would be: a byte flipped or a
# file truncated in the stored copy (after clearing the read-only mark, as a
# person would), a manifest deleted or garbled. The write-side fault -- the one
# the restore's own copy-time hash cannot see -- is injected into the OWNING
# module (`utils.copying._hash_copy`), never the facade, and only for restores
# into a test-restore folder.
#
# Every "the temporary folder is gone" check asserts the exact folder the code
# made (recorded through `tempfile.mkdtemp`) AND that nothing is left under the
# temp base; every "a result was not stored/logged" check has a positive control
# in the same file. `tempfile.tempdir` is pointed at a throwaway base, so a stray
# `safekeep-restore-test-*` anywhere would be seen.
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils import backup_utils, copying, jobs_store, locks, restore_test, runlog, runner, verify
from utils.fs_utils import long_path
from helpers import app_data_dir, clear_ro, clear_ro_tree, is_read_only
from test_long_paths import _deep_file, _write, _x, windows_only
from test_restore_check import flip_byte, make_backup, manifest_lines, stored, write_manifest

AUTO_RESTORE_TEST = True      # conftest switches the automatic run off everywhere else
REPO = Path(__file__).resolve().parent.parent
GiB = 1024 ** 3


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep this file's jobs off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def tbase(tmp_path, monkeypatch):
    """A throwaway temp base. `tempfile.tempdir` points at it, so the code's
    `mkdtemp(dir=None)` lands here; `made` records every folder it created."""
    base = tmp_path / "tempbase"
    base.mkdir()
    made = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(tempfile, "tempdir", str(base))
    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    return SimpleNamespace(path=base, made=made)


def assert_clean(tbase, expect_made=1):
    """The folder(s) the code made are gone and nothing is left under the base."""
    assert len(tbase.made) == expect_made, tbase.made
    assert all(Path(folder).parent == tbase.path for folder in tbase.made)   # under the OS temp, nowhere else
    assert all(not os.path.lexists(_x(folder)) for folder in tbase.made)
    assert list(tbase.path.iterdir()) == []


def forty_files():
    """40 files in 7 folders; the largest is `d4/f039.txt`."""
    return {f"d{i % 7}/f{i:03}.txt": f"file {i} ".encode() * (i + 1) for i in range(40)}


class ForcedRng:
    """An rng whose `sample` returns the forced paths first, then the rest in
    order, so a test can put one file in the sample whatever the tree's size."""

    def __init__(self, *forced):
        self.forced = list(forced)

    def sample(self, population, count):
        """`count` items: the forced ones that are in `population`, then the rest."""
        population = list(population)
        head = [item for item in self.forced if item in population]
        return (head + [item for item in population if item not in head])[:count]


def write_side_fault(monkeypatch, name="b.txt", how="flip"):
    """After the real copy into a test-restore folder, change `name`'s WRITTEN
    bytes: what a failing disk, an antivirus or a filter driver could do on the
    write. The bytes READ from the backup (and so the copy-time hash) are fine.
    Returns the list of destinations it changed (the injection fired)."""
    real = copying._hash_copy
    hits = []

    def faulty(src, dst):
        result = real(src, dst)
        if restore_test.TEMP_PREFIX in dst and dst.replace("\\", "/").endswith("/" + name):
            hits.append(dst)
            clear_ro(dst)
            with open(_x(dst), "r+b") as handle:
                if how == "flip":
                    first = handle.read(1)
                    handle.seek(0)
                    handle.write(bytes([first[0] ^ 1]))
                else:
                    handle.truncate(1)
        return result

    monkeypatch.setattr(copying, "_hash_copy", faulty)
    return hits


def read_log(job):
    """The raw text of the job's run log file, and its parsed lines (oldest first)."""
    path = runlog.log_path(job["id"])
    text = open(path, encoding="utf-8").read()
    return text, [json.loads(line) for line in text.splitlines()]


def restore_lines(job):
    """The job's run-log lines that are test restores (the backups that made the
    fixture's versions logged lines of their own), oldest first."""
    return [line for line in reversed(runlog.read_runs(job["id"]))
            if line["outcome"].startswith("restore_test")]


def assert_no_paths(text, tmp_path, tbase):
    """No absolute path of the run, the temp base or a test-restore folder."""
    for forbidden in (str(tmp_path), str(tbase.path), tempfile.gettempdir(),
                      restore_test.TEMP_PREFIX, "\\\\?\\"):
        assert forbidden not in text and forbidden.replace("\\", "\\\\") not in text, forbidden


# ---------------------------------------------------------------------------
# The sample rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count, random_count, total", [
    (1, 1, 1), (4, 4, 4), (5, 5, 5), (100, 5, 6), (5000, 50, 51)])
def test_sample_size_rules(count, random_count, total):
    sizes = {f"f{i:05}.txt": i + 1 for i in range(count)}      # f{count-1} is the largest

    assert restore_test.default_sample_size(count) == random_count
    chosen = restore_test.choose_sample(sizes, rng=random.Random(1))

    assert len(chosen) == total and len(set(chosen)) == total      # no duplicates
    assert chosen[0] == f"f{count - 1:05}.txt"                     # the largest, first
    assert set(chosen) <= set(sizes)


def test_the_largest_file_is_included_for_every_seed_and_there_are_no_duplicates():
    sizes = {f"a/f{i:03}.bin": 10 + (i * 7) % 90 for i in range(200)}
    largest = max(sizes, key=lambda rel: (sizes[rel], rel))

    for seed in range(40):
        chosen = restore_test.choose_sample(sizes, rng=random.Random(seed))
        assert largest in chosen and len(chosen) == len(set(chosen)) == 11     # ceil(5%)=10, +1


def test_a_fixed_seed_gives_the_same_sample_whatever_order_the_manifest_listed():
    sizes = {f"f{i:03}": i + 1 for i in range(60)}
    shuffled = dict(reversed(list(sizes.items())))

    assert (restore_test.choose_sample(sizes, rng=random.Random(7))
            == restore_test.choose_sample(shuffled, rng=random.Random(7)))
    assert (restore_test.choose_sample(sizes, rng=random.Random(7))
            != restore_test.choose_sample(sizes, rng=random.Random(8)))


def test_the_largest_is_not_drawn_twice_when_it_sorts_first():
    sizes = {"a_big": 999, "b": 1, "c": 2, "d": 3, "e": 4, "f": 5, "g": 6}

    chosen = restore_test.choose_sample(sizes, rng=ForcedRng("a_big", "b", "c"))

    assert chosen.count("a_big") == 1 and len(chosen) == len(set(chosen)) == 6


def test_an_explicit_sample_size_and_no_largest_are_honoured():
    sizes = {f"f{i}": i + 1 for i in range(30)}

    chosen = restore_test.choose_sample(sizes, sample=3, rng=random.Random(1))
    assert len(chosen) == 4 and chosen[0] == "f29"
    chosen = restore_test.choose_sample(sizes, sample=3, rng=random.Random(1), include_largest=False)
    assert len(chosen) == 3
    assert len(restore_test.choose_sample(sizes, sample=500, rng=random.Random(1))) == 30


def test_the_automatic_bounds_leave_out_huge_files_and_cap_the_total():
    sizes = {"huge.iso": 3 * GiB, "big.bin": GiB, "x": 5, "y": 6}

    chosen = restore_test.choose_sample(sizes, rng=random.Random(1), max_file=GiB)
    assert "huge.iso" not in chosen and chosen[0] == "big.bin"       # the largest of the rest
    capped = restore_test.choose_sample(sizes, rng=random.Random(1), max_file=GiB, max_bytes=100)
    assert capped and "big.bin" not in capped and set(capped) <= {"x", "y"}


def test_restore_version_only_restores_exactly_the_named_files(tmp_path):
    job, stamp, _ = make_backup(tmp_path)
    out = tmp_path / "only"
    out.mkdir()

    result = backup_utils.restore_version(job, stamp, str(out), only={"a.txt", "nested/deep/c.bin"})

    assert result["ok"] and result["files"] == 2 and result["checked"] == 2
    assert result["missing_from_version"] == [] and result["verified"] is True
    restored = sorted(os.path.relpath(os.path.join(d, f), result["path"]).replace(os.sep, "/")
                      for d, _, fs in os.walk(result["path"]) for f in fs)
    assert restored == ["a.txt", "nested/deep/c.bin"]            # nothing else, no stray folders
    assert not os.path.exists(os.path.join(result["path"], "d.txt"))
    assert not os.path.exists(os.path.join(result["path"], "nested", "b.txt"))
    everything = tmp_path / "everything"
    everything.mkdir()                                            # control: the default is unchanged
    assert backup_utils.restore_version(job, stamp, str(everything))["files"] == 5


# ---------------------------------------------------------------------------
# The Done-when: healthy, corrupted, cleaned up, legacy
# ---------------------------------------------------------------------------

def test_a_healthy_version_passes_with_the_sample_size_reported(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)

    result = restore_test.restore_test(job)

    assert result["status"] == "passed" and result["version"] == stamp
    assert result["sampled"] == 5 and result["checked"] == 5 and result["failed"] == 0
    assert result["failed_sample"] == [] and result["cleanup_failed"] == 0
    assert result["includes_largest"] is True and result["largest"] == "nested/deep/c.bin"
    assert result["message"] == "Test restore passed: 5 files restored and checked."
    assert_clean(tbase)


def test_a_bigger_version_reports_the_default_sample_and_includes_the_largest(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path, forty_files())

    result = restore_test.restore_test(job, rng=random.Random(3))

    assert result["status"] == "passed" and result["sampled"] == 6      # 5 random + the largest
    assert result["largest"] == "d4/f039.txt" and result["includes_largest"] is True
    assert result["message"] == "Test restore passed: 6 files restored and checked."
    assert_clean(tbase)


def test_the_sample_is_what_restore_version_was_asked_to_restore(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path, forty_files())
    asked = []
    real = restore_test.restore_version
    monkeypatch.setattr(restore_test, "restore_version",
                        lambda *a, **k: (asked.append(k["only"]), real(*a, **k))[1])

    restore_test.restore_test(job, rng=random.Random(5))

    assert len(asked) == 1 and len(asked[0]) == 6 and "d4/f039.txt" in asked[0]


def test_one_sampled_file_corrupted_on_the_target_fails_naming_it(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    flip_byte(stored(job, stamp, "nested/b.txt"), 3)

    result = restore_test.restore_test(job)

    assert result["status"] == "failed"
    assert result["failed"] == 1 and result["failed_sample"] == ["nested/b.txt"]
    assert result["sampled"] == 5 and result["checked"] == 4
    assert result["failed_why"] == {"nested/b.txt": restore_test.WHY_HASH}
    assert result["message"] == "Test restore FOUND A PROBLEM: nested/b.txt did not match."
    assert_clean(tbase)


def test_a_corrupted_file_in_a_big_tree_fails_when_it_is_in_the_sample(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path, forty_files())
    flip_byte(stored(job, stamp, "d2/f009.txt"), 2)

    caught = restore_test.restore_test(job, rng=ForcedRng("d2/f009.txt"))

    assert caught["status"] == "failed" and caught["failed_sample"] == ["d2/f009.txt"]
    assert caught["sampled"] == 6 and caught["checked"] == 5
    assert_clean(tbase)

    # It is a SAMPLE: the same damage in a file the sample did not pick is not seen.
    missed = restore_test.restore_test(job, rng=ForcedRng("d0/f000.txt"))
    assert missed["status"] == "passed" and "d2/f009.txt" not in missed["failed_sample"]
    assert_clean(tbase, expect_made=2)


def test_a_truncated_stored_file_is_named(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    path = stored(job, stamp, "a.txt")
    clear_ro(path)
    with open(_x(path), "r+b") as handle:
        handle.truncate(2)

    result = restore_test.restore_test(job)

    assert result["status"] == "failed" and result["failed_sample"] == ["a.txt"]
    assert_clean(tbase)


def test_a_sampled_file_missing_from_the_version_is_a_failure_not_a_skip(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    path = stored(job, stamp, "d.txt")
    clear_ro(path)
    os.remove(_x(path))

    result = restore_test.restore_test(job)

    assert result["status"] == "failed" and result["failed_sample"] == ["d.txt"]
    assert result["failed_why"] == {"d.txt": restore_test.WHY_MISSING}
    assert result["sampled"] == 5 and result["checked"] == 4
    assert_clean(tbase)


def test_every_failing_file_is_counted_but_only_five_names_are_kept(tmp_path, tbase):
    files = {f"f{i}.txt": f"content {i}".encode() for i in range(8)}
    job, stamp, _ = make_backup(tmp_path, files)
    for rel in files:
        flip_byte(stored(job, stamp, rel), 0)

    result = restore_test.restore_test(job, sample=50)

    assert result["failed"] == 8 and result["sampled"] == 8 and result["checked"] == 0
    assert len(result["failed_sample"]) == 5 and result["failed_sample"] == sorted(files)[:5]
    assert result["message"].endswith("f0.txt, f1.txt, f2.txt, f3.txt, f4.txt and 3 more did not match.")
    assert_clean(tbase)


# --- the disk re-read: what the restore's own check cannot see ---------------------

def test_the_disk_reread_catches_a_write_side_fault_the_restore_hash_check_misses(
        tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)
    hits = write_side_fault(monkeypatch)

    # What card 6ajl8vjD's restore says about the same fault: all is well, because
    # its hash is of the bytes READ from the backup.
    out = tmp_path / (restore_test.TEMP_PREFIX + "plain")   # the fault only fires for these
    out.mkdir()
    plain = backup_utils.restore_version(job, stamp, str(out))
    assert hits and plain["verified"] is True and plain["mismatched"] == []
    on_disk = Path(plain["path"]) / "nested" / "b.txt"
    assert on_disk.read_bytes() != b"bravo bravo"                    # ...yet the file IS wrong
    hits.clear()

    result = restore_test.restore_test(job)

    assert hits, "the fault never fired"
    assert result["status"] == "failed" and result["failed_sample"] == ["nested/b.txt"]
    assert result["failed_why"] == {"nested/b.txt": restore_test.WHY_HASH}
    assert_clean(tbase)


def test_a_write_that_dropped_bytes_is_a_size_failure(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)
    hits = write_side_fault(monkeypatch, how="truncate")

    result = restore_test.restore_test(job)

    assert hits and result["status"] == "failed"
    assert result["failed_why"] == {"nested/b.txt": restore_test.WHY_SIZE}


def test_without_the_write_fault_the_same_harness_passes(tmp_path, tbase, monkeypatch):
    """The positive control for the two tests above: same wrapper, no change made."""
    job, stamp, _ = make_backup(tmp_path)
    hits = write_side_fault(monkeypatch, name="no-such-file.txt")

    result = restore_test.restore_test(job)

    assert hits == [] and result["status"] == "passed"


# --- cannot be tested --------------------------------------------------------------

def test_a_legacy_version_with_no_manifest_cannot_be_tested(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(backup_utils.manifest_path(job, stamp))

    result = restore_test.restore_test(job, version=stamp)

    assert result["status"] == "cannot_test" and result["reason"] == "no manifest"
    assert result["message"] == "Test restore could not be run: no manifest."
    assert result["sampled"] == 0 and result["checked"] == 0
    assert tbase.made == [] and list(tbase.path.iterdir()) == []      # nothing was even created


def test_a_damaged_manifest_cannot_be_tested(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    lines = manifest_lines(job, stamp)
    lines[2] = "{not json"
    write_manifest(job, stamp, lines)

    result = restore_test.restore_test(job, version=stamp)

    assert result["status"] == "cannot_test" and result["reason"] == "manifest damaged"
    assert tbase.made == []


def test_a_manifest_with_no_trailer_cannot_be_tested(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    write_manifest(job, stamp, manifest_lines(job, stamp)[:-1])       # the end trailer is gone

    result = restore_test.restore_test(job, version=stamp)

    assert result["status"] == "cannot_test" and result["reason"] == "manifest damaged"
    assert tbase.made == []


def test_a_manifest_line_with_no_size_cannot_be_trusted(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    lines = manifest_lines(job, stamp)
    entry = json.loads(lines[1])
    del entry["size"]
    lines[1] = json.dumps(entry)
    write_manifest(job, stamp, lines)

    result = restore_test.restore_test(job, version=stamp)

    assert result["status"] == "cannot_test" and result["reason"] == "manifest damaged"


def test_a_version_made_with_verification_off_cannot_be_tested(tmp_path, tbase):
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "a.txt").write_bytes(b"alpha")
    job = backup_utils.new_job("Off", str(source), str(target), schedule="manual", verify="off")
    made = backup_utils.run_job(job)
    assert made["ok"] and made["verified"] is False
    stamp = made["version"]

    result = restore_test.restore_test(job, version=stamp)

    assert result["status"] == "cannot_test" and result["reason"] == "not verified"
    assert tbase.made == []
    # ...and "newest verified" finds nothing to test either.
    assert restore_test.restore_test(job)["reason"] == "there is no verified backup to test"


def test_no_version_and_an_unknown_version_cannot_be_tested(tmp_path, tbase):
    job = backup_utils.new_job("Empty", str(tmp_path / "s"), str(tmp_path / "d"), schedule="manual")

    assert restore_test.restore_test(job)["reason"] == "there is no verified backup to test"
    assert restore_test.restore_test(job, version="20200101-000000")["reason"] == \
        "that backup version was not found"


def test_a_manifest_that_lists_no_files_has_nothing_to_test(tmp_path, tbase):
    job = backup_utils.new_job("Hollow", str(tmp_path / "s"), str(tmp_path / "d"), schedule="manual")
    stamp = "20261001-120000"
    os.makedirs(os.path.join(backup_utils.job_root(job), stamp))
    lines = [{"type": "header", "version": 1, "stamp": stamp, "source": "x"},
             {"type": "end", "files": 0, "bytes": 0, "verify": "full", "verified": True}]
    with open(backup_utils.manifest_path(job, stamp), "w", encoding="utf-8") as handle:
        handle.write("\n".join(json.dumps(line) for line in lines) + "\n")

    result = restore_test.restore_test(job)

    assert result["status"] == "cannot_test" and "nothing to test" in result["reason"]
    assert tbase.made == []


def test_a_temp_folder_that_cannot_be_made_cannot_be_tested(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(restore_test.tempfile, "mkdtemp", refuse)

    result = restore_test.restore_test(job)

    assert result["status"] == "cannot_test" and "temporary folder could not be made" in result["reason"]


def test_an_unwritable_temp_folder_cannot_be_tested_not_failed(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)
    full = copying.TargetWriteFailed("x", OSError(28, "No space left on device"))
    real = copying._hash_copy

    def fill(src, dst):
        if restore_test.TEMP_PREFIX in dst:
            raise full
        return real(src, dst)

    monkeypatch.setattr(copying, "_hash_copy", fill)

    result = restore_test.restore_test(job)

    assert result["status"] == "cannot_test" and "temporary folder is full" in result["reason"]
    assert result["failed"] == 0
    assert_clean(tbase)


# ---------------------------------------------------------------------------
# Cleanup, abort, lock
# ---------------------------------------------------------------------------

def test_the_temp_folder_is_removed_when_the_test_raises(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("the comparison blew up")

    monkeypatch.setattr(restore_test, "_compare", boom)

    with pytest.raises(RuntimeError, match="blew up"):
        restore_test.restore_test(job)

    assert_clean(tbase)
    assert not locks.is_held(runner._job_lock_path(job))               # the lock went too


def test_a_cancelled_test_removes_its_folder_and_stores_and_logs_nothing(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)

    result = restore_test.restore_test(job, should_abort=lambda: True)

    assert result["status"] == "cancelled" and result["sampled"] == 0
    assert_clean(tbase)
    assert "last_restore_test" not in backup_utils.get_job(job["id"])
    assert restore_lines(job) == []
    assert restore_test.restore_test(job)["status"] == "passed" and len(restore_lines(job)) == 1  # control


def test_a_cancel_between_files_during_the_comparison_removes_the_folder(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    calls = []

    def abort_after_the_copy():
        calls.append(1)
        return len(calls) > 6          # 5 files copied, then the first comparison is refused

    result = restore_test.restore_test(job, should_abort=abort_after_the_copy)

    assert result["status"] == "cancelled" and len(calls) > 6
    assert_clean(tbase)


def test_a_cleanup_that_fails_is_reported_not_hidden(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)
    attempts = []

    def refuse(path):
        attempts.append(path)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(restore_test, "_rmtree", refuse)
    try:
        result = restore_test.restore_test(job)

        assert attempts, "the injection never fired"
        assert result["status"] == "passed"                              # the test itself did pass
        assert result["cleanup_failed"] >= 5 and result["leftover"] == tbase.made[0]
        assert "could not be removed" in result["message"]
        assert tbase.made[0] not in result["message"]                    # never the path, in the message
        assert os.path.isdir(_x(tbase.made[0]))                          # and it really is still there
    finally:
        monkeypatch.undo()
        clear_ro_tree(tbase.path)
        shutil.rmtree(_x(tbase.made[0]), ignore_errors=True)


def test_a_source_read_only_file_is_restored_read_only_and_still_cleaned_up(tmp_path, tbase, monkeypatch):
    files = {"plain.txt": b"plain", "locked.txt": b"read only in the source"}
    source = tmp_path / "src"
    source.mkdir()
    for rel, data in files.items():
        (source / rel).write_bytes(data)
    os.chmod(source / "locked.txt", 0o444)
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Ro", str(source), str(target), schedule="manual")
    assert backup_utils.run_job(job)["ok"]
    seen = []
    real = verify._rehash
    monkeypatch.setattr(verify, "_rehash",
                        lambda path: (seen.append((os.path.basename(path), is_read_only(path))),
                                      real(path))[1])

    result = restore_test.restore_test(job)

    assert result["status"] == "passed"
    assert ("locked.txt", True) in seen and ("plain.txt", False) in seen   # the fixture is real
    assert_clean(tbase)


def test_a_busy_job_is_reported_busy_and_nothing_is_touched(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    lock = locks.acquire(runner._job_lock_path(job), "a backup")
    try:
        result = restore_test.restore_test(job)

        assert result["status"] == "busy" and result["sampled"] == 0
        assert "running" in result["message"]
        assert tbase.made == []
        assert "last_restore_test" not in backup_utils.get_job(job["id"])
        assert restore_lines(job) == []
    finally:
        lock.release()
    assert restore_test.restore_test(job)["status"] == "passed"           # control: free, it runs


def test_the_run_lock_is_held_for_the_whole_test_and_released_after(tmp_path, tbase, monkeypatch):
    job, stamp, _ = make_backup(tmp_path)
    during = []
    real = restore_test._compare

    def spy(*args, **kwargs):
        during.append(locks.is_held(runner._job_lock_path(job)))
        return real(*args, **kwargs)

    monkeypatch.setattr(restore_test, "_compare", spy)

    restore_test.restore_test(job)

    assert during == [True] and not locks.is_held(runner._job_lock_path(job))


@windows_only
def test_a_deep_path_a_trailing_dot_and_unicode_names_are_tested(tmp_path, tbase):
    source = tmp_path / "src"
    deep = _deep_file(source, 280)
    _write(deep, b"deep content")
    _write(source / "notes.", b"trailing dot")
    _write(source / "résumé-日本.txt", b"unicode name")
    _write(source / "plain.txt", b"plain")
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Odd names", str(source), str(target), schedule="manual")
    assert backup_utils.run_job(job)["ok"]

    result = restore_test.restore_test(job)

    assert result["status"] == "passed" and result["sampled"] == 4
    assert_clean(tbase)
    # Positive control: the deep file really is in the sample and really is checked.
    flip_byte(os.path.join(backup_utils.version_path(job, backup_utils.latest_version(job)),
                           *os.path.relpath(deep, source).split(os.sep)), 0)
    again = restore_test.restore_test(job)
    assert again["status"] == "failed" and again["failed"] == 1
    assert again["failed_sample"][0].endswith("f.bin") and len(again["failed_sample"][0]) > 100


# ---------------------------------------------------------------------------
# What is stored on the job
# ---------------------------------------------------------------------------

def test_the_stored_field_has_exactly_the_documented_shape(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    flip_byte(stored(job, stamp, "nested/b.txt"), 0)

    result = restore_test.restore_test(job)

    field = backup_utils.get_job(job["id"])["last_restore_test"]
    assert list(field) == ["time", "version", "status", "sampled", "checked", "failed",
                           "failed_sample", "reason"]
    assert field["status"] == "failed" and field["version"] == stamp
    assert (field["sampled"], field["checked"], field["failed"]) == (5, 4, 1)
    assert field["failed_sample"] == ["nested/b.txt"] and field["reason"] == ""
    datetime.fromisoformat(field["time"])
    assert result["stored"] is True


def test_cannot_test_is_stored_with_its_reason_and_busy_and_cancelled_are_not(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    restore_test.restore_test(job)                           # something is stored to protect
    field = backup_utils.get_job(job["id"])["last_restore_test"]
    assert field["status"] == "passed"

    lock = locks.acquire(runner._job_lock_path(job), "a backup")
    try:
        assert restore_test.restore_test(job)["status"] == "busy"
    finally:
        lock.release()
    assert restore_test.restore_test(job, should_abort=lambda: True)["status"] == "cancelled"
    assert backup_utils.get_job(job["id"])["last_restore_test"] == field   # neither overwrote it

    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(backup_utils.manifest_path(job, stamp))
    restore_test.restore_test(job, version=stamp)
    field = backup_utils.get_job(job["id"])["last_restore_test"]
    assert field["status"] == "cannot_test" and field["reason"] == "no manifest"


def test_a_job_that_is_not_in_the_jobs_file_is_tested_but_nothing_is_stored(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)           # never saved

    result = restore_test.restore_test(job)

    assert result["status"] == "passed" and result["stored"] is False


def test_a_stale_settings_save_and_a_run_record_do_not_roll_the_result_back(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    restore_test.restore_test(job)                           # first result: passed
    stale = backup_utils.get_job(job["id"])                  # an edit form loaded now...
    assert stale["last_restore_test"]["status"] == "passed"
    flip_byte(stored(job, stamp, "a.txt"), 0)
    restore_test.restore_test(job)                           # ...and a newer result lands: failed
    assert "last_restore_test" not in jobs_store.SETTINGS_KEYS

    stale["name"] = "Renamed"
    backup_utils.save_job_settings(stale)

    after = backup_utils.get_job(job["id"])
    assert after["name"] == "Renamed" and after["last_restore_test"]["status"] == "failed"

    backup_utils.record_run(job["id"], {"ok": True, "files": 5, "message": "m"})
    assert backup_utils.get_job(job["id"])["last_restore_test"]["status"] == "failed"


def test_recording_a_result_changes_no_other_field(tmp_path):
    job = backup_utils.new_job("Fields", str(tmp_path / "s"), str(tmp_path / "d"), schedule="manual")
    job.update(last_run="2026-11-20T01:00:00", last_result="ok_skipped", extra_future_key=[1, 2])
    backup_utils.save_job(job)
    before = backup_utils.get_job(job["id"])

    backup_utils.record_restore_test(job["id"], {"status": "passed", "sampled": 3, "checked": 3})

    after = backup_utils.get_job(job["id"])
    assert {k: v for k, v in after.items() if k != "last_restore_test"} == before
    assert backup_utils.record_restore_test("nosuchid", {"status": "passed"}) is None
    assert backup_utils.record_restore_test(job["id"], {"status": "busy"}) is None
    assert backup_utils.get_job(job["id"]) == after


@pytest.mark.parametrize("junk", [None, 42, "passed", [], ["passed"], {}, {"status": "bogus"},
                                  {"status": None}, {"status": ["passed"]}, True])
def test_junk_reads_as_absent_and_never_raises(junk):
    assert backup_utils.clean_restore_test(junk) is None
    job = {"id": "j", "last_restore_test": junk, "last_result": "ok"}
    assert backup_utils.job_restore_test(job) is None
    assert backup_utils.job_result(job) == "ok" and backup_utils.result_is_ok(job["last_result"])


def test_junk_fields_are_coerced_and_the_name_list_is_capped_at_read_time():
    cleaned = backup_utils.clean_restore_test({
        "status": "failed", "time": "yesterday", "version": "not a stamp", "sampled": "12",
        "checked": -3, "failed": True, "failed_sample": [f"n{i}" for i in range(50)] + [7, None],
        "reason": "x" * 5000})

    assert cleaned["time"] == "" and cleaned["version"] == ""
    assert (cleaned["sampled"], cleaned["checked"], cleaned["failed"]) == (0, 0, 0)
    assert cleaned["failed_sample"] == [f"n{i}" for i in range(5)]
    assert len(cleaned["reason"]) == 260
    assert backup_utils.clean_restore_test({"status": "passed", "failed_sample": "oops"}
                                           )["failed_sample"] == []


# ---------------------------------------------------------------------------
# The run log
# ---------------------------------------------------------------------------

def test_one_log_line_per_test_with_counts_and_no_absolute_path(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    flip_byte(stored(job, stamp, "nested/b.txt"), 0)
    before = len(read_log(job)[1])                                   # the fixture's own backup line

    restore_test.restore_test(job)
    text, lines = read_log(job)

    assert len(lines) == before + 1                                  # exactly one more line
    line = lines[-1]
    assert tuple(line) == runlog.LINE_KEYS + runlog.RESTORE_LINE_KEYS
    assert line["outcome"] == "restore_test_failed" and line["trigger"] == "manual"
    assert (line["restore_sampled"], line["restore_checked"], line["restore_failed"]) == (5, 4, 1)
    assert line["restore_failed_paths"] == ["nested/b.txt"] and line["version"] == stamp
    assert line["message"] == "Test restore FOUND A PROBLEM: nested/b.txt did not match."
    assert (line["files"], line["skipped"], line["pruned"]) == (0, 0, 0)
    assert lines[0]["outcome"] == "ok" and tuple(lines[0]) == runlog.LINE_KEYS   # unchanged
    assert_no_paths(json.dumps(line), tmp_path, tbase)       # the backup's own line names its target


def test_passed_and_skipped_tests_log_their_own_outcome(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)

    restore_test.restore_test(job)
    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(backup_utils.manifest_path(job, stamp))
    restore_test.restore_test(job, version=stamp)
    text, lines = read_log(job)
    lines = [line for line in lines if line["outcome"].startswith("restore_test")]

    assert [line["outcome"] for line in lines] == ["restore_test_passed", "restore_test_skipped"]
    assert lines[0]["restore_checked"] == 5 and lines[0]["restore_failed_paths"] == []
    assert lines[1]["message"] == "Test restore could not be run: no manifest."
    assert set("restore_test_passed restore_test_failed restore_test_skipped".split()) <= set(runlog.OUTCOMES)
    assert_no_paths(json.dumps(lines), tmp_path, tbase)


def test_a_busy_or_cancelled_test_writes_no_line_and_the_backup_outcomes_are_unchanged(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    lock = locks.acquire(runner._job_lock_path(job), "a backup")
    try:
        restore_test.restore_test(job)
    finally:
        lock.release()
    restore_test.restore_test(job, should_abort=lambda: True)

    assert restore_lines(job) == []
    for result, word in (({"ok": True}, "ok"), ({"ok": True, "not_copied": 2}, "ok_skipped"),
                         ({"ok": False}, "failed"), ({"busy": True}, "busy")):
        assert backup_utils.outcome_word(result) == word


def test_log_false_and_store_false_leave_no_trace(tmp_path, tbase):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)

    result = restore_test.restore_test(job, log=False, store=False)

    assert result["status"] == "passed" and result["stored"] is False
    assert restore_lines(job) == []
    assert "last_restore_test" not in backup_utils.get_job(job["id"])


# ---------------------------------------------------------------------------
# The automatic run after a backup
# ---------------------------------------------------------------------------

def auto_job(tmp_path, files=None, **options):
    """A SAVED job (an unsaved one is never tested automatically) with nothing run."""
    files = files or {"a.txt": b"alpha", "nested/b.txt": b"bravo bravo", "c.bin": b"\x01" * 80}
    source, target = tmp_path / "src", tmp_path / "dst"
    for rel, data in files.items():
        (source / rel).parent.mkdir(parents=True, exist_ok=True)
        (source / rel).write_bytes(data)
    target.mkdir(exist_ok=True)
    job = backup_utils.new_job("Auto", str(source), str(target), schedule="manual", **options)
    backup_utils.save_job(job)
    return job


def test_the_first_successful_backup_runs_a_test_and_says_so(tmp_path, tbase):
    job = auto_job(tmp_path)

    result = backup_utils.run_job(job)

    assert result["ok"] is True and result["verified"] is True
    assert result["message"].endswith(" Test restore passed: 3 files restored and checked.")
    assert result["restore_test"]["status"] == "passed" and result["restore_test"]["sampled"] == 3
    assert "restore_test_log" not in result
    field = backup_utils.get_job(job["id"])["last_restore_test"]
    assert field["status"] == "passed" and field["version"] == result["version"]
    assert_clean(tbase)
    outcomes = [(line["outcome"], line["trigger"]) for line in runlog.read_runs(job["id"])]
    assert outcomes == [("restore_test_passed", "auto"), ("ok", "manual")]   # newest first


def test_the_backup_result_is_otherwise_what_it_was_without_the_test(tmp_path, tbase):
    saved = backup_utils.run_job(auto_job(tmp_path / "a"))
    plain_job = backup_utils.new_job("Auto", str(tmp_path / "a" / "src"),
                                     str(tmp_path / "plain-dst"), schedule="manual")
    (tmp_path / "plain-dst").mkdir()
    plain = backup_utils.run_job(plain_job)                 # unsaved: no test, the old behaviour

    assert "restore_test" not in plain and plain["message"].count("Test restore") == 0
    differing = {key for key in saved if saved[key] != plain.get(key)}
    assert {"message", "restore_test"} <= differing <= {
        "message", "version", "manifest", "finished_utc", "restore_test"}
    assert saved["message"].startswith(plain["message"].split(" to ")[0])
    assert (saved["ok"], saved["files"], saved["verified"]) == (plain["ok"], plain["files"], plain["verified"])


def test_a_passed_test_is_not_repeated_on_later_backups(tmp_path, tbase, monkeypatch):
    job = auto_job(tmp_path)
    calls = []
    real = restore_test._run_test
    monkeypatch.setattr(restore_test, "_run_test", lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    first = backup_utils.run_job(job)
    second = backup_utils.run_job(job)
    third = backup_utils.run_job(job)

    assert len(calls) == 1 and second["ok"] and third["ok"]
    assert "Test restore" in first["message"] and "Test restore" not in second["message"]
    assert "restore_test" not in second
    assert sum(1 for line in runlog.read_runs(job["id"]) if line["outcome"].startswith("restore_test")) == 1


def test_no_test_after_a_failed_run_or_a_cancelled_run(tmp_path, tbase, monkeypatch):
    job = auto_job(tmp_path)
    calls = []
    real = restore_test.automatic
    monkeypatch.setattr(runner.restore_test, "automatic",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])

    cancelled = backup_utils.run_job(job, should_abort=lambda: True)
    shutil.rmtree(job["source"])
    failed = backup_utils.run_job(job)

    assert cancelled["ok"] is False and cancelled.get("cancelled") is True
    assert failed["ok"] is False
    for result in (cancelled, failed):                         # not even a "could not be run" sentence
        assert "Test restore" not in result["message"] and "restore_test" not in result
    assert calls == [] and "last_restore_test" not in backup_utils.get_job(job["id"])
    assert tbase.made == []
    # Control: the same job, source back, does call it.
    (Path(job["source"])).mkdir()
    (Path(job["source"]) / "a.txt").write_bytes(b"alpha")
    assert backup_utils.run_job(job)["ok"] and calls == [1]


def test_a_failed_test_is_reported_but_the_backup_stays_ok_and_is_retried_until_it_passes(
        tmp_path, tbase, monkeypatch):
    job = auto_job(tmp_path)
    healthy_copy = copying._hash_copy
    hits = write_side_fault(monkeypatch, name="b.txt")

    first = backup_utils.run_job(job)

    assert hits and first["ok"] is True and first["verified"] is True
    assert first["message"].endswith(" Test restore FOUND A PROBLEM: nested/b.txt did not match.")
    assert first["restore_test"]["failed_sample"] == ["nested/b.txt"]
    assert backup_utils.record_run(job["id"], first)["last_result"] == "ok"
    assert backup_utils.get_job(job["id"])["last_restore_test"]["status"] == "failed"
    assert tbase.made and all(not os.path.lexists(_x(f)) for f in tbase.made)

    monkeypatch.setattr(copying, "_hash_copy", healthy_copy)         # the disk "heals"
    second = backup_utils.run_job(job)
    assert second["message"].endswith(" Test restore passed: 3 files restored and checked.")
    assert backup_utils.get_job(job["id"])["last_restore_test"]["status"] == "passed"
    third = backup_utils.run_job(job)
    assert "Test restore" not in third["message"]
    assert [l["outcome"] for l in reversed(runlog.read_runs(job["id"]))
            if l["outcome"].startswith("restore_test")] == ["restore_test_failed", "restore_test_passed"]


def test_the_backup_screen_shows_a_failed_test_restore_as_a_warning_not_plain_ok(
        tmp_path, tbase, monkeypatch):
    """Review gate: the result line after a run whose automatic test restore FOUND A
    PROBLEM was shown in the calm `Hint` style, the same as a clean backup. The
    real engine result goes through the real tab's `_finish_run`."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    from tabs import backup_tab
    from utils import scheduler
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    job = auto_job(tmp_path)
    healthy_copy = copying._hash_copy
    hits = write_side_fault(monkeypatch, name="b.txt")
    bad = backup_utils.run_job(job)
    assert hits and bad["ok"] is True and bad["restore_test"]["status"] == "failed"

    tab = backup_tab.BackupTab()
    tab._finish_run(job, bad)
    assert "Test restore FOUND A PROBLEM" in tab.backup_status.text()
    assert tab.backup_status.objectName() == "StatusWarn"
    assert tab._backup_notice_warn is True               # and after the remeasure too

    # Control: the same tab, a clean run whose test passed, is the calm style.
    monkeypatch.setattr(copying, "_hash_copy", healthy_copy)
    clean = auto_job(tmp_path / "clean")
    good = backup_utils.run_job(clean)
    assert good["restore_test"]["status"] == "passed"
    tab._finish_run(clean, good)
    assert tab.backup_status.objectName() == "Hint"


def test_a_stored_cannot_test_is_retried_after_the_next_backup(tmp_path, tbase):
    job = auto_job(tmp_path)
    backup_utils.record_restore_test(job["id"], {"status": "cannot_test", "reason": "no manifest"})

    result = backup_utils.run_job(job)

    assert result["restore_test"]["status"] == "passed"
    assert backup_utils.get_job(job["id"])["last_restore_test"]["status"] == "passed"


def test_a_job_with_verification_off_gets_no_automatic_test(tmp_path, tbase):
    job = auto_job(tmp_path, verify="off")

    result = backup_utils.run_job(job)

    assert result["ok"] is True and "restore_test" not in result and tbase.made == []
    assert "last_restore_test" not in backup_utils.get_job(job["id"])


def test_an_exception_in_the_test_never_fails_the_backup(tmp_path, tbase, monkeypatch):
    job = auto_job(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom in the test")

    monkeypatch.setattr(restore_test, "_run_test", boom)

    result = backup_utils.run_job(job)

    assert result["ok"] is True and result["verified"] is True
    assert result["message"].endswith(" Test restore could not be run: RuntimeError: boom in the test.")
    assert "Traceback" not in result["message"]
    assert backup_utils.is_verified(job, result["version"])
    assert "last_restore_test" not in backup_utils.get_job(job["id"])
    assert backup_utils.record_run(job["id"], result)["last_result"] == "ok"


def test_a_cancel_during_the_test_stores_and_says_nothing(tmp_path, tbase, monkeypatch):
    job = auto_job(tmp_path)
    flag = {"stop": False}
    real = restore_test.restore_version

    def stop_then_restore(*args, **kwargs):
        flag["stop"] = True                      # the person presses Cancel as the test starts
        return real(*args, **kwargs)

    monkeypatch.setattr(restore_test, "restore_version", stop_then_restore)

    result = backup_utils.run_job(job, should_abort=lambda: flag["stop"])

    assert result["ok"] is True and "Test restore" not in result["message"]
    assert "restore_test" not in result
    assert "last_restore_test" not in backup_utils.get_job(job["id"])
    assert [l["outcome"] for l in runlog.read_runs(job["id"])] == ["ok"]
    assert_clean(tbase)


def test_the_automatic_sample_skips_a_file_over_1_gib_and_on_demand_never_does(
        tmp_path, tbase, monkeypatch):
    job = auto_job(tmp_path, {"small.txt": b"s", "big.bin": b"b" * 50, "mid.txt": b"m" * 10})
    real_entries = restore_test._trusted_entries

    def inflate(job_, version):
        entries, reason = real_entries(job_, version)
        if entries:
            size, digest = entries["big.bin"]
            entries["big.bin"] = (2 * GiB, digest)       # claims to be 2 GiB; no real giant file
        return entries, reason

    asked = []
    real_restore = restore_test.restore_version
    monkeypatch.setattr(restore_test, "_trusted_entries", inflate)
    monkeypatch.setattr(restore_test, "restore_version",
                        lambda *a, **k: (asked.append(set(k["only"])), real_restore(*a, **k))[1])

    auto = backup_utils.run_job(job)
    on_demand = restore_test.restore_test(job, sample=50)

    assert auto["restore_test"]["includes_largest"] is False
    assert asked[0] == {"small.txt", "mid.txt"}
    assert "big.bin" in asked[1] and on_demand["includes_largest"] is True


def test_the_test_line_follows_the_backups_line_and_neither_carries_the_other_fields(tmp_path, tbase):
    job = auto_job(tmp_path)

    backup_utils.run_job(job)
    text, lines = read_log(job)

    assert [line["outcome"] for line in lines] == ["ok", "restore_test_passed"]   # file order: oldest first
    assert lines[0]["files"] == 3 and lines[1]["files"] == 0 and lines[1]["restore_sampled"] == 3
    assert tuple(lines[0]) == runlog.LINE_KEYS                 # a backup's line is what it always was
    assert tuple(lines[1]) == runlog.LINE_KEYS + runlog.RESTORE_LINE_KEYS
    assert restore_test.TEMP_PREFIX not in text and str(tbase.path) not in text


# ---------------------------------------------------------------------------
# The headless entry points
# ---------------------------------------------------------------------------

@pytest.fixture
def cli():
    """main.py's functions (imports PySide6 but builds no QApplication)."""
    import main
    return main


def test_the_cli_exits_0_for_a_healthy_version_and_stores_the_result(tmp_path, tbase, cli, capsys):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)

    assert cli.test_restore_cli(job["id"]) == 0

    out = capsys.readouterr()
    assert out.out == "[OK] Test restore passed: 5 files restored and checked.\n" and out.err == ""
    assert backup_utils.get_job(job["id"])["last_restore_test"]["status"] == "passed"
    assert [l["outcome"] for l in restore_lines(job)] == ["restore_test_passed"]
    assert_clean(tbase)


def test_the_cli_exits_5_and_names_the_failing_files(tmp_path, tbase, cli, capsys):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    flip_byte(stored(job, stamp, "a.txt"), 0)
    flip_byte(stored(job, stamp, "nested/b.txt"), 0)

    assert cli.test_restore_cli(job["id"]) == 5

    out = capsys.readouterr().out
    assert out.startswith("[PROBLEMS] Test restore FOUND A PROBLEM: a.txt, nested/b.txt did not match.")
    assert "  a.txt: checksum differs" in out and "  nested/b.txt: checksum differs" in out
    assert backup_utils.get_job(job["id"])["last_restore_test"]["failed"] == 2


def test_the_cli_exits_6_when_the_version_cannot_be_tested(tmp_path, tbase, cli, capsys):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(backup_utils.manifest_path(job, stamp))

    assert cli.test_restore_cli(job["id"]) == 6

    assert capsys.readouterr().out.startswith("[CANNOT TEST] Test restore could not be run: ")
    assert backup_utils.get_job(job["id"])["last_restore_test"]["status"] == "cannot_test"


def test_the_cli_exits_2_for_an_unknown_job_and_3_for_a_damaged_jobs_file(tmp_path, cli, capsys):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)

    assert cli.test_restore_cli("nosuchid") == 2
    assert "No backup job with id 'nosuchid'" in capsys.readouterr().out

    Path(jobs_store.JOBS_FILE).write_text("{ not json", encoding="utf-8")
    assert cli.test_restore_cli(job["id"]) == 3
    assert "damaged" in capsys.readouterr().err


def test_the_cli_exits_4_for_a_running_job_and_writes_nothing(tmp_path, tbase, cli, capsys):
    job, stamp, _ = make_backup(tmp_path)
    backup_utils.save_job(job)
    lock = locks.acquire(runner._job_lock_path(job), "a backup")
    try:
        assert cli.test_restore_cli(job["id"]) == 4
    finally:
        lock.release()

    assert "[SKIPPED]" in capsys.readouterr().err
    assert "last_restore_test" not in backup_utils.get_job(job["id"]) and tbase.made == []


def test_the_exit_codes_do_not_collide_and_the_usage_documents_them(cli):
    assert {cli.TEST_RESTORE_PROBLEMS, cli.TEST_RESTORE_CANNOT} == {5, 6}
    used_by_others = {0, 1, 2, 3, 4}
    assert not {cli.TEST_RESTORE_PROBLEMS, cli.TEST_RESTORE_CANNOT} & used_by_others
    usage = cli.HEADLESS_USAGE
    assert "--test-restore <job id>" in usage and "5 problems found" in usage
    assert "6 cannot be" in usage and "4 job is running" in usage


def test_the_headless_backup_warns_on_stderr_when_its_test_restore_fails(
        tmp_path, tbase, cli, capsys, monkeypatch):
    job = auto_job(tmp_path)
    write_side_fault(monkeypatch, name="b.txt")

    assert cli.run_backup_job_cli(job["id"]) == 0          # the backup itself succeeded

    out = capsys.readouterr()
    assert "[WARN] Test restore found a problem: nested/b.txt" in out.err
    assert "[OK] Backed up 3 file(s)" in out.out and "FOUND A PROBLEM" in out.out


def test_a_clean_headless_backup_prints_no_test_restore_warning(tmp_path, tbase, cli, capsys):
    job = auto_job(tmp_path)

    assert cli.run_backup_job_cli(job["id"]) == 0

    out = capsys.readouterr()
    assert "Test restore found a problem" not in out.err and "Test restore passed" in out.out


def test_the_real_cli_passes_then_exits_5_for_a_corrupted_stored_file(tmp_path):
    """End to end: `python main.py --test-restore <id>` in its own process, with
    APPDATA and the temp folder pointed at fresh folders."""
    appdata = tmp_path / "appdata"
    data = app_data_dir(appdata)
    data.mkdir(parents=True)
    child_temp = tmp_path / "childtemp"
    child_temp.mkdir()
    job, stamp, _ = make_backup(tmp_path)
    (data / "backup_jobs.json").write_text(json.dumps([job]), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata), USERPROFILE=str(appdata),
               TEMP=str(child_temp), TMP=str(child_temp), TMPDIR=str(child_temp),
               QT_QPA_PLATFORM="offscreen")

    def run(*args):
        return subprocess.run([sys.executable, str(REPO / "main.py"), *args], cwd=str(REPO),
                              env=env, capture_output=True, text=True, timeout=180)

    good = run("--test-restore", job["id"])
    assert good.returncode == 0, (good.stdout, good.stderr)
    assert "[OK] Test restore passed: 5 files restored and checked." in good.stdout
    stored_job = json.loads((data / "backup_jobs.json").read_text(encoding="utf-8"))[0]
    assert stored_job["last_restore_test"]["status"] == "passed"
    assert list(child_temp.iterdir()) == []

    flip_byte(stored(job, stamp, "nested/b.txt"), 1)
    bad = run("--test-restore", job["id"])
    assert bad.returncode == 5, (bad.stdout, bad.stderr)
    assert "[PROBLEMS] Test restore FOUND A PROBLEM: nested/b.txt did not match." in bad.stdout
    stored_job = json.loads((data / "backup_jobs.json").read_text(encoding="utf-8"))[0]
    assert stored_job["last_restore_test"]["failed_sample"] == ["nested/b.txt"]
    assert list(child_temp.iterdir()) == []
    lines = [json.loads(l) for l in (data / "logs" / f"{job['id']}.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [l["outcome"] for l in lines] == ["restore_test_passed", "restore_test_failed"]

    usage = run("--test-restore")
    assert usage.returncode == 2 and "--test-restore <job id>" in usage.stdout
