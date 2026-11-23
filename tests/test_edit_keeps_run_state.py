# tests/test_edit_keeps_run_state.py
# The Backup screen loads its jobs once and the job form edits that copy. Saved
# whole, a copy loaded while a (scheduled) run was going wrote the run's start
# marker back after the run had finished: the job then read "Interrupted" and
# the run log gained a false line. A copy older than a release undid it. The
# form now saves only its own fields (`jobs_store.save_job_settings`).
import os
import subprocess
import sys

import pytest

from utils import backup_utils, jobs_store, scheduler


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


def dead_pid():
    """The pid of a process that has exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@pytest.fixture
def stale_row(tmp_path):
    """A job as the screen loaded it mid-run (marker of a now-dead process),
    and the stored job after that run recorded its "ok"."""
    job = backup_utils.new_job("Edit", str(tmp_path / "src"), str(tmp_path / "dst"),
                               schedule="manual")
    job["running"] = {"since": "2026-11-20T20:00:00", "pid": dead_pid(),
                      "start_time": None, "stamp": "20261120-200000", "token": "t"}
    backup_utils.save_job(job)
    row = dict(job)                                   # what the screen holds
    backup_utils.record_run(job["id"], {"ok": True, "files": 3})
    return row


def test_a_stale_row_saved_from_the_form_does_not_bring_back_a_finished_run(stale_row):
    edited = dict(stale_row, name="Renamed")
    backup_utils.save_job_settings(edited)

    stored = backup_utils.get_job(stale_row["id"])
    assert stored["name"] == "Renamed"
    assert "running" not in stored and stored["last_result"] == "ok"
    assert backup_utils.job_interrupted(stored) is None
    assert backup_utils.settle_all_interrupted(backup_utils.load_jobs()) is False


def test_a_stale_row_does_not_undo_a_release(tmp_path):
    job = backup_utils.new_job("Held", str(tmp_path / "src"), str(tmp_path / "dst"),
                               schedule="manual")
    job.update({"last_pruning_held": True, "last_baseline_files": 10, "last_files": 6})
    backup_utils.save_job(job)
    row = dict(job)
    release = {"stamp": "20261120-200000", "files": 10, "level": 6,
               "since": "20261121-200000", "time": "2026-11-22T09:00:00"}
    with jobs_store._jobs_file_lock():                # what release_hold writes
        stored = jobs_store.get_job(job["id"])
        stored.update(hold_released=release, last_pruning_held=False, last_baseline_files=0)
        jobs_store._save_job(stored)

    backup_utils.save_job_settings(dict(row, retention=7))

    stored = backup_utils.get_job(job["id"])
    assert stored["retention"] == 7
    assert stored["hold_released"] == release and stored["last_pruning_held"] is False


def test_every_form_field_is_saved_and_a_new_job_is_saved_whole(tmp_path):
    job = backup_utils.new_job("New", str(tmp_path / "a"), str(tmp_path / "b"),
                               schedule="manual")
    backup_utils.save_job_settings(job)
    assert backup_utils.get_job(job["id"]) == job

    changed = {"name": "N2", "source": str(tmp_path / "c"), "target": str(tmp_path / "d"),
               "schedule": "daily", "at": "07:15", "retention": 3,
               "target_volume": "1234", "verify": "quick"}
    assert set(changed) == set(backup_utils.SETTINGS_KEYS)
    backup_utils.save_job_settings(dict(job, **changed, last_result="failed: stale"))
    stored = backup_utils.get_job(job["id"])
    assert {key: stored[key] for key in changed} == changed
    assert stored["last_result"] == ""                # not taken from the caller


def test_the_job_form_save_goes_through_the_settings_only_path(stale_row, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from tabs import backup_tab

    def no_scheduler(*args, **kwargs):
        raise AssertionError("the real scheduler must not run in a test")
    monkeypatch.setattr(scheduler, "_run", no_scheduler)
    monkeypatch.setattr(backup_utils, "sync_task", lambda job: (True, ""))
    tab = backup_tab.BackupTab()
    monkeypatch.setattr(tab, "_precheck", lambda *args, **kwargs: None)

    tab._save_and_schedule(dict(stale_row, name="From the form"), "Updated")

    stored = backup_utils.get_job(stale_row["id"])
    assert stored["name"] == "From the form"
    assert "running" not in stored and stored["last_result"] == "ok"
