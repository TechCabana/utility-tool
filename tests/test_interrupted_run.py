# tests/test_interrupted_run.py
# Card PQPfznG1: a run records that it STARTED, so a run that is killed or
# crashes mid-copy reads as "interrupted" instead of leaving the previous run's
# "ok" standing. The marker is a `running` field on the job (jobs_store), written
# just before copying (runner._mark_running), removed by record_run in the same
# write as the outcome, and read by runner.job_interrupted / settle_interrupted.
#
# Isolation: conftest points APPDATA, the locks folder and the logs folder at
# throwaway directories; each test points the jobs file at tmp_path. The tests
# that run real child processes use `shared_appdata`, which sends the child's
# APPDATA to a fresh folder AND points this process's jobs file, locks and logs
# at the same folder, then asserts it. Nothing here touches the real
# %APPDATA%\Safekeep or %APPDATA%\UtilityTool, the registry or the scheduler.
# Faults are patched on the module that owns the name (`utils.runner`,
# `utils.verify`, `utils.jobs_store`, `utils.locks`), never on the facade.
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from utils import backup_utils, jobs_store, locks, preflight, runlog, runner, verify
from utils.fs_utils import short_when

REPO = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def shared_appdata(tmp_path, monkeypatch):
    """A fresh app-data root shared by this process and any child process.

    The child gets it as APPDATA (its jobs file, locks and logs land under
    `<root>/Safekeep`); this process is pointed at the same three places, so
    what the child leaves behind is what the assertions read.
    """
    root = tmp_path / "appdata"
    data = root / "Safekeep"
    (data / "locks").mkdir(parents=True)
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(data / "backup_jobs.json"))
    monkeypatch.setattr(jobs_store, "_locks_dir", lambda: str(data / "locks"))
    monkeypatch.setattr(runlog, "_logs_dir", lambda: str(data / "logs"))
    assert Path(jobs_store.JOBS_FILE).parent == data and str(data).startswith(str(tmp_path))
    return root


def child_env(appdata):
    """The environment of a child that must see `appdata` as the whole app-data root."""
    return dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
                USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")


def make_job(tmp_path, **extra):
    """A saved job over a 2-file source whose target does not exist yet."""
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.txt").write_text("beta", encoding="utf-8")
    job = backup_utils.new_job("j", str(source), str(tmp_path / "dst"), schedule="manual")
    job.update(extra)
    backup_utils.save_job(job)
    return job


def stored(job):
    """The job as the jobs file holds it right now."""
    return backup_utils.get_job(job["id"])


def finish(job, **kwargs):
    """Run and record, as the app does; returns the run's result."""
    result = backup_utils.run_job(job, **kwargs)
    backup_utils.record_run(job["id"], result)
    return result


def job_lock(job):
    return runner._job_lock_path(job)


def interrupted_lines(job):
    """The run-log lines whose outcome is "interrupted", oldest first."""
    return [r for r in reversed(runlog.read_runs(job["id"])) if r["outcome"] == "interrupted"]


def dead_pid():
    """The pid of a process that has definitely exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def plant(job, pid, start_time=None, since=None, stamp=None):
    """Store a start marker exactly as `_mark_running` would, for any owner."""
    marker = {"since": since or datetime.now().isoformat(timespec="seconds"),
              "pid": pid, "start_time": start_time, "stamp": stamp, "token": "t"}
    assert jobs_store.set_running(job["id"], marker)
    return marker


def kill_tree(pid):
    """Terminate `pid` and its children by PID (never by image name)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        os.kill(pid, 9)


def wait_gone(pid, seconds=15.0):
    """Block until `pid` is not alive, so state is read only after the work stopped."""
    end = time.monotonic() + seconds
    while locks.pid_alive(pid):
        assert time.monotonic() < end, f"process {pid} did not exit"
        time.sleep(0.05)


# A child that takes a REAL run through run_job and dies without any cleanup the
# moment copying begins -- after the marker is written, before any result.
DIE_IN_COPY = (
    "import os, sys\n"
    "from utils import backup_utils, runner\n"
    "runner.copy_tree = lambda *a, **k: os._exit(9)\n"
    "backup_utils.run_job(backup_utils.get_job(sys.argv[1]))\n"
)

SLEEPER = ("import os, sys, time\n"
           "print(os.getpid(), flush=True)\n"
           "time.sleep(120)\n")


def die_mid_run(job, appdata):
    """Run `job` in a child process that dies in the copy; returns it, exited."""
    proc = subprocess.Popen([sys.executable, "-c", DIE_IN_COPY, job["id"]],
                            cwd=str(REPO), env=child_env(appdata))
    proc.wait(timeout=120)
    return proc


@pytest.fixture
def live_owner():
    """A real second process that stays alive: (its real pid, its start time)."""
    proc = subprocess.Popen([sys.executable, "-c", SLEEPER], cwd=str(REPO),
                            stdout=subprocess.PIPE, text=True)
    pid = int(proc.stdout.readline())
    start = locks.process_start_time(pid)
    assert locks.pid_alive(pid)
    try:
        yield pid, start
    finally:
        kill_tree(proc.pid)
        kill_tree(pid)
        proc.stdout.close()
        proc.wait(timeout=15)


# --- the Done-when ------------------------------------------------------------

def test_an_abandoned_run_reads_interrupted_and_a_completed_run_clears_it(shared_appdata, tmp_path):
    job = make_job(tmp_path)
    assert finish(job)["ok"]
    before = stored(job)
    assert before["last_result"] == "ok" and "running" not in before

    proc = die_mid_run(job, shared_appdata)             # the real code path, killed mid-copy
    assert proc.returncode == 9

    after = stored(job)
    marker = after["running"]
    wait_gone(marker["pid"])
    # What the card reported: the earlier result stands, byte for byte...
    assert after["last_result"] == "ok" and after["last_run"] == before["last_run"]
    # ...but the job now reads as interrupted, with when it started.
    info = backup_utils.job_interrupted(after)
    assert info == {"since": marker["since"], "stamp": marker["stamp"], "settled": False}
    assert datetime.fromisoformat(info["since"]) <= datetime.now()
    # The leftovers the card found are really there.
    root = Path(backup_utils.job_root(job))
    assert (root / (marker["stamp"] + ".partial")).is_dir()
    assert (root / (marker["stamp"] + ".manifest.jsonl.tmp")).is_file()

    result = finish(job)                                # a completed run afterwards
    assert result["ok"]
    final = stored(job)
    assert "running" not in final and final["last_result"] == "ok"
    assert backup_utils.job_interrupted(final) is None
    # ...and the run that found it recorded the interrupted one first, once.
    lines = interrupted_lines(job)
    assert len(lines) == 1 and lines[0]["start"] == marker["since"]
    assert lines[0]["version"] == marker["stamp"]


@pytest.mark.skipif(not IS_WINDOWS, reason="taskkill; the kill is by PID")
def test_a_real_headless_run_killed_by_pid_mid_copy_reads_interrupted(shared_appdata, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    block = os.urandom(8 * 1024 * 1024)
    with open(source / "big.bin", "wb") as handle:
        for _ in range(40):                             # 320 MB: seconds of copying
            handle.write(block)
    job = backup_utils.new_job("big", str(source), str(tmp_path / "dst"), schedule="manual")
    job["last_precheck"] = {}                           # skip the first-run walk
    job["last_run"], job["last_result"] = "2026-11-13T02:00:00", "ok"
    backup_utils.save_job(job)

    proc = subprocess.Popen([sys.executable, str(REPO / "main.py"), "--run-backup-job", job["id"]],
                            cwd=str(REPO), env=child_env(shared_appdata),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    marker, deadline = None, time.monotonic() + 120
    while marker is None and time.monotonic() < deadline:
        try:
            marker = json.loads(Path(jobs_store.JOBS_FILE).read_text(encoding="utf-8"))[0].get("running")
        except (OSError, ValueError, IndexError):
            marker = None
        time.sleep(0.005)
    assert marker, "the run never recorded that it had started"
    kill_tree(proc.pid)
    proc.wait(timeout=30)
    wait_gone(marker["pid"])                            # the work stopped; only now read state
    assert proc.returncode != 0

    after = stored(job)
    assert after["last_result"] == "ok" and after["last_run"] == "2026-11-13T02:00:00"
    info = backup_utils.job_interrupted(after)
    assert info and info["since"] == marker["since"] and info["settled"] is False
    root = Path(backup_utils.job_root(job))
    assert (root / (marker["stamp"] + ".partial")).is_dir(), "killed too late: the copy had finished"

    for old in source.iterdir():                        # now a small run completes
        old.unlink()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    assert finish(job)["ok"]
    final = stored(job)
    assert "running" not in final and final["last_result"] == "ok"
    assert len(interrupted_lines(job)) == 1


# --- when the marker is and is not written -----------------------------------

def test_the_marker_exists_during_the_copy_and_adds_only_its_own_field(tmp_path, monkeypatch):
    job = make_job(tmp_path, last_precheck={"x": 1}, future_field=[1, 2])
    seen = {}
    real = runner.copy_tree

    def spy(*args, **kwargs):
        seen["job"] = json.loads(Path(jobs_store.JOBS_FILE).read_text(encoding="utf-8"))[0]
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "copy_tree", spy)
    before = stored(job)
    result = backup_utils.run_job(job)
    marker = seen["job"]["running"]
    assert marker["pid"] == os.getpid()
    assert marker["start_time"] == locks.process_start_time(os.getpid())
    assert marker["stamp"] == result["version"]
    assert abs((datetime.now() - datetime.fromisoformat(marker["since"])).total_seconds()) < 120
    assert isinstance(marker["token"], str) and marker["token"]
    assert {k: v for k, v in seen["job"].items() if k != "running"} == before
    # Our own live process owns it: running, never interrupted.
    assert backup_utils.job_interrupted(seen["job"]) is None
    assert "running" in stored(job)                     # until record_run
    backup_utils.record_run(job["id"], result)
    assert "running" not in stored(job)


def test_a_busy_run_and_a_refused_run_write_nothing(tmp_path, monkeypatch):
    jobs_file = Path(jobs_store.JOBS_FILE)

    busy_job = make_job(tmp_path)
    before = jobs_file.read_bytes()
    with locks.acquire(job_lock(busy_job)):
        assert backup_utils.run_job(busy_job)["busy"] is True
    assert jobs_file.read_bytes() == before
    assert not Path(str(jobs_file) + ".bak").exists()

    # A pre-flight guard (source missing) refuses before anything is copied.
    gone = backup_utils.new_job("gone", str(tmp_path / "nosuch"), str(tmp_path / "d2"),
                                schedule="manual")
    backup_utils.save_job(gone)
    before = jobs_file.read_bytes()
    result = backup_utils.run_job(gone)
    assert result["ok"] is False and not result.get("busy")
    assert jobs_file.read_bytes() == before

    # Target inside the source: another guard.
    nested = backup_utils.new_job("nested", str(tmp_path / "src"),
                                  str(tmp_path / "src" / "inside"), schedule="manual")
    backup_utils.save_job(nested)
    before = jobs_file.read_bytes()
    assert backup_utils.run_job(nested)["ok"] is False
    assert jobs_file.read_bytes() == before

    # Positive control: a run that gets past the guards DOES change the file.
    bytes_at_copy = []
    real = runner.copy_tree
    monkeypatch.setattr(runner, "copy_tree",
                        lambda *a, **k: (bytes_at_copy.append(jobs_file.read_bytes()), real(*a, **k))[1])
    before = jobs_file.read_bytes()
    assert backup_utils.run_job(busy_job)["ok"]
    assert bytes_at_copy and bytes_at_copy[0] != before


def test_a_marker_that_cannot_be_written_never_fails_the_backup(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    before = Path(jobs_store.JOBS_FILE).read_bytes()

    def lock_timeout(*_a, **_k):
        raise OSError("Timed out waiting for another Safekeep process")

    monkeypatch.setattr(jobs_store, "set_running", lock_timeout)
    result = backup_utils.run_job(job)
    assert result["ok"] is True
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before


def test_a_damaged_jobs_file_is_still_reported_by_record_run_not_by_the_marker(tmp_path):
    job = make_job(tmp_path)
    jobs_file = Path(jobs_store.JOBS_FILE)
    jobs_file.write_bytes(b"{ not json")
    result = backup_utils.run_job(job)                  # the marker write must not raise or repair
    assert result["ok"] is True
    assert jobs_file.read_bytes() == b"{ not json"
    assert not Path(str(jobs_file) + ".bak").exists()
    with pytest.raises(backup_utils.JobsFileCorrupt):
        backup_utils.record_run(job["id"], result)
    assert backup_utils.settle_interrupted(job["id"]) is None   # and neither does settling


# --- when the marker is cleared -----------------------------------------------

@pytest.mark.parametrize("how", ["ok", "failed", "cancelled"])
def test_record_run_clears_the_marker_for_every_kind_of_outcome(tmp_path, monkeypatch, how):
    job = make_job(tmp_path)
    kwargs = {}
    if how == "failed":
        monkeypatch.setattr(verify, "_verify_copy", lambda *a, **k: (1, 1, ["a.txt"]))
    if how == "cancelled":
        kwargs["should_abort"] = lambda: True
    result = backup_utils.run_job(job, **kwargs)
    assert result["ok"] is (how == "ok")
    assert stored(job)["running"]["pid"] == os.getpid()  # positive control: it was there
    backup_utils.record_run(job["id"], result)
    final = stored(job)
    assert "running" not in final
    assert final["last_result"].startswith({"ok": "ok", "failed": "failed",
                                            "cancelled": "failed: Backup cancelled"}[how])


@pytest.mark.parametrize("error", [RuntimeError("boom"), KeyboardInterrupt()])
def test_a_run_that_raises_outside_the_catch_list_clears_its_marker(tmp_path, monkeypatch, error):
    job = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    seen = {}

    def raising(*_args, **_kwargs):
        seen["marker"] = stored(job).get("running")      # present while the work runs
        raise error

    monkeypatch.setattr(runner, "copy_tree", raising)
    with pytest.raises(type(error)):
        backup_utils.run_job(job)
    assert seen["marker"] is not None
    final = stored(job)
    assert "running" not in final                        # cleared on the way out
    assert final["last_result"].startswith(f"failed: {type(error).__name__}")
    assert not os.path.exists(job_lock(job))
    assert runlog.read_runs(job["id"])[0]["outcome"] == "failed"


def test_a_raise_before_the_marker_changes_nothing(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    before = Path(jobs_store.JOBS_FILE).read_bytes()

    def boom(_job):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(preflight, "preflight", boom)
    with pytest.raises(RuntimeError):
        backup_utils.run_job(job)
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before


# --- other fields survive -------------------------------------------------------

def test_other_fields_survive_the_marker_write_and_the_clear(tmp_path, monkeypatch):
    release = {"stamp": "20261101-010101", "files": 10, "level": 4,
               "since": "20261102-010101", "time": "2026-11-02T01:01:01"}
    job = make_job(tmp_path, hold_released=release, last_precheck={"problems": 0},
                   future_field={"keep": True})
    real = runner.copy_tree

    def edit_meanwhile(*args, **kwargs):
        """The GUI edits the schedule and the name while the run is copying."""
        edited = dict(stored(job), at="03:30", name="Renamed")
        assert "running" in edited
        backup_utils.save_job(edited)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "copy_tree", edit_meanwhile)
    result = backup_utils.run_job(job)
    backup_utils.record_run(job["id"], result)
    final = stored(job)
    assert (final["at"], final["name"]) == ("03:30", "Renamed")
    assert final["hold_released"] == release and final["last_precheck"] == {"problems": 0}
    assert final["future_field"] == {"keep": True} and "running" not in final


def test_a_run_started_from_a_stale_job_dict_keeps_edits_made_since(tmp_path):
    """The GUI holds the job it loaded; the file was edited after that. The run's
    marker write and clear must not put the old values back."""
    job = make_job(tmp_path)                             # the dict the run is started from
    backup_utils.save_job(dict(job, at="04:45", schedule="weekly", last_precheck={"p": 2}))
    result = backup_utils.run_job(job)
    mid = stored(job)
    assert (mid["at"], mid["schedule"], mid["last_precheck"]) == ("04:45", "weekly", {"p": 2})
    assert "running" in mid
    backup_utils.record_run(job["id"], result)
    final = stored(job)
    assert (final["at"], final["schedule"], final["last_precheck"]) == ("04:45", "weekly", {"p": 2})


def test_set_running_never_writes_back_a_stale_job(tmp_path):
    job = make_job(tmp_path)                             # schedule "manual" in this dict
    backup_utils.save_job(dict(job, schedule="weekly", last_precheck={"p": 1}))
    assert jobs_store.set_running(job["id"], jobs_store.new_running_marker(None))
    final = stored(job)
    assert final["schedule"] == "weekly" and final["last_precheck"] == {"p": 1}
    assert final["running"]["pid"] == os.getpid()
    assert jobs_store.set_running("no-such-job", jobs_store.new_running_marker(None)) is False


# --- who is the owner: running vs interrupted -----------------------------------

def test_an_owner_that_is_alive_means_running_even_after_days(tmp_path, live_owner):
    pid, start = live_owner
    job = make_job(tmp_path)
    old = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
    plant(job, pid, start_time=start, since=old)         # verified owner, three days in
    before = Path(jobs_store.JOBS_FILE).read_bytes()
    assert backup_utils.job_interrupted(stored(job)) is None
    assert backup_utils.settle_interrupted(job["id"]) is None
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before
    assert interrupted_lines(job) == []
    # Positive control: the same marker reads as interrupted once the owner is gone.
    kill_tree(pid)
    wait_gone(pid)
    assert backup_utils.job_interrupted(stored(job))["since"] == old


def test_a_reused_pid_is_interrupted_and_start_time_unreadable_falls_back_to_age(
        tmp_path, live_owner, monkeypatch):
    pid, start = live_owner
    job = make_job(tmp_path)
    plant(job, pid, start_time="1")                      # alive, but a DIFFERENT process's time
    assert backup_utils.job_interrupted(stored(job)) is not None
    plant(job, pid, start_time=start)                    # same pid and time: the owner
    assert backup_utils.job_interrupted(stored(job)) is None

    # Cannot read the start time now: unverified, so the lock's age rule applies.
    monkeypatch.setattr(locks, "process_start_time", lambda _pid: None)
    plant(job, pid, start_time=start)                    # young
    assert backup_utils.job_interrupted(stored(job)) is None
    old = (datetime.now() - timedelta(seconds=locks.STALE_AFTER + 60)).isoformat(timespec="seconds")
    plant(job, pid, start_time=start, since=old)         # older than a day
    assert backup_utils.job_interrupted(stored(job)) is not None
    # A marker written when the start time could not be read: the same rule.
    plant(job, pid, start_time=None)
    assert backup_utils.job_interrupted(stored(job)) is None
    plant(job, pid, start_time=None, since=old)
    assert backup_utils.job_interrupted(stored(job)) is not None


def test_a_live_per_job_lock_means_another_run_is_in_progress(tmp_path):
    job = make_job(tmp_path)
    plant(job, dead_pid(), since="2026-11-14T02:00:00")
    before = Path(jobs_store.JOBS_FILE).read_bytes()
    with locks.acquire(job_lock(job)):                   # a live run took the job over
        assert backup_utils.job_interrupted(stored(job)) is None
        assert backup_utils.settle_interrupted(job["id"]) is None
        assert Path(jobs_store.JOBS_FILE).read_bytes() == before
    assert backup_utils.job_interrupted(stored(job)) is not None   # released: it was dead
    with locks.acquire(job_lock(job)):                   # the lock holder itself may settle
        assert backup_utils.settle_interrupted(job["id"], check_lock=False)["since"] == "2026-11-14T02:00:00"


JUNK = [
    None, "x", 5, [], {}, {"pid": 7},
    {"since": "not a time", "pid": 7},
    {"since": "2026-11-14T02:00:00", "pid": 0},
    {"since": "2026-11-14T02:00:00", "pid": True},
    {"since": "2026-11-14T02:00:00", "pid": "7"},
    {"since": "2026-11-14T02:00:00", "pid": 7, "start_time": 5},
    {"since": 5, "pid": 7},
]


@pytest.mark.parametrize("junk", JUNK, ids=[repr(j)[:30] for j in JUNK])
def test_a_marker_with_garbage_fields_reads_as_none(tmp_path, junk):
    job = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    jobs = backup_utils.load_jobs()
    jobs[0]["running"] = junk
    backup_utils.save_jobs(jobs)
    before = Path(jobs_store.JOBS_FILE).read_bytes()
    job_now = stored(job)
    assert jobs_store.job_running(job_now) is None
    assert backup_utils.job_interrupted(job_now) is None
    assert backup_utils.settle_interrupted(job["id"]) is None
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before
    finish(job)                                          # the next run replaces the junk
    assert "running" not in stored(job)


def test_a_junk_stamp_is_dropped_but_the_marker_stays_valid(tmp_path):
    job = make_job(tmp_path)
    plant(job, dead_pid(), stamp="C:/secret/path")
    found = jobs_store.job_running(stored(job))
    assert found["stamp"] is None and found["pid"] > 0


def test_a_job_from_an_older_jobs_file_is_unchanged(tmp_path):
    job = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    assert "running" not in stored(job)
    assert backup_utils.job_interrupted(stored(job)) is None
    assert backup_utils.job_interrupted({"id": "x"}) is None
    assert backup_utils.job_interrupted({"last_result": 5, "last_run": "z"}) is None
    assert backup_utils.settle_interrupted(job["id"]) is None
    assert finish(job)["ok"] and stored(job)["last_result"] == "ok"


# --- recording the interrupted run -----------------------------------------------

def test_settling_records_the_run_once_everywhere_it_is_read(tmp_path):
    job = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    plant(job, dead_pid(), since="2026-11-14T02:00:00", stamp="20261114-020000")

    marker = backup_utils.settle_interrupted(job["id"])
    assert marker["since"] == "2026-11-14T02:00:00"
    after = stored(job)
    assert "running" not in after
    assert after["last_run"] == "2026-11-14T02:00:00"
    assert after["last_result"].startswith("interrupted: started 2026-11-14T02:00:00")
    assert not after["last_result"].startswith("ok")      # not ok, even to a reader of last_result alone
    assert backup_utils.job_interrupted(after) == {
        "since": "2026-11-14T02:00:00", "stamp": None, "settled": True}

    assert backup_utils.settle_interrupted(job["id"]) is None   # idempotent: no second line
    assert backup_utils.settle_all_interrupted([stored(job)]) is False
    lines = interrupted_lines(job)
    assert len(lines) == 1
    line = lines[0]
    assert set(line) == set(runlog.LINE_KEYS) and line["outcome"] in runlog.OUTCOMES
    assert (line["trigger"], line["start"], line["end"], line["version"], line["cause"]) == (
        "recovery", "2026-11-14T02:00:00", "2026-11-14T02:00:00", "20261114-020000", "interrupted")
    assert (line["files"], line["bytes"], line["skipped"], line["offenders"]) == (0, 0, 0, [])
    text = json.dumps(line)
    assert str(tmp_path) not in text and not re.search(r"[A-Za-z]:[\\/]", text)


def test_each_interrupted_run_is_logged_once_by_the_run_that_follows(tmp_path):
    job = make_job(tmp_path)
    plant(job, dead_pid(), since="2026-11-10T02:00:00")
    finish(job)
    plant(job, dead_pid(), since="2026-11-12T02:00:00")
    finish(job)
    finish(job)                                          # nothing interrupted before this one
    assert [r["start"] for r in interrupted_lines(job)] == [
        "2026-11-10T02:00:00", "2026-11-12T02:00:00"]
    assert len(runlog.read_runs(job["id"])) == 5         # 3 runs + 2 interrupted
    assert stored(job)["last_result"] == "ok"


def test_a_run_refused_by_a_guard_still_records_the_earlier_interruption(tmp_path):
    job = make_job(tmp_path)
    plant(job, dead_pid(), since="2026-11-14T02:00:00")
    backup_utils.save_job(dict(stored(job), source=str(tmp_path / "gone")))
    result = finish(stored(job))                         # the job as saved: its source is gone
    assert result["ok"] is False
    assert [r["start"] for r in interrupted_lines(job)] == ["2026-11-14T02:00:00"]
    assert stored(job)["last_result"].startswith("failed") and "running" not in stored(job)


# --- the Backup tab ---------------------------------------------------------------

@pytest.fixture
def qt_app():
    """An offscreen QApplication; no QSettings is constructed by these tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def status_label(row):
    """The Status cell of a JobRow: the label that carries a Status* object name."""
    from PySide6 import QtWidgets
    found = [label for label in row.findChildren(QtWidgets.QLabel)
             if label.objectName().startswith("Status")]
    assert len(found) == 1, [(l.objectName(), l.text()) for l in found]
    return found[0]


def test_an_interrupted_job_card_says_so_in_the_failure_style(qt_app, tmp_path, live_owner):
    from tabs.backup_tab import JobRow
    base = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    since = "2026-11-14T02:00:00"

    # Positive control: the same job as a plain OK card.
    ok_label = status_label(JobRow(dict(base)))
    assert (ok_label.objectName(), ok_label.text()) == ("StatusOk", "OK")
    assert "Interrupted" not in ok_label.text() and ok_label.toolTip() == ""

    # A start marker with no owner: not yet settled, still shown.
    plant(base, dead_pid(), since=since)
    label = status_label(JobRow(stored(base)))
    assert label.objectName() == "StatusError"
    assert label.text() == f"Interrupted: started {short_when(since)} and never finished"
    assert "unplugged" in label.toolTip() and "next run starts cleanly" in label.toolTip()
    assert label.accessibleDescription() == label.toolTip()

    # The same fact after settling (the job's own last_result carries it).
    backup_utils.settle_interrupted(base["id"])
    settled = status_label(JobRow(stored(base)))
    assert (settled.objectName(), settled.text()) == ("StatusError", label.text())

    # A run that is alive owns the marker: the card shows the earlier result, not "interrupted".
    pid, start = live_owner
    running = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    running["id"] = "live0001"
    backup_utils.save_job(running)
    plant(running, pid, start_time=start, since=since)
    live_label = status_label(JobRow(stored(running)))
    assert (live_label.objectName(), live_label.text()) == ("StatusOk", "OK")


def test_a_first_run_that_died_reads_interrupted_not_never_run(qt_app, tmp_path):
    from tabs.backup_tab import JobRow
    job = make_job(tmp_path)                             # no last_run at all
    assert JobRow._status(None, stored(job)) == ("Never run", "Hint")   # positive control
    plant(job, dead_pid(), since="2026-11-14T02:00:00")
    assert status_label(JobRow(stored(job))).objectName() == "StatusError"


def test_a_junk_marker_does_not_break_the_row(qt_app, tmp_path):
    from tabs.backup_tab import JobRow
    job = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok",
                   running={"since": "x", "pid": "7"})
    label = status_label(JobRow(stored(job)))
    assert (label.objectName(), label.text()) == ("StatusOk", "OK")


def test_loading_the_tab_settles_a_dead_marker_but_not_a_live_one(qt_app, tmp_path, monkeypatch,
                                                                  live_owner):
    from tabs import backup_tab
    from utils import scheduler
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))   # no real schtasks
    pid, start = live_owner
    dead = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    plant(dead, dead_pid(), since="2026-11-14T02:00:00")
    live = backup_utils.new_job("live", str(tmp_path / "src"), str(tmp_path / "d3"),
                                schedule="manual")
    live.update(last_run="2026-11-13T02:00:00", last_result="ok")
    backup_utils.save_job(live)
    plant(live, pid, start_time=start, since="2026-11-14T03:00:00")

    tab = backup_tab.BackupTab()
    tab.refresh_backup()
    assert stored(dead)["last_result"].startswith("interrupted")
    assert stored(live)["last_result"] == "ok" and "running" in stored(live)
    by_id = {job["id"]: job for job in tab.jobs}
    assert by_id[dead["id"]]["last_result"].startswith("interrupted")
    tab.refresh_backup()                                  # loading again does not log again
    assert len(interrupted_lines(dead)) == 1 and interrupted_lines(live) == []


# --- the headless run -------------------------------------------------------------

@pytest.fixture
def cli():
    """main.py's functions (imports PySide6 but builds no QApplication)."""
    import main
    return main


def test_the_notice_is_one_plain_line_and_only_for_an_interrupted_job(tmp_path, cli):
    job = make_job(tmp_path, last_run="2026-11-13T02:00:00", last_result="ok")
    assert cli.interrupted_notice(stored(job)) == ""      # positive control: an ok job
    plant(job, dead_pid(), since="2026-11-14T02:00:00")
    notice = cli.interrupted_notice(stored(job))
    assert notice == (f"The previous run of this backup was interrupted (started "
                      f"{short_when('2026-11-14T02:00:00')}) and never finished.")
    assert "\n" not in notice
    backup_utils.settle_interrupted(job["id"])
    assert cli.interrupted_notice(stored(job)) == notice  # still the previous run, settled or not


def test_the_headless_run_warns_once_runs_anyway_and_keeps_its_exit_code(tmp_path, cli, capsys):
    job = make_job(tmp_path, last_precheck={"problems": 0})
    plant(job, dead_pid(), since="2026-11-14T02:00:00")
    assert cli.run_backup_job_cli(job["id"]) == 0
    err = capsys.readouterr().err
    assert err.count("previous run of this backup was interrupted") == 1
    assert "[WARN]" in err
    final = stored(job)
    assert final["last_result"] == "ok" and "running" not in final
    assert len(interrupted_lines(job)) == 1
    assert cli.run_backup_job_cli(job["id"]) == 0         # the run recorded a result: no repeat
    assert "interrupted" not in capsys.readouterr().err


def test_a_real_headless_process_prints_the_notice_after_a_killed_run(shared_appdata, tmp_path):
    job = make_job(tmp_path, last_precheck={"problems": 0})
    assert die_mid_run(job, shared_appdata).returncode == 9
    marker = stored(job)["running"]
    wait_gone(marker["pid"])

    proc = subprocess.run([sys.executable, str(REPO / "main.py"), "--run-backup-job", job["id"]],
                          cwd=str(REPO), env=child_env(shared_appdata),
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert proc.stderr.count("The previous run of this backup was interrupted") == 1
    assert f"started {short_when(marker['since'])}" in proc.stderr
    final = stored(job)
    assert final["last_result"] == "ok" and "running" not in final
    assert len(interrupted_lines(job)) == 1
