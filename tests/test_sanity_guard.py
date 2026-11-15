# tests/test_sanity_guard.py
# Card 51: the source sanity guard. A run that copied more than 30% fewer files
# than the largest retained VERIFIED version holds still succeeds and keeps its
# version, but pruning is held for it. Real trees under tmp_path; the one fake
# is a clock that moves one second per run so every run gets its own stamp
# without sleeping. Faults are patched on the module that owns them
# (`utils.runner`, `utils.versions`, `utils.copying`), never on the facade.
import errno
import json
import os
import subprocess
import sys
from datetime import datetime as real_datetime, timedelta
from pathlib import Path

import pytest

from utils import backup_utils, copying, jobs_store, runner, versions

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture(autouse=True)
def ticking_clock(monkeypatch):
    """A `datetime.now()` for the stamp claim that advances one second a call."""
    start = real_datetime.now().replace(microsecond=0)
    ticks = iter(range(10 ** 6))

    class Tick(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=next(ticks))

    monkeypatch.setattr(runner, "datetime", Tick)


def set_files(source, count):
    """Make `source` hold exactly `count` small files (file0..file{count-1})."""
    source.mkdir(parents=True, exist_ok=True)
    for old in source.iterdir():
        old.unlink()
    for i in range(count):
        (source / f"file{i}.txt").write_text(f"contents {i}", encoding="utf-8")


@pytest.fixture
def make_job(tmp_path):
    """Build and save a job over `tmp_path/src` (initially 10 files)."""
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


@pytest.fixture
def prune_calls(monkeypatch):
    """Record every call `run_job` makes to prune, then do the real prune."""
    calls = []
    real = runner.prune_versions
    monkeypatch.setattr(runner, "prune_versions",
                        lambda job: calls.append(job["id"]) or real(job))
    return calls


# --- the card's done-when scenario ---------------------------------------------

def test_done_when_ten_files_to_six_holds_pruning_then_nine_prunes_normally(make_job):
    job, source = make_job(retention=2)

    # Positive control: with a healthy source the third run DOES prune, so an
    # empty `pruned` below means the guard held it, not that nothing was due.
    results = [run(job)[0] for _ in range(3)]
    assert [r["pruned"] for r in results[:2]] == [[], []]
    assert results[2]["pruned"] == [results[0]["version"]]
    kept = [results[1]["version"], results[2]["version"]]
    assert backup_utils.version_names(job) == kept

    set_files(source, 6)
    held, stored = run(job)
    assert held["ok"] is True
    assert held["pruning_held"] is True
    assert held["baseline_files"] == 10 and held["files"] == 6
    assert held["pruned"] == []
    assert "Pruning was held because this backup has 6 files and an earlier backup has 10 files (40% fewer)" in held["message"]
    assert "older backups were kept" in held["message"]
    assert "Removed" not in held["message"]
    # Retention is 2 and 3 versions exist: the guard is the only reason.
    assert backup_utils.version_names(job) == kept + [held["version"]]
    assert stored["last_pruning_held"] is True

    set_files(source, 9)
    again, stored = run(job)
    assert again["ok"] and again["pruning_held"] is False
    assert len(again["pruned"]) == 2                      # 4 versions -> 2 kept
    assert backup_utils.version_names(job) == [held["version"], again["version"]]
    assert stored["last_pruning_held"] is False


def test_the_new_version_is_still_promoted_and_verified_on_a_held_run(make_job):
    job, source = make_job()
    run(job)
    set_files(source, 5)
    held, _ = run(job)

    assert held["pruning_held"] and held["verified"] is True
    assert backup_utils.is_verified(job, held["version"])
    assert os.path.isdir(backup_utils.version_path(job, held["version"]))
    trailer = backup_utils.read_manifest_summary(job, held["version"])
    assert trailer["files"] == 5 and trailer["verified"] is True


def test_prune_is_not_even_attempted_on_a_held_run(make_job, prune_calls):
    job, source = make_job()
    run(job)
    assert prune_calls == [job["id"]]                      # a normal run does call it

    set_files(source, 4)
    held, _ = run(job)
    assert held["pruning_held"]
    assert prune_calls == [job["id"]]                      # ...and the held one did not


# --- the threshold ---------------------------------------------------------------

@pytest.mark.parametrize("before, after, held", [
    (10, 7, False),     # exactly 30% gone: "more than 30%" is not met
    (10, 6, True),
    (20, 14, False),
    (20, 13, True),
    (100, 70, False),
    (100, 69, True),
    (10, 10, False),
    (10, 15, False),    # growth never holds
    (2, 1, True),       # no minimum-files floor: a tiny source is covered too
    (3, 2, True),
    (4, 2, True),
])
def test_the_threshold_is_more_than_thirty_percent_with_no_floor(make_job, before, after, held):
    job, source = make_job(files=before)
    run(job)
    set_files(source, after)
    result, _ = run(job)

    assert result["ok"]
    assert result["pruning_held"] is held
    assert result["baseline_files"] == (before if held else 0)


@pytest.mark.parametrize("baseline, files, held", [
    (1000, 700, False), (1000, 699, True), (10 ** 6, 700000, False),
    (10 ** 6, 699999, True), (7, 4, True), (7, 5, False),
])
def test_the_boundary_holds_at_any_scale_without_float_drift(monkeypatch, baseline, files, held):
    monkeypatch.setattr(versions, "baseline_files", lambda job, exclude=None: baseline)
    assert (versions.sanity_drop_baseline({}, files) == baseline) is held


def test_a_first_run_has_no_baseline_and_never_holds(make_job):
    job, _ = make_job(files=1)
    result, stored = run(job)

    assert result["ok"] and result["pruning_held"] is False
    assert result["baseline_files"] == 0
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0


# --- which versions count as the baseline -----------------------------------------

def test_the_baseline_is_the_largest_retained_version_not_the_previous_one(make_job):
    job, source = make_job(retention=5)
    run(job)                                               # 10 files
    set_files(source, 6)
    first_low, _ = run(job)
    assert first_low["pruning_held"]

    # Same low count again: the previous version also has 6, so a guard that
    # compared with it would let this through. The 10-file version is retained.
    second_low, _ = run(job)
    assert second_low["pruning_held"] is True
    assert second_low["baseline_files"] == 10
    assert second_low["pruned"] == []


def test_an_unverified_earlier_version_is_not_a_baseline(make_job):
    job, source = make_job()
    off, _ = run(dict(job, verify="off"))
    assert off["verified"] is False
    set_files(source, 5)
    result, _ = run(job)

    assert result["ok"] and result["pruning_held"] is False
    assert result["baseline_files"] == 0


def test_an_unverified_new_version_is_still_judged_against_a_verified_baseline(make_job):
    job, source = make_job()
    run(job)
    set_files(source, 5)
    result, _ = run(dict(job, verify="off"))

    assert result["verified"] is False and result["pruning_held"] is True


def test_a_quick_mode_run_is_judged_like_any_other(make_job):
    job, source = make_job(verify="quick")
    run(job)
    set_files(source, 5)
    result, _ = run(job)

    assert result["verify_mode"] == "quick" and result["pruning_held"] is True


def test_legacy_and_damaged_versions_are_not_a_baseline(make_job):
    job, source = make_job()
    root = backup_utils.job_root(job)
    os.makedirs(os.path.join(root, "20200101-000000"))                    # no manifest at all
    os.makedirs(os.path.join(root, "20200102-000000"))
    with open(os.path.join(root, "20200102-000000" + backup_utils.MANIFEST_SUFFIX), "w") as f:
        f.write('{"type": "end", "files": 999\n')                         # truncated trailer
    set_files(source, 3)
    result, _ = run(job)

    assert result["ok"] and result["pruning_held"] is False


def test_a_junk_files_value_in_a_trailer_is_ignored(make_job):
    job, source = make_job()
    run(job)
    stamp = backup_utils.version_names(job)[0]
    path = backup_utils.manifest_path(job, stamp)
    lines = open(path, encoding="utf-8").read().splitlines()
    trailer = json.loads(lines[-1])
    trailer["files"] = "ten"
    lines[-1] = json.dumps(trailer)
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    set_files(source, 1)

    result, _ = run(job)
    assert result["ok"] and result["pruning_held"] is False


# --- what is stored on the job ------------------------------------------------------

def test_a_held_run_stores_the_fields_and_a_normal_run_clears_them(make_job):
    job, source = make_job()
    run(job)
    set_files(source, 6)
    _, stored = run(job)

    assert stored["last_result"] == "ok"                  # existing readers keep working
    assert stored["last_pruning_held"] is True
    assert stored["last_baseline_files"] == 10 and stored["last_files"] == 6
    assert backup_utils.job_pruning_held(stored) is True
    assert backup_utils.job_baseline_files(stored) == 10
    assert backup_utils.job_last_files(stored) == 6

    set_files(source, 10)
    _, stored = run(job)
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0
    assert backup_utils.job_pruning_held(stored) is False


def test_a_failed_run_does_not_set_the_hold_fields(make_job):
    job, source = make_job()
    run(job)
    stored = backup_utils.record_run(job["id"], {
        "ok": False, "message": "drive gone", "pruning_held": True,
        "baseline_files": 10, "files": 6})
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0

    # ...and a real failure (the source is gone) after a held run clears it.
    set_files(source, 5)
    _, held = run(job)
    assert held["last_pruning_held"] is True
    for entry in source.iterdir():
        entry.unlink()
    source.rmdir()
    failed, stored = run(job)
    assert failed["ok"] is False
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0


def test_a_new_job_starts_not_held():
    job = backup_utils.new_job("n", "s", "t")
    assert job["last_pruning_held"] is False
    assert job["last_baseline_files"] == 0 and job["last_files"] == 0
    assert backup_utils.job_pruning_held(job) is False


def test_an_old_jobs_file_without_the_fields_reads_as_not_held():
    old = {"id": "abc12345", "name": "Old", "source": "s", "target": "t",
           "last_run": "2026-01-01T00:00:00", "last_result": "ok"}
    with open(jobs_store.JOBS_FILE, "w", encoding="utf-8") as handle:
        json.dump([old], handle)

    loaded = backup_utils.get_job("abc12345")
    assert backup_utils.job_pruning_held(loaded) is False
    assert backup_utils.job_baseline_files(loaded) == 0


@pytest.mark.parametrize("fields", [
    {"last_pruning_held": "yes", "last_baseline_files": 10, "last_files": 6},
    {"last_pruning_held": 1, "last_baseline_files": 10, "last_files": 6},
    {"last_pruning_held": True, "last_baseline_files": "10", "last_files": 6},
    {"last_pruning_held": True, "last_baseline_files": -1, "last_files": 6},
    {"last_pruning_held": True, "last_baseline_files": 10, "last_files": None},
    {"last_pruning_held": True, "last_baseline_files": True, "last_files": 6},
    {"last_pruning_held": True},
])
def test_junk_values_read_as_not_held(fields):
    assert backup_utils.job_pruning_held(fields) is False
    assert backup_utils.job_baseline_files(fields) == 0


# --- a run that skipped files AND held pruning --------------------------------------

def test_skipped_files_and_a_held_prune_are_reported_together(make_job, monkeypatch):
    job, source = make_job()
    run(job)
    set_files(source, 6)                # 6 left, one of them locked: 5 land
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) == "file0.txt":
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)
    result, stored = run(job)

    assert result["ok"] and result["not_copied"] == 1 and result["files"] == 5
    assert result["pruning_held"] is True
    assert "could not be read and were skipped" in result["message"]
    assert "Pruning was held" in result["message"]
    assert stored["last_skipped"] == 1 and stored["last_pruning_held"] is True


# --- the screen and the headless run --------------------------------------------------

def test_the_jobs_table_status_for_each_combination():
    pytest.importorskip("PySide6")
    from tabs.backup_tab import JobRow

    base = {"last_run": "2026-11-15T10:00:00", "last_result": "ok", "last_skipped": 0,
            "last_pruning_held": False, "last_baseline_files": 0, "last_files": 6}
    held = dict(base, last_pruning_held=True, last_baseline_files=10)

    assert JobRow._status(None, base) == ("OK", "StatusOk")
    assert JobRow._status(None, held) == ("OK, pruning held", "StatusWarn")
    assert JobRow._status(None, dict(held, last_skipped=3)) == (
        "OK, 3 skipped, pruning held", "StatusWarn")
    assert JobRow._status(None, dict(base, last_skipped=3)) == ("OK, 3 skipped", "StatusWarn")
    assert JobRow._status(None, dict(held, last_result="failed: x"))[1] == "StatusError"

    detail = JobRow._warning_detail(held)
    assert "this backup has 6 files and an earlier backup has 10 files (40% fewer)" in detail
    both = JobRow._warning_detail(dict(held, last_skipped=2, last_skipped_sample=["a.txt"]))
    assert "2 file(s) could not be read" in both and "Pruning was held" in both


def test_the_headless_run_warns_on_stderr_and_still_exits_zero(tmp_path, make_job):
    job, source = make_job(retention=3)
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    # The CLI reads <APPDATA>\Safekeep\backup_jobs.json; seed it, then run twice.
    jobs_file = appdata / "Safekeep" / "backup_jobs.json"
    jobs_file.write_text(Path(jobs_store.JOBS_FILE).read_text(encoding="utf-8"), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")

    def cli():
        return subprocess.run([sys.executable, str(REPO / "main.py"), "--run-backup-job",
                               job["id"]], cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=120)

    first = cli()
    assert first.returncode == 0 and "[WARN]" not in first.stderr, first.stderr
    set_files(source, 5)
    import time
    time.sleep(1.1)                                        # a distinct version stamp
    second = cli()

    assert second.returncode == 0, second.stderr
    assert "[OK] " in second.stdout
    assert ("[WARN] Pruning was held because this backup has 5 files and an earlier "
            "backup has 10 files (50% fewer)") in second.stderr
