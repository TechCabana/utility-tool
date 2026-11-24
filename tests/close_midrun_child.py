# tests/close_midrun_child.py
# Not a test (pytest does not collect it): the REAL app, run as a child process by
# tests/test_close_midrun.py. It boots `main.main()` -- the real window, the real
# Backup tab, the real QThread worker and the real `app.exec()` -- starts a manual
# backup of a job, and closes the app while that run is in flight. Then the child
# exits the way the app exits, and writes down what it saw for the parent to read.
#
# Isolation (asserted before anything runs): APPDATA is a temp root the parent
# set, the jobs file resolves inside it, and QSettings is replaced by an IniFormat
# settings file inside the same root, so nothing can reach the real registry key
# or the real %APPDATA%. The scheduler is stubbed. Nothing here reads a .env.
#
# argv[1] is a JSON object: {"job_id", "close", "phase", "started", "report"}.
#   close  "window"        the window is closed (what the X button does)
#          "quit"          QApplication.quit() (a quit action, or a session end)
#          "after_finish"  control: wait for the run to finish and be recorded, then close
#   phase  "copy"          the first file copy blocks for a moment, so the close lands mid-copy
#          "verify"        the first re-hash blocks, so the close lands mid-verify
#          "prune"         the version is already promoted; pruning blocks (no abort check there)
import json
import os
import sys
import time

root = os.environ["CLOSE_TEST_ROOT"]
appdata = os.environ["APPDATA"]
assert os.path.normcase(appdata).startswith(os.path.normcase(root)), (appdata, root)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

args = json.loads(sys.argv[1])

import main  # noqa: E402
from PySide6 import QtCore, QtWidgets  # noqa: E402
from tabs import backup_tab  # noqa: E402
from utils import backup_utils, copying, jobs_store, runner, scheduler, verify  # noqa: E402

assert os.path.normcase(jobs_store.JOBS_FILE).startswith(os.path.normcase(root)), jobs_store.JOBS_FILE
scheduler._run = lambda command: (True, "")      # no real schtasks, whatever happens

# QSettings(org, app) is the REGISTRY on Windows; every one built by main.py goes
# to an ini file inside the temp root instead, and each is asserted.
RealSettings = QtCore.QSettings
_made = []


def ini_settings(*_ignored):
    """A settings object on a throwaway ini file, never the registry."""
    path = os.path.join(root, f"settings-{len(_made)}.ini")
    settings = RealSettings(path, RealSettings.Format.IniFormat)
    assert os.path.normcase(settings.fileName()).startswith(os.path.normcase(root)), settings.fileName()
    _made.append(path)
    return settings


QtCore.QSettings = ini_settings

report = {"events": [], "close": args["close"], "phase": args["phase"]}


def note(event):
    """Append a timestamped event, in the order the app lived it."""
    report["events"].append(event)
    print("EVENT", event, file=sys.stderr, flush=True)


# --- a slow copy / verify, so the close can land inside the phase asked for ------
_real_copy, _real_rehash = copying._hash_copy, verify._rehash


def _signal_started():
    """Tell the parent's wait loop (below) that the phase has begun, then block briefly."""
    if not os.path.exists(args["started"]):
        open(args["started"], "w").close()
        note("phase_started")
    time.sleep(0.4)


if args["phase"] == "copy":
    copying._hash_copy = lambda src, dst: (_signal_started(), _real_copy(src, dst))[1]
elif args["phase"] == "verify":
    verify._rehash = lambda path: (_signal_started(), _real_rehash(path))[1]
else:
    # After the version was promoted: the one step `run_job` does not check for an abort.
    _real_prune = runner.prune_versions
    runner.prune_versions = lambda *a, **k: (_signal_started(), _real_prune(*a, **k))[1]

# --- what the app does at the end, observed without changing it ------------------
# (Replacing a connected slot such as `shutdown` or `on_backup_finished` with a
# wrapper crashes PySide intermittently, so slots are never wrapped: the engine
# calls and plain-function signal observers below are what is watched.)
_real_record = backup_utils.record_run
_real_run_job = backup_utils.run_job


def record_run(job_id, result):
    """The call that writes the outcome AND clears the start marker."""
    note(f"record_run(ok={result.get('ok')})")
    return _real_record(job_id, result)


def run_job(*a, **k):
    """The engine call on the worker thread; its result is what the worker emits."""
    result = _real_run_job(*a, **k)
    note(f"run_job_returned(ok={result.get('ok')}, message={result.get('message')!r})")
    return result


backup_utils.record_run = record_run
backup_utils.run_job = run_job

# --- the window, captured, and the run + the close driven from the real event loop
RealWindow = main.MainWindow
holder = {}


class CapturingWindow(RealWindow):
    """The real MainWindow; remembers itself and schedules the drive."""

    def __init__(self, settings):
        super().__init__(settings)
        holder["window"] = self
        QtCore.QTimer.singleShot(0, begin)


main.MainWindow = CapturingWindow
state = {"started": False, "closed": False}


def begin():
    """Open the Backup page; `check` then drives the run and the close."""
    window = holder["window"]
    window.go_to_tab(1, animate=False)
    holder["tab"] = window.stacked.widget(1)
    assert isinstance(holder["tab"], backup_tab.BackupTab)
    poll = QtCore.QTimer(window)
    poll.setInterval(25)
    poll.timeout.connect(check)
    poll.start()
    holder["poll"] = poll


def check():
    """Start the run once the page's own first measurement is done, then close the
    app the moment the chosen condition holds, once."""
    tab = holder["tab"]
    if not state["started"]:
        if tab.backup_thread is not None:      # opening the page measures drift first
            return
        state["started"] = True
        note("run_requested")
        tab.run_backup_job(backup_utils.get_job(args["job_id"]))
        return
    if state["closed"]:
        return
    if args["close"] == "after_finish":
        ready = any(e.startswith("record_run") for e in report["events"])
    else:
        ready = os.path.exists(args["started"])
    if not ready:
        return
    state["closed"] = True
    holder["poll"].stop()
    thread = tab.backup_thread
    note(f"closing_via_{args['close']}(worker_thread_running={thread.isRunning() if thread else None})")
    if args["close"] == "quit":
        QtWidgets.QApplication.instance().quit()
    else:
        holder["window"].close()


rc = main.main()
tab = holder["tab"]
note(f"app_exec_returned({rc})")
# After the loop ended: the worker thread is gone, and if `on_backup_finished` had
# ever run for the run in flight it would have taken the handler (`_backup_done`).
report["worker_thread_after"] = None if tab.backup_thread is None else "still set"
report["finish_handler_never_ran"] = tab._backup_done is not None
report["pid"] = os.getpid()
report["settings_files"] = _made
with open(args["report"], "w", encoding="utf-8") as handle:
    json.dump(report, handle)
sys.exit(rc)
