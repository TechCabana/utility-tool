# tests/test_release_hold.py
# Card QnFM2GMZ: an explicit "this is expected" action releases a pruning hold.
# The source sanity guard keeps one older, larger backup (the baseline) aside
# while the source stays more than 30% below it; `runner.release_hold` records
# on the job that the owner says the shrink was on purpose, and from then on
# the guard compares with the level the source was at, not with the old big
# versions still on disk. Real trees under tmp_path; the one fake is a clock
# that moves a second per call so every run gets its own stamp. Faults are
# patched on the module that owns them (`utils.runner`, `utils.versions`,
# `utils.jobs_store`), never on the facade. The GUI tests drive a real
# BackupTab and a real ConfirmDialog (only exec() is replaced, to press a
# button), offscreen, with no QSettings; every "is absent" assertion on a
# widget is paired with a case where the same widget is present.
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime as real_datetime, timedelta
from pathlib import Path

import pytest

from helpers import clear_ro, clear_ro_tree
from utils import backup_utils, jobs_store, locks, migrate, runlog, runner, scheduler, versions

REPO = Path(__file__).resolve().parent.parent
PLAIN_NOTHING = "Nothing to release: this backup is not holding any older version."
QUESTION = ("Your folder now has 6 files; an earlier backup has 10. "
            "Stop keeping the earlier one?")


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    path = tmp_path / "backup_jobs.json"
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(path))
    assert jobs_store.JOBS_FILE.startswith(str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def ticking_clock(monkeypatch):
    """A `datetime.now()` for runner that advances one second a call."""
    start = real_datetime.now().replace(microsecond=0)
    ticks = iter(range(10 ** 6))

    class Tick(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=next(ticks))

    monkeypatch.setattr(runner, "datetime", Tick)


def set_files(source, count):
    """Make `source` hold exactly `count` small files."""
    source.mkdir(parents=True, exist_ok=True)
    for old in source.iterdir():
        old.unlink()
    for i in range(count):
        (source / f"file{i}.txt").write_text(f"contents {i}", encoding="utf-8")


@pytest.fixture
def make_job(tmp_path):
    """Build and save a job over `tmp_path/src` (10 files unless told)."""
    def build(retention=2, files=10, **extra):
        source = tmp_path / "src"
        set_files(source, files)
        target = tmp_path / "dst"
        target.mkdir(exist_ok=True)
        job = backup_utils.new_job("Guard", str(source), str(target),
                                   schedule="manual", retention=retention, **extra)
        backup_utils.save_job(job)
        return job, source
    return build


def run(job):
    """Run and record, as the app does; returns (result, stored job)."""
    result = backup_utils.run_job(job)
    backup_utils.record_run(job["id"], result)
    return result, backup_utils.get_job(job["id"])


def names(job):
    """The retained version stamps, oldest first."""
    return backup_utils.version_names(job)


class Scene:
    """A job whose source went from 10 files to 6 and is now held."""

    def __init__(self, job, source, big, held):
        self.job, self.source, self.big, self.held = job, source, big, held

    @property
    def baseline(self):
        """The version the guard protects: the newest 10-file one."""
        return self.big[-1]["version"]


@pytest.fixture
def scene(make_job):
    """Build a held scene: `big_runs` runs at 10 files, then one at 6."""
    def build(retention=2, big_runs=3):
        job, source = make_job(retention=retention)
        big = [run(job)[0] for _ in range(big_runs)]
        set_files(source, 6)
        held, stored = run(job)
        assert held["pruning_held"] is True and held["baseline_files"] == 10
        assert stored["last_pruning_held"] is True
        return Scene(job, source, big, held)
    return build


@pytest.fixture
def prune_calls(monkeypatch):
    """Record every `run_job` prune (job id, protected set), then really prune."""
    calls = []
    real = runner.prune_versions
    monkeypatch.setattr(runner, "prune_versions",
                        lambda job, protect=(), **kw: calls.append((job["id"], set(protect)))
                        or real(job, protect, **kw))
    return calls


def tree_listing(root):
    """Every folder and file under `root`, as sorted relative paths."""
    found = []
    for folder, dirs, files in os.walk(root):
        for entry in dirs + files:
            found.append(os.path.relpath(os.path.join(folder, entry), root))
    return sorted(found)


# --- the card's done-when ---------------------------------------------------------------

def test_done_when_releasing_lets_the_next_run_prune_the_old_large_version(scene):
    s = scene(retention=2, big_runs=3)
    big = s.baseline

    # Positive control: WITHOUT a release the same low source holds again and the
    # old large version survives, so its absence below is the release's doing.
    control, _ = run(s.job)
    assert control["pruning_held"] is True and big in names(s.job)

    result = backup_utils.release_hold(s.job["id"])
    assert result == {"ok": True, "released": True, "message": result["message"],
                      "baseline_files": 10, "current_files": 6, "baseline_stamp": big}
    assert big in names(s.job)                      # releasing deletes nothing itself

    after, stored = run(s.job)
    assert after["ok"] and after["pruning_held"] is False and after["baseline_files"] == 0
    assert big in after["pruned"] and big not in names(s.job)
    assert len(names(s.job)) == 2                   # plain retention again
    assert stored["last_pruning_held"] is False


def test_a_big_version_still_inside_retention_neither_holds_again_nor_survives_for_ever(scene):
    s = scene(retention=3, big_runs=3)
    big = s.baseline
    assert big in names(s.job) and len(names(s.job)) == 4        # the hold's extra version
    backup_utils.release_hold(s.job["id"])

    first, _ = run(s.job)
    # The 10-file version is still retained (3 newest are kept) but is no longer
    # a baseline: this 6-file run does not hold.
    assert first["pruning_held"] is False and big in names(s.job)
    assert big not in first["pruned"] and len(first["pruned"]) == 2

    second, stored = run(s.job)
    assert second["pruning_held"] is False
    assert big in second["pruned"] and big not in names(s.job)   # outside retention at last
    assert stored["last_pruning_held"] is False


def test_a_source_that_drops_a_further_thirty_percent_below_the_new_level_holds_again(
        scene, prune_calls):
    s = scene()
    backup_utils.release_hold(s.job["id"])
    level_version = names(s.job)[-1]                # the 6-file version at release time

    set_files(s.source, 4)                          # 6 -> 4 is 33% below the new level
    result, stored = run(s.job)

    assert result["ok"] and result["pruning_held"] is True
    assert result["baseline_files"] == 6 and result["baseline_stamp"] == level_version
    assert prune_calls[-1] == (s.job["id"], {level_version})   # the then-largest, protected
    assert stored["last_pruning_held"] is True and stored["last_baseline_files"] == 6
    assert "earlier backup has 6 files" in result["message"]


@pytest.mark.parametrize("files, held", [(5, False), (6, False), (4, True), (3, True)])
def test_the_new_level_not_the_old_baseline_sets_the_threshold(scene, files, held):
    # 5 files is 50% below the old 10-file baseline (it would have held) but only
    # 17% below the released level of 6.
    s = scene()
    backup_utils.release_hold(s.job["id"])

    set_files(s.source, files)
    result, _ = run(s.job)

    assert result["pruning_held"] is held
    assert result["baseline_files"] == (6 if held else 0)


def test_releasing_with_no_active_hold_is_a_no_op_with_a_plain_message(make_job, tmp_path):
    job, source = make_job()
    run(job)                                        # healthy: no hold
    before_file = Path(jobs_store.JOBS_FILE).read_bytes()
    before_tree = tree_listing(job["target"])
    lines_before = runlog.read_runs(job["id"])

    result = backup_utils.release_hold(job["id"])

    assert result["ok"] is True and result["released"] is False
    assert result["message"] == PLAIN_NOTHING
    assert result["baseline_files"] == 0 and result["baseline_stamp"] is None
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before_file    # nothing written
    assert tree_listing(job["target"]) == before_tree
    assert runlog.read_runs(job["id"]) == lines_before               # and nothing logged
    assert "hold_released" not in backup_utils.get_job(job["id"])


def test_the_confirmation_dialog_names_both_file_counts(tab, scene):
    s = scene()
    tab.refresh_backup()
    tab.answer = "cancel"                            # only the question is under test

    tab.release_pruning_hold(s.job)

    (text,) = tab.shown
    assert QUESTION in text
    # Positive control for the counts: a different hold shows different numbers.
    stored = backup_utils.get_job(s.job["id"])
    backup_utils.save_job(dict(stored, last_files=7, last_baseline_files=12))
    tab.release_pruning_hold(s.job)
    assert "Your folder now has 7 files; an earlier backup has 12." in tab.shown[1]


# --- the engine function's other contracts ---------------------------------------------

def test_the_acknowledgement_is_stored_on_the_job_with_the_stamp_and_counts(scene):
    s = scene()
    result = backup_utils.release_hold(s.job["id"])
    stored = backup_utils.get_job(s.job["id"])

    ack = stored["hold_released"]
    assert set(ack) == {"stamp", "files", "level", "since", "time"}
    assert (ack["stamp"], ack["files"], ack["level"]) == (s.baseline, 10, 6)
    assert ack["since"] == names(s.job)[-1] == s.held["version"]
    real_datetime.fromisoformat(ack["time"])
    assert backup_utils.job_hold_released(stored) == ack
    # The card stops saying "pruning held", and the message is honest about the disk.
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0
    assert backup_utils.job_pruning_held(stored) is False
    assert "Nothing was deleted now" in result["message"]
    assert "2 backups you keep" in result["message"]


def test_release_never_deletes_or_moves_anything(scene, monkeypatch):
    s = scene()
    before = tree_listing(s.job["target"])
    monkeypatch.setattr(versions, "_rmtree", lambda path: pytest.fail(f"deleted {path}"))
    real_remove = os.remove
    monkeypatch.setattr(os, "remove", lambda p, *a, **k: pytest.fail(f"removed {p}")
                        if str(p).startswith(s.job["target"]) else real_remove(p, *a, **k))

    result = backup_utils.release_hold(s.job["id"])

    assert result["released"] is True
    assert tree_listing(s.job["target"]) == before
    assert s.baseline in names(s.job)


def test_a_second_release_with_no_new_hold_is_a_no_op_and_keeps_the_first_record(scene):
    s = scene()
    backup_utils.release_hold(s.job["id"])
    first = backup_utils.get_job(s.job["id"])["hold_released"]

    again = backup_utils.release_hold(s.job["id"])

    assert again["released"] is False and again["message"] == PLAIN_NOTHING
    assert backup_utils.get_job(s.job["id"])["hold_released"] == first


def test_a_later_hold_can_be_released_again_and_replaces_the_record(scene):
    s = scene()
    backup_utils.release_hold(s.job["id"])
    first = backup_utils.get_job(s.job["id"])["hold_released"]
    set_files(s.source, 4)
    held, _ = run(s.job)
    assert held["pruning_held"] and held["baseline_files"] == 6

    second = backup_utils.release_hold(s.job["id"])

    assert second["released"] and (second["baseline_files"], second["current_files"]) == (6, 4)
    ack = backup_utils.get_job(s.job["id"])["hold_released"]
    assert ack != first and (ack["files"], ack["level"]) == (6, 4)
    assert ack["since"] == held["version"]
    set_files(s.source, 3)                         # 25% below the new level: fine
    assert run(s.job)[0]["pruning_held"] is False
    set_files(s.source, 2)                         # 50% below the 4-file level: holds
    assert run(s.job)[0]["pruning_held"] is True


def test_growing_back_and_shrinking_again_is_guarded_against_the_new_larger_self(scene):
    s = scene()
    backup_utils.release_hold(s.job["id"])
    set_files(s.source, 12)
    grown, _ = run(s.job)
    assert grown["pruning_held"] is False

    set_files(s.source, 6)
    result, _ = run(s.job)

    assert result["pruning_held"] is True
    assert result["baseline_files"] == 12 and result["baseline_stamp"] == grown["version"]


def test_an_unknown_job_is_a_clear_error(make_job):
    make_job()
    result = backup_utils.release_hold("nosuchid")
    assert result["ok"] is False and result["released"] is False
    assert result["cause"] == "unknown_job" and "nosuchid" in result["message"]


def test_a_damaged_jobs_file_raises_the_usual_error_and_writes_nothing(make_job):
    make_job()
    Path(jobs_store.JOBS_FILE).write_bytes(b"damaged\xff")
    with pytest.raises(backup_utils.JobsFileCorrupt):
        backup_utils.release_hold("whatever")
    assert Path(jobs_store.JOBS_FILE).read_bytes() == b"damaged\xff"


def test_a_job_with_no_versions_on_disk_still_clears_its_flag(scene):
    s = scene()
    for stamp in names(s.job):
        clear_ro_tree(backup_utils.version_path(s.job, stamp))
        clear_ro(backup_utils.manifest_path(s.job, stamp))
        shutil.rmtree(backup_utils.version_path(s.job, stamp))
        os.remove(backup_utils.manifest_path(s.job, stamp))

    result = backup_utils.release_hold(s.job["id"])

    stored = backup_utils.get_job(s.job["id"])
    assert result["released"] is True and result["baseline_stamp"] is None
    assert stored["last_pruning_held"] is False
    assert "hold_released" not in stored            # nothing to be "since"


# --- concurrency -------------------------------------------------------------------------

def test_a_release_during_a_run_is_refused_and_changes_nothing(scene):
    s = scene()
    before = Path(jobs_store.JOBS_FILE).read_bytes()
    run_lock = locks.acquire(runner._job_lock_path(s.job), "a backup of the job")
    try:
        result = backup_utils.release_hold(s.job["id"])
    finally:
        run_lock.release()

    assert result["ok"] is False and result["released"] is False and result["busy"] is True
    assert result["message"] == "A backup is running; try again when it finishes."
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before
    assert backup_utils.release_hold(s.job["id"])["released"] is True   # free again: works


def test_a_run_started_after_a_release_cannot_undo_it(scene):
    s = scene()
    stale = dict(s.job)                             # the GUI's copy, loaded before the release
    backup_utils.release_hold(s.job["id"])
    assert "hold_released" not in stale

    result, stored = run(stale)                     # run with the OLD dict

    assert result["pruning_held"] is False
    assert stored["hold_released"]["level"] == 6


def test_the_release_survives_a_failed_run_and_unrelated_writes(scene):
    s = scene()
    backup_utils.release_hold(s.job["id"])
    ack = backup_utils.get_job(s.job["id"])["hold_released"]

    backup_utils.record_run(s.job["id"], {"ok": False, "message": "drive gone",
                                          "pruning_held": True, "baseline_files": 10})
    assert backup_utils.get_job(s.job["id"])["hold_released"] == ack
    backup_utils.record_precheck(s.job["id"], {"problems": 0})
    assert backup_utils.get_job(s.job["id"])["hold_released"] == ack
    backup_utils.save_job(dict(backup_utils.get_job(s.job["id"]), name="Renamed"))
    assert backup_utils.get_job(s.job["id"])["hold_released"] == ack
    # ...and the failed run did not bring the hold marker back.
    assert backup_utils.job_pruning_held(backup_utils.get_job(s.job["id"])) is False


# --- a job that never heard of the field ------------------------------------------------

def test_a_legacy_job_without_the_field_guards_exactly_as_before(make_job):
    job, source = make_job()
    run(job)
    assert "hold_released" not in backup_utils.get_job(job["id"])
    assert backup_utils.job_hold_released(backup_utils.get_job(job["id"])) is None
    set_files(source, 6)

    result, _ = run(job)

    assert result["pruning_held"] is True and result["baseline_files"] == 10


@pytest.mark.parametrize("junk", [
    "yes", 7, [], {}, {"stamp": "20260101-000000"},
    {"stamp": "20260101-000000", "files": 10, "level": 6, "since": "not-a-stamp"},
    {"stamp": "20260101-000000", "files": 10, "level": 6, "since": 20260101},
    {"stamp": "nope", "files": 10, "level": 6, "since": "20260101-000000"},
    {"stamp": "20260101-000000", "files": 0, "level": 6, "since": "20260101-000000"},
    {"stamp": "20260101-000000", "files": 10, "level": True, "since": "20260101-000000"},
    {"stamp": "20260101-000000", "files": "10", "level": 6, "since": "20260101-000000"},
])
def test_a_junk_acknowledgement_reads_as_none_and_the_guard_is_unchanged(make_job, junk):
    job, source = make_job()
    run(job)
    set_files(source, 6)
    assert backup_utils.job_hold_released(dict(job, hold_released=junk)) is None

    result = backup_utils.run_job(dict(job, hold_released=junk))

    assert result["pruning_held"] is True and result["baseline_files"] == 10


def test_a_valid_acknowledgement_reads_back_and_an_empty_stamp_is_allowed():
    good = {"stamp": "", "files": 10, "level": 6, "since": "20260101-000000", "time": "t"}
    assert backup_utils.job_hold_released({"hold_released": good}) == good


def test_the_question_wording_and_its_singular():
    assert backup_utils.release_hold_question(6, 10) == QUESTION
    assert backup_utils.release_hold_question(1, 10).startswith("Your folder now has 1 file;")


# --- the run log -------------------------------------------------------------------------

def test_a_release_logs_one_line_with_counts_and_a_stamp_and_no_paths(scene, tmp_path):
    s = scene()
    before = runlog.read_runs(s.job["id"])

    backup_utils.release_hold(s.job["id"], trigger="cli")

    runs = runlog.read_runs(s.job["id"])
    assert len(runs) == len(before) + 1
    entry = runs[0]
    assert tuple(entry) == runlog.LINE_KEYS
    assert (entry["outcome"], entry["trigger"], entry["job"]) == ("hold_released", "cli", s.job["id"])
    assert (entry["files"], entry["baseline_files"], entry["baseline_stamp"]) == (6, 10, s.baseline)
    assert entry["outcome"] in runlog.OUTCOMES and entry["pruning_held"] is False
    text = Path(runlog.log_path(s.job["id"])).read_text(encoding="utf-8").splitlines()[-1]
    assert str(tmp_path) not in text and s.job["source"] not in text
    assert not re.search(r"[A-Za-z]:[\\/]", text)


def test_a_held_run_logs_the_baseline_it_kept_aside(scene):
    s = scene()
    entry = runlog.read_runs(s.job["id"])[0]
    assert entry["outcome"] == "ok_pruning_held"
    assert (entry["baseline_files"], entry["baseline_stamp"]) == (10, s.baseline)
    assert runlog.read_runs(s.job["id"])[-1]["baseline_files"] == 0   # a first run: none


def test_a_stamp_that_is_a_path_never_reaches_the_log():
    line = runlog.build_line({}, {"ok": True, "baseline_stamp": "C:/secret/folder",
                                  "baseline_files": 3}, "manual",
                             real_datetime.now(), real_datetime.now())
    assert line["baseline_stamp"] is None and line["baseline_files"] == 3


# --- the command line ---------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch, capsys):
    """`main.release_hold_cli` with the one-time data migration stubbed."""
    pytest.importorskip("PySide6")
    import main
    monkeypatch.setattr(migrate, "migrate_data", lambda: None)

    def call(job_id):
        code = main.release_hold_cli(job_id)
        out = capsys.readouterr()
        return code, out.out, out.err
    return call


def test_cli_exit_0_when_released_and_it_prints_the_message(scene, cli):
    s = scene()
    code, out, _ = cli(s.job["id"])
    assert code == 0 and out.startswith("[OK] Done: the earlier backup (10 files)")
    assert backup_utils.get_job(s.job["id"])["hold_released"]["files"] == 10


def test_cli_exit_0_for_a_no_op_with_the_plain_message(make_job, cli):
    job, _ = make_job()
    run(job)
    code, out, _ = cli(job["id"])
    assert code == 0 and PLAIN_NOTHING in out


def test_cli_exit_2_for_an_unknown_job(make_job, cli):
    make_job()
    code, out, _ = cli("nosuchid")
    assert code == 2 and "No backup job with id 'nosuchid'" in out


def test_cli_exit_3_for_a_damaged_jobs_file_and_nothing_is_written(make_job, cli):
    make_job()
    Path(jobs_store.JOBS_FILE).write_bytes(b"damaged\xff")
    code, _, err = cli("whatever")
    assert code == 3 and "damaged" in err
    assert Path(jobs_store.JOBS_FILE).read_bytes() == b"damaged\xff"


def test_cli_exit_4_while_the_job_is_running_and_the_hold_stays(scene, cli):
    s = scene()
    run_lock = locks.acquire(runner._job_lock_path(s.job), "a backup of the job")
    try:
        code, _, err = cli(s.job["id"])
    finally:
        run_lock.release()
    assert code == 4 and "A backup is running; try again when it finishes." in err
    assert backup_utils.job_pruning_held(backup_utils.get_job(s.job["id"])) is True


def test_cli_exit_1_when_the_acknowledgement_cannot_be_saved(scene, cli, monkeypatch):
    s = scene()

    def refuse(job):
        raise OSError("disk full")
    monkeypatch.setattr(jobs_store, "_save_job", refuse)
    code, _, err = cli(s.job["id"])
    assert code == 1 and "disk full" in err


def test_the_usage_text_documents_the_new_flag_and_its_exit_codes():
    pytest.importorskip("PySide6")
    import main
    usage = main.HEADLESS_USAGE
    assert "--release-hold <job id>" in usage
    assert "4 job is running" in usage and "2 no such job" in usage


def test_one_real_subprocess_releases_the_hold_with_a_temp_appdata(scene, tmp_path):
    s = scene()
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    (appdata / "Safekeep" / "backup_jobs.json").write_text(
        Path(jobs_store.JOBS_FILE).read_text(encoding="utf-8"), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")
    assert env["APPDATA"] == str(appdata)           # never the real %APPDATA%
    before = tree_listing(s.job["target"])

    def cli(*args):
        return subprocess.run([sys.executable, str(REPO / "main.py"), *args],
                              cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=120)

    first = cli("--release-hold", s.job["id"])
    assert first.returncode == 0, first.stderr
    assert first.stdout.startswith("[OK] Done: the earlier backup (10 files)")
    stored = json.loads((appdata / "Safekeep" / "backup_jobs.json").read_text(encoding="utf-8"))
    (job,) = [j for j in stored if j["id"] == s.job["id"]]
    assert job["last_pruning_held"] is False and job["hold_released"]["level"] == 6
    assert tree_listing(s.job["target"]) == before                      # nothing deleted
    lines = (appdata / "Safekeep" / "logs" / f"{s.job['id']}.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["outcome"] == "hold_released"
    assert json.loads(lines[-1])["trigger"] == "cli"

    second = cli("--release-hold", s.job["id"])
    assert second.returncode == 0 and PLAIN_NOTHING in second.stdout
    assert cli("--release-hold", "nosuchid").returncode == 2
    bare = cli("--release-hold")
    assert bare.returncode == 2 and "--release-hold <job id>" in bare.stdout


# --- the Backup screen --------------------------------------------------------------------

@pytest.fixture
def qt_app():
    """An offscreen QApplication; no QSettings is constructed by these tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def tab(qt_app, monkeypatch):
    """A real BackupTab; its confirmation dialog is real but answered by `answer`.

    No real scheduler: any `schtasks` call fails the test.
    """
    from PySide6 import QtWidgets
    from tabs import backup_tab

    def no_scheduler(args):
        pytest.fail(f"the scheduler was touched: {args}")
    monkeypatch.setattr(scheduler, "_run", no_scheduler)
    shown, defaults = [], []

    def exec_(self):
        shown.append(" ".join(label.text() for label in self.findChildren(QtWidgets.QLabel)))
        defaults.append((self.cancel_button.isDefault(), self.confirm_button.isDefault(),
                         self.confirm_button.objectName()))
        (self.confirm_button if widget.answer == "confirm" else self.cancel_button).click()
        return self.result()
    monkeypatch.setattr(backup_tab.ConfirmDialog, "exec", exec_)
    widget = backup_tab.BackupTab()
    widget.answer = "confirm"
    widget.shown, widget.defaults = shown, defaults
    return widget


def release_buttons(tab):
    """Every push button offering the release in the rows the jobs table holds NOW.

    Rows from before a refresh are only scheduled for deletion (no event loop
    runs here), so they are still children of the table's host; the layout is
    what says which rows are on screen.
    """
    from PySide6 import QtWidgets
    from tabs.backup_tab import RELEASE_HOLD_TEXT
    rows = [tab.jobs_layout.itemAt(i).widget() for i in range(tab.jobs_layout.count())]
    return [b for row in rows for b in row.findChildren(QtWidgets.QPushButton)
            if b.text() == RELEASE_HOLD_TEXT]


def test_the_button_shows_only_while_a_hold_is_active(tab, scene):
    from PySide6 import QtCore
    from tabs.backup_tab import RELEASE_HOLD_TEXT
    assert RELEASE_HOLD_TEXT == "This is expected: forget the larger backup"
    s = scene()
    tab.refresh_backup()

    # Positive control: a held job offers it, enabled and keyboard reachable.
    (button,) = release_buttons(tab)
    assert not button.isHidden() and button.isEnabled()
    assert button.focusPolicy() == QtCore.Qt.StrongFocus and button.autoDefault()
    assert s.job["name"] in button.accessibleName()

    backup_utils.release_hold(s.job["id"])
    tab.refresh_backup()
    assert release_buttons(tab) == []                # the same job, no hold: no button


def test_a_job_that_never_held_has_no_button(tab, make_job):
    job, _ = make_job()
    run(job)
    tab.refresh_backup()
    assert [j["id"] for j in tab.jobs] == [job["id"]] and release_buttons(tab) == []


def test_a_failed_row_that_is_still_held_still_offers_it(tab, scene):
    s = scene()
    backup_utils.record_run(s.job["id"], {"ok": False, "message": "no space"})
    tab.refresh_backup()
    assert len(release_buttons(tab)) == 1


def test_clicking_confirms_with_cancel_as_the_default_then_releases_and_says_so(tab, scene):
    s = scene()
    tab.refresh_backup()
    (button,) = release_buttons(tab)

    button.click()

    (text,) = tab.shown
    assert QUESTION in text
    assert "Nothing is deleted right now" in text and "number of backups you keep" in text
    # Cancel is the default; the confirm button is the ordinary (not red) one.
    assert tab.defaults == [(True, False, "Primary")]
    stored = backup_utils.get_job(s.job["id"])
    assert stored["hold_released"]["files"] == 10 and stored["last_pruning_held"] is False
    assert "no longer kept aside" in tab.backup_status.text()
    assert not tab.backup_status.isHidden()
    assert release_buttons(tab) == []                # the card refreshed
    assert s.baseline in names(s.job)                # and nothing was deleted by it


def test_cancelling_changes_nothing(tab, scene):
    s = scene()
    tab.refresh_backup()
    before = Path(jobs_store.JOBS_FILE).read_bytes()
    tab.answer = "cancel"

    release_buttons(tab)[0].click()

    assert len(tab.shown) == 1                       # the question really was asked
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before
    assert len(release_buttons(tab)) == 1
    assert "hold_released" not in backup_utils.get_job(s.job["id"])


def test_a_hold_a_run_ended_meanwhile_is_not_asked_about(tab, scene):
    s = scene()
    tab.refresh_backup()
    set_files(s.source, 10)
    run(s.job)                                       # the source recovered: hold over

    release_buttons(tab)[0].click()                  # the row is stale

    assert tab.shown == []                           # no question about a hold that is gone
    assert PLAIN_NOTHING in tab.backup_status.text()
    assert release_buttons(tab) == []


def test_nothing_happens_while_an_operation_is_running(tab, scene):
    s = scene()
    tab.refresh_backup()
    tab.backup_thread = object()                     # an operation in flight
    tab._set_backup_controls(enabled=False)
    assert not release_buttons(tab)[0].isEnabled()

    tab.release_pruning_hold(s.job)

    assert tab.shown == [] and "hold_released" not in backup_utils.get_job(s.job["id"])
    tab.backup_thread = None


def test_a_release_that_cannot_be_saved_says_the_earlier_backup_is_still_kept(
        tab, scene, monkeypatch):
    s = scene()
    tab.refresh_backup()

    def refuse(job):
        raise OSError("disk full")
    monkeypatch.setattr(jobs_store, "_save_job", refuse)
    release_buttons(tab)[0].click()

    assert "still being kept" in tab.backup_status.text() and "disk full" in tab.backup_status.text()
    assert len(release_buttons(tab)) == 1


def test_a_release_refused_because_the_job_is_running_says_so(tab, scene):
    s = scene()
    tab.refresh_backup()
    run_lock = locks.acquire(runner._job_lock_path(s.job), "a backup of the job")
    try:
        release_buttons(tab)[0].click()
    finally:
        run_lock.release()
    assert "A backup is running; try again when it finishes." in tab.backup_status.text()
    assert len(release_buttons(tab)) == 1


@pytest.mark.parametrize("key_name", ["Key_Return", "Key_Enter", "Key_Space"])
def test_the_button_works_from_the_keyboard(qt_app, scene, key_name):
    from PySide6 import QtCore, QtTest
    from tabs.backup_tab import JobRow
    s = scene()
    row = JobRow(backup_utils.get_job(s.job["id"]))
    asked = []
    row.release_hold_requested.connect(asked.append)
    assert row.release_btn is not None

    QtTest.QTest.keyClick(row.release_btn, getattr(QtCore.Qt, key_name))

    assert [j["id"] for j in asked] == [s.job["id"]]


def test_a_row_without_a_hold_has_no_release_button_and_the_same_columns(qt_app, make_job):
    from tabs.backup_tab import JobRow
    job, _ = make_job()
    run(job)
    row = JobRow(backup_utils.get_job(job["id"]))
    assert row.release_btn is None
