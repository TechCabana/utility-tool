# tests/test_restore_candidates.py
# Card 45: Restore prefers the newest VERIFIED version and says when it falls
# back. `restore_candidates` is the engine answer; `restore_version(job, None,
# ...)` is the default restore that follows it.
#
# Versions are built two ways: hand-made folders with a hand-written manifest
# trailer (fast, exact control over what "verified" looks like on disk) and
# real `run_job` runs (the content tests, so what lands in the restored folder
# is what a real backup of that content holds).
import json
import os

import pytest

from utils import backup_utils, jobs_store

OLD, MID, NEW = "20260101-000000", "20260102-000000", "20260103-000000"


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def job(tmp_path):
    """A job whose target is empty; versions are added by the tests."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    target = tmp_path / "dst"
    target.mkdir()
    return backup_utils.new_job("Cand job", str(source), str(target),
                                schedule="manual", retention=9)


def fake_version(job, stamp, verify=None, trailer=None):
    """A version folder plus a manifest ending in the trailer for `verify`
    ("full" / "quick" / "off"), a legacy trailer (verify=None, verified true),
    or `trailer` raw bytes; `trailer="none"` writes no manifest at all."""
    os.makedirs(backup_utils.version_path(job, stamp))
    if trailer == "none":
        return
    path = backup_utils.manifest_path(job, stamp)
    if trailer is not None:
        with open(path, "wb") as handle:
            handle.write(trailer)
        return
    end = {"type": "end", "files": 0, "bytes": 0,
           "verified": verify != "off"}
    if verify is not None:
        end["verify"] = verify
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "header", "version": 1, "stamp": stamp}) + "\n")
        handle.write(json.dumps(end) + "\n")


def plan(job):
    return backup_utils.restore_candidates(job)


def test_a_newer_off_version_is_passed_over_for_the_verified_one(job):
    fake_version(job, OLD, "full")
    fake_version(job, NEW, "off")
    assert plan(job) == {"preferred": OLD, "latest": NEW, "fallback": False,
                         "newer_unverified": [NEW]}


def test_only_unverified_versions_fall_back_to_the_latest(job):
    fake_version(job, OLD, "off")
    fake_version(job, NEW, "off")
    assert plan(job) == {"preferred": NEW, "latest": NEW, "fallback": True,
                         "newer_unverified": []}


def test_legacy_versions_with_no_manifest_fall_back_to_the_latest(job):
    fake_version(job, OLD, trailer="none")
    fake_version(job, NEW, trailer="none")
    assert plan(job) == {"preferred": NEW, "latest": NEW, "fallback": True,
                         "newer_unverified": []}


def test_all_verified_prefers_the_latest_with_nothing_passed_over(job):
    fake_version(job, OLD, "full")
    fake_version(job, NEW, "full")
    assert plan(job) == {"preferred": NEW, "latest": NEW, "fallback": False,
                         "newer_unverified": []}


def test_a_quick_version_counts_as_verified(job):
    fake_version(job, OLD, "full")
    fake_version(job, NEW, "quick")
    assert plan(job)["preferred"] == NEW
    assert plan(job)["newer_unverified"] == []


def test_a_trailer_written_before_modes_existed_counts_as_verified(job):
    fake_version(job, OLD, None)     # verified true, no "verify" key
    assert plan(job)["preferred"] == OLD and plan(job)["fallback"] is False


@pytest.mark.parametrize("body", [
    b"",                                                           # empty
    b'{"type":"header","version":1}\n{"type":"end","fi',           # truncated
    b"\x00\xff garbage \x13\n\x88",                                # junk
    b'{"type":"header","version":1}\n{"type":"file","path":"a"}\n',  # no trailer
])
def test_a_damaged_manifest_is_treated_as_unverified(job, body):
    fake_version(job, OLD, "full")
    fake_version(job, NEW, trailer=body)
    assert plan(job) == {"preferred": OLD, "latest": NEW, "fallback": False,
                         "newer_unverified": [NEW]}


def test_no_versions_gives_nothing(job):
    assert plan(job) == {"preferred": None, "latest": None, "fallback": False,
                         "newer_unverified": []}


def test_a_job_that_was_never_backed_up_has_no_target_folder_at_all(job):
    # The job root does not exist yet: still Nones, not an exception.
    assert not os.path.exists(backup_utils.job_root(job))
    assert plan(job)["preferred"] is None


def test_an_unverified_version_older_than_the_verified_one_is_not_listed(job):
    fake_version(job, OLD, "off")
    fake_version(job, MID, "full")
    fake_version(job, NEW, "off")
    assert plan(job) == {"preferred": MID, "latest": NEW, "fallback": False,
                         "newer_unverified": [NEW]}


def test_several_newer_unverified_versions_are_all_listed_oldest_first(job):
    fake_version(job, OLD, "full")
    fake_version(job, MID, "off")
    fake_version(job, NEW, trailer="none")
    assert plan(job)["newer_unverified"] == [MID, NEW]


def test_a_manifest_with_no_folder_is_not_a_candidate(job):
    # An orphan manifest (crash leftover) is not a version, verified or not.
    fake_version(job, OLD, "full")
    with open(backup_utils.manifest_path(job, NEW), "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "end", "files": 0, "bytes": 0,
                                 "verified": True, "verify": "full"}) + "\n")
    assert plan(job) == {"preferred": OLD, "latest": OLD, "fallback": False,
                         "newer_unverified": []}


def test_version_when_formats_a_stamp_the_way_the_restore_row_shows_it():
    assert backup_utils.version_when("20260106-143005") == "2026-01-06 14:30"
    assert backup_utils.version_when("not-a-stamp") == "not-a-stamp"
    assert backup_utils.version_when(None) is None


# ---------------------------------------------------------------------------
# The default restore, on real backups
# ---------------------------------------------------------------------------

def back_up(job, source, content, verify):
    """One real run of `job` over a source holding `content`, in `verify` mode."""
    (source / "a.txt").write_text(content, encoding="utf-8")
    job["verify"] = verify
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    return result["version"]


def restored_text(result):
    assert result["ok"], result["message"]
    with open(os.path.join(result["path"], "a.txt"), encoding="utf-8") as handle:
        return handle.read()


def test_default_restore_brings_back_the_verified_version_not_the_newer_off_one(job, tmp_path):
    source = tmp_path / "src"
    verified = back_up(job, source, "verified content", "full")
    newer = back_up(job, source, "newer unverified content", "off")
    assert verified < newer
    assert plan(job)["preferred"] == verified and plan(job)["latest"] == newer

    default = backup_utils.restore_version(job, None, str(tmp_path / "out_default"))
    assert restored_text(default) == "verified content"
    assert default["path"].endswith(f"-restored-{verified}")

    explicit = backup_utils.restore_version(job, newer, str(tmp_path / "out_newest"))
    assert restored_text(explicit) == "newer unverified content"


def test_default_restore_of_a_job_with_nothing_verified_restores_the_latest(job, tmp_path):
    source = tmp_path / "src"
    back_up(job, source, "first off", "off")
    back_up(job, source, "second off", "off")
    assert plan(job)["fallback"] is True
    result = backup_utils.restore_version(job, None, str(tmp_path / "out"))
    assert restored_text(result) == "second off"


def test_default_restore_with_no_backup_says_so_and_writes_nothing(job, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = backup_utils.restore_version(job, None, str(out))
    assert result["ok"] is False and "no backup" in result["message"].lower()
    assert os.listdir(out) == []
