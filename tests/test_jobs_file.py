# tests/test_jobs_file.py
# The jobs file (backup_jobs.json) must survive a bad write and must never be
# mistaken for "no jobs": atomic save, the .bak copy, JobsFileCorrupt on load,
# restore from the .bak, and the headless CLI's exit code on a damaged file.
#
# JOBS_FILE is monkeypatched to tmp_path; the CLI test runs a subprocess with
# APPDATA pointed at tmp_path so nothing real is read or written.
import os
import subprocess
import sys
from pathlib import Path

import pytest

from utils import backup_utils

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def jobs_file(tmp_path, monkeypatch):
    path = tmp_path / "backup_jobs.json"
    monkeypatch.setattr(backup_utils, "JOBS_FILE", str(path))
    return path


def _job(name="n"):
    return backup_utils.new_job(name, "s", "t", schedule="manual")


def _leftovers(directory):
    """Names in `directory` other than the jobs file and its .bak."""
    return sorted(p.name for p in directory.iterdir()
                  if p.name not in ("backup_jobs.json", "backup_jobs.json.bak"))


# --- atomic save -----------------------------------------------------------

def test_failed_replace_leaves_previous_contents_and_no_temp_file(jobs_file, monkeypatch):
    backup_utils.save_job(_job("first"))
    before = jobs_file.read_bytes()
    real_replace = os.replace

    def boom(src, dst):
        if str(dst) == str(jobs_file):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(backup_utils.os, "replace", boom)
    with pytest.raises(OSError, match="Could not write"):
        backup_utils.save_job(_job("second"))

    assert jobs_file.read_bytes() == before
    assert _leftovers(jobs_file.parent) == []


def test_failed_write_before_replace_cleans_up_the_temp_file(jobs_file, monkeypatch):
    backup_utils.save_job(_job("first"))
    before = jobs_file.read_bytes()

    def boom(fd):
        raise OSError("fsync failed")

    monkeypatch.setattr(backup_utils.os, "fsync", boom)
    with pytest.raises(OSError):
        backup_utils.save_job(_job("second"))

    assert jobs_file.read_bytes() == before
    assert _leftovers(jobs_file.parent) == []


def test_save_creates_the_file_when_missing(jobs_file):
    backup_utils.save_job(_job("only"))
    assert [j["name"] for j in backup_utils.load_jobs()] == ["only"]
    assert not (jobs_file.parent / "backup_jobs.json.bak").exists()


# --- load: corrupt is not empty ---------------------------------------------

def test_missing_file_is_no_jobs(jobs_file):
    assert not jobs_file.exists()
    assert backup_utils.load_jobs() == []


@pytest.mark.parametrize("content", [
    '[{"id": "a", "name": "x"',          # truncated mid-write
    "",                                   # zero-length after a crash
    "{not json",
    '{"jobs": []}',                       # valid JSON, not a list
    '["not", "objects"]',                 # a list, but not of jobs
])
def test_corrupt_file_raises_instead_of_reading_as_no_jobs(jobs_file, content):
    jobs_file.write_text(content, encoding="utf-8")
    with pytest.raises(backup_utils.JobsFileCorrupt) as caught:
        backup_utils.load_jobs()
    assert str(jobs_file) in str(caught.value)
    assert caught.value.has_backup is False
    assert "No usable backup" in str(caught.value)


def test_non_utf8_bytes_are_corrupt(jobs_file):
    jobs_file.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(backup_utils.JobsFileCorrupt):
        backup_utils.load_jobs()


def test_get_save_remove_and_record_run_all_refuse_a_corrupt_file(jobs_file):
    jobs_file.write_text("{broken", encoding="utf-8")
    raw = jobs_file.read_bytes()
    for call in (lambda: backup_utils.get_job("x"),
                 lambda: backup_utils.save_job(_job()),
                 lambda: backup_utils.save_jobs([_job()]),
                 lambda: backup_utils.remove_job("x"),
                 lambda: backup_utils.record_run("x", {"ok": True})):
        with pytest.raises(backup_utils.JobsFileCorrupt):
            call()
    assert jobs_file.read_bytes() == raw          # byte-identical
    assert _leftovers(jobs_file.parent) == []


# --- the .bak copy -----------------------------------------------------------

def test_bak_holds_the_previous_good_contents(jobs_file):
    backup_utils.save_job(_job("one"))
    first = jobs_file.read_bytes()
    backup_utils.save_job(_job("two"))
    bak = jobs_file.parent / "backup_jobs.json.bak"
    assert bak.read_bytes() == first


def test_corrupt_current_file_never_overwrites_a_good_bak(jobs_file):
    backup_utils.save_job(_job("one"))
    backup_utils.save_job(_job("two"))
    bak = jobs_file.parent / "backup_jobs.json.bak"
    good_bak = bak.read_bytes()
    jobs_file.write_text("{truncated", encoding="utf-8")

    with pytest.raises(backup_utils.JobsFileCorrupt) as caught:
        backup_utils.save_job(_job("three"))

    assert caught.value.has_backup is True
    assert bak.read_bytes() == good_bak


# --- restore -----------------------------------------------------------------

def test_restore_brings_back_the_bak_and_keeps_the_corrupt_file(jobs_file):
    backup_utils.save_job(_job("one"))
    backup_utils.save_job(_job("two"))
    good_bak = (jobs_file.parent / "backup_jobs.json.bak").read_bytes()
    jobs_file.write_bytes(b'{"half": ')

    assert backup_utils.restore_jobs_from_backup() == 1

    assert [j["name"] for j in backup_utils.load_jobs()] == ["one"]
    assert jobs_file.read_bytes() == good_bak
    kept = [p for p in jobs_file.parent.iterdir()
            if p.name.startswith("backup_jobs.json.corrupt-")]
    assert len(kept) == 1 and kept[0].read_bytes() == b'{"half": '
    # and saving works again
    backup_utils.save_job(_job("three"))
    assert len(backup_utils.load_jobs()) == 2


def test_restore_with_no_bak_fails_clearly_and_writes_nothing(jobs_file):
    jobs_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(backup_utils.JobsFileCorrupt, match="no usable backup"):
        backup_utils.restore_jobs_from_backup()
    assert jobs_file.read_text(encoding="utf-8") == "{broken"
    assert _leftovers(jobs_file.parent) == []


def test_restore_refuses_a_corrupt_bak(jobs_file):
    jobs_file.write_text("{broken", encoding="utf-8")
    (jobs_file.parent / "backup_jobs.json.bak").write_text("also {broken", encoding="utf-8")
    with pytest.raises(backup_utils.JobsFileCorrupt):
        backup_utils.restore_jobs_from_backup()
    assert jobs_file.read_text(encoding="utf-8") == "{broken"


# --- headless CLI --------------------------------------------------------------

def _run_cli(appdata, *args):
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, str(REPO / "main.py"), *args],
                          cwd=str(REPO), env=env, capture_output=True,
                          text=True, timeout=120)


def test_cli_exits_3_with_an_error_on_a_corrupt_jobs_file(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    jobs = appdata / "Safekeep" / "backup_jobs.json"
    jobs.write_text('[{"id": "abc"', encoding="utf-8")
    before = jobs.read_bytes()

    proc = _run_cli(appdata, "--run-backup-job", "abc")

    assert proc.returncode == 3, proc.stderr
    assert "[ERROR]" in proc.stderr and "damaged" in proc.stderr
    assert jobs.read_bytes() == before
    assert sorted(p.name for p in jobs.parent.iterdir()) == ["backup_jobs.json"]


def test_cli_unknown_job_still_exits_2(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    (appdata / "Safekeep" / "backup_jobs.json").write_text("[]", encoding="utf-8")
    assert _run_cli(appdata, "--run-backup-job", "nope").returncode == 2
