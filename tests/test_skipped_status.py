# tests/test_skipped_status.py
# Card 50: a run that skipped files is stored as "ok" with a skipped COUNT and
# a few NAMES beside it, so the screen can say "OK, N skipped" instead of
# plain OK. Faults are injected at the source read (`copying.open`) or at
# `os.scandir`, the same way tests/test_target_failures.py does.
import errno
import json
import os

import pytest

from utils import backup_utils, copying, jobs_store
from utils.fs_utils import plain_path


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def job(tmp_path):
    """A saved job over 20 top-level files and a `sub` folder of 4."""
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    for i in range(20):
        (source / f"file{i}.txt").write_text(f"secret contents {i}")
    for i in range(4):
        (source / "sub" / f"deep{i}.txt").write_text(f"deep secret {i}")
    target = tmp_path / "dst"
    target.mkdir()
    made = backup_utils.new_job("Skips", str(source), str(target),
                                schedule="manual", retention=3)
    backup_utils.save_job(made)
    return made


def lock_files(monkeypatch, names):
    """Make opening any source file called one of `names` fail for reading."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) in names:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)


def run_and_record(job):
    result = backup_utils.run_job(job)
    backup_utils.record_run(job["id"], result)
    return result, backup_utils.get_job(job["id"])


def test_a_run_with_one_unreadable_file_is_ok_and_records_one_skipped(job, monkeypatch):
    lock_files(monkeypatch, {"file3.txt"})
    result, stored = run_and_record(job)

    assert result["ok"]
    assert stored["last_result"] == "ok"            # existing readers keep working
    assert stored["last_skipped"] == 1
    assert stored["last_skipped_sample"] == ["file3.txt"]
    assert backup_utils.job_skipped(stored) == 1


def test_the_stored_count_is_the_one_the_run_message_uses(job, monkeypatch):
    lock_files(monkeypatch, {"file1.txt", "deep2.txt"})
    result, stored = run_and_record(job)

    assert stored["last_skipped"] == result["not_copied"] == 2
    assert f"{stored['last_skipped']} file(s) could not be read" in result["message"]
    assert sorted(stored["last_skipped_sample"]) == ["deep2.txt", "file1.txt"]


def test_a_clean_run_stores_zero_skipped(job):
    _, stored = run_and_record(job)

    assert stored["last_result"] == "ok"
    assert stored["last_skipped"] == 0
    assert stored["last_skipped_sample"] == []
    assert backup_utils.job_skipped(stored) == 0


def test_a_clean_run_clears_an_earlier_runs_skips(job, monkeypatch):
    with monkeypatch.context() as patch:
        lock_files(patch, {"file0.txt"})
        _, first = run_and_record(job)
    assert first["last_skipped"] == 1

    _, second = run_and_record(job)
    assert second["last_skipped"] == 0 and second["last_skipped_sample"] == []


def test_a_failed_run_stores_zero_skipped_even_with_skip_data_in_the_result(job):
    stored = backup_utils.record_run(job["id"], {
        "ok": False, "message": "drive gone", "errors": ["a.txt: denied"],
        "not_copied": 7})

    assert stored["last_result"] == "failed: drive gone"
    assert stored["last_skipped"] == 0
    assert stored["last_skipped_sample"] == []


def test_a_failed_run_after_a_skipping_one_resets_the_count(job, monkeypatch):
    lock_files(monkeypatch, {"file0.txt"})
    run_and_record(job)
    stored = backup_utils.record_run(job["id"], {"ok": False, "message": "boom"})
    assert stored["last_skipped"] == 0


def test_an_unlistable_folder_counts_its_files_not_one(job, monkeypatch):
    real_scandir = os.scandir
    bad = os.path.normcase(os.path.abspath(os.path.join(job["source"], "sub")))
    listings = []

    def fake_scandir(path="."):
        # `sub` is listed three times: pre-flight's count, the copy's own
        # count, then the copy itself. The two counts must see its 4 files;
        # only the copy's listing (the third) fails.
        if os.path.normcase(os.path.abspath(plain_path(str(path)))) == bad:
            listings.append(path)
            if len(listings) > 2:
                raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    result, stored = run_and_record(job)

    assert result["ok"] and len(result["errors"]) == 1   # one error entry...
    assert stored["last_skipped"] == 4                   # ...four files
    assert stored["last_skipped_sample"] == ["sub"]


def test_the_sample_is_capped_at_five_names(job, monkeypatch):
    names = {f"file{i}.txt" for i in range(6)}
    lock_files(monkeypatch, names)
    result, stored = run_and_record(job)

    assert result["ok"]
    assert stored["last_skipped"] == 6
    assert len(stored["last_skipped_sample"]) == jobs_store.SKIPPED_SAMPLE_MAX == 5


def test_an_old_jobs_file_without_the_field_reads_as_zero(tmp_path):
    old = {"id": "abc12345", "name": "Old", "source": "s", "target": "t",
           "last_run": "2026-01-01T00:00:00", "last_result": "ok"}
    with open(jobs_store.JOBS_FILE, "w", encoding="utf-8") as handle:
        json.dump([old], handle)

    loaded = backup_utils.get_job("abc12345")
    assert backup_utils.job_skipped(loaded) == 0
    assert backup_utils.job_skipped_sample(loaded) == []


@pytest.mark.parametrize("junk", ["3", -2, None, True, 1.5, [1]])
def test_a_hand_edited_count_never_crashes_the_reader(junk):
    assert backup_utils.job_skipped({"last_skipped": junk}) == 0


def test_record_run_stores_names_and_counts_never_contents_or_error_text(job, monkeypatch):
    lock_files(monkeypatch, {"file2.txt"})
    _, stored = run_and_record(job)

    on_disk = open(jobs_store.JOBS_FILE, encoding="utf-8").read()
    assert "secret contents" not in on_disk and "deep secret" not in on_disk
    assert "Permission denied" not in on_disk       # OS text (and its path) stay out
    assert stored["last_skipped_sample"] == ["file2.txt"]
    assert all(isinstance(n, str) and os.sep not in n for n in stored["last_skipped_sample"])
