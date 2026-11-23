# tests/test_lock_start_time.py
# Card hHQdL83O: the run lock records its owner's process start time, so a
# reused pid (a different process) is told from the real owner, and a backup
# that legitimately runs longer than a day is not given a concurrent twin.
#
# Every lock here is planted in tmp_path or the conftest's throwaway locks
# folder; nothing touches the real %APPDATA%. Tests that need "a different
# start time" or "start time unknown" patch `utils.locks.process_start_time`
# (the owning module, never the backup_utils facade).
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from utils import backup_utils, jobs_store, locks


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """run_job records its result in the jobs file: keep that in tmp_path."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def tree(tmp_path):
    """A job over a 2-file source whose target folder does not exist yet."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.txt").write_text("beta", encoding="utf-8")
    job = backup_utils.new_job("j", str(source), str(tmp_path / "dst"), schedule="manual")
    return job, tmp_path / "dst"


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


OWN_START = locks.process_start_time(os.getpid())
needs_real_start_time = pytest.mark.skipif(
    OWN_START is None, reason="process start time is not readable on this platform")


def old_started(extra=60):
    """An ISO timestamp just past the 24 h age rule."""
    return (datetime.now() - timedelta(seconds=locks.STALE_AFTER + extra)).isoformat()


def holder_of(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- the two Done-when tests -------------------------------------------------

def test_live_holder_with_a_lock_older_than_24_hours_is_refused_as_busy(tmp_path, monkeypatch):
    """The owner is verified (same pid, same start time), so age is irrelevant."""
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: "T1")
    write_lock(path, os.getpid(), started=old_started(), start_time="T1")
    with pytest.raises(locks.LockBusy) as busy:
        locks.acquire(path)
    assert busy.value.holder["pid"] == os.getpid()
    assert holder_of(path)["token"] == "t"  # untouched, not taken over


@pytest.mark.parametrize("now, recorded", [("NOW", "THEN"), ("THEN", "NOW")])  # either order
def test_live_pid_with_a_different_start_time_is_taken_over_as_stale(tmp_path, monkeypatch,
                                                                     now, recorded):
    """A fresh lock whose pid now belongs to another process is stale at once."""
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: now)
    write_lock(path, os.getpid(), start_time=recorded)  # started: just now
    lock = locks.acquire(path)
    assert lock.recovered == os.getpid()
    assert lock.recovered_reused is True
    lock.release()
    assert not os.path.exists(path)


# --- the verified owner, and its safety cap ---------------------------------

def test_verified_live_owner_beyond_the_safety_cap_is_taken_over(tmp_path, monkeypatch):
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: "T1")
    beyond = (datetime.now() - timedelta(seconds=locks.VERIFIED_OWNER_STALE_AFTER + 60)).isoformat()
    write_lock(path, os.getpid(), started=beyond, start_time="T1")
    lock = locks.acquire(path)
    assert lock.recovered == os.getpid() and lock.recovered_reused is False
    lock.release()


def test_verified_live_owner_just_inside_the_safety_cap_stays_busy(tmp_path, monkeypatch):
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: "T1")
    inside = (datetime.now() - timedelta(seconds=locks.VERIFIED_OWNER_STALE_AFTER - 600)).isoformat()
    write_lock(path, os.getpid(), started=inside, start_time="T1")
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)


def test_the_safety_cap_is_far_beyond_the_old_24_hour_rule():
    assert locks.VERIFIED_OWNER_STALE_AFTER >= 7 * locks.STALE_AFTER


# --- old-format locks (no start_time) behave exactly as before ---------------

def test_old_format_lock_of_a_dead_pid_is_stale(tmp_path):
    path = str(tmp_path / "a.lock")
    gone = dead_pid()
    write_lock(path, gone)  # no start_time key
    lock = locks.acquire(path)
    assert lock.recovered == gone and lock.recovered_reused is False
    lock.release()


def test_old_format_fresh_lock_of_a_live_pid_is_busy(tmp_path):
    path = str(tmp_path / "a.lock")
    write_lock(path, os.getpid())
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)


def test_old_format_lock_of_a_live_pid_older_than_24_hours_is_taken_over(tmp_path):
    path = str(tmp_path / "a.lock")
    write_lock(path, os.getpid(), started=old_started())
    lock = locks.acquire(path)
    assert lock.recovered == os.getpid() and lock.recovered_reused is False
    lock.release()


def test_old_format_lock_just_under_24_hours_is_busy(tmp_path):
    path = str(tmp_path / "a.lock")
    young = (datetime.now() - timedelta(seconds=locks.STALE_AFTER - 600)).isoformat()
    write_lock(path, os.getpid(), started=young)
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)


def test_a_null_recorded_start_time_is_treated_as_old_format(tmp_path):
    """The writer could not read its own start time: age rule, as before."""
    path = str(tmp_path / "a.lock")
    write_lock(path, os.getpid(), start_time=None)
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)
    write_lock(path, os.getpid(), started=old_started(), start_time=None)
    lock = locks.acquire(path)
    assert lock.recovered_reused is False
    lock.release()


@pytest.mark.parametrize("garbage", [123, "", [OWN_START], {"t": 1}, True])
def test_a_recorded_start_time_that_is_not_text_is_treated_as_old_format(tmp_path, garbage):
    """Anything this app could not have written proves nothing about the owner.

    Compared as-is, any non-text value "differed" from the live owner's real
    start time, so a fresh lock held by a running backup was taken over as a
    reused pid -- two runs writing one job at once.
    """
    path = str(tmp_path / "a.lock")
    write_lock(path, os.getpid(), start_time=garbage)   # live owner, fresh lock
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)
    write_lock(path, os.getpid(), started=old_started(), start_time=garbage)
    lock = locks.acquire(path)                         # the 24 h rule still applies
    assert lock.recovered_reused is False
    lock.release()


# --- the current start time cannot be read: fall back to the age rule --------

def test_unreadable_current_start_time_falls_back_to_the_age_rule(tmp_path, monkeypatch):
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: None)
    write_lock(path, os.getpid(), start_time="T1")  # fresh: busy, not "different"
    with pytest.raises(locks.LockBusy):
        locks.acquire(path)
    write_lock(path, os.getpid(), started=old_started(), start_time="T1")  # > 24 h: stale
    lock = locks.acquire(path)
    assert lock.recovered == os.getpid() and lock.recovered_reused is False
    lock.release()


def test_dead_pid_with_a_recorded_start_time_is_stale_without_asking_for_a_start_time(tmp_path, monkeypatch):
    path = str(tmp_path / "a.lock")
    asked = []
    monkeypatch.setattr(locks, "process_start_time", lambda pid: asked.append(pid) or "x")
    gone = dead_pid()
    write_lock(path, gone, start_time="T1")
    lock = locks.acquire(path)
    assert lock.recovered == gone and lock.recovered_reused is False
    assert gone not in asked
    lock.release()


# --- what acquire writes ------------------------------------------------------

@needs_real_start_time
def test_acquire_records_the_owners_real_start_time(tmp_path):
    path = str(tmp_path / "a.lock")
    with locks.acquire(path, "thing"):
        assert holder_of(path)["start_time"] == locks.process_start_time(os.getpid())
        assert holder_of(path)["start_time"] == OWN_START


def test_acquire_writes_whatever_process_start_time_says(tmp_path, monkeypatch):
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: f"fake-{pid}")
    with locks.acquire(path):
        assert holder_of(path)["start_time"] == f"fake-{os.getpid()}"


def test_acquire_still_works_when_the_start_time_is_unreadable(tmp_path, monkeypatch):
    path = str(tmp_path / "a.lock")
    monkeypatch.setattr(locks, "process_start_time", lambda pid: None)
    with locks.acquire(path):
        assert holder_of(path)["start_time"] is None
        with pytest.raises(locks.LockBusy):
            locks.acquire(path)


@needs_real_start_time
def test_a_real_lock_held_by_this_process_is_busy_even_when_its_started_field_is_old(tmp_path):
    """End to end with the real process_start_time: rewrite `started` to 25 h ago."""
    path = str(tmp_path / "a.lock")
    with locks.acquire(path):
        body = holder_of(path)
        body["started"] = old_started()
        Path(path).write_text(json.dumps(body), encoding="utf-8")
        with pytest.raises(locks.LockBusy):
            locks.acquire(path)


# --- the message shown to the user -------------------------------------------

def test_run_job_says_so_when_the_stale_lock_was_a_reused_pid(tree, monkeypatch):
    job, _ = tree
    monkeypatch.setattr(locks, "process_start_time", lambda pid: "NOW")
    write_lock(job_lock(job), os.getpid(), start_time="THEN")
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert (f"Recovered a stale lock left by process {os.getpid()}, whose id has "
            "since been reused by another process.") in result["message"]


def test_run_job_keeps_the_plain_message_for_a_dead_owner(tree):
    job, _ = tree
    gone = dead_pid()
    write_lock(job_lock(job), gone, start_time="T1")
    result = backup_utils.run_job(job)
    assert f"Recovered a stale lock left by process {gone}." in result["message"]
    assert "reused" not in result["message"]


# --- process_start_time itself (real) ----------------------------------------

@needs_real_start_time
def test_process_start_time_is_stable_for_the_current_process():
    first = locks.process_start_time(os.getpid())
    assert isinstance(first, str) and first
    assert locks.process_start_time(os.getpid()) == first


@needs_real_start_time
def test_a_freshly_spawned_child_has_a_different_start_time_from_its_parent():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        # the child exists as soon as Popen returns, but give a slow start a moment
        deadline = time.monotonic() + 10
        child_start = None
        while child_start is None and time.monotonic() < deadline:
            child_start = locks.process_start_time(child.pid)
            if child_start is None:
                time.sleep(0.05)
        assert child_start is not None
        assert child_start != OWN_START
        assert locks.process_start_time(child.pid) == child_start  # stable
    finally:
        child.kill()
        child.wait(timeout=30)
    assert child.poll() is not None  # proved exited, by its pid's own wait
    assert locks.pid_alive(child.pid) is False


def test_process_start_time_is_none_for_bad_pids():
    for bad in (0, -1, "x", None, 1.5):
        assert locks.process_start_time(bad) is None


def test_process_start_time_is_none_for_a_pid_that_does_not_exist():
    assert locks.process_start_time(dead_pid()) is None


# --- the parsers that cannot run on this OS, tested on canned input -----------

def _stat_line(comm, start_ticks="123456"):
    """A /proc/<pid>/stat line: fields 3..21 are filler, field 22 is start_ticks."""
    fillers = ["S", "1", "1", "1", "0", "-1", "4194560"] + ["0"] * 12  # fields 3..21
    assert len(fillers) == 19
    return f"4242 ({comm}) " + " ".join(fillers + [start_ticks, "0", "0"])


def test_proc_stat_parser_reads_field_22():
    assert locks._parse_proc_stat(_stat_line("python").encode()) == "123456"


def test_proc_stat_parser_survives_spaces_and_parens_in_the_command_name():
    for comm in ("my prog", "a) b", "evil) S 9 9 9 (x", "(((", ")"):
        assert locks._parse_proc_stat(_stat_line(comm, "777").encode()) == "777", comm


def test_proc_stat_parser_returns_none_on_junk():
    for junk in (b"", b"no parens here", b"1 (x) S 1 2", b"1 (x) " + b"S " * 30):
        assert locks._parse_proc_stat(junk) is None, junk


class _Done:
    def __init__(self, out, code=0):
        self.stdout, self.returncode = out, code


def test_ps_start_time_reads_lstart(monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = args, kwargs
        return _Done("Sat Nov 22 17:33:54 2026\n")

    monkeypatch.setattr(locks.subprocess, "run", fake_run)
    assert locks._ps_start_time(321) == "Sat Nov 22 17:33:54 2026"
    assert seen["args"][:2] == ["ps", "-o"] and "321" in seen["args"]
    assert seen["kwargs"]["timeout"] > 0 and seen["kwargs"]["env"]["LC_ALL"] == "C"


def test_ps_start_time_is_none_when_ps_is_missing_slow_or_finds_nothing(monkeypatch):
    def missing(*_a, **_k):
        raise FileNotFoundError("ps")

    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired("ps", 5)

    monkeypatch.setattr(locks.subprocess, "run", missing)
    assert locks._ps_start_time(5) is None
    monkeypatch.setattr(locks.subprocess, "run", slow)
    assert locks._ps_start_time(5) is None
    monkeypatch.setattr(locks.subprocess, "run", lambda *_a, **_k: _Done("", 1))
    assert locks._ps_start_time(5) is None
    monkeypatch.setattr(locks.subprocess, "run", lambda *_a, **_k: _Done("  \n", 0))
    assert locks._ps_start_time(5) is None
