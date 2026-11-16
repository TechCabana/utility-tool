# tests/test_locks.py
# The per-job run lock and the jobs-file lock (utils/locks.py, used by
# backup_utils.run_job / save_job / remove_job / record_run).
#
# tests/conftest.py redirects backup_utils._locks_dir() to a temp folder, so
# nothing here touches the real %APPDATA%. No scheduler is involved. The CLI
# test runs a real `python main.py --run-backup-job` subprocess with APPDATA
# pointed at tmp_path.
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from utils import backup_utils, locks

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_utils, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def tree(tmp_path):
    """A job over a 2-file source whose target folder does NOT exist yet, so
    "the target was not touched" is observable as the folder still being absent."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.txt").write_text("beta", encoding="utf-8")
    target = tmp_path / "dst"
    job = backup_utils.new_job("j", str(source), str(target), schedule="manual")
    return job, target


def dead_pid() -> int:
    """The pid of a process that has definitely exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_lock(path, pid, started=None, **extra):
    """Plant a lock file as another process would have left it."""
    started = started or datetime.now().isoformat(timespec="seconds")
    Path(path).write_text(json.dumps({"pid": pid, "started": started, "what": "x",
                                      "token": "t", **extra}), encoding="utf-8")


def job_lock(job):
    return backup_utils._job_lock_path(job)


# --- the primitive ---------------------------------------------------------

def test_acquire_then_release_removes_the_file(tmp_path):
    path = str(tmp_path / "a.lock")
    lock = locks.acquire(path, "thing")
    held = json.loads(Path(path).read_text(encoding="utf-8"))
    assert held["pid"] == os.getpid() and held["what"] == "thing"
    lock.release()
    assert not os.path.exists(path)


def test_second_acquire_is_busy_and_reports_the_holder(tmp_path):
    path = str(tmp_path / "a.lock")
    with locks.acquire(path):
        with pytest.raises(locks.LockBusy) as busy:
            locks.acquire(path)
        assert busy.value.holder["pid"] == os.getpid()
    locks.acquire(path).release()  # free again once released


def test_pid_alive_tells_a_live_process_from_an_exited_one():
    assert locks.pid_alive(os.getpid()) is True
    assert locks.pid_alive(dead_pid()) is False
    assert locks.pid_alive(0) is False and locks.pid_alive(-5) is False


def test_lock_of_an_exited_process_is_taken_over(tmp_path):
    path = str(tmp_path / "a.lock")
    gone = dead_pid()
    write_lock(path, gone)
    lock = locks.acquire(path)
    assert lock.recovered == gone
    lock.release()
    assert not os.path.exists(path)


def test_old_lock_with_a_live_pid_is_taken_over(tmp_path):
    path = str(tmp_path / "a.lock")
    old = (datetime.now() - timedelta(seconds=locks.STALE_AFTER + 60)).isoformat()
    write_lock(path, os.getpid(), started=old)  # pid is alive: only the age says stale
    lock = locks.acquire(path)
    assert lock.recovered == os.getpid()
    lock.release()


def test_young_live_lock_is_not_taken_over(tmp_path):
    path = str(tmp_path / "a.lock")
    young = (datetime.now() - timedelta(seconds=locks.STALE_AFTER - 600)).isoformat()
    write_lock(path, os.getpid(), started=young)
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)


def test_corrupt_lock_is_busy_when_fresh_and_stale_when_old(tmp_path):
    path = str(tmp_path / "a.lock")
    for junk in (b"", b"not json", b'{"pid": "x"}', b"[1, 2]"):
        Path(path).write_bytes(junk)
        with pytest.raises(locks.LockBusy):  # a writer may be mid-create
            locks.acquire(path)
        old = time.time() - locks.CORRUPT_GRACE - 30
        os.utime(path, (old, old))
        lock = locks.acquire(path)
        assert lock.recovered == 0  # taken over; no pid to name
        lock.release()
        assert not os.path.exists(path)


def test_takeover_race_has_exactly_one_winner(tmp_path):
    """Many threads see the same dead lock at once; the rename decides."""
    path = str(tmp_path / "a.lock")
    gone = dead_pid()
    for round_number in range(25):
        write_lock(path, gone)
        barrier = threading.Barrier(8)
        wins, errors = [], []

        def contend():
            barrier.wait()
            try:
                wins.append(locks.acquire(path))
            except locks.LockBusy:
                pass
            except BaseException as exc:  # a crash here is a failure too
                errors.append(exc)

        threads = [threading.Thread(target=contend) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert len(wins) == 1, f"round {round_number}: {len(wins)} winners"
        wins[0].release()
        assert not os.path.exists(path)
        assert [p for p in tmp_path.iterdir()
                if ".stale-" in p.name or p.name.endswith(".takeover")] == []


def test_release_does_not_delete_a_lock_someone_else_now_holds(tmp_path):
    path = str(tmp_path / "a.lock")
    mine = locks.acquire(path)
    write_lock(path, 4242, token="someone-else")  # taken over (e.g. 24 h timeout)
    mine.release()
    assert json.loads(Path(path).read_text(encoding="utf-8"))["token"] == "someone-else"


def test_acquire_wait_times_out_with_a_clear_oserror(tmp_path):
    path = str(tmp_path / "a.lock")
    with locks.acquire(path):
        started = time.monotonic()
        with pytest.raises(OSError, match="Timed out"):
            locks.acquire_wait(path, timeout=0.2)
        assert time.monotonic() - started < 2


# --- run_job and the per-job lock ------------------------------------------

def test_second_run_of_the_same_job_is_refused_and_touches_nothing(tree):
    job, target = tree
    release_first = threading.Event()
    first_started = threading.Event()
    first_result = []

    def slow_progress(_done, _total):
        first_started.set()
        assert release_first.wait(30)

    runner = threading.Thread(
        target=lambda: first_result.append(backup_utils.run_job(job, on_progress=slow_progress)))
    runner.start()
    try:
        assert first_started.wait(30)
        shutil_before = sorted(p.name for p in target.rglob("*"))
        second = backup_utils.run_job(job)
        assert second["ok"] is False and second["busy"] is True
        assert f"process {os.getpid()}" in second["message"]
        assert "already running" in second["message"] and "Nothing was changed" in second["message"]
        assert sorted(p.name for p in target.rglob("*")) == shutil_before  # no new folder/manifest
    finally:
        release_first.set()
        runner.join()
    assert first_result[0]["ok"], first_result[0]["message"]
    assert not os.path.exists(job_lock(job))
    assert backup_utils.run_job(job)["ok"]  # free again afterwards


def test_busy_run_does_not_create_the_target(tree):
    job, target = tree
    with locks.acquire(job_lock(job)):
        result = backup_utils.run_job(job)
    assert result["busy"] is True
    assert not target.exists()


def test_stale_lock_from_a_dead_process_is_taken_over_and_reported(tree):
    job, target = tree
    gone = dead_pid()
    write_lock(job_lock(job), gone)
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert f"Recovered a stale lock left by process {gone}." in result["message"]
    assert "busy" not in result
    assert not os.path.exists(job_lock(job))


def test_recovery_note_is_also_added_to_a_failed_run(tree):
    job, target = tree
    job["source"] = str(Path(job["source"]) / "missing")
    gone = dead_pid()
    write_lock(job_lock(job), gone)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert result["message"].endswith(f"Recovered a stale lock left by process {gone}.")


def test_lock_is_released_after_success(tree):
    job, _ = tree
    assert backup_utils.run_job(job)["ok"]
    assert not os.path.exists(job_lock(job))


def test_lock_is_released_after_a_failed_run(tree):
    job, target = tree
    job["source"] = str(Path(job["source"]) / "missing")
    result = backup_utils.run_job(job)
    assert result["ok"] is False and "busy" not in result
    assert not os.path.exists(job_lock(job))


def test_lock_is_released_after_a_cancel(tree):
    job, _ = tree
    result = backup_utils.run_job(job, should_abort=lambda: True)
    assert result["ok"] is False and result["message"] == "Backup cancelled"
    assert not os.path.exists(job_lock(job))


def test_lock_is_released_when_run_job_raises(tree, monkeypatch):
    job, _ = tree

    def boom(_job):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(backup_utils, "preflight", boom)
    with pytest.raises(RuntimeError):
        backup_utils.run_job(job)
    assert not os.path.exists(job_lock(job))


def test_the_lock_is_held_while_the_run_is_in_progress(tree):
    job, _ = tree
    seen = []
    backup_utils.run_job(job, on_phase=lambda _p: seen.append(os.path.exists(job_lock(job))))
    assert seen and all(seen)


def test_different_jobs_do_not_block_each_other(tree, tmp_path):
    job, _ = tree
    other = backup_utils.new_job("o", job["source"], str(tmp_path / "dst2"), schedule="manual")
    with locks.acquire(job_lock(job)):
        assert backup_utils.run_job(other)["ok"]
        assert backup_utils.run_job(job)["busy"] is True


def test_job_lock_path_stays_inside_the_locks_folder(isolated_locks_dir):
    for bad in ("../evil", "..\\evil", "a/b", "..", "C:\\x", "a\x00b"):
        path = backup_utils._job_lock_path({"id": bad})
        assert os.path.dirname(path) == str(isolated_locks_dir), bad
    assert backup_utils._job_lock_path({"id": "ab12cd34"}).endswith("ab12cd34.lock")
    assert backup_utils._job_lock_path({"id": ""}) is None
    assert backup_utils._job_lock_path({}) is None
    assert backup_utils.run_job({"id": ""})["ok"] is False


# --- the jobs-file lock -----------------------------------------------------

def _hammer_jobs_file(monkeypatch, count=12):
    """Seed `count` jobs, then update `count` of them and add `count` new ones
    concurrently (2 x count threads, 24 by default). Returns (job ids, errors).

    `save_jobs` is slowed a few ms so the load -> write window is wide enough
    for the race to be certain rather than rare; the lock is what has to
    close it.
    """
    jobs = [backup_utils.new_job(f"job{i}", "s", "t", schedule="manual") for i in range(count)]
    for job in jobs:
        backup_utils.save_job(job)
    real_save_jobs = backup_utils.save_jobs

    def slow_save_jobs(items):
        time.sleep(0.005)
        real_save_jobs(items)

    monkeypatch.setattr(backup_utils, "save_jobs", slow_save_jobs)
    added = [backup_utils.new_job(f"new{i}", "s", "t", schedule="manual") for i in range(count)]
    errors = []
    barrier = threading.Barrier(count * 2)

    def update(i):
        barrier.wait()
        try:
            backup_utils.record_run(jobs[i]["id"], {"ok": False, "message": f"m{i}"})
        except BaseException as exc:
            errors.append(exc)

    def add(i):
        barrier.wait()
        try:
            backup_utils.save_job(added[i])
        except BaseException as exc:
            errors.append(exc)

    threads = ([threading.Thread(target=update, args=(i,)) for i in range(count)]
               + [threading.Thread(target=add, args=(i,)) for i in range(count)])
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.setattr(backup_utils, "save_jobs", real_save_jobs)
    return jobs, added, errors


def test_concurrent_record_run_and_save_job_lose_no_update(monkeypatch):
    jobs, added, errors = _hammer_jobs_file(monkeypatch)
    assert not errors, errors
    stored = {j["id"]: j for j in backup_utils.load_jobs()}
    assert len(stored) == len(jobs) + len(added)
    for i, job in enumerate(jobs):
        assert stored[job["id"]]["last_result"] == f"failed: m{i}"
    assert all(job["id"] in stored for job in added)


def test_the_same_harness_loses_updates_without_the_lock(monkeypatch):
    """Positive control: with the lock stubbed out the harness must see losses."""
    monkeypatch.setattr(backup_utils, "_jobs_file_lock", contextlib.nullcontext)
    jobs, added, errors = _hammer_jobs_file(monkeypatch)
    stored = {j["id"]: j for j in backup_utils.load_jobs()}
    intact = (len(stored) == len(jobs) + len(added)
              and all(stored[j["id"]]["last_result"] == f"failed: m{i}" for i, j in enumerate(jobs)))
    assert not intact


def test_remove_job_and_save_job_do_not_resurrect_each_other(monkeypatch):
    keep = backup_utils.new_job("keep", "s", "t", schedule="manual")
    drop = backup_utils.new_job("drop", "s", "t", schedule="manual")
    backup_utils.save_job(keep)
    backup_utils.save_job(drop)
    real = backup_utils.save_jobs
    monkeypatch.setattr(backup_utils, "save_jobs", lambda items: (time.sleep(0.05), real(items)))
    extra = backup_utils.new_job("extra", "s", "t", schedule="manual")
    threads = [threading.Thread(target=backup_utils.remove_job, args=(drop["id"],)),
               threading.Thread(target=backup_utils.save_job, args=(extra,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    ids = {j["id"] for j in backup_utils.load_jobs()}
    assert ids == {keep["id"], extra["id"]}


def test_jobs_file_lock_is_released_even_when_the_save_fails(monkeypatch, isolated_locks_dir):
    def boom(_items):
        raise OSError("disk full")

    monkeypatch.setattr(backup_utils, "save_jobs", boom)
    with pytest.raises(OSError):
        backup_utils.save_job(backup_utils.new_job("a", "s", "t", schedule="manual"))
    assert not (isolated_locks_dir / "jobs-file.lock").exists()


# --- headless CLI -----------------------------------------------------------

def test_cli_exits_4_when_the_job_is_already_running_and_records_nothing(tmp_path):
    appdata = tmp_path / "appdata"
    data = appdata / "Safekeep"
    data.mkdir(parents=True)
    job = backup_utils.new_job("j", str(tmp_path / "nosuch-src"), str(tmp_path / "dst"),
                               schedule="manual")
    job["last_run"], job["last_result"] = "2026-01-01T10:00:00", "ok"
    jobs_file = data / "backup_jobs.json"
    jobs_file.write_text(json.dumps([job]), encoding="utf-8")
    before = jobs_file.read_bytes()

    lock_dir = data / "locks"
    lock_dir.mkdir()
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")
    with locks.acquire(str(lock_dir / f"{job['id']}.lock"), "test holder"):
        proc = subprocess.run([sys.executable, str(REPO / "main.py"),
                               "--run-backup-job", job["id"]],
                              cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=120)

    assert proc.returncode == 4, (proc.stdout, proc.stderr)
    assert "[SKIPPED]" in proc.stderr and "already running" in proc.stderr
    assert jobs_file.read_bytes() == before          # last_result untouched
    assert json.loads(jobs_file.read_text(encoding="utf-8"))[0]["last_result"] == "ok"
    assert not (tmp_path / "dst").exists()
    assert not (data / "backup_jobs.json.bak").exists()  # record_run never saved
