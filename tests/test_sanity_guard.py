# tests/test_sanity_guard.py
# Card 51: the source sanity guard. A run that copied more than 30% fewer files
# than the largest retained version with a complete manifest (verified or not)
# still succeeds and keeps its version, and that largest version is kept aside
# while the others prune as usual (the bounded hold). Real trees under tmp_path; the one fake
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

from helpers import app_data_dir, clear_ro
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
    """Record every call `run_job` makes to prune (job id, protected set),
    then do the real prune."""
    calls = []
    real = runner.prune_versions
    monkeypatch.setattr(runner, "prune_versions",
                        lambda job, protect=(), **kw: calls.append((job["id"], set(protect)))
                        or real(job, protect, **kw))
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
    # Bounded hold: the sentence now says what is kept and that the rest prune.
    assert ("That earlier, larger backup is being kept; other old backups are "
            "pruned as usual") in held["message"]
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


def test_a_held_run_still_prunes_but_protects_the_baseline(make_job, prune_calls):
    # Replaces "prune is not even attempted on a held run": skipping the prune
    # outright let a permanently smaller source grow the target until every
    # run failed for space (bounded-hold redesign). Now the prune runs and
    # only the baseline is protected.
    job, source = make_job()
    first, _ = run(job)
    assert prune_calls == [(job["id"], set())]             # a normal run protects nothing

    set_files(source, 4)
    held, _ = run(job)
    assert held["pruning_held"]
    assert prune_calls[1] == (job["id"], {first["version"]})


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


def test_an_unverified_earlier_version_is_a_baseline(make_job):
    # Was "...is NOT a baseline". A file count is a property of the source,
    # not of how the copy was checked, and excluding "off" versions left every
    # verification-off job unguarded (10 -> 1 files never held).
    job, source = make_job()
    off, _ = run(dict(job, verify="off"))
    assert off["verified"] is False
    set_files(source, 5)
    result, _ = run(job)

    assert result["ok"] and result["pruning_held"] is True
    assert result["baseline_files"] == 10


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
    clear_ro(path)      # a finished version's manifest is read-only (card fEC32w6P)
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

    assert stored["last_result"] == "ok_pruning_held"     # the real word; startswith("ok") readers still succeed
    assert stored["last_pruning_held"] is True
    assert stored["last_baseline_files"] == 10 and stored["last_files"] == 6
    assert backup_utils.job_pruning_held(stored) is True
    assert backup_utils.job_baseline_files(stored) == 10
    assert backup_utils.job_last_files(stored) == 6

    set_files(source, 10)
    _, stored = run(job)
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0
    assert backup_utils.job_pruning_held(stored) is False


def test_a_failed_run_neither_sets_nor_clears_the_hold_fields(make_job):
    # Was "a failed run does not set the hold fields" and asserted a failure
    # CLEARED them. That hid the cause of the low-space failures a hold leads
    # to, so a failure now leaves all three exactly as the last success set them.
    job, source = make_job()
    run(job)
    stored = backup_utils.record_run(job["id"], {
        "ok": False, "message": "drive gone", "pruning_held": True,
        "baseline_files": 10, "files": 6})
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0
    assert stored["last_files"] == 10

    # ...and a real failure (the source is gone) after a held run keeps it.
    set_files(source, 5)
    _, held = run(job)
    assert held["last_pruning_held"] is True
    for entry in source.iterdir():
        entry.unlink()
    source.rmdir()
    failed, stored = run(job)
    assert failed["ok"] is False
    assert stored["last_pruning_held"] is True and stored["last_baseline_files"] == 10
    assert stored["last_files"] == 5 and stored["last_skipped"] == 0


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
    app_data_dir(appdata).mkdir(parents=True)
    # The CLI reads backup_jobs.json from its app-data folder; seed it, then run twice.
    jobs_file = app_data_dir(appdata) / "backup_jobs.json"
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


# --- the bounded hold (review-gate redesign) ---------------------------------------
# Skipping the prune outright let a source that stayed small grow the target
# without bound until every run failed pre-flight for space. Now the hold
# protects one version, the baseline, and everything else prunes by retention.

FILE_BYTES = 10_000


def set_sized_files(source, count):
    """Like `set_files`, but every file is exactly `FILE_BYTES` long, so the
    space a version takes on the target is known."""
    source.mkdir(parents=True, exist_ok=True)
    for old in source.iterdir():
        old.unlink()
    for i in range(count):
        (source / f"file{i}.bin").write_bytes(bytes([i % 251]) * FILE_BYTES)


def tree_bytes(path):
    """Bytes of every file under `path` (versions, manifests, leftovers)."""
    return sum(os.path.getsize(os.path.join(d, f))
               for d, _, names in os.walk(path) for f in names)


def stamps_with_files(job):
    """{stamp: files} from each retained version's trailer."""
    return {name: (backup_utils.read_manifest_summary(job, name) or {}).get("files")
            for name in backup_utils.version_names(job)}


def test_reviewer_scenario_a_permanently_smaller_source_never_fills_a_finite_disk(
        make_job, monkeypatch):
    from utils import preflight
    job, source = make_job(retention=2)
    set_sized_files(source, 10)
    target = job["target"]
    # A finite drive: free = capacity - what the target holds. Room for the
    # baseline, two 6-file versions and the next run's 1.1x margin, but not
    # for an unbounded pile of 6-file versions.
    capacity = 350_000
    asked = []

    def free(path):
        asked.append(path)
        return capacity - tree_bytes(target)

    monkeypatch.setattr(preflight, "_free_bytes", free)
    assert 2 * 10 * FILE_BYTES + 10 * 6 * FILE_BYTES > capacity   # unbounded would not fit

    results = []
    for n in range(12):
        if n == 2:
            set_sized_files(source, 6)
        result, stored = run(backup_utils.get_job(job["id"]))
        results.append(result)
        assert result["ok"], (n + 1, result["message"])
        assert len(backup_utils.version_names(job)) <= 2 + 1, n + 1
    assert asked                                            # the finite drive was consulted

    baseline = results[1]["version"]                        # ties -> the newest 10-file one
    assert stamps_with_files(job)[baseline] == 10
    assert all(r["pruning_held"] for r in results[2:])
    assert all(r["baseline_files"] == 10 for r in results[2:])
    assert results[0]["version"] not in backup_utils.version_names(job)
    assert stored["last_pruning_held"] is True


def test_the_baseline_survives_a_fully_rolled_retention_window(make_job):
    job, source = make_job(retention=1)
    first, _ = run(job)
    set_files(source, 6)
    held = [run(job)[0] for _ in range(4)]

    assert all(r["pruning_held"] for r in held)
    # Retention 1: only the newest ordinary version, plus the protected baseline.
    assert backup_utils.version_names(job) == [first["version"], held[-1]["version"]]
    assert held[-1]["pruned"] == [held[-2]["version"]]
    assert held[-1]["message"].endswith("before the next run. Removed 1 old version(s)")


def test_protect_and_the_newest_verified_rule_both_hold(make_job):
    job, source = make_job(retention=1)
    off_big, _ = run(dict(job, verify="off"))               # the baseline: 10 files, unverified
    set_files(source, 6)
    verified_small, _ = run(job)                            # full verify, 6 files
    off_small, _ = run(dict(job, verify="off"))             # newest, unverified

    assert off_small["pruning_held"] and off_small["baseline_files"] == 10
    # Retention 1 would prune `verified_small`, but it is the newest verified
    # version; `off_big` is protected as the baseline. Nothing goes.
    assert off_small["pruned"] == []
    assert backup_utils.version_names(job) == [
        off_big["version"], verified_small["version"], off_small["version"]]


def test_prune_versions_protect_does_not_count_toward_retention(make_job):
    job, _ = make_job(retention=2)
    stamps = [run(job)[0]["version"] for _ in range(2)]
    root = backup_utils.job_root(job)
    for name in ("20200101-000000", "20200102-000000"):     # two old legacy versions
        os.makedirs(os.path.join(root, name))

    # Protect the OLDER real version: if it counted toward retention, the two
    # newest names would be "kept" by it and both legacy versions would go.
    removed = backup_utils.prune_versions(job, protect={stamps[0]})
    assert removed == ["20200101-000000"]
    assert backup_utils.version_names(job) == ["20200102-000000"] + stamps


def test_when_the_count_recovers_the_hold_ends_and_the_baseline_prunes(make_job):
    job, source = make_job(retention=2)
    big = [run(job)[0] for _ in range(2)]
    set_files(source, 6)
    small = [run(job)[0] for _ in range(3)]
    assert big[1]["version"] in backup_utils.version_names(job)

    set_files(source, 9)
    recovered, stored = run(job)
    assert recovered["pruning_held"] is False and recovered["baseline_files"] == 0
    assert big[1]["version"] in recovered["pruned"]
    assert backup_utils.version_names(job) == [small[-1]["version"], recovered["version"]]
    assert stored["last_pruning_held"] is False and stored["last_baseline_files"] == 0


def test_an_all_off_job_going_from_ten_files_to_one_holds(make_job):
    job, source = make_job(verify="off")
    first, _ = run(job)
    set_files(source, 1)
    result, stored = run(job)

    assert result["verified"] is False
    assert result["pruning_held"] is True and result["baseline_files"] == 10
    assert first["version"] in backup_utils.version_names(job)
    assert stored["last_pruning_held"] is True


def test_an_off_version_with_a_damaged_trailer_is_not_a_baseline(make_job):
    job, source = make_job(verify="off")
    run(job)
    stamp = backup_utils.version_names(job)[0]
    path = backup_utils.manifest_path(job, stamp)
    clear_ro(path)
    with open(path, "rb") as handle:
        data = handle.read()
    with open(path, "wb") as handle:
        handle.write(data[:-10])                            # truncated mid-trailer
    set_files(source, 1)

    result, _ = run(job)
    assert result["ok"] and result["pruning_held"] is False


def test_a_failed_space_preflight_keeps_the_hold_and_a_success_recomputes_it(
        make_job, monkeypatch):
    from utils import preflight
    job, source = make_job()
    run(job)
    set_files(source, 6)
    _, stored = run(job)
    assert stored["last_pruning_held"] is True and stored["last_files"] == 6

    monkeypatch.setattr(preflight, "_free_bytes", lambda path: 0)
    failed, stored = run(backup_utils.get_job(job["id"]))
    assert failed["ok"] is False
    assert failed["message"].startswith(preflight.LOW_SPACE_PREFIX)
    assert preflight.HELD_LOW_SPACE_SENTENCE in failed["message"]
    assert stored["last_result"].startswith("failed")
    assert stored["last_pruning_held"] is True
    assert stored["last_baseline_files"] == 10 and stored["last_files"] == 6
    assert stored["last_skipped"] == 0

    monkeypatch.setattr(preflight, "_free_bytes", lambda path: 10 ** 12)
    set_files(source, 10)
    ok, stored = run(backup_utils.get_job(job["id"]))
    assert ok["ok"] and ok["pruning_held"] is False
    assert stored["last_pruning_held"] is False and stored["last_files"] == 10


def test_a_failed_run_after_skips_still_stores_zero_skipped(make_job):
    job, _ = make_job()
    run(job)
    stored = backup_utils.record_run(job["id"], {
        "ok": True, "message": "m", "files": 9, "errors": ["a.txt: denied"],
        "not_copied": 1, "pruning_held": False, "baseline_files": 0})
    assert stored["last_skipped"] == 1
    stored = backup_utils.record_run(job["id"], {"ok": False, "message": "gone"})
    assert stored["last_skipped"] == 0 and stored["last_files"] == 9


def test_the_low_space_message_names_the_kept_backups_only_when_held(tmp_path):
    from utils import preflight
    source = tmp_path / "s"
    set_files(source, 3)
    (tmp_path / "t").mkdir()
    plain = {"source": str(source), "target": str(tmp_path / "t")}
    held = dict(plain, last_pruning_held=True, last_baseline_files=10, last_files=3)

    for job, expect in ((plain, False), (held, True),
                        (dict(held, last_pruning_held="yes"), False)):
        message = preflight.preflight(job, free_space=lambda path: 0)
        assert message.startswith(preflight.LOW_SPACE_PREFIX)
        assert (preflight.HELD_LOW_SPACE_SENTENCE in message) is expect
    assert preflight.HELD_LOW_SPACE_SENTENCE.startswith(
        "Older backups are being kept aside because your source folder shrank.")
    # Other refusals never mention it.
    assert "kept aside" not in preflight.preflight(dict(held, source=str(tmp_path / "x")))


def test_a_failed_row_that_is_still_held_says_so_in_its_tooltip():
    pytest.importorskip("PySide6")
    from tabs.backup_tab import HELD_AFTER_FAILURE, JobRow

    failed = {"last_run": "2026-11-15T10:00:00", "last_result": "failed: no space",
              "last_skipped": 0, "last_pruning_held": False,
              "last_baseline_files": 0, "last_files": 6}
    held = dict(failed, last_pruning_held=True, last_baseline_files=10)
    old = {"last_run": "2026-11-15T10:00:00", "last_result": "failed: x"}

    assert HELD_AFTER_FAILURE == "Older backups are being kept because the source shrank."
    assert JobRow._status(None, held) == ("Failed", "StatusError")   # failure stays primary
    assert JobRow._warning_detail(held) == HELD_AFTER_FAILURE
    assert JobRow._warning_detail(failed) == ""
    assert JobRow._warning_detail(old) == ""                          # old jobs file: not held
    # An OK row is unchanged: the full sentence, not the failure line.
    ok = JobRow._warning_detail(dict(held, last_result="ok"))
    assert "Pruning was held" in ok and HELD_AFTER_FAILURE not in ok
