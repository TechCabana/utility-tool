# tests/test_clock_change.py
# Version names are local clock time (accepted). In the hour the clocks go back
# a later run can carry an EARLIER name: 01:30 BST, then 01:15 GMT 45 minutes
# later. Pruning by name alone then deleted the backup the run had just made
# and kept the older one. Real run_job calls; the one fake is the runner's
# clock (`utils.runner.datetime`, the owning module, not the facade).
from datetime import datetime as real_datetime, timezone

import pytest

from utils import backup_utils, jobs_store, runner, versions

FIRST_PASS = real_datetime(2026, 10, 25, 1, 30)    # 01:30 BST
SECOND_PASS = real_datetime(2026, 10, 25, 1, 15)   # 01:15 GMT, 45 minutes LATER
# The same two moments in UTC (card ved3wVOQ orders versions by it): 00:30Z, 01:15Z.
UTC_OF = {FIRST_PASS: real_datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
          SECOND_PASS: real_datetime(2026, 10, 25, 1, 15, tzinfo=timezone.utc)}


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def clock(monkeypatch):
    """A settable wall clock for the stamp claim, and the matching UTC clock."""
    now = {"value": FIRST_PASS}

    class Wall(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return now["value"]

    monkeypatch.setattr(runner, "datetime", Wall)
    monkeypatch.setattr(runner, "_utc_now", lambda: UTC_OF[now["value"]])
    return now


def run_twice_across_the_change(tmp_path, clock, retention):
    """Back up 'old' content at 01:30 BST, then 'NEW' content at 01:15 GMT."""
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    job = backup_utils.new_job("Clocks", str(source), str(target),
                               schedule="manual", retention=retention)
    backup_utils.save_job(job)
    results = []
    for when, content in ((FIRST_PASS, "old"), (SECOND_PASS, "NEW")):
        for i in range(3):
            (source / f"f{i}.txt").write_text(f"{content} {i}", encoding="utf-8")
        clock["value"] = when
        results.append(backup_utils.run_job(job))
    return job, results


def contents(job, name):
    """What f0.txt holds in version `name`."""
    with open(f"{versions.version_path(job, name)}/f0.txt", encoding="utf-8") as fh:
        return fh.read()


def test_retention_one_keeps_the_backup_just_made_not_the_older_one(tmp_path, clock):
    job, (first, second) = run_twice_across_the_change(tmp_path, clock, retention=1)

    assert first["ok"] and second["ok"]
    # The second run's name sorts BEFORE the first one's: the case itself.
    assert second["version"] < first["version"]
    assert versions.version_names(job) == [second["version"]]
    assert contents(job, second["version"]) == "NEW 0"
    assert second["pruned"] == [first["version"]]
    assert backup_utils.is_verified(job, second["version"])


def test_a_larger_retention_keeps_both_and_prunes_nothing(tmp_path, clock):
    job, (first, second) = run_twice_across_the_change(tmp_path, clock, retention=5)

    assert second["pruned"] == []
    assert sorted(versions.version_names(job)) == sorted([first["version"], second["version"]])


def test_prune_without_newest_orders_by_the_recorded_time_not_the_name(tmp_path, clock):
    # The old call shape is unchanged (no `newest`), but card ved3wVOQ orders
    # by the recorded finish time: the second run finished later (01:15Z after
    # 00:30Z) and survives, although its name sorts first.
    job, (first, second) = run_twice_across_the_change(tmp_path, clock, retention=5)
    job["retention"] = 1

    assert versions.prune_versions(job) == [first["version"]]
    assert versions.version_names(job) == [second["version"]]
