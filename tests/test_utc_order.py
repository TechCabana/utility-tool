# tests/test_utc_order.py
# Card ved3wVOQ: versions are ordered by a UTC finish time recorded in the
# manifest trailer, not by folder name. Names are local clock time, so in the
# hour the clocks go back a later run can carry an EARLIER name.
#
# Two kinds of fixture:
#   * real `run_job` calls with the LOCAL clock (`utils.runner.datetime`, which
#     names versions) and the UTC clock (`utils.runner._utc_now`) patched
#     independently -- the Done-when scenario, 01:30 BST then 01:15 GMT;
#   * hand-made versions with a hand-written trailer, where name order and UTC
#     order are made to DISAGREE so a consumer that still ordered by name
#     fails. Every "uses the new order" test is built that way.
import json
import os
import time
from datetime import datetime as real_datetime, timezone

import pytest

from helpers import clear_ro
from utils import backup_utils, jobs_store, runner, versions

N1, N2, N3 = "20260101-000000", "20260201-000000", "20260301-000000"
T1, T2, T3 = ("2026-05-01T10:00:00Z", "2026-05-02T10:00:00Z", "2026-05-03T10:00:00Z")


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    assert jobs_store.JOBS_FILE.startswith(str(tmp_path))


# ---------------------------------------------------------------------------
# Real runs across the autumn clock change
# ---------------------------------------------------------------------------

def local(hour, minute):
    """A local wall-clock time on the day the clocks went back."""
    return real_datetime(2026, 10, 25, hour, minute)


def utc(hour, minute):
    """A UTC time on the same day, timezone-aware."""
    return real_datetime(2026, 10, 25, hour, minute, tzinfo=timezone.utc)


# (local clock, UTC clock). 01:30 BST is 00:30Z; 01:15 GMT, 45 minutes LATER,
# is 01:15Z; 01:35 GMT is 01:35Z.
FIRST = (local(1, 30), utc(0, 30))
SECOND = (local(1, 15), utc(1, 15))
THIRD = (local(1, 35), utc(1, 35))


@pytest.fixture
def clocks(monkeypatch):
    """Two independent settable clocks: the local one that names versions and
    the UTC one that orders them."""
    state = {"local": FIRST[0], "utc": FIRST[1]}

    class Wall(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return state["local"]

    monkeypatch.setattr(runner, "datetime", Wall)
    monkeypatch.setattr(runner, "_utc_now", lambda: state["utc"])
    return state


def make_job(tmp_path, retention):
    """A saved manual job over `tmp_path/src` and `tmp_path/dst`."""
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    job = backup_utils.new_job("Clocks", str(source), str(target),
                               schedule="manual", retention=retention)
    backup_utils.save_job(job)
    return job, source


def run_at(job, source, content, when, clocks):
    """Back up `source` holding `content` with the clocks at `when`; record it."""
    for i in range(3):
        (source / f"f{i}.txt").write_text(f"{content} {i}", encoding="utf-8")
    clocks["local"], clocks["utc"] = when
    result = backup_utils.run_job(job)
    assert result["ok"], result
    backup_utils.record_run(job["id"], result)
    return result


def f0(job, name):
    """What f0.txt holds in version `name`."""
    with open(f"{versions.version_path(job, name)}/f0.txt", encoding="utf-8") as fh:
        return fh.read()


def test_done_when_restore_default_and_pruning_treat_the_later_backup_as_newer(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    first = run_at(job, source, "old", FIRST, clocks)
    second = run_at(job, source, "NEWER", SECOND, clocks)

    # The case itself: the later backup has the EARLIER name.
    assert second["version"] < first["version"]
    assert versions.version_names(job) == [second["version"], first["version"]]
    # ... and ordering follows the recorded UTC time instead.
    assert versions.ordered_versions(job) == [first["version"], second["version"]]
    assert versions.latest_version(job) == second["version"]

    # Restore's default is the later (01:15 GMT) version, with its content.
    plan = versions.restore_candidates(job)
    assert plan == {"preferred": second["version"], "latest": second["version"],
                    "fallback": False, "newer_unverified": []}
    restored = versions.restore_version(job, None, str(tmp_path / "out"))
    assert restored["ok"] and restored["verified"] is True
    assert restored["path"].endswith("-restored-" + second["version"])
    with open(os.path.join(restored["path"], "f0.txt"), encoding="utf-8") as fh:
        assert fh.read() == "NEWER 0"

    # Pruning: a later clean-up at retention 1 removes the OLDER backup.
    job["retention"] = 1
    assert versions.prune_versions(job) == [first["version"]]
    assert versions.version_names(job) == [second["version"]]
    assert f0(job, second["version"]) == "NEWER 0"


def test_retention_one_keeps_the_backup_just_made(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=1)
    first = run_at(job, source, "old", FIRST, clocks)
    second = run_at(job, source, "NEWER", SECOND, clocks)

    assert second["pruned"] == [first["version"]]
    assert versions.version_names(job) == [second["version"]]
    assert f0(job, second["version"]) == "NEWER 0"


def test_a_third_run_removes_the_oldest_by_utc_not_the_one_with_the_smallest_name(tmp_path, clocks):
    # Names: second (011500) < first (013000) < third (013500). By NAME the
    # oldest is `second`, which `newest=` does not protect (the third run made
    # `third`): only the UTC order removes `first`, the backup really made first.
    job, source = make_job(tmp_path, retention=2)
    first = run_at(job, source, "old", FIRST, clocks)
    second = run_at(job, source, "NEWER", SECOND, clocks)
    third = run_at(job, source, "NEWEST", THIRD, clocks)

    assert sorted([first["version"], second["version"], third["version"]]) == [
        second["version"], first["version"], third["version"]]
    assert third["pruned"] == [first["version"]]
    assert versions.ordered_versions(job) == [second["version"], third["version"]]


def test_a_larger_retention_keeps_both_ordered_by_utc(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    first = run_at(job, source, "old", FIRST, clocks)
    second = run_at(job, source, "NEWER", SECOND, clocks)

    assert second["pruned"] == []
    assert versions.ordered_versions(job) == [first["version"], second["version"]]


def test_changes_since_compares_with_the_newest_by_utc(tmp_path, clocks):
    # Name order says `first` ("old 0": 5 bytes) is the newest; UTC says
    # `second` ("NEWER 0": 7 bytes) is, and the source still holds NEWER, so
    # nothing has changed since the real latest backup.
    job, source = make_job(tmp_path, retention=5)
    first = run_at(job, source, "old", FIRST, clocks)
    second = run_at(job, source, "NEWER", SECOND, clocks)

    changes = backup_utils.changes_since(job)
    assert changes["ok"] and changes["version"] == second["version"]
    assert changes["changed"] == 0
    assert changes["restore"]["preferred"] == second["version"]
    assert changes["preferred_when"] == versions.version_when(second["version"])
    assert versions.version_when(first["version"]) != changes["preferred_when"]


def test_the_trailer_and_the_job_record_the_utc_finish_time(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    first = run_at(job, source, "old", FIRST, clocks)
    second = run_at(job, source, "NEWER", SECOND, clocks)

    assert first["finished_utc"] == "2026-10-25T00:30:00Z"
    assert second["finished_utc"] == "2026-10-25T01:15:00Z"
    trailer = versions.read_manifest_summary(job, second["version"])
    assert trailer["finished_utc"] == "2026-10-25T01:15:00Z"
    assert trailer["verified"] is True and trailer["files"] == 3
    # The raw last line of the manifest carries it too (the thing on disk).
    with open(backup_utils.manifest_path(job, second["version"]), encoding="utf-8") as fh:
        assert json.loads(fh.read().splitlines()[-1])["finished_utc"] == "2026-10-25T01:15:00Z"
    assert backup_utils.get_job(job["id"])["last_finished_utc"] == "2026-10-25T01:15:00Z"


def test_a_clock_set_back_by_hand_behaves_like_before(tmp_path, clocks):
    # Not a DST change: the local AND UTC clocks both go back an hour. Then the
    # recorded UTC order agrees with the name order (the second run really did
    # claim an earlier time), the oldest by that order is pruned as before, and
    # the version a run has just made is still never deleted (`newest=`).
    job, source = make_job(tmp_path, retention=1)
    first = run_at(job, source, "old", (local(10, 0), utc(10, 0)), clocks)
    second = run_at(job, source, "NEWER", (local(9, 0), utc(9, 0)), clocks)

    assert second["version"] < first["version"]
    assert second["pruned"] == [first["version"]]
    assert versions.version_names(job) == [second["version"]]
    assert versions.ordered_versions(job) == [second["version"]]


def test_two_runs_that_claim_the_same_utc_second_order_by_name(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    first = run_at(job, source, "old", (local(8, 0), utc(8, 0)), clocks)
    second = run_at(job, source, "NEWER", (real_datetime(2026, 10, 25, 8, 0, 1), utc(8, 0)), clocks)

    assert first["version"] < second["version"]
    assert versions.ordered_versions(job) == [first["version"], second["version"]]


# ---------------------------------------------------------------------------
# Hand-made versions: the ordering rule
# ---------------------------------------------------------------------------

@pytest.fixture
def job(tmp_path):
    """A job with an empty target; the tests add versions by hand."""
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    made = backup_utils.new_job("Order", str(source), str(target),
                                schedule="manual", retention=9)
    os.makedirs(versions.job_root(made))
    return made


def fake_version(job, stamp, finished=None, verify="full", files=3, extra=None,
                 raw=None, manifest=True):
    """A version folder `stamp` and (unless `manifest` is False) a manifest
    ending in a trailer: `finished` is its `finished_utc` (None leaves the key
    out, the pre-change shape), `extra` adds or overrides trailer keys, `raw`
    replaces the whole last line with these bytes."""
    os.makedirs(versions.version_path(job, stamp))
    if not manifest:
        return
    end = {"type": "end", "files": files, "bytes": 0, "verify": verify,
           "verified": verify != "off"}
    if finished is not None:
        end["finished_utc"] = finished
    end.update(extra or {})
    last = raw if raw is not None else json.dumps(end).encode("utf-8")
    with open(backup_utils.manifest_path(job, stamp), "wb") as handle:
        handle.write(json.dumps({"type": "header", "version": 1, "stamp": stamp}).encode() + b"\n")
        handle.write(last + b"\n")


@pytest.mark.parametrize("spec, expected", [
    # (name, finished_utc or None) per version, then the expected order.
    pytest.param({N1: None, N2: None}, [N1, N2], id="legacy-legacy-by-name"),
    pytest.param({N2: None, N1: None}, [N1, N2], id="legacy-legacy-listing-order-irrelevant"),
    pytest.param({N1: None, N2: T1}, [N1, N2], id="legacy-then-utc-names-agree"),
    pytest.param({N2: None, N1: T1}, [N2, N1], id="legacy-before-utc-even-when-its-name-is-later"),
    pytest.param({N1: T1, N2: T2}, [N1, N2], id="utc-utc-agree"),
    pytest.param({N1: T2, N2: T1}, [N2, N1], id="utc-utc-disagree-utc-wins"),
    pytest.param({N1: T1, N2: T1}, [N1, N2], id="utc-tie-by-name"),
    pytest.param({N3: T1, N1: None, N2: T3}, [N1, N3, N2], id="three-way-mixture"),
    pytest.param({N3: None, N2: T2, N1: T3}, [N3, N2, N1], id="two-utc-after-a-later-named-legacy"),
])
def test_ordering_rule_for_every_mix_of_versions_with_and_without_a_utc_time(job, spec, expected):
    for stamp, finished in spec.items():
        fake_version(job, stamp, finished)

    assert versions.ordered_versions(job) == expected
    assert versions.latest_version(job) == expected[-1]
    assert versions.restore_candidates(job)["preferred"] == expected[-1]


@pytest.mark.parametrize("junk", [
    123, True, None, [], {}, "", "yesterday", " 2026-05-01T10:00:00Z",
    "2026-05-01T10:00:00Z ", "2026-05-01T10:00:00", "2026-05-01 10:00:00Z",
    "2026-05-01T10:00:00+00:00", "2026-13-01T10:00:00Z", "2026-05-32T10:00:00Z",
    "2026-05-01T25:00:00Z", "2026-5-1T10:0:0Z", "2026-05-01T10:00:00.5Z",
])
def test_a_junk_finished_utc_is_ignored_and_the_version_orders_by_name(job, junk):
    # N2 carries the junk (so it is "legacy"); N1 has a real UTC time. Legacy
    # sorts before real UTC, so a TRUSTED junk value could not give this order.
    fake_version(job, N1, T1)
    fake_version(job, N2, None, extra={"finished_utc": junk})

    assert "finished_utc" not in versions.read_manifest_summary(job, N2)
    assert versions.ordered_versions(job) == [N2, N1]
    assert versions.is_verified(job, N2)


@pytest.mark.parametrize("raw", [
    b"{not json", b"", b"[1, 2]", b'{"type": "end"', b"x" * 5000,
    b"[" * 4000,
    b'{"type": "end", "files": 1, "verify": "bogus", "verified": true}',
    b'{"type": "end", "files": 1, "verify": "off", "verified": true}',
], ids=["garbage", "empty", "array", "truncated", "too-long", "deeply-nested",
        "unknown-mode", "mode-disagrees-with-flag"])
def test_a_damaged_trailer_falls_back_to_the_name_without_raising(job, raw):
    fake_version(job, N1, T1)
    fake_version(job, N2, raw=raw)

    assert versions.ordered_versions(job) == [N2, N1]       # damaged = no UTC time
    assert versions.read_manifest_summary(job, N2) is None
    assert versions.latest_version(job) == N1
    assert versions.restore_candidates(job)["preferred"] == N1


def test_a_missing_manifest_orders_by_name_and_does_not_raise(job):
    fake_version(job, N3, manifest=False)
    fake_version(job, N1, manifest=False)
    fake_version(job, N2, T1)

    assert versions.ordered_versions(job) == [N1, N3, N2]


def test_partial_folders_and_stray_files_are_not_versions(job):
    fake_version(job, N1, T1)
    os.makedirs(os.path.join(versions.job_root(job), N2 + ".partial"))
    with open(os.path.join(versions.job_root(job), N3), "w", encoding="utf-8") as fh:
        fh.write("a file, not a folder")
    os.makedirs(os.path.join(versions.job_root(job), "notes"))

    assert versions.ordered_versions(job) == [N1]


def test_an_unverified_newest_version_is_not_preferred_but_is_the_latest(job):
    fake_version(job, N2, T1, verify="full")
    fake_version(job, N1, T2, verify="off")          # newest by UTC, oldest by name

    assert versions.ordered_versions(job) == [N2, N1]
    assert versions.restore_candidates(job) == {
        "preferred": N2, "latest": N1, "fallback": False, "newer_unverified": [N1]}
    assert versions.verified_versions(job) == [N2]


def test_verified_versions_lists_the_verified_in_utc_order(job):
    fake_version(job, N1, T2, verify="quick")
    fake_version(job, N2, T1, verify="full")
    fake_version(job, N3, T3, verify="off")

    assert versions.verified_versions(job) == [N2, N1]


def test_a_trailer_with_unknown_extra_keys_is_read_and_still_verified(job):
    fake_version(job, N1, T1, extra={"added_by_a_later_build": {"x": 1}})

    summary = versions.read_manifest_summary(job, N1)
    assert summary["finished_utc"] == T1 and summary["added_by_a_later_build"] == {"x": 1}
    assert versions.is_verified(job, N1)


# ---------------------------------------------------------------------------
# Every consumer, with name order and UTC order DISAGREEING
# ---------------------------------------------------------------------------

def test_latest_version_is_the_utc_newest_not_the_name_newest(job):
    fake_version(job, N1, T2)
    fake_version(job, N2, T1)

    assert versions.version_names(job)[-1] == N2
    assert versions.latest_version(job) == N1


def test_restore_candidates_uses_utc_for_preferred_latest_and_newer_unverified(job):
    # By name the newest verified is N3 and N2 is the older unverified one.
    # By UTC: N3 (T1) < N1 (T2, verified) < N2 (T3, off).
    fake_version(job, N3, T1, verify="full")
    fake_version(job, N1, T2, verify="quick")
    fake_version(job, N2, T3, verify="off")

    assert versions.restore_candidates(job) == {
        "preferred": N1, "latest": N2, "fallback": False, "newer_unverified": [N2]}


def test_restore_candidates_fallback_picks_the_utc_newest_when_nothing_is_verified(job):
    fake_version(job, N2, T1, verify="off")
    fake_version(job, N1, T2, verify="off")

    assert versions.restore_candidates(job) == {
        "preferred": N1, "latest": N1, "fallback": True, "newer_unverified": []}


def test_prune_removes_the_utc_oldest_and_keeps_the_newest_verified(job):
    job["retention"] = 2
    fake_version(job, N3, T1)                  # oldest by UTC, newest by name
    fake_version(job, N1, T2)
    fake_version(job, N2, T3)

    assert versions.prune_versions(job) == [N3]
    assert versions.ordered_versions(job) == [N1, N2]
    assert not os.path.exists(backup_utils.manifest_path(job, N3))


def test_prune_never_deletes_the_newest_verified_by_utc(job):
    # Retention 1; the UTC-newest version is unverified, so the newest VERIFIED
    # (N3 by name, but UTC-older than N1) is protected and N1 is what goes.
    job["retention"] = 1
    fake_version(job, N3, T2, verify="full")
    fake_version(job, N2, T1, verify="off")
    fake_version(job, N1, T3, verify="off")

    removed = versions.prune_versions(job)

    assert N3 not in removed
    assert versions.ordered_versions(job) == [N3, N1]


def test_prune_honours_newest_over_the_recorded_order(job):
    # `newest=` still wins: the caller's version is kept even when its UTC time
    # is the oldest (a clock set back by hand).
    job["retention"] = 1
    fake_version(job, N1, T3)
    fake_version(job, N2, T1)

    assert versions.prune_versions(job, newest=N2) == [N1]
    assert versions.ordered_versions(job) == [N2]


def release(job, since, files=80, level=60):
    """Give `job` a valid hold release as `runner.release_hold` records it."""
    job["hold_released"] = {"stamp": "", "files": files, "level": level,
                            "since": since, "time": "2026-05-01T00:00:00"}


def test_sanity_baseline_without_a_release_is_the_utc_ordered_largest_newest_tie(job):
    fake_version(job, N1, T2, files=100)
    fake_version(job, N2, T1, files=100)     # a tie: the UTC-newest (N1) wins

    assert versions.sanity_baseline(job) == (100, N1)
    assert versions.sanity_baseline(job, exclude=N1) == (100, N2)


def test_sanity_baseline_release_since_is_compared_in_utc_order(job):
    # since = S. A has an older NAME but a LATER UTC time (counts); B has a
    # later name but an EARLIER UTC time (does not). By name it is the reverse.
    S, A, B = "20260105-000000", "20260103-000000", "20260107-000000"
    fake_version(job, S, "2026-05-05T12:00:00Z", files=60)
    fake_version(job, A, "2026-05-06T00:00:00Z", files=100)
    fake_version(job, B, "2026-05-04T00:00:00Z", files=90)
    release(job, S)

    assert versions.sanity_baseline(job) == (100, A)
    # By name only (what the old code did) the answer would be (90, B).
    assert backup_utils.baseline_files(job) == 100


def test_sanity_baseline_release_for_an_ordinary_history_is_unchanged(job):
    # Names and UTC agree (the normal case): versions older than `since` are
    # ignored, `since` itself and everything after it count.
    for i, (stamp, files) in enumerate([(N1, 100), (N2, 80), (N3, 60)]):
        fake_version(job, stamp, f"2026-05-0{i + 1}T10:00:00Z", files=files)
    assert versions.sanity_baseline(job) == (100, N1)

    release(job, N2)
    assert versions.sanity_baseline(job) == (80, N2)
    release(job, N3)
    assert versions.sanity_baseline(job) == (60, N3)


def test_sanity_baseline_release_since_a_version_pruned_away_keys_by_name(job):
    # `since` no longer exists, so it has no UTC time and keys like a legacy
    # version: legacy versions with an older name are out, a newer name and
    # every version with a UTC time are in.
    gone = "20260115-000000"
    fake_version(job, N1, None, files=100)          # legacy, name older than gone
    fake_version(job, N2, None, files=70)           # legacy, name newer than gone
    fake_version(job, N3, T1, files=90)             # has a UTC time
    release(job, gone)

    assert versions.sanity_baseline(job) == (90, N3)
    assert versions.sanity_baseline(job, exclude=N3) == (70, N2)


def test_sanity_baseline_reads_legacy_versions_by_name_when_none_has_a_utc_time(job):
    fake_version(job, N2, None, files=50)
    fake_version(job, N1, None, files=50)

    assert versions.sanity_baseline(job) == (50, N2)      # the newest by name wins the tie


def test_changes_since_uses_the_utc_newest_version(tmp_path, job):
    # Hand-made: N2 is newest by name, N1 by UTC; each holds different content.
    source = job["source"]
    for stamp, text in ((N1, "alpha"), (N2, "different")):
        fake_version(job, stamp, T2 if stamp == N1 else T1)
        with open(os.path.join(versions.version_path(job, stamp), "a.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)
        os.utime(os.path.join(versions.version_path(job, stamp), "a.txt"),
                 (os.path.getmtime(os.path.join(source, "a.txt")),) * 2)

    changes = backup_utils.changes_since(job)

    assert changes["version"] == N1 and changes["changed"] == 0
    assert changes["when"] == versions.version_when(N1)


# ---------------------------------------------------------------------------
# Older versions and manifests still work
# ---------------------------------------------------------------------------

def strip_utc(job, stamp):
    """Rewrite version `stamp`'s manifest as an older build wrote it: no
    `finished_utc` in the trailer (and nothing else changed)."""
    path = backup_utils.manifest_path(job, stamp)
    clear_ro(path)      # a finished version's manifest is read-only (card fEC32w6P)
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    end = json.loads(lines[-1])
    del end["finished_utc"]
    lines[-1] = json.dumps(end)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def test_a_manifest_written_before_this_change_still_verifies_and_restores(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    made = run_at(job, source, "old", FIRST, clocks)
    strip_utc(job, made["version"])

    assert "finished_utc" not in versions.read_manifest_summary(job, made["version"])
    assert versions.is_verified(job, made["version"])
    restored = versions.restore_version(job, None, str(tmp_path / "out"))
    assert restored["ok"] and restored["verified"] is True
    assert restored["unchecked_reason"] is None
    assert versions.ordered_versions(job) == [made["version"]]


def test_a_new_trailer_is_accepted_by_the_restore_manifest_check(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    made = run_at(job, source, "old", FIRST, clocks)

    assert versions.read_manifest_summary(job, made["version"])["finished_utc"]
    restored = versions.restore_version(job, made["version"], str(tmp_path / "out"))
    assert restored["verified"] is True and restored["checked"] == 3
    assert restored["unchecked_reason"] is None


def test_the_new_trailer_still_passes_a_full_reverification_of_its_copy(tmp_path, clocks):
    # verify.py reads manifest lines by type, so the extra trailer key must not
    # break re-checking a finished copy against its manifest.
    from utils import verify
    job, source = make_job(tmp_path, retention=5)
    made = run_at(job, source, "old", FIRST, clocks)

    checked, bad, named = verify._verify_copy(
        versions.version_path(job, made["version"]),
        backup_utils.manifest_path(job, made["version"]), None, None, 3)
    assert (checked, bad, named) == (3, 0, [])


def test_versions_made_before_and_after_this_change_order_old_first(tmp_path, clocks):
    job, source = make_job(tmp_path, retention=5)
    old = run_at(job, source, "old", FIRST, clocks)        # made first, then stripped
    strip_utc(job, old["version"])
    new = run_at(job, source, "NEWER", SECOND, clocks)     # a LATER run with an EARLIER name

    assert new["version"] < old["version"]
    # Whatever the names say, the one without a UTC time is the older.
    assert versions.ordered_versions(job) == [old["version"], new["version"]]
    assert versions.latest_version(job) == new["version"]


# ---------------------------------------------------------------------------
# last_finished_utc on the job
# ---------------------------------------------------------------------------

def test_record_run_stores_a_valid_finished_utc_and_ignores_junk(job):
    backup_utils.save_job(job)
    base = {"ok": True, "message": "x", "files": 3}

    backup_utils.record_run(job["id"], dict(base, finished_utc=T1))
    assert backup_utils.get_job(job["id"])["last_finished_utc"] == T1
    for junk in ("junk", 5, None, "2026-13-01T00:00:00Z", ""):
        backup_utils.record_run(job["id"], dict(base, finished_utc=junk))
        assert backup_utils.get_job(job["id"])["last_finished_utc"] == T1
    # A failed run says nothing about when the last backup finished.
    backup_utils.record_run(job["id"], {"ok": False, "message": "no", "finished_utc": T2})
    assert backup_utils.get_job(job["id"])["last_finished_utc"] == T1


def test_a_job_without_the_field_is_left_without_it(job):
    backup_utils.save_job(job)
    backup_utils.record_run(job["id"], {"ok": True, "message": "x", "files": 3})

    assert "last_finished_utc" not in backup_utils.get_job(job["id"])
    assert "last_finished_utc" not in backup_utils.new_job("n", "s", "t")


def test_a_settings_save_from_a_stale_dict_does_not_roll_back_last_finished_utc(job):
    backup_utils.save_job(job)
    stale = dict(backup_utils.get_job(job["id"]))               # loaded before the run
    backup_utils.record_run(job["id"], {"ok": True, "message": "x", "files": 3,
                                        "finished_utc": T2})
    stale["last_finished_utc"] = T1                              # what the GUI copy might hold
    stale["name"] = "Renamed"

    backup_utils.save_job_settings(stale)

    stored = backup_utils.get_job(job["id"])
    assert stored["name"] == "Renamed" and stored["last_finished_utc"] == T2
    assert "last_finished_utc" not in jobs_store.SETTINGS_KEYS


# ---------------------------------------------------------------------------
# The helpers, and speed
# ---------------------------------------------------------------------------

def test_utc_stamp_formats_naive_as_utc_and_converts_aware():
    assert versions.utc_stamp(real_datetime(2026, 10, 25, 1, 45, 12)) == "2026-10-25T01:45:12Z"
    from datetime import timedelta
    plus_one = timezone(timedelta(hours=1))
    assert versions.utc_stamp(real_datetime(2026, 10, 25, 2, 45, 12, tzinfo=plus_one)) == "2026-10-25T01:45:12Z"


def test_the_real_utc_clock_is_aware_and_independent_of_the_patched_local_one(monkeypatch):
    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2000, 1, 1)

    monkeypatch.setattr(runner, "datetime", Frozen)

    assert runner._utc_now().tzinfo is not None
    assert runner._utc_now().year >= 2026


def test_ordering_200_versions_reads_200_trailers_quickly(job):
    stamps = [f"2026{(i // 28) + 1:02d}{(i % 28) + 1:02d}-000000" for i in range(200)]
    for i, stamp in enumerate(stamps):
        fake_version(job, stamp, f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
    reads = []
    real_last_line = versions._last_line

    def counting(path, limit=4096):
        reads.append(path)
        return real_last_line(path, limit)

    versions._last_line = counting
    try:
        started = time.perf_counter()
        ordered = versions.ordered_versions(job)
        cold = time.perf_counter() - started    # includes Defender's first scan of 200 new files
        reads.clear()
        started = time.perf_counter()
        versions.ordered_versions(job)
        elapsed = time.perf_counter() - started
    finally:
        versions._last_line = real_last_line

    assert ordered == stamps
    assert len(reads) == 200                      # one tail read per version, no more
    assert cold < 15.0 and elapsed < 2.0
    print(f"ordered 200 versions: {cold * 1000:.0f} ms cold, {elapsed * 1000:.0f} ms warm")
