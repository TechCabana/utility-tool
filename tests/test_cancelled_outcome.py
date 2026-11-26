# tests/test_cancelled_outcome.py
# Card bricnEaC (owner decision 2026-11-26): FAILED means something outside the
# person's control went wrong; CANCELLED means the person deliberately stopped the
# run. A run stopped through the abort flag (the Backup tab worker's `abort()`, which
# a Cancel and closing the app mid-run both raise) stores the plain word `cancelled`
# in `last_result`, shows as "Cancelled" in the neutral `Hint` style, writes ONE
# `cancelled` run-log line, and leaves the previous good backup as the newest
# verified version. Every other failure (a full drive, a failed verification, a
# killed process = Interrupted) stays exactly as it was.
#
# There is no Cancel BUTTON in the tab yet (it is a later card); `abort()` on the
# worker is the one hook it will call, so "pressing Cancel" below is that call, made
# mid-copy on the real tab, real QThread worker and real Qt event loop. Closing the
# app mid-copy / mid-verify is covered by tests/test_close_midrun.py (a child process
# running the real app).
import errno
import hashlib
import json
import os
import threading

import pytest

from test_skipped_status import lock_files
from test_target_failures import fail_destination_writes
from utils import backup_utils, copying, jobs_store, runlog, runner

JUNK = [None, 42, 1.5, True, [], {}, b"cancelled", ["cancelled"]]


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture(autouse=True)
def no_scheduler(monkeypatch):
    """No real schtasks, whatever a Backup tab does while loading."""
    from utils import scheduler
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))


@pytest.fixture
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def job(tmp_path):
    """A saved manual job over six small files, retention 3."""
    source = tmp_path / "src"
    source.mkdir()
    for i in range(6):
        (source / f"f{i}.txt").write_text(f"contents {i}" * 50, encoding="utf-8")
    target = tmp_path / "dst"
    target.mkdir()
    made = backup_utils.new_job("Cancel", str(source), str(target), schedule="manual", retention=3)
    backup_utils.save_job(made)
    return made


def stored(job):
    """The job as the jobs file holds it right now."""
    return backup_utils.get_job(job["id"])


def good_backup(job):
    """One real, recorded, verified run; returns its version stamp."""
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    backup_utils.record_run(job["id"], result)
    return result["version"]


def two_good_backups(job):
    """Two good versions, then retention 1: a run that PRUNED would now delete the
    older one, so "nothing pruned" is something the fixture could have shown wrong."""
    first = good_backup(job)
    second = good_backup(job)                 # the stamp claim waits for a new second
    assert first != second
    jobs_store._save_job(dict(stored(job), retention=1))
    job = stored(job)
    assert job["retention"] == 1 and backup_utils.version_names(job) == [first, second]
    return job, first, second


def spin_until(done, seconds=60):
    """Run the REAL Qt event loop until `done()`; never a hand-rolled pump."""
    from PySide6 import QtCore
    loop = QtCore.QEventLoop()
    poll = QtCore.QTimer()
    poll.setInterval(20)
    poll.timeout.connect(lambda: loop.quit() if done() else None)
    QtCore.QTimer.singleShot(int(seconds * 1000), loop.quit)
    poll.start()
    loop.exec()
    poll.stop()
    assert done(), "the worker did not finish in the real event loop"


class Dialogs:
    """Records every dialog a test could have opened (none is allowed to show)."""

    def __init__(self, monkeypatch):
        from PySide6 import QtWidgets
        self.opened = []
        for name in ("warning", "critical", "information", "question"):
            monkeypatch.setattr(QtWidgets.QMessageBox, name,
                                staticmethod(lambda *a, _n=name, **k: self.opened.append(_n)))
        monkeypatch.setattr(QtWidgets.QDialog, "exec", lambda s: self.opened.append("exec") or 0)


def block_copy(monkeypatch):
    """Make the next file copy wait until released, so the run is provably mid-copy."""
    started, release, real = threading.Event(), threading.Event(), copying._hash_copy

    def slow(src, dst):
        started.set()
        assert release.wait(30), "the test never released the copy"
        return real(src, dst)

    monkeypatch.setattr(copying, "_hash_copy", slow)
    return started, release


def status_cell(tab, job):
    """The Status label of `job`'s card on a live tab (the one next to its name)."""
    from PySide6 import QtWidgets
    from tabs import backup_tab
    rows = [r for r in tab.findChildren(backup_tab.JobRow) if r.job["id"] == job["id"]]
    assert len(rows) == 1
    texts = {"Cancelled", "Failed", "OK", "Never run"}
    found = [l for l in rows[0].findChildren(QtWidgets.QLabel)
             if l.text() in texts or l.text().startswith(("OK,", "Interrupted"))]
    assert len(found) == 1, [(l.objectName(), l.text()) for l in found]
    return found[0]


def run_on_tab(tab, job):
    """Start `job` on the tab's real worker thread; returns once it is running."""
    tab._start_run(job)
    assert tab.backup_thread is not None


def sha_tree(root):
    """{relative path: sha256} for every file under `root`, computed independently."""
    found = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                found[os.path.relpath(full, root)] = hashlib.sha256(handle.read()).hexdigest()
    return found


def leftovers(job):
    """Half-finished copies and temp manifests under the job's folder."""
    return sorted(n for n in os.listdir(backup_utils.job_root(job))
                  if n.endswith((".partial", ".tmp")))


# --- the Done-when: Cancel mid-copy ---------------------------------------------------------------

def test_cancel_mid_copy_stores_and_shows_cancelled_and_keeps_the_good_backup(
        qt_app, job, monkeypatch):
    from tabs import backup_tab
    job, first, second = two_good_backups(job)
    before = stored(job)
    before["last_run"] = "2026-11-20T02:00:00"                 # so a new last_run is visible
    jobs_store._save_job(before)
    second_hashes = sha_tree(backup_utils.version_path(job, second))
    runs_before = len(runlog.read_runs(job["id"]))
    dialogs = Dialogs(monkeypatch)

    tab = backup_tab.BackupTab()
    started, release = block_copy(monkeypatch)
    run_on_tab(tab, stored(job))
    assert started.wait(10)                                    # provably mid-copy
    assert tab.backup_worker is not None
    tab.backup_worker.abort()                                  # what a Cancel button calls
    release.set()
    spin_until(lambda: tab.backup_thread is None)

    # stored: the plain word, a new last_run, and nothing the good run set was touched
    after = stored(job)
    assert after["last_result"] == "cancelled"
    assert after["last_run"] != "2026-11-20T02:00:00" and "running" not in after
    for key in ("last_finished_utc", "last_files", "last_pruning_held", "last_baseline_files",
                "target_volume", "retention"):
        assert after.get(key) == before.get(key), key

    # shown: neutral Hint, never the error or warning style, with the plain tooltip
    label = status_cell(tab, job)
    assert (label.objectName(), label.text()) == ("Hint", "Cancelled")
    assert label.objectName() not in ("StatusError", "StatusWarn")
    assert label.toolTip() == backup_tab.CANCELLED_DETAIL
    assert "You stopped this backup before it finished" in label.toolTip()
    assert "Nothing was changed in your earlier backups" in label.toolTip()
    assert label.accessibleName() == "Cancel: Cancelled"
    assert label.accessibleDescription() == backup_tab.CANCELLED_DETAIL
    assert "Cancelled" in label.accessibleName() and "Failed" not in label.accessibleName()
    # the status line under the table is calm too, and no dialog opened
    assert tab.backup_status.objectName() == "Hint"
    assert tab.backup_status.text().startswith("Backup cancelled.")
    assert dialogs.opened == []

    # the run log: ONE cancelled line for this run, no failed / interrupted beside it
    outcomes = [line["outcome"] for line in runlog.read_runs(job["id"])]
    assert len(outcomes) == runs_before + 1 and outcomes[0] == "cancelled"
    assert outcomes.count("cancelled") == 1 and not {"failed", "interrupted"} & set(outcomes)
    assert runlog.read_runs(job["id"])[0]["message"] == "Backup cancelled"

    # the previous good backup is still the newest verified version, restore's default,
    # and nothing was pruned or left half-copied
    assert backup_utils.version_names(job) == [first, second]
    assert backup_utils.latest_version(job) == second
    assert backup_utils.is_verified(job, second)
    plan = backup_utils.restore_candidates(job)
    assert plan["preferred"] == second and plan["latest"] == second and not plan["fallback"]
    assert sha_tree(backup_utils.version_path(job, second)) == second_hashes
    assert leftovers(job) == []

    # positive control for "nothing pruned": the same job, run to the end, DOES prune
    # down to retention 1, so the cancelled run really left the older version alone
    result = backup_utils.run_job(stored(job))
    assert result["ok"] and len(backup_utils.version_names(job)) == 1


def test_a_disk_full_failure_in_the_same_harness_still_shows_failed(qt_app, job, monkeypatch):
    """Positive control for the test above: the same tab, worker and loop, but the
    run FAILS (ENOSPC), and the card says Failed in the error style -- so the
    `Hint` / `Cancelled` assertions there could have said something else."""
    from tabs import backup_tab
    first = good_backup(job)
    before = stored(job)
    Dialogs(monkeypatch)
    fail_destination_writes(monkeypatch, backup_utils.job_root(job), 2,
                            OSError(errno.ENOSPC, "No space left on device"))

    tab = backup_tab.BackupTab()
    run_on_tab(tab, stored(job))
    spin_until(lambda: tab.backup_thread is None)

    after = stored(job)
    assert after["last_result"].startswith("failed: ") and "The backup drive is full" in after["last_result"]
    label = status_cell(tab, job)
    assert (label.objectName(), label.text()) == ("StatusError", "Failed")
    assert label.toolTip() != backup_tab.CANCELLED_DETAIL
    assert tab.backup_status.objectName() == "StatusError"
    assert runlog.read_runs(job["id"])[0]["outcome"] == "failed"
    assert backup_utils.version_names(job) == [first] and after["last_finished_utc"] == before["last_finished_utc"]


def test_a_failed_verification_still_shows_failed(qt_app, job, monkeypatch):
    """Cancelled is only the abort flag: a verification failure is Failed."""
    from utils import verify
    monkeypatch.setattr(verify, "_verify_copy", lambda *a, **k: (1, 1, ["f0.txt"]))
    result = backup_utils.run_job(job)
    assert not result["ok"] and "cancelled" not in result
    backup_utils.record_run(job["id"], result)
    assert stored(job)["last_result"].startswith("failed: Backup verification failed")
    assert runlog.read_runs(job["id"])[0]["outcome"] == "failed"


# --- the marker: one source for the stored word and the run-log word ------------------------------

@pytest.mark.parametrize("result, word", [
    ({"ok": False, "cancelled": True, "message": "Backup cancelled"}, "cancelled"),
    ({"ok": False, "cancelled": True, "message": "anything at all"}, "cancelled"),
    ({"ok": False, "message": "Backup cancelled"}, "failed"),         # the text is not the marker
    ({"ok": False, "cancelled": 1, "message": "x"}, "failed"),        # exactly True, as `interrupted`
    ({"ok": False, "cancelled": False, "message": "x"}, "failed"),
    ({"ok": True, "cancelled": True, "message": "x"}, "ok"),          # a finished run is not cancelled
    ({"ok": False, "busy": True, "cancelled": True}, "busy"),
    ({"ok": False, "interrupted": True, "cancelled": True}, "interrupted"),
    ({"ok": False, "message": "Backup failed: disk full"}, "failed"),
])
def test_the_outcome_word_follows_the_cancelled_marker_only(result, word):
    assert jobs_store.outcome_word(result) == word
    assert runlog._outcome(result) == word                     # the log and the job share it
    if word not in ("busy", "interrupted"):                    # those win in `outcome_word` itself
        assert backup_utils.run_cancelled(result) is (word == "cancelled")


def test_only_the_abort_paths_of_the_engine_carry_the_marker(job):
    cancelled = backup_utils.run_job(job, should_abort=lambda: True)
    assert cancelled["cancelled"] is True and cancelled["ok"] is False
    assert cancelled["message"] == "Backup cancelled"
    assert runlog.read_runs(job["id"])[0]["outcome"] == "cancelled"
    # a run that fails for another reason, and one that succeeds, never carry it
    bad = dict(job, source=os.path.join(job["source"], "missing"))
    assert "cancelled" not in backup_utils.run_job(bad)
    assert "cancelled" not in backup_utils.run_job(job)


def test_cancelling_during_verification_is_cancelled_too(job, monkeypatch):
    from utils import verify
    state = {"abort": False}
    real = verify._rehash

    def stop_on_first_rehash(path):
        state["abort"] = True
        return real(path)

    monkeypatch.setattr(verify, "_rehash", stop_on_first_rehash)
    result = backup_utils.run_job(job, should_abort=lambda: state["abort"])
    assert state["abort"] and result["ok"] is False and result["cancelled"] is True
    backup_utils.record_run(job["id"], result)
    assert stored(job)["last_result"] == "cancelled"
    assert backup_utils.version_names(job) == [] and leftovers(job) == []


def test_a_sentence_the_engine_appends_to_the_message_does_not_hide_a_cancel(job):
    """`_with_sweep` and a recovered lock add text AFTER the message; the marker does
    not care (a text test would have to)."""
    result = dict(backup_utils.run_job(job, should_abort=lambda: True),
                  message="Backup cancelled. Recovered a stale lock left by process 7.")
    assert backup_utils.outcome_word(result) == "cancelled"
    backup_utils.record_run(job["id"], result)
    assert stored(job)["last_result"] == "cancelled"


# --- what a cancelled run stores, next to a failed one --------------------------------------------

def test_a_cancelled_run_leaves_exactly_what_a_failed_run_leaves_but_the_word(job, monkeypatch):
    """Decision: a stopped run copied nothing that counts, so the sibling counts go to
    zero as a failed run's do, and the last-good fields stay (only an ok run writes
    them). Proved by recording a failed and a cancelled run over the SAME good run."""
    with monkeypatch.context() as patched:
        lock_files(patched, {"f0.txt"})
        good_backup(job)                                       # ok_skipped: counts to clear
    base = stored(job)
    assert base["last_result"] == "ok_skipped" and base["last_skipped"] == 1

    def record(result):
        jobs_store._save_job(dict(base))                       # the same starting point
        return jobs_store.record_run(job["id"], result)

    cancelled = record({"ok": False, "cancelled": True, "message": "Backup cancelled"})
    failed = record({"ok": False, "message": "Backup cancelled"})
    ignore = ("last_result", "last_run")
    assert {k: v for k, v in cancelled.items() if k not in ignore} == \
        {k: v for k, v in failed.items() if k not in ignore}
    assert cancelled["last_result"] == "cancelled" and failed["last_result"].startswith("failed")
    assert (cancelled["last_skipped"], cancelled["last_changed"], cancelled["last_links"]) == (0, 0, 0)
    for key in ("last_finished_utc", "last_files", "last_pruning_held", "last_baseline_files"):
        assert cancelled[key] == base[key], key


def test_a_cancelled_run_does_not_clear_a_pruning_hold_or_the_hold_flags(job):
    held = dict(stored(job), last_result="ok_pruning_held", last_pruning_held=True,
                last_baseline_files=40, last_files=10, last_run="2026-11-20T02:00:00",
                last_finished_utc="2026-11-20T01:00:00Z")
    jobs_store._save_job(held)
    after = jobs_store.record_run(job["id"], {"ok": False, "cancelled": True, "message": "Backup cancelled"})
    assert after["last_result"] == "cancelled"
    assert (after["last_pruning_held"], after["last_baseline_files"], after["last_files"],
            after["last_finished_utc"]) == (True, 40, 10, "2026-11-20T01:00:00Z")


# --- the readers: Cancelled is neither a success nor a failure --------------------------------------

CASES = [
    # stored value, ok, cancelled
    ("ok", True, False), ("ok_skipped", True, False), ("ok_pruning_held", True, False),
    ("cancelled", False, True),
    ("failed: Backup cancelled", False, True),                 # legacy, mapped at read time
    ("failed: Backup cancelled. Cleaned up 1 abandoned leftover(s) from earlier runs.", False, True),
    ("failed: Backup cancelled. Recovered a stale lock left by process 7.", False, True),
    ("failed: boom", False, False), ("failed: ", False, False), ("failed", False, False),
    ("failed: Backup cancelled by the disk", False, False),    # not the engine's sentence
    ("failed: Backup failed: Backup cancelled", False, False),
    ("failed: backup cancelled", False, False),                # exact, case included
    ("interrupted: started 2026-11-20T02:00:00 and never finished", False, False),
    ("Cancelled", False, False), ("cancelled: x", False, False), (" cancelled", False, False),
    ("", False, False),
] + [(j, False, False) for j in JUNK]


@pytest.mark.parametrize("value, ok, cancelled", CASES, ids=[repr(c[0])[:40] for c in CASES])
def test_result_is_ok_and_result_is_cancelled_table(value, ok, cancelled):
    assert backup_utils.result_is_ok(value) is ok
    assert backup_utils.result_is_cancelled(value) is cancelled
    assert not (ok and cancelled)                              # never both
    word = backup_utils.job_result({"last_result": value})
    assert isinstance(word, str) and (word == "cancelled") is cancelled


@pytest.mark.parametrize("value, ok, cancelled", CASES, ids=[repr(c[0])[:40] for c in CASES])
def test_every_tab_reader_agrees_with_the_table(qt_app, value, ok, cancelled):
    from tabs.backup_tab import CANCELLED_DETAIL, JobRow
    row = {"id": "abc12345", "name": "J", "last_run": "2026-11-20T02:00:00", "last_result": value}
    text, style = JobRow._status(None, row)                    # never raises
    detail = JobRow._warning_detail(row)
    if cancelled:
        assert (text, style) == ("Cancelled", "Hint") and detail == CANCELLED_DETAIL
    else:
        assert text != "Cancelled" and CANCELLED_DETAIL not in detail
        # an ok word keeps its ok / warning style; everything else is still a failure
        assert style in (("StatusOk", "StatusWarn") if ok else ("StatusError",))
    assert JobRow(row) is not None                             # the card builds for every value


def test_the_legacy_text_is_mapped_for_display_and_the_file_is_never_rewritten(qt_app, job):
    from tabs import backup_tab
    legacy = dict(stored(job), last_run="2026-11-20T02:00:00", last_result="failed: Backup cancelled")
    jobs_store._save_job(legacy)
    path = jobs_store.JOBS_FILE
    with open(path, "rb") as handle:
        before = handle.read()

    tab = backup_tab.BackupTab()
    tab.refresh_backup()
    label = status_cell(tab, job)
    assert (label.objectName(), label.text()) == ("Hint", "Cancelled")
    assert label.toolTip() == backup_tab.CANCELLED_DETAIL
    assert stored(job)["last_result"] == "failed: Backup cancelled"   # read at READ time only
    with open(path, "rb") as handle:
        assert handle.read() == before

    other = dict(legacy, last_result="failed: The backup drive is full")      # control: any other failed:
    jobs_store._save_job(other)
    fresh = backup_tab.BackupTab()                            # a fresh tab: the old rows await deleteLater
    fresh.refresh_backup()
    label = status_cell(fresh, job)
    assert (label.objectName(), label.text()) == ("StatusError", "Failed")


def test_a_cancelled_card_shows_the_last_run_time_like_other_states(qt_app):
    from tabs.backup_tab import JobRow
    from utils.fs_utils import short_when
    row = {"id": "abc12345", "name": "J", "last_run": "2026-11-20T02:00:00", "last_result": "cancelled"}
    assert JobRow._last_run(None, row) == short_when("2026-11-20T02:00:00") != "-"
    never = dict(row, last_run="")
    assert JobRow._status(None, never) == ("Never run", "Hint")        # no last_run: still never run


def test_a_cancelled_card_keeps_an_earlier_pruning_hold_note(qt_app):
    from tabs.backup_tab import CANCELLED_DETAIL, HELD_AFTER_FAILURE, JobRow
    row = {"id": "abc12345", "name": "J", "last_run": "2026-11-20T02:00:00",
           "last_result": "cancelled", "last_pruning_held": True, "last_baseline_files": 9, "last_files": 3}
    assert JobRow._warning_detail(row) == CANCELLED_DETAIL + "\n\n" + HELD_AFTER_FAILURE


def test_the_cancelled_style_is_a_token_the_stylesheet_already_has(qt_app):
    """No new colour: `Hint` is the muted caption style `Never run` already uses."""
    from tabs import backup_tab
    from pathlib import Path
    qss = (Path(backup_tab.__file__).resolve().parent.parent / "styles" / "theme.qss.tmpl").read_text(encoding="utf-8")
    assert "QLabel#Hint" in qss
    row = {"id": "a", "name": "J", "last_run": "2026-11-20T02:00:00"}
    assert backup_tab.JobRow._status(None, dict(row, last_result="cancelled"))[1] == \
        backup_tab.JobRow._status(None, dict(row, last_run=""))[1] == "Hint"


# --- _finish_run: what the tab says right after a run ---------------------------------------------

def test_finish_run_says_cancelled_calmly_and_opens_nothing(qt_app, job, monkeypatch):
    from tabs import backup_tab
    dialogs = Dialogs(monkeypatch)
    tab = backup_tab.BackupTab()
    tab._finish_run(job, {"ok": False, "cancelled": True, "message": "Backup cancelled"})
    assert tab.backup_status.objectName() == "Hint"
    assert tab.backup_status.text() == "Backup cancelled. Your earlier backups were not changed."
    assert stored(job)["last_result"] == "cancelled"
    assert dialogs.opened == [] and tab._remeasure is False

    # with the engine's own trailing sentence the line still reads once, with one stop
    tab._finish_run(job, {"ok": False, "cancelled": True,
                          "message": "Backup cancelled. Cleaned up 1 abandoned leftover(s) from earlier runs."})
    assert "runs.. " not in tab.backup_status.text() and tab.backup_status.objectName() == "Hint"

    # controls: a failure is still the red line, an ok run still reports and remeasures
    tab._finish_run(job, {"ok": False, "message": "Backup failed: The backup drive is full."})
    assert tab.backup_status.objectName() == "StatusError"
    tab._finish_run(job, {"ok": True, "message": "Backed up 6 files."})
    assert tab.backup_status.objectName() == "Hint" and tab._remeasure is True
    assert stored(job)["last_result"] == "ok"


# --- the other readers ------------------------------------------------------------------------------

def test_the_headless_runner_exits_one_for_every_non_ok_result_and_ctrl_c_is_unchanged(
        job, monkeypatch, capsys):
    """A headless run passes no abort flag, so it cannot produce a cancelled result; the
    branch is still pinned: a non-ok run exits 1, ok exits 0, and the stored word is the
    run's own. A Ctrl+C is NOT a cancelled run: it is recorded `failed: KeyboardInterrupt`
    exactly as before (owner question in the card's report)."""
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))
    real_run_job = backup_utils.run_job
    cancelled = {"ok": False, "cancelled": True, "message": "Backup cancelled"}
    monkeypatch.setattr(backup_utils, "run_job", lambda *a, **k: dict(cancelled))
    assert main.run_backup_job_cli(job["id"]) == 1
    assert "[ERROR] Backup cancelled" in capsys.readouterr().out
    assert stored(job)["last_result"] == "cancelled"
    monkeypatch.setattr(backup_utils, "run_job", lambda *a, **k: {"ok": True, "message": "done"})
    assert main.run_backup_job_cli(job["id"]) == 0 and stored(job)["last_result"] == "ok"
    monkeypatch.setattr(backup_utils, "run_job", lambda *a, **k: {"ok": False, "message": "boom"})
    assert main.run_backup_job_cli(job["id"]) == 1 and stored(job)["last_result"] == "failed: boom"
    monkeypatch.setattr(backup_utils, "run_job", real_run_job)

    def raising(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner, "copy_tree", raising)
    with pytest.raises(KeyboardInterrupt):
        backup_utils.run_job(stored(job))
    assert stored(job)["last_result"].startswith("failed: KeyboardInterrupt")
    assert runlog.read_runs(job["id"])[0]["outcome"] == "failed"


def test_a_cancelled_restore_does_not_touch_the_jobs_file(job, tmp_path):
    """A restore is its own path (card 102): its result also carries `cancelled`, and
    nothing about it is recorded on the job."""
    good_backup(job)
    with open(jobs_store.JOBS_FILE, "rb") as handle:
        before = handle.read()
    result = backup_utils.restore_version(job, None, str(tmp_path / "out"), should_abort=lambda: True)
    assert result["ok"] is False and result["cancelled"] is True
    with open(jobs_store.JOBS_FILE, "rb") as handle:
        assert handle.read() == before
    assert stored(job)["last_result"] == "ok"
    assert [line["outcome"] for line in runlog.read_runs(job["id"])] == ["ok"]


def test_the_run_log_line_is_valid_json_with_the_cancelled_outcome(job):
    backup_utils.record_run(job["id"], backup_utils.run_job(job, should_abort=lambda: True))
    with open(runlog.log_path(job["id"]), encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    assert [line["outcome"] for line in lines] == ["cancelled"]
    assert "cancelled" in runlog.OUTCOMES
    assert stored(job)["last_result"] == lines[0]["outcome"]            # the job and the log agree
