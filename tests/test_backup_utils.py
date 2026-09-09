# tests/test_backup_utils.py
# Coverage for utils/backup_utils.py -- job storage, the versioned copy,
# retention pruning, the changes-since comparison, restore, and the schtasks
# argument builder.
#
# Its own file rather than an extension of test_disk_utils.py: that suite is
# already 700 lines about scanning and hashing, and this module is about
# writing files somewhere else. Same convention as tests/test_presets.py for
# the storage half -- JOBS_FILE is monkeypatched to tmp_path so a test run
# never touches the real user's backup_jobs.json.
#
# The schtasks calls are tested through `schtasks_args`, which builds the
# argv without running it. Registering a real scheduled task from a test
# would leave one behind on the machine running the suite.
import json
import os
import time

import pytest

from utils import backup_utils


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_utils, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def tree(tmp_path):
    """A small source tree, an empty target, and a job wired to both."""
    source = tmp_path / "src"
    (source / "nested").mkdir(parents=True)
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "nested" / "b.txt").write_text("beta", encoding="utf-8")
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Test job", str(source), str(target),
                               schedule="manual", retention=2)
    return job, source, target


# ---------------------------------------------------------------------------
# Job storage
# ---------------------------------------------------------------------------

def test_load_jobs_is_empty_on_first_run():
    assert backup_utils.load_jobs() == []


def test_new_job_gets_an_id_and_normalises_an_unknown_schedule():
    job = backup_utils.new_job("n", "s", "t", schedule="hourly")
    assert job["id"]
    assert job["schedule"] == "manual"


def test_save_job_inserts_then_replaces_by_id():
    job = backup_utils.new_job("first", "s", "t")
    backup_utils.save_job(job)
    assert [j["name"] for j in backup_utils.load_jobs()] == ["first"]

    job["name"] = "renamed"
    backup_utils.save_job(job)
    stored = backup_utils.load_jobs()
    assert len(stored) == 1
    assert stored[0]["name"] == "renamed"


def test_get_and_remove_job():
    job = backup_utils.new_job("n", "s", "t")
    backup_utils.save_job(job)
    assert backup_utils.get_job(job["id"])["name"] == "n"
    backup_utils.remove_job(job["id"])
    assert backup_utils.get_job(job["id"]) is None


def test_load_jobs_survives_corrupt_json():
    with open(backup_utils.JOBS_FILE, "w", encoding="utf-8") as handle:
        handle.write("{not a list")
    assert backup_utils.load_jobs() == []


def test_load_jobs_rejects_a_non_list_document():
    with open(backup_utils.JOBS_FILE, "w", encoding="utf-8") as handle:
        json.dump({"jobs": []}, handle)
    assert backup_utils.load_jobs() == []


# ---------------------------------------------------------------------------
# Running a job
# ---------------------------------------------------------------------------

def test_run_job_writes_a_versioned_copy(tree):
    job, source, target = tree
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["files"] == 2
    version = target / job["id"] / result["version"]
    assert (version / "a.txt").read_text(encoding="utf-8") == "alpha"
    assert (version / "nested" / "b.txt").read_text(encoding="utf-8") == "beta"


def test_run_job_reports_progress_for_every_file(tree):
    job, _, _ = tree
    seen = []
    backup_utils.run_job(job, on_progress=lambda done, total: seen.append((done, total)))
    assert seen == [(1, 2), (2, 2)]


def test_two_runs_make_two_versions(tree):
    job, _, _ = tree
    first = backup_utils.run_job(job)["version"]
    # The stamp is per-second, so a second run inside the same second would
    # land on the same directory name.
    time.sleep(1.1)
    second = backup_utils.run_job(job)["version"]
    assert first != second
    assert backup_utils.version_names(job) == sorted([first, second])
    assert backup_utils.latest_version(job) == second


def test_run_job_leaves_no_partial_directory_behind(tree):
    job, _, target = tree
    backup_utils.run_job(job)
    names = os.listdir(target / job["id"])
    assert not [n for n in names if n.endswith(".partial")]


def test_run_job_reports_a_missing_source_without_raising(tmp_path):
    job = backup_utils.new_job("gone", str(tmp_path / "nope"), str(tmp_path))
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "Source folder not found" in result["message"]


def test_run_job_reports_an_unwritable_target_without_raising(tree, monkeypatch):
    job, _, _ = tree

    def boom(*args, **kwargs):
        raise OSError("device not ready")

    monkeypatch.setattr(backup_utils.os, "makedirs", boom)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "not writable" in result["message"]


def test_run_job_with_no_target_set_is_reported(tmp_path):
    job = backup_utils.new_job("n", str(tmp_path), "")
    assert backup_utils.run_job(job)["ok"] is False


def test_abort_mid_copy_removes_the_partial_and_reports_cancelled(tree):
    job, _, target = tree
    result = backup_utils.run_job(job, should_abort=lambda: True)
    assert result["ok"] is False
    assert result["message"] == "Backup cancelled"
    assert backup_utils.version_names(job) == []
    assert os.listdir(target / job["id"]) == []


def test_record_run_writes_the_outcome_onto_the_stored_job(tree):
    job, _, _ = tree
    backup_utils.save_job(job)
    backup_utils.record_run(job["id"], {"ok": True, "message": "done"})
    stored = backup_utils.get_job(job["id"])
    assert stored["last_run"]
    assert stored["last_result"] == "ok"


def test_record_run_marks_a_failure(tree):
    job, _, _ = tree
    backup_utils.save_job(job)
    backup_utils.record_run(job["id"], {"ok": False, "message": "no drive"})
    assert backup_utils.get_job(job["id"])["last_result"].startswith("failed")


def test_record_run_on_an_unknown_job_is_none():
    assert backup_utils.record_run("nosuchid", {"ok": True}) is None


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def _fake_versions(job, *names):
    for name in names:
        path = os.path.join(backup_utils.job_root(job), name)
        os.makedirs(path, exist_ok=True)


def test_prune_keeps_only_the_newest_n_versions(tree):
    job, _, _ = tree
    job["retention"] = 2
    _fake_versions(job, "20260101-000000", "20260102-000000",
                   "20260103-000000", "20260104-000000")
    removed = backup_utils.prune_versions(job)
    assert removed == ["20260101-000000", "20260102-000000"]
    assert backup_utils.version_names(job) == ["20260103-000000", "20260104-000000"]


def test_prune_keeps_at_least_one_version_however_low_retention_goes(tree):
    job, _, _ = tree
    job["retention"] = 0
    _fake_versions(job, "20260101-000000", "20260102-000000")
    backup_utils.prune_versions(job)
    assert backup_utils.version_names(job) == ["20260102-000000"]


def test_version_names_ignores_anything_not_a_timestamp(tree):
    job, _, _ = tree
    _fake_versions(job, "20260101-000000", "20260101-000000.partial", "notes")
    assert backup_utils.version_names(job) == ["20260101-000000"]


def test_version_names_is_empty_when_the_target_does_not_exist(tree):
    job, _, _ = tree
    job["target"] = os.path.join(job["target"], "never", "made")
    assert backup_utils.version_names(job) == []
    assert backup_utils.latest_version(job) is None


def test_version_datetime_parses_a_stamp_and_rejects_junk():
    assert backup_utils.version_datetime("20260101-123456").year == 2026
    assert backup_utils.version_datetime("nope") is None


# ---------------------------------------------------------------------------
# Changes since the last backup
# ---------------------------------------------------------------------------

def test_changes_since_reports_nothing_before_a_first_backup(tree):
    job, _, _ = tree
    changes = backup_utils.changes_since(job)
    assert changes["ok"] is False
    assert changes["message"] == "Never backed up"


def test_changes_since_is_zero_immediately_after_a_backup(tree):
    job, _, _ = tree
    backup_utils.run_job(job)
    changes = backup_utils.changes_since(job)
    assert changes["ok"] is True
    assert changes["changed"] == 0
    assert changes["message"] == "No changes since the last backup"


def test_changes_since_counts_a_modified_file(tree):
    job, source, _ = tree
    backup_utils.run_job(job)
    # A different length as well as a different mtime: the comparison tests
    # both, and a test that only moved the clock would not prove the size
    # half works.
    (source / "a.txt").write_text("alpha edited", encoding="utf-8")
    changes = backup_utils.changes_since(job)
    assert changes["modified"] == 1
    assert changes["changed"] == 1
    assert changes["bytes"] == len("alpha edited")


def test_changes_since_counts_an_added_and_a_removed_file(tree):
    job, source, _ = tree
    backup_utils.run_job(job)
    (source / "c.txt").write_text("gamma", encoding="utf-8")
    (source / "a.txt").unlink()
    changes = backup_utils.changes_since(job)
    assert (changes["added"], changes["removed"], changes["modified"]) == (1, 1, 0)
    assert changes["changed"] == 2


def test_changes_since_reports_a_missing_source(tmp_path):
    job = backup_utils.new_job("n", str(tmp_path / "gone"), str(tmp_path))
    assert backup_utils.changes_since(job)["ok"] is False


def test_changes_since_carries_the_version_it_compared_against(tree):
    job, _, _ = tree
    version = backup_utils.run_job(job)["version"]
    changes = backup_utils.changes_since(job)
    assert changes["version"] == version
    assert changes["when"]


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def test_restore_writes_into_a_new_named_folder_and_never_over_the_source(tree, tmp_path):
    job, source, _ = tree
    version = backup_utils.run_job(job)["version"]
    (source / "a.txt").write_text("edited since the backup", encoding="utf-8")

    destination = tmp_path / "restored"
    destination.mkdir()
    result = backup_utils.restore_version(job, version, str(destination))

    assert result["ok"], result["message"]
    folder = os.path.join(str(destination), f"Test_job-restored-{version}")
    assert result["path"] == folder
    assert open(os.path.join(folder, "a.txt"), encoding="utf-8").read() == "alpha"
    # The live source is untouched -- restoring is a copy to a new place.
    assert (source / "a.txt").read_text(encoding="utf-8") == "edited since the backup"


def test_restore_of_an_unknown_version_is_reported(tree, tmp_path):
    job, _, _ = tree
    result = backup_utils.restore_version(job, "20200101-000000", str(tmp_path))
    assert result["ok"] is False
    assert "not found" in result["message"]


def test_restore_cancels_cleanly(tree, tmp_path):
    job, _, _ = tree
    version = backup_utils.run_job(job)["version"]
    result = backup_utils.restore_version(job, version, str(tmp_path),
                                          should_abort=lambda: True)
    assert result["ok"] is False
    assert result["message"] == "Restore cancelled"


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------

def test_walk_tree_returns_every_file_by_relative_path(tree):
    _, source, _ = tree
    files, skipped = backup_utils.walk_tree(str(source))
    assert set(files) == {"a.txt", "nested/b.txt"}
    assert skipped == 0


def test_walk_tree_of_a_missing_directory_counts_it_as_skipped(tmp_path):
    files, skipped = backup_utils.walk_tree(str(tmp_path / "nope"))
    assert files == {}
    assert skipped == 1


# ---------------------------------------------------------------------------
# Windows Task Scheduler
# ---------------------------------------------------------------------------

def test_task_name_is_prefixed_and_keyed_on_the_job_id():
    assert backup_utils.task_name("abc123") == "UtilityTool_Backup_abc123"


def test_job_command_invokes_the_headless_cli_flag():
    command = backup_utils.job_command("abc123")
    assert "--run-backup-job abc123" in command
    assert command.startswith('"')  # the interpreter path is quoted


def test_schtasks_args_for_a_daily_job():
    job = backup_utils.new_job("n", "s", "t", schedule="daily", at="07:30")
    args = backup_utils.schtasks_args(job)
    assert args[:2] == ["schtasks", "/create"]
    assert args[args.index("/sc") + 1] == "DAILY"
    assert args[args.index("/st") + 1] == "07:30"
    assert args[args.index("/tn") + 1] == backup_utils.task_name(job["id"])
    assert "/f" in args  # replace an existing task rather than failing


def test_schtasks_args_for_a_weekly_job_names_a_day():
    job = backup_utils.new_job("n", "s", "t", schedule="weekly")
    args = backup_utils.schtasks_args(job)
    assert args[args.index("/sc") + 1] == "WEEKLY"
    assert args[args.index("/d") + 1] == "MON"


def test_schtasks_args_is_none_for_a_manual_job():
    job = backup_utils.new_job("n", "s", "t", schedule="manual")
    assert backup_utils.schtasks_args(job) is None


def test_register_task_removes_any_task_for_a_job_switched_to_manual(monkeypatch):
    removed = []
    monkeypatch.setattr(backup_utils, "unregister_task",
                        lambda job_id: removed.append(job_id) or (True, ""))
    job = backup_utils.new_job("n", "s", "t", schedule="manual")
    ok, message = backup_utils.register_task(job)
    assert ok is True
    assert removed == [job["id"]]
    assert "Run Now" in message


def test_schedule_label_reads_for_each_schedule():
    daily = backup_utils.new_job("n", "s", "t", schedule="daily", at="06:00")
    weekly = backup_utils.new_job("n", "s", "t", schedule="weekly", at="06:00")
    manual = backup_utils.new_job("n", "s", "t", schedule="manual")
    assert backup_utils.schedule_label(daily) == "Daily at 06:00"
    assert backup_utils.schedule_label(weekly) == "Weekly, Mon at 06:00"
    assert backup_utils.schedule_label(manual) == "Manual only"
