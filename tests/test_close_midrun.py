# tests/test_close_midrun.py
# Card RIzMAyRb: what does closing the app while a manual backup runs do to the
# job? The Interrupted tooltip and the README name its causes, so a cause is
# listed if and only if it really produces the Interrupted state.
#
# Finding (reproduced below with the REAL app, `main.main()`, in a child process
# that exits through the real Qt event loop): closing the window, or quitting the
# application, does not wait for the run. The tab's `shutdown`
# (QApplication.aboutToQuit) aborts and joins the worker thread, so the copy stops
# (or, once past the last abort check, finishes) and a half-copy is discarded.
# The worker's `finished` signal is queued to the UI thread, whose event loop has
# already ended, so it is never delivered. Until the review gate's fix, that meant
# `_finish_run` -> `record_run` never ran, the start marker stayed and the job read
# "Interrupted" on the next start -- even after a run that had finished and
# verified. `shutdown` now records the result the joined worker returned, so a
# close mid-run reads as the cancelled run it was, a close during the final prune
# reads as the good backup it made, and "the app was closed" is NOT a cause the
# Interrupted tooltip or the README list.
#
# Isolation: every child gets APPDATA = a temp root (asserted by the child before
# anything runs), a QSettings forced to an ini file inside that root (the real one
# is the registry on Windows), and a stubbed scheduler. This process is pointed at
# the same folder, so nothing here touches the real %APPDATA%\Safekeep, the
# registry or Task Scheduler. Faults are patched on the owning modules.
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from utils import backup_utils, jobs_store, locks, runlog, runner
from utils.fs_utils import short_when

REPO = Path(__file__).resolve().parent.parent
CHILD = REPO / "tests" / "close_midrun_child.py"
SINCE = "2026-11-14T02:00:00"


@pytest.fixture
def app_root(tmp_path, monkeypatch):
    """A fresh app-data root shared with the child process (its APPDATA)."""
    root = tmp_path / "appdata"
    data = root / "Safekeep"
    (data / "locks").mkdir(parents=True)
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(data / "backup_jobs.json"))
    monkeypatch.setattr(jobs_store, "_locks_dir", lambda: str(data / "locks"))
    monkeypatch.setattr(runlog, "_logs_dir", lambda: str(data / "logs"))
    assert Path(jobs_store.JOBS_FILE).parent == data and str(data).startswith(str(tmp_path))
    return root


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    """No real schtasks, whatever a Backup tab does while loading."""
    from utils import scheduler
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))


@pytest.fixture
def qt_app():
    """An offscreen QApplication; no QSettings is constructed by these tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def make_job(tmp_path):
    """A saved job over a 4-file source whose last run was a plain OK."""
    source = tmp_path / "src"
    source.mkdir(parents=True, exist_ok=True)
    for name in "abcd":
        (source / f"{name}.txt").write_text(name * 100, encoding="utf-8")
    job = backup_utils.new_job("j", str(source), str(tmp_path / "dst"), schedule="manual")
    job.update(last_run="2026-11-13T02:00:00", last_result="ok")
    backup_utils.save_job(job)
    return job


def stored(job):
    """The job as the jobs file holds it right now."""
    return backup_utils.get_job(job["id"])


def run_app(app_root, tmp_path, job, close, phase):
    """Run the real app in a child, start `job`, close it as `close` says at `phase`.

    Returns the child's own report. The child has exited (and its pid is dead) by
    the time this returns; a child that hangs is killed by PID, tree and all.
    """
    report_file = tmp_path / "report.json"
    spec = {"job_id": job["id"], "close": close, "phase": phase,
            "started": str(tmp_path / "phase-started"), "report": str(report_file)}
    env = dict(os.environ, APPDATA=str(app_root), HOME=str(app_root),
               USERPROFILE=str(app_root), CLOSE_TEST_ROOT=str(app_root),
               QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen([sys.executable, str(CHILD), json.dumps(spec)], env=env,
                            cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        output, _ = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True) \
            if os.name == "nt" else os.kill(proc.pid, 9)
        proc.communicate()
        pytest.fail("the app child did not exit by itself")
    assert proc.returncode == 0, output.decode("utf-8", "replace")[-2000:]
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert not locks.pid_alive(report["pid"])          # a real process exit, not a flag
    return report


def events(report, prefix):
    """The child's recorded events that start with `prefix`."""
    return [e for e in report["events"] if e.startswith(prefix)]


def reopen(job):
    """What a restart shows: a fresh Backup tab loads (and settles) the jobs, and the
    job's card is built from the stored job. Returns (Status label, the job as stored)."""
    from PySide6 import QtWidgets
    from tabs import backup_tab
    tab = backup_tab.BackupTab()
    tab.refresh_backup()
    # Cancelled (card bricnEaC) is neutral `Hint`, not a Status* style, so it is
    # picked out by its text as well.
    label = [l for l in backup_tab.JobRow(stored(job)).findChildren(QtWidgets.QLabel)
             if l.objectName().startswith("Status") or l.text() == "Cancelled"]
    assert len(label) == 1, [(l.objectName(), l.text()) for l in label]
    return label[0], stored(job)


def version_folders(job):
    """What the target holds for the job: finished versions and any leftovers."""
    root = Path(job["target"])
    names = sorted(p.name for p in root.rglob("*")) if root.exists() else []
    return ([n for n in names if re.fullmatch(r"\d{8}-\d{6}", n)],
            [n for n in names if n.endswith((".partial", ".tmp"))])


# --- the finding ------------------------------------------------------------------

@pytest.mark.parametrize("close, phase", [
    ("window", "copy"),     # the X button, while files are being copied
    ("quit", "copy"),       # QApplication.quit() (a quit action, a session end)
    ("window", "verify"),   # the X button, while the copy is being checked
])
def test_closing_the_app_mid_run_records_the_run_as_cancelled_not_interrupted(
        app_root, tmp_path, qt_app, close, phase):
    job = make_job(tmp_path)
    good = backup_utils.run_job(job)                  # a real earlier backup the close must not touch
    assert good["ok"], good["message"]
    backup_utils.record_run(job["id"], good)
    good_stamp, good_utc = good["version"], stored(job)["last_finished_utc"]
    report = run_app(app_root, tmp_path, job, close, phase)

    # On close: the worker was still running; shutdown stopped and joined it, the
    # engine returned "cancelled", and shutdown recorded that result itself (the
    # queued `finished` signal is never delivered once the event loop has ended).
    assert events(report, f"closing_via_{close}(worker_thread_running=True)")
    assert events(report, "run_job_returned(ok=False, message='Backup cancelled')")
    assert events(report, "record_run(ok=False)")
    assert report["finish_handler_never_ran"] is False and report["worker_thread_after"] is None

    # What the process left: no marker, the cancelled run recorded, no half-copy,
    # no version, and no lock that could block the next run.
    # Card bricnEaC: the person closed the app, so the run is stored as `cancelled`
    # (it was `failed: Backup cancelled`). The run's message stays in the log.
    after = stored(job)
    assert "running" not in after and after["last_result"] == "cancelled"
    assert runner.job_interrupted(after) is None
    assert version_folders(job) == ([good_stamp], [])    # only the earlier good version
    assert os.listdir(jobs_store._locks_dir()) == []
    # The run log agrees with the job: ONE "cancelled" line for the closed run (on
    # top of the earlier good run's "ok"), and no "interrupted" or "failed" line.
    assert [line["outcome"] for line in runlog.read_runs(job["id"])] == ["cancelled", "ok"]
    assert runlog.read_runs(job["id"])[0]["message"] == "Backup cancelled"
    # The earlier good backup is still the newest verified version and restore's default.
    assert backup_utils.latest_version(job) == good_stamp
    assert backup_utils.restore_candidates(job)["preferred"] == good_stamp
    assert backup_utils.is_verified(job, good_stamp) and after["last_finished_utc"] == good_utc

    # The restart: the job reads Cancelled (neutral, not the error style), never
    # Failed and never Interrupted.
    label, settled = reopen(job)
    assert (label.objectName(), label.text()) == ("Hint", "Cancelled")
    assert "never finished" not in label.toolTip()
    assert label.toolTip().startswith("You stopped this backup before it finished.")
    assert label.accessibleDescription() == label.toolTip()
    assert settled["last_result"] == "cancelled" and "running" not in settled
    assert [line["outcome"] for line in runlog.read_runs(job["id"])] == ["cancelled", "ok"]

    # ...and a new manual run starts at once (the dead owner's lock is already gone).
    result = backup_utils.run_job(stored(job))
    assert result["ok"] is True and not result.get("busy"), result
    backup_utils.record_run(job["id"], result)
    assert runner.job_interrupted(stored(job)) is None and stored(job)["last_result"] == "ok"
    assert len(version_folders(job)[0]) == 2


def test_closing_after_the_outcome_was_recorded_leaves_no_marker(app_root, tmp_path, qt_app):
    """Control: the same app and close, but the run had already recorded its outcome."""
    job = make_job(tmp_path)
    report = run_app(app_root, tmp_path, job, "after_finish", "copy")
    assert events(report, "record_run(ok=True)")
    after = stored(job)
    assert "running" not in after and after["last_result"].startswith("ok")
    assert runner.job_interrupted(after) is None
    label, _ = reopen(job)
    assert (label.objectName(), label.text()) == ("StatusOk", "OK") and label.toolTip() == ""


def test_closing_just_after_the_copy_is_promoted_records_the_good_backup(
        app_root, tmp_path, qt_app):
    """Pruning, after promotion, is the one step with no abort check, so `shutdown`
    waits for the run to FINISH -- a complete, verified version is on the target.
    Before the review gate's fix its result was thrown away and the job read
    Interrupted although the backup was good; now `shutdown` records it."""
    job = make_job(tmp_path)
    report = run_app(app_root, tmp_path, job, "window", "prune")
    assert events(report, "run_job_returned(ok=True")
    assert events(report, "record_run(ok=True)")
    versions, leftovers = version_folders(job)
    assert len(versions) == 1 and leftovers == []
    manifest = Path(job["target"]).rglob(f"{versions[0]}.manifest.jsonl")
    trailer = json.loads(next(manifest).read_text(encoding="utf-8").splitlines()[-1])
    assert trailer["type"] == "end" and trailer["verified"] is True
    after = stored(job)
    assert "running" not in after and after["last_result"] == "ok"
    assert after["last_finished_utc"] == trailer["finished_utc"]
    label, _ = reopen(job)
    assert (label.objectName(), label.text()) == ("StatusOk", "OK")
    assert [line["outcome"] for line in runlog.read_runs(job["id"])] == ["ok"]


# --- positive controls for the instrument ----------------------------------------

def dead_pid():
    """The pid of a process that has definitely exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def plant(job, pid, start_time=None):
    """Store a start marker exactly as `_mark_running` would, for any owner."""
    marker = {"since": SINCE, "pid": pid, "start_time": start_time, "stamp": None, "token": "t"}
    assert jobs_store.set_running(job["id"], marker)


def test_the_reader_tells_an_orphaned_marker_from_a_live_one(app_root, tmp_path, qt_app):
    """If `reopen` always said "OK", or always "Interrupted", the tests above would
    prove nothing: here it says each, for a marker whose owner is gone and one whose
    owner (this very process) is alive."""
    live = make_job(tmp_path)
    plant(live, os.getpid(), locks.process_start_time(os.getpid()))
    label, _ = reopen(live)
    assert (label.objectName(), label.text()) == ("StatusOk", "OK")

    dead = make_job(tmp_path / "other")
    plant(dead, dead_pid())
    label, _ = reopen(dead)
    assert label.objectName() == "StatusError" and "never finished" in label.text()
    assert "crashed" in label.toolTip()

    # Only the Interrupted state lists causes: an ordinary failed run (here the
    # very message a run cancelled by closing the app records) does not.
    failed = make_job(tmp_path / "third")
    backup_utils.record_run(failed["id"], {"ok": False, "message": "Backup failed: drive is full"})
    assert stored(failed)["last_result"].startswith("failed")
    label, _ = reopen(failed)
    assert label.text() == "Failed" and "crashed" not in label.toolTip()
    # ...and a run the person stopped is Cancelled, which lists no causes either.
    stopped = make_job(tmp_path / "fourth")
    backup_utils.record_run(stopped["id"], {"ok": False, "cancelled": True,
                                            "message": "Backup cancelled"})
    label, _ = reopen(stopped)
    assert label.text() == "Cancelled" and "crashed" not in label.toolTip()


def test_the_tooltip_and_the_readme_do_not_list_closing_the_app_as_a_cause():
    """Closing the app records the run (cancelled, or the finished result), so it
    is not a cause of Interrupted; the README says what closing does instead."""
    from tabs.backup_tab import INTERRUPTED_DETAIL
    assert "closed" not in INTERRUPTED_DETAIL
    assert "unplugged" in INTERRUPTED_DETAIL and "crashed" in INTERRUPTED_DETAIL
    readme = " ".join((REPO / "README.md").read_text(encoding="utf-8").split())
    paragraph = readme[readme.index("**An interrupted run is not resumed.**"):][:900]
    assert "the app is closed" not in paragraph
    assert "Closing the app while a backup runs cancels it" in paragraph
    assert "was closed, killed or crashed" not in readme
    assert "killed or crashed before it recorded a result" in readme


# --- shutdown hands over only a backup run's result -------------------------------

@pytest.mark.parametrize("record_on_close", [True, False])
def test_shutdown_records_a_joined_backup_run_and_nothing_else(app_root, qt_app, record_on_close):
    """In-process, no event loop: the worker's queued `finished` is never delivered,
    exactly as after `app.exec()` returns. A backup run (`record_on_close=True`) has
    its result handed to its handler by `shutdown`; a restore or a measurement
    (False) is not, so closing never opens a restore's result dialog."""
    import threading
    from tabs import backup_tab
    tab = backup_tab.BackupTab()
    started, seen = threading.Event(), []

    def work(_progress, should_abort, _phase):
        """Runs until shutdown aborts it, like a copy in flight."""
        started.set()
        while not should_abort():
            threading.Event().wait(0.01)
        return {"ok": False, "cancelled": True, "message": "Backup cancelled"}

    tab._start_backup("working", work, seen.append, record_on_close=record_on_close)
    assert started.wait(10)
    tab.shutdown()
    assert tab.backup_thread is None and tab._backup_done is None
    assert seen == ([{"ok": False, "cancelled": True, "message": "Backup cancelled"}]
                    if record_on_close else [])
    tab.shutdown()                                   # a second call finds nothing to do
    assert len(seen) == (1 if record_on_close else 0)
