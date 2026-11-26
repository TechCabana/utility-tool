# tests/test_stored_outcome_word.py
# Card C5HuzNVK: `last_result` stores the run's real outcome word (`ok`,
# `ok_skipped`, `ok_changed`, `ok_links`, `ok_pruning_held`; `failed: ...` and
# `interrupted: ...` keep their text forms) instead of a plain `ok`. The word is
# the HEADLINE; the sibling count fields are the DETAIL, and the Backup tab builds
# its text from the siblings only, so a job saved by an older build (plain `ok`
# beside a count) must display exactly like a new one.
#
# Real runs go through the same fault injections the sibling suites use
# (`copying.open` for an unreadable file, `copying._after_read` for a file that
# changes mid-copy, a real junction for a shortcut). The golden run-log lines at
# the end were produced by the code BEFORE this card and pinned here.
import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

from test_changed_during_copy import Meddler, VICTIM, make_job as make_changing_job
from test_skipped_status import lock_files
from utils import backup_utils, jobs_store, runlog, runner

windows_only = pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
OK_WORDS = ("ok", "ok_pruning_held", "ok_skipped", "ok_changed", "ok_links")


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def job(tmp_path):
    """A saved job over eight small files (six at the root, two in `sub/`)."""
    return make_changing_job(tmp_path)


@pytest.fixture
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def run_and_record(job):
    """Run the job and record the result, as the tab and the headless run do."""
    result = backup_utils.run_job(job)
    backup_utils.record_run(job["id"], result)
    return result, backup_utils.get_job(job["id"])


def last_line(job):
    """The newest run-log line of `job`."""
    return runlog.read_runs(job["id"])[0]


def status_of(stored):
    """The tab's Status text and object name for a stored job."""
    from tabs.backup_tab import JobRow
    return JobRow._status(None, stored)


def status_label(row):
    """The one Status* label of a JobRow."""
    from PySide6 import QtWidgets
    found = [label for label in row.findChildren(QtWidgets.QLabel)
             if label.objectName().startswith("Status")]
    assert len(found) == 1, [(l.objectName(), l.text()) for l in found]
    return found[0]


def record(**fields):
    """A finished job record as the jobs file holds it, plus `fields`."""
    return {"id": "abc12345", "name": "J", "last_run": "2026-11-20T02:00:00", **fields}


# --- the Done-when: real runs store the word, the tab text is unchanged ---------------

def test_a_run_that_skipped_a_file_stores_ok_skipped_and_the_text_is_unchanged(
        qt_app, job, monkeypatch):
    lock_files(monkeypatch, {"a01.txt"})
    result, stored = run_and_record(job)

    assert result["ok"] and result["not_copied"] == 1
    assert stored["last_result"] == "ok_skipped"
    assert last_line(job)["outcome"] == stored["last_result"]    # the log and the job agree
    assert status_of(stored) == ("OK, 1 skipped", "StatusWarn")
    assert stored["last_skipped"] == 1 and stored["last_skipped_sample"] == ["a01.txt"]


def test_a_run_with_a_file_that_changed_stores_ok_changed_and_the_text_is_unchanged(
        qt_app, job, monkeypatch):
    Meddler(monkeypatch, {VICTIM: 99})
    result, stored = run_and_record(job)

    assert result["ok"] and result["changed"] == 1
    assert stored["last_result"] == "ok_changed"
    assert last_line(job)["outcome"] == stored["last_result"]
    assert status_of(stored) == ("OK, 1 changed during backup", "StatusWarn")


@windows_only
def test_a_run_that_did_not_follow_a_shortcut_stores_ok_links_and_the_text_is_unchanged(
        qt_app, tmp_path, job):
    import _winapi
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep-out.txt").write_text("not in the version")
    link = tmp_path / "src" / "Photos"
    _winapi.CreateJunction(str(elsewhere), str(link))
    try:
        result, stored = run_and_record(job)
    finally:
        os.rmdir(link)                                           # the link itself, never its target
    assert (elsewhere / "keep-out.txt").read_text() == "not in the version"

    assert result["ok"] and result["links_skipped"] == 1
    assert stored["last_result"] == "ok_links"
    assert last_line(job)["outcome"] == stored["last_result"]
    assert status_of(stored) == ("OK, 1 shortcut not followed", "StatusWarn")


def test_a_clean_run_stores_plain_ok(qt_app, job):
    _, stored = run_and_record(job)

    assert stored["last_result"] == "ok" and last_line(job)["outcome"] == "ok"
    assert status_of(stored) == ("OK", "StatusOk")


def test_a_run_with_skipped_and_changed_stores_one_word_and_the_text_comes_from_the_siblings(
        qt_app, job, monkeypatch):
    lock_files(monkeypatch, {"a01.txt"})
    Meddler(monkeypatch, {VICTIM: 99})
    _, stored = run_and_record(job)

    # ONE word, the highest-ranked (skipped outranks changed) ...
    assert stored["last_result"] == "ok_skipped" == last_line(job)["outcome"]
    # ... and every count survives in the siblings, which the tab reads.
    assert (stored["last_skipped"], stored["last_changed"], stored["last_links"]) == (1, 1, 0)
    assert status_of(stored) == ("OK, 1 skipped, 1 changed during backup", "StatusWarn")


# --- the one ranking, over what record_run is given ---------------------------------------

HELD = {"pruning_held": True, "baseline_files": 10, "files": 6}
RANKING = [
    ("clean", {"ok": True}, "ok"),
    ("skipped through errors", {"ok": True, "errors": ["a.txt: denied"]}, "ok_skipped"),
    ("skipped through not_copied", {"ok": True, "not_copied": 2}, "ok_skipped"),
    ("changed", {"ok": True, "changed": 1}, "ok_changed"),
    ("links", {"ok": True, "links_skipped": 3}, "ok_links"),
    ("held", {"ok": True, **HELD}, "ok_pruning_held"),
    ("held outranks skipped", {"ok": True, "not_copied": 1, **HELD}, "ok_pruning_held"),
    ("skipped outranks changed", {"ok": True, "not_copied": 1, "changed": 1}, "ok_skipped"),
    ("changed outranks links", {"ok": True, "changed": 1, "links_skipped": 1}, "ok_changed"),
    ("everything", {"ok": True, "not_copied": 1, "changed": 1, "links_skipped": 1, **HELD},
     "ok_pruning_held"),
    ("held flag must be exactly True", {"ok": True, "pruning_held": "yes"}, "ok"),
    ("junk counts count for nothing",
     {"ok": True, "changed": "3", "links_skipped": -1, "not_copied": None}, "ok"),
    ("failed", {"ok": False, "message": "drive gone"}, "failed: drive gone"),
    ("failed keeps no leftovers",
     {"ok": False, "message": "boom", "errors": ["a: x"], "changed": 1, "links_skipped": 1,
      **HELD}, "failed: boom"),
    # Card bricnEaC: a run the person stopped stores the plain word; the text alone
    # is not the marker, so a failure that merely says so stays a failure.
    ("a stopped run is stored as cancelled",
     {"ok": False, "cancelled": True, "message": "Backup cancelled"}, "cancelled"),
    ("a failure that only says cancelled stays failed",
     {"ok": False, "message": "Backup cancelled"}, "failed: Backup cancelled"),
]


@pytest.mark.parametrize("label, result, word", RANKING, ids=[r[0] for r in RANKING])
def test_record_run_stores_the_word_the_run_log_ranks(tmp_path, label, result, word):
    saved = backup_utils.new_job("R", str(tmp_path / "s"), str(tmp_path / "t"), schedule="manual")
    backup_utils.save_job(saved)
    stored = backup_utils.record_run(saved["id"], result)

    assert stored["last_result"] == word
    assert backup_utils.get_job(saved["id"])["last_result"] == word       # on disk, not just returned
    now = datetime.now()
    line = runlog.build_line(saved, result, "manual", now, now)
    assert line["outcome"] == jobs_store.outcome_word(result)             # one function for both
    if result["ok"]:
        assert line["outcome"] == word                                    # the log's word IS the stored word
    else:                                                                 # a failed run finished nothing
        assert (stored["last_skipped"], stored["last_changed"], stored["last_links"]) == (0, 0, 0)
        assert (line["skipped"], line["changed"], line["links"]) == (0, 0, 0)


def test_the_outcome_function_keeps_the_words_the_run_log_has_always_had():
    f = jobs_store.outcome_word
    assert f({"busy": True, "ok": True}) == "busy"
    assert f({"interrupted": True}) == "interrupted"
    assert f({"hold_released": True, "ok": True}) == "hold_released"
    assert f({"ok": False, "cancelled": True, "message": "Backup cancelled"}) == "cancelled"
    assert f({"ok": False, "message": "Backup cancelled"}) == "failed"     # card bricnEaC: the marker, not the text
    assert f({"ok": False, "message": "x"}) == "failed"
    assert f({"ok": True, "pruning_held": True}) == "ok_pruning_held"
    assert set(OK_WORDS) <= set(runlog.OUTCOMES)                           # the log can say every stored word


# --- older records display exactly like new ones ----------------------------------------------

SIBLINGS = [
    ("skipped", {"last_skipped": 1, "last_skipped_sample": ["a.txt"]}, "ok_skipped",
     "OK, 1 skipped"),
    ("changed", {"last_changed": 2, "last_changed_sample": ["b.txt", "c.txt"]}, "ok_changed",
     "OK, 2 changed during backup"),
    ("links", {"last_links": 2, "last_links_sample": ["Photos"]}, "ok_links",
     "OK, 2 shortcuts not followed"),
    ("held", {"last_pruning_held": True, "last_baseline_files": 10, "last_files": 6},
     "ok_pruning_held", "OK, pruning held"),
    ("combined", {"last_skipped": 1, "last_skipped_sample": ["a.txt"], "last_changed": 1,
                  "last_changed_sample": ["b.txt"], "last_links": 2, "last_links_sample": ["P"]},
     "ok_skipped", "OK, 1 skipped, 1 changed during backup, 2 shortcuts not followed"),
    ("nothing", {}, "ok", "OK"),
]


@pytest.mark.parametrize("label, siblings, word, text", SIBLINGS, ids=[s[0] for s in SIBLINGS])
def test_an_older_plain_ok_record_displays_exactly_like_the_new_word(
        qt_app, label, siblings, word, text):
    from tabs.backup_tab import JobRow
    old = record(last_result="ok", **siblings)               # what an older build stored
    new = record(last_result=word, **siblings)               # what this build stores

    assert JobRow._status(None, old)[0] == text              # the expected text is real ...
    assert JobRow._status(None, old) == JobRow._status(None, new)
    assert JobRow._warning_detail(old) == JobRow._warning_detail(new)
    old_label, new_label = status_label(JobRow(old)), status_label(JobRow(new))
    assert old_label.text() == new_label.text() == text
    assert old_label.objectName() == new_label.objectName()
    assert old_label.toolTip() == new_label.toolTip()
    assert old_label.accessibleDescription() == new_label.accessibleDescription()


@pytest.mark.parametrize("word", OK_WORDS)
def test_a_word_with_no_sibling_counts_shows_plain_ok_and_does_not_crash(qt_app, word):
    # A hand-edited record: the word says something happened, the siblings (the
    # source of every count and name) say nothing did. The siblings win.
    from tabs.backup_tab import JobRow
    hand_edited = record(last_result=word)
    assert JobRow._status(None, hand_edited) == ("OK", "StatusOk")
    assert JobRow._warning_detail(hand_edited) == ""
    assert status_label(JobRow(hand_edited)).text() == "OK"


# --- junk in last_result ------------------------------------------------------------------------

JUNK_NOT_OK = [42, None, True, 1.5, ["ok"], {"ok": 1}, "", "OK", "x" * 100_000,
               "failed", "interrupted"]


@pytest.mark.parametrize("junk", JUNK_NOT_OK, ids=[repr(j)[:20] for j in JUNK_NOT_OK])
def test_junk_in_last_result_never_raises_and_reads_as_not_success(qt_app, junk):
    from tabs.backup_tab import INTERRUPTED_DETAIL, JobRow
    expected = "Interrupted" if junk == "interrupted" else "Failed"
    stored = record(last_result=junk)

    text, style = JobRow._status(None, stored)
    assert text.startswith(expected) and style == "StatusError"
    assert JobRow._warning_detail(stored) == (INTERRUPTED_DETAIL if junk == "interrupted" else "")
    assert status_label(JobRow(stored)).objectName() == "StatusError"
    assert backup_utils.result_is_ok(junk) is (isinstance(junk, str) and junk.startswith("ok"))
    if junk != "interrupted":                                  # an interrupted word wins over "Never run" today
        never_run = {k: v for k, v in stored.items() if k != "last_run"}
        assert JobRow._status(None, never_run) == ("Never run", "Hint")


def test_an_unknown_ok_prefixed_word_reads_as_success_exactly_as_before(qt_app):
    # Every reader tests startswith("ok"); a word this build does not know keeps
    # that meaning rather than turning into a failure.
    assert backup_utils.result_is_ok("ok_banana") is True
    assert status_of(record(last_result="ok_banana")) == ("OK", "StatusOk")


@pytest.mark.parametrize("value, expected", [
    ("ok", True), ("ok_skipped", True), ("ok_changed", True), ("ok_links", True),
    ("ok_pruning_held", True), ("failed: boom", False), ("interrupted: started x", False),
    ("", False), (None, False), (7, False)])
def test_result_is_ok(value, expected):
    assert backup_utils.result_is_ok(value) is expected


@pytest.mark.parametrize("value, expected", [("ok_links", "ok_links"), (None, ""), (3, ""), ("", "")])
def test_job_result_is_always_a_string(value, expected):
    assert backup_utils.job_result({"last_result": value}) == expected
    assert backup_utils.job_result({}) == ""


# --- every word against every reader --------------------------------------------------------------

@pytest.mark.parametrize("label, siblings, word, text", SIBLINGS, ids=[s[0] for s in SIBLINGS])
def test_every_ok_word_is_success_for_every_stored_value_reader(
        qt_app, label, siblings, word, text):
    from tabs.backup_tab import HELD_AFTER_FAILURE, INTERRUPTED_DETAIL, JobRow
    stored = record(last_result=word, **siblings)

    assert backup_utils.result_is_ok(word)
    assert JobRow._status(None, stored)[0].startswith("OK")            # not "Failed"
    assert JobRow._status(None, stored)[1] in ("StatusOk", "StatusWarn")
    assert runner.job_interrupted(stored) is None                      # not "interrupted"
    detail = JobRow._warning_detail(stored)
    assert INTERRUPTED_DETAIL not in detail and detail != HELD_AFTER_FAILURE   # not a failure branch
    assert status_label(JobRow(stored)).text() == text


def test_failed_and_interrupted_words_are_not_success_for_any_reader(qt_app):
    from tabs.backup_tab import JobRow
    failed = record(last_result="failed: drive gone", last_skipped=3, last_changed=1, last_links=1)
    assert not backup_utils.result_is_ok(failed["last_result"])
    assert JobRow._status(None, failed) == ("Failed", "StatusError")
    assert "skipped" not in JobRow._warning_detail(failed)             # stale counts never shown
    gone = record(last_result=runner.interrupted_result("2026-11-20T02:00:00"))
    assert not backup_utils.result_is_ok(gone["last_result"])
    assert runner.job_interrupted(gone) == {
        "since": "2026-11-20T02:00:00", "stamp": None, "settled": True}
    assert JobRow._status(None, gone)[1] == "StatusError"
    assert JobRow._status(None, gone)[0].startswith("Interrupted")


FINISH_CASES = [
    ("clean", {"ok": True, "message": "m"}, "ok", "Hint"),
    ("skipped", {"ok": True, "message": "m", "not_copied": 1}, "ok_skipped", "StatusWarn"),
    ("changed", {"ok": True, "message": "m", "changed": 1}, "ok_changed", "StatusWarn"),
    ("links", {"ok": True, "message": "m", "links_skipped": 1}, "ok_links", "StatusWarn"),
    ("held", {"ok": True, "message": "m", **HELD}, "ok_pruning_held", "StatusWarn"),
    ("failed", {"ok": False, "message": "boom"}, "failed: boom", "StatusError"),
]


@pytest.mark.parametrize("label, result, word, style", FINISH_CASES, ids=[c[0] for c in FINISH_CASES])
def test_finish_run_records_the_word_and_the_notice_style_is_unchanged(
        qt_app, monkeypatch, job, label, result, word, style):
    from tabs import backup_tab
    from utils import scheduler
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))
    tab = backup_tab.BackupTab()
    tab._finish_run(job, result)

    assert tab.backup_status.objectName() == style
    assert backup_utils.get_job(job["id"])["last_result"] == word


def test_the_headless_run_stores_the_word_and_keeps_its_exit_code_and_warning(
        job, monkeypatch, capsys):
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))   # skip the first-run pre-check
    lock_files(monkeypatch, {"a01.txt"})
    code = main.run_backup_job_cli(job["id"])
    out, err = capsys.readouterr()

    assert code == 0 and "[OK] " in out
    assert "[WARN] 1 file(s) were not copied" in err
    assert backup_utils.get_job(job["id"])["last_result"] == "ok_skipped"


def test_the_headless_run_of_a_failing_job_still_exits_one(tmp_path, job, capsys):
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))
    for entry in os.scandir(job["source"]):                  # empty source: the run is refused
        if entry.is_file():
            os.remove(entry.path)
        else:
            for inner in os.scandir(entry.path):
                os.remove(inner.path)
            os.rmdir(entry.path)
    code = main.run_backup_job_cli(job["id"])

    assert code == 1 and "[ERROR] " in capsys.readouterr().out
    assert backup_utils.get_job(job["id"])["last_result"].startswith("failed: ")


# --- the other writers of last_result ---------------------------------------------------------------

def dead_pid():
    """The pid of a process that has exited."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_an_interrupted_settle_and_an_abandoned_run_keep_their_text_forms(tmp_path):
    saved = backup_utils.new_job("I", str(tmp_path / "s"), str(tmp_path / "t"), schedule="manual")
    saved["running"] = {"since": "2026-11-20T20:00:00", "pid": dead_pid(), "start_time": None,
                        "stamp": "20261120-200000", "token": "t"}
    backup_utils.save_job(saved)
    settled = runner.settle_interrupted(saved["id"], check_lock=False)
    assert settled is not None
    assert backup_utils.get_job(saved["id"])["last_result"] == (
        "interrupted: started 2026-11-20T20:00:00 and never finished")

    runner._abandon_run(saved, RuntimeError("boom"))
    assert backup_utils.get_job(saved["id"])["last_result"] == "failed: RuntimeError: boom"


def test_saving_the_job_form_does_not_touch_the_stored_word(tmp_path):
    saved = backup_utils.new_job("S", str(tmp_path / "s"), str(tmp_path / "t"), schedule="manual")
    backup_utils.save_job(saved)
    backup_utils.record_run(saved["id"], {"ok": True, "not_copied": 1})
    assert backup_utils.get_job(saved["id"])["last_result"] == "ok_skipped"

    stale_row = dict(saved, name="Renamed", last_result="ok", last_skipped=0)   # the screen's old copy
    backup_utils.save_job_settings(stale_row)

    stored = backup_utils.get_job(saved["id"])
    assert stored["name"] == "Renamed"
    assert stored["last_result"] == "ok_skipped" and stored["last_skipped"] == 1


def test_a_legacy_jobs_file_loads_and_resaves_without_rewriting_the_old_word(tmp_path, qt_app):
    legacy = record(source="s", target="t", schedule="manual", retention=3, verify="full",
                    last_result="ok", last_skipped=2, last_skipped_sample=["a.txt", "b.txt"])
    with open(jobs_store.JOBS_FILE, "w", encoding="utf-8") as handle:
        json.dump([legacy], handle)

    loaded = backup_utils.get_job("abc12345")
    assert status_of(loaded) == ("OK, 2 skipped", "StatusWarn")      # derived from the siblings
    backup_utils.save_job_settings(dict(loaded, name="Renamed"))      # an unrelated save
    backup_utils.save_job(backup_utils.get_job("abc12345"))           # and a whole-job save

    again = backup_utils.get_job("abc12345")
    assert again["last_result"] == "ok" and again["last_skipped"] == 2 and again["name"] == "Renamed"
    assert status_of(again) == ("OK, 2 skipped", "StatusWarn")


def test_releasing_a_hold_does_not_leave_the_word_saying_held(tmp_path):
    saved = backup_utils.new_job("H", str(tmp_path / "s"), str(tmp_path / "t"), schedule="manual")
    backup_utils.save_job(saved)
    backup_utils.record_run(saved["id"], {"ok": True, "not_copied": 1, "changed": 1, **HELD})
    assert backup_utils.get_job(saved["id"])["last_result"] == "ok_pruning_held"

    released = backup_utils.release_hold(saved["id"])

    assert released["ok"] and released["released"] is True
    stored = backup_utils.get_job(saved["id"])
    assert stored["last_pruning_held"] is False
    # the next-ranked thing the siblings still carry; its counts were never touched
    assert stored["last_result"] == "ok_skipped" and stored["last_skipped"] == 1


def test_releasing_with_nothing_held_leaves_the_stored_word_alone(tmp_path):
    saved = backup_utils.new_job("N", str(tmp_path / "s"), str(tmp_path / "t"), schedule="manual")
    backup_utils.save_job(saved)
    backup_utils.record_run(saved["id"], {"ok": True, "changed": 1})
    backup_utils.release_hold(saved["id"])
    assert backup_utils.get_job(saved["id"])["last_result"] == "ok_changed"


# --- the run log is byte-identical to what the code produced before this card -----------------------

START, END = datetime(2026, 11, 20, 2, 0, 0), datetime(2026, 11, 20, 2, 5, 0)
LOG_JOB = {"id": "abc12345", "name": "Docs", "verify": "full"}
GOLDEN_LINES = [
    ({"ok": True, "message": "Backed up 8 file(s)", "files": 8, "bytes": 100,
      "version": "20261120-020000"},
     '{"v": 1, "start": "2026-11-20T02:00:00", "end": "2026-11-20T02:05:00", "trigger": "manual", "job": "abc12345", "name": "Docs", "outcome": "ok", "files": 8, "bytes": 100, "skipped": 0, "skipped_paths": [], "skipped_more": 0, "pruned": 0, "version": "20261120-020000", "verify_mode": "full", "cause": null, "message": "Backed up 8 file(s)", "pruning_held": false, "offenders": [], "baseline_files": 0, "baseline_stamp": null, "retried": 0, "recopied": 0, "changed": 0, "links": 0}'),
    ({"ok": True, "message": "Backed up 8 file(s)", "files": 8, "errors": ["a.txt: denied"],
      "not_copied": 1, "changed": 1, "links_skipped": 2, "version": "20261120-020000"},
     '{"v": 1, "start": "2026-11-20T02:00:00", "end": "2026-11-20T02:05:00", "trigger": "manual", "job": "abc12345", "name": "Docs", "outcome": "ok_skipped", "files": 8, "bytes": 0, "skipped": 1, "skipped_paths": ["a.txt"], "skipped_more": 0, "pruned": 0, "version": "20261120-020000", "verify_mode": "full", "cause": null, "message": "Backed up 8 file(s)", "pruning_held": false, "offenders": [], "baseline_files": 0, "baseline_stamp": null, "retried": 0, "recopied": 0, "changed": 1, "links": 2}'),
    ({"ok": True, "message": "m", "files": 6, "pruning_held": True, "baseline_files": 10,
      "version": "20261120-020000"},
     '{"v": 1, "start": "2026-11-20T02:00:00", "end": "2026-11-20T02:05:00", "trigger": "manual", "job": "abc12345", "name": "Docs", "outcome": "ok_pruning_held", "files": 6, "bytes": 0, "skipped": 0, "skipped_paths": [], "skipped_more": 0, "pruned": 0, "version": "20261120-020000", "verify_mode": "full", "cause": null, "message": "m", "pruning_held": true, "offenders": [], "baseline_files": 10, "baseline_stamp": null, "retried": 0, "recopied": 0, "changed": 0, "links": 0}'),
    ({"ok": False, "message": "drive gone", "cause": "drive_gone"},
     '{"v": 1, "start": "2026-11-20T02:00:00", "end": "2026-11-20T02:05:00", "trigger": "manual", "job": "abc12345", "name": "Docs", "outcome": "failed", "files": 0, "bytes": 0, "skipped": 0, "skipped_paths": [], "skipped_more": 0, "pruned": 0, "version": null, "verify_mode": "full", "cause": "drive_gone", "message": "drive gone", "pruning_held": false, "offenders": [], "baseline_files": 0, "baseline_stamp": null, "retried": 0, "recopied": 0, "changed": 0, "links": 0}'),
]


@pytest.mark.parametrize("result, golden", GOLDEN_LINES, ids=["ok", "all_three", "held", "failed"])
def test_the_run_log_line_is_byte_identical_to_the_pre_card_output(result, golden):
    assert json.dumps(runlog.build_line(LOG_JOB, result, "manual", START, END)) == golden


@pytest.mark.parametrize("result, outcome", [
    ({"ok": True, "busy": True}, "busy"),
    ({"ok": False, "interrupted": True}, "interrupted"),
    ({"ok": True, "hold_released": True}, "hold_released"),
    ({"ok": False, "cancelled": True, "message": "Backup cancelled by the user"}, "cancelled"),
    ({"ok": False, "message": "Backup cancelled by the user"}, "failed"),         # card bricnEaC: the marker, not the text
    ({"ok": False, "message": "boom"}, "failed"),
    ({"ok": True, "pruning_held": True, "not_copied": 1}, "ok_pruning_held"),
    ({"ok": True, "not_copied": 1, "changed": 1, "links_skipped": 1}, "ok_skipped"),
    ({"ok": True, "changed": 1, "links_skipped": 1}, "ok_changed"),
    ({"ok": True, "links_skipped": 1}, "ok_links"),
    ({"ok": True}, "ok"),
])
def test_the_run_log_outcome_for_every_ranking_case_is_what_it_was(result, outcome):
    assert runlog.build_line(LOG_JOB, result, "manual", START, END)["outcome"] == outcome
