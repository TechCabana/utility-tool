# tests/test_sweep.py
# Coverage for utils/backup_utils.sweep_stale and its wiring into _run_job:
# abandoned `.partial` folders, `.tmp` manifests and orphan manifests older than
# 7 days are removed; the newest `.partial` is always kept; promoted versions,
# their manifests and anything that does not match the exact name shapes are
# never touched; one undeletable item never fails the run.
#
# Everything is built by NAME in tmp_path and aged with the injected `now=`, so
# nothing sleeps. Free space is injected by patching backup_utils._free_bytes
# with a function that inspects the real filesystem.
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from utils import backup_utils

GB = 1024 ** 3
NOW = datetime(2026, 11, 13, 12, 0, 0)
OLD1, OLD2, OLD3 = "20200101-000000", "20200102-000000", "20200103-000000"
YOUNG = "20261112-000000"  # one day before NOW


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_utils, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def tree(tmp_path):
    """A 1-file source and a job whose root folder exists and is empty."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    target = tmp_path / "dst"
    job = backup_utils.new_job("j", str(source), str(target), schedule="manual")
    root = target / job["id"]
    root.mkdir(parents=True)
    return job, root


def partial(root, stamp, with_file=True):
    """Create `<stamp>.partial/` (holding a file) and return its path."""
    path = root / f"{stamp}.partial"
    path.mkdir()
    if with_file:
        (path / "data.bin").write_text("x", encoding="utf-8")
    return path


def tmp_manifest(root, stamp):
    path = root / f"{stamp}.manifest.jsonl.tmp"
    path.write_text("{}\n", encoding="utf-8")
    return path


def manifest(root, stamp):
    path = root / f"{stamp}.manifest.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def names(root):
    return sorted(p.name for p in root.iterdir())


# --- sweep_stale: what goes ---------------------------------------------------

def test_missing_job_root_is_an_empty_sweep(tmp_path):
    job = backup_utils.new_job("j", str(tmp_path / "s"), str(tmp_path / "nope"))
    assert backup_utils.sweep_stale(job, now=NOW) == {
        "removed": [], "errors": [], "kept_for_inspection": None}
    assert not (tmp_path / "nope").exists()  # a sweep never creates anything


def test_old_partial_is_removed_while_a_newer_one_is_kept(tree):
    job, root = tree
    partial(root, OLD1)
    partial(root, YOUNG)
    result = backup_utils.sweep_stale(job, now=NOW)
    # OLD1 is not the newest, so the keep-newest rule does not save it.
    assert result["removed"] == [f"{OLD1}.partial"]
    assert names(root) == [f"{YOUNG}.partial"]
    assert result["errors"] == []
    assert result["kept_for_inspection"] is None  # the survivor is simply young


def test_only_the_newest_partial_survives_when_several_are_old(tree):
    job, root = tree
    for stamp in (OLD1, OLD2, OLD3):
        partial(root, stamp)
        tmp_manifest(root, stamp)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert sorted(result["removed"]) == sorted([
        f"{OLD1}.partial", f"{OLD2}.partial",
        f"{OLD1}.manifest.jsonl.tmp", f"{OLD2}.manifest.jsonl.tmp"])
    assert result["kept_for_inspection"] == f"{OLD3}.partial"
    # the kept partial's own tmp manifest stays with it
    assert names(root) == [f"{OLD3}.manifest.jsonl.tmp", f"{OLD3}.partial"]


def test_old_tmp_and_orphan_manifests_are_removed(tree):
    job, root = tree
    tmp_manifest(root, OLD1)
    manifest(root, OLD2)  # no folder OLD2 -> orphan
    result = backup_utils.sweep_stale(job, now=NOW)
    assert sorted(result["removed"]) == [f"{OLD1}.manifest.jsonl.tmp",
                                         f"{OLD2}.manifest.jsonl"]
    assert names(root) == []


def test_young_leftovers_are_kept(tree):
    job, root = tree
    partial(root, YOUNG)
    tmp_manifest(root, YOUNG)
    manifest(root, "20261113-000000")  # an orphan, but from today
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == []
    assert len(names(root)) == 3


def test_age_is_strictly_older_than_the_cutoff(tree):
    job, root = tree
    exactly = (NOW - timedelta(days=7)).strftime(backup_utils.VERSION_FORMAT)
    just_over = (NOW - timedelta(days=7, seconds=1)).strftime(backup_utils.VERSION_FORMAT)
    tmp_manifest(root, exactly)
    tmp_manifest(root, just_over)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == [f"{just_over}.manifest.jsonl.tmp"]
    assert names(root) == [f"{exactly}.manifest.jsonl.tmp"]


def test_max_age_days_is_honoured(tree):
    job, root = tree
    tmp_manifest(root, YOUNG)  # one day old
    assert backup_utils.sweep_stale(job, now=NOW)["removed"] == []
    assert backup_utils.sweep_stale(job, now=NOW, max_age_days=0)["removed"] == [
        f"{YOUNG}.manifest.jsonl.tmp"]


def test_default_now_is_the_real_clock(tree):
    job, root = tree
    tmp_manifest(root, OLD1)
    assert backup_utils.sweep_stale(job)["removed"] == [f"{OLD1}.manifest.jsonl.tmp"]


def test_read_only_files_inside_a_partial_are_removed(tree):
    job, root = tree
    old = partial(root, OLD1)
    partial(root, OLD2)
    os.chmod(old / "data.bin", 0o444)  # a backup keeps the source's read-only bit
    assert backup_utils.sweep_stale(job, now=NOW)["removed"] == [f"{OLD1}.partial"]
    assert not old.exists()


# --- sweep_stale: what never goes -----------------------------------------------

def test_a_manifest_whose_version_folder_exists_is_never_removed(tree):
    job, root = tree
    (root / OLD1).mkdir()
    (root / OLD1 / "a.txt").write_text("kept", encoding="utf-8")
    manifest(root, OLD1)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == []
    assert names(root) == [OLD1, f"{OLD1}.manifest.jsonl"]
    assert (root / OLD1 / "a.txt").read_text(encoding="utf-8") == "kept"


def test_promoted_versions_are_never_touched(tree):
    job, root = tree
    for stamp in (OLD1, YOUNG):
        (root / stamp).mkdir()
        manifest(root, stamp)
    partial(root, OLD2)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == []  # OLD2 is the newest partial -> kept
    assert result["kept_for_inspection"] == f"{OLD2}.partial"
    assert backup_utils.version_names(job) == [OLD1, YOUNG]


def test_names_that_are_not_exact_matches_are_never_touched(tree):
    job, root = tree
    (root / "notes.txt").write_text("mine", encoding="utf-8")
    (root / f"{OLD1}.partial.bak").write_text("mine", encoding="utf-8")
    (root / "x.partial").mkdir()
    (root / f"x{OLD1}.partial").mkdir()
    (root / f"{OLD1}.partial.d").mkdir()
    (root / "20201399-000000.partial").mkdir()  # not a real date
    (root / f"{OLD1}.partial.txt").write_text("mine", encoding="utf-8")
    (root / f"{OLD2}.partial").write_text("a FILE, not a partial folder", encoding="utf-8")
    (root / f"{OLD2}.manifest.jsonl.tmp").mkdir()  # a DIRECTORY, not a tmp manifest
    (root / f"{OLD3}.manifest.jsonl").mkdir()  # a DIRECTORY, not a manifest
    (root / f"{OLD1}.manifest.jsonl.tmp.bak").write_text("mine", encoding="utf-8")
    (root / f"{OLD1}.manifest.json").write_text("mine", encoding="utf-8")
    before = names(root)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result == {"removed": [], "errors": [], "kept_for_inspection": None}
    assert names(root) == before


def _make_link(link, target):
    """Create a symlink, or a Windows junction; skip if neither is allowed."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if done.returncode == 0:
            return
    pytest.skip("cannot create a symlink or junction here (no privilege)")


def test_a_link_named_like_a_partial_is_not_followed_or_removed(tree, tmp_path):
    job, root = tree
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete", encoding="utf-8")
    _make_link(root / f"{OLD1}.partial", outside)
    # a newer real partial: were the link counted as a candidate/newest, this
    # real one would be swept or the link's target would be walked.
    partial(root, OLD2)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == []
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "do not delete"
    assert os.path.lexists(root / f"{OLD1}.partial")


def test_a_link_is_never_the_newest_partial(tree, tmp_path):
    job, root = tree
    outside = tmp_path / "precious"
    outside.mkdir()
    partial(root, OLD1)
    partial(root, OLD2)
    _make_link(root / f"{OLD3}.partial", outside)  # newest by name, but a link
    result = backup_utils.sweep_stale(job, now=NOW)
    # the link is invisible, so OLD2 is the newest real partial and is kept
    assert result["removed"] == [f"{OLD1}.partial"]
    assert result["kept_for_inspection"] == f"{OLD2}.partial"
    assert outside.exists()


# --- sweep_stale: failures ------------------------------------------------------

def test_an_undeletable_folder_is_reported_and_the_sweep_carries_on(tree, monkeypatch):
    job, root = tree
    for stamp in (OLD1, OLD2, OLD3):
        partial(root, stamp)
    real = backup_utils._rmtree

    def locked(path):
        if os.path.basename(path) == f"{OLD1}.partial":
            raise PermissionError(13, "in use", path)
        real(path)

    monkeypatch.setattr(backup_utils, "_rmtree", locked)
    result = backup_utils.sweep_stale(job, now=NOW)  # must not raise
    assert result["removed"] == [f"{OLD2}.partial"]
    assert len(result["errors"]) == 1 and f"{OLD1}.partial" in result["errors"][0]
    assert names(root) == [f"{OLD1}.partial", f"{OLD3}.partial"]


def test_an_undeletable_manifest_is_reported_and_the_sweep_carries_on(tree, monkeypatch):
    job, root = tree
    tmp_manifest(root, OLD1)
    tmp_manifest(root, OLD2)
    real = os.unlink

    def locked(path, *args, **kwargs):
        if os.path.basename(path) == f"{OLD1}.manifest.jsonl.tmp":
            raise PermissionError(13, "in use", path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(backup_utils.os, "unlink", locked)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == [f"{OLD2}.manifest.jsonl.tmp"]
    assert len(result["errors"]) == 1 and OLD1 in result["errors"][0]


def test_an_unreadable_root_is_an_error_not_an_exception(tree, monkeypatch):
    job, _ = tree

    def boom(_path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(backup_utils.os, "scandir", boom)
    result = backup_utils.sweep_stale(job, now=NOW)
    assert result["removed"] == [] and len(result["errors"]) == 1


# --- wired into run_job ---------------------------------------------------------

def test_run_reports_what_it_swept_and_still_succeeds(tree):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)
    manifest(root, OLD3)  # orphan
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert sorted(result["swept"]) == [f"{OLD1}.partial", f"{OLD3}.manifest.jsonl"]
    assert "Cleaned up 2 abandoned leftover(s) from earlier runs." in result["message"]
    assert (root / f"{OLD2}.partial").exists()  # the newest failed one is kept
    assert backup_utils.is_verified(job, result["version"])


def test_a_clean_run_has_an_empty_swept_list_and_no_note(tree):
    job, _ = tree
    result = backup_utils.run_job(job)
    assert result["ok"] and result["swept"] == []
    assert "Cleaned up" not in result["message"]


def test_a_locked_leftover_does_not_fail_the_run(tree, monkeypatch):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)
    real = backup_utils._rmtree

    def locked(path):
        if os.path.basename(path) == f"{OLD1}.partial":
            raise PermissionError(13, "in use", path)
        real(path)

    monkeypatch.setattr(backup_utils, "_rmtree", locked)
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert result["swept"] == []
    assert "1 leftover(s) could not be removed." in result["message"]
    assert (root / f"{OLD1}.partial").exists()


def test_the_sweep_runs_before_the_stamp_is_claimed(tree, monkeypatch):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)
    seen = []
    real_claim = backup_utils._claim_stamp
    monkeypatch.setattr(backup_utils, "_claim_stamp",
                        lambda r, j: seen.append(names(root)) or real_claim(r, j))
    assert backup_utils.run_job(job)["ok"]
    assert seen == [[f"{OLD2}.partial"]]  # OLD1 already gone, nothing new yet


def space_unless_gone(root, gone):
    """A free-space function reading the REAL filesystem: low while `gone` exists."""
    def free(_path):
        return 0 if (root / gone).exists() else 10 * GB
    return free


def test_low_space_message_is_identified_by_its_prefix(tree):
    job, _ = tree
    problem = backup_utils.preflight(job, free_space=lambda p: 0)
    assert problem.startswith(backup_utils.LOW_SPACE_PREFIX)


def test_a_full_target_is_cured_by_sweeping_then_the_run_succeeds(tree, monkeypatch):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)  # newest -> kept
    monkeypatch.setattr(backup_utils, "_free_bytes",
                        space_unless_gone(root, f"{OLD1}.partial"))
    # without the sweep this very job fails pre-flight:
    assert backup_utils.preflight(job).startswith(backup_utils.LOW_SPACE_PREFIX)
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert result["swept"] == [f"{OLD1}.partial"]
    assert "Cleaned up 1 abandoned leftover(s)" in result["message"]
    assert (root / f"{OLD2}.partial").exists()
    assert backup_utils.is_verified(job, result["version"])


def test_still_full_after_the_sweep_returns_the_low_space_failure(tree, monkeypatch):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)
    monkeypatch.setattr(backup_utils, "_free_bytes", lambda p: 0)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert result["message"].startswith(backup_utils.LOW_SPACE_PREFIX)
    assert result["swept"] == [f"{OLD1}.partial"]
    # the sweep deleted, and nothing was written: no stamp, no partial, no manifest
    assert names(root) == [f"{OLD2}.partial"]


def test_a_full_target_with_nothing_to_sweep_fails_untouched(tree, monkeypatch):
    job, root = tree
    partial(root, YOUNG)
    monkeypatch.setattr(backup_utils, "_free_bytes", lambda p: 0)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert result["message"].startswith(backup_utils.LOW_SPACE_PREFIX)
    assert result["swept"] == [] and "Cleaned up" not in result["message"]
    assert names(root) == [f"{YOUNG}.partial"]


def test_a_missing_source_does_not_trigger_a_sweep(tree):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)
    job["source"] = str(root.parent / "gone")
    result = backup_utils.run_job(job)
    assert result["ok"] is False and "swept" not in result
    assert names(root) == [f"{OLD1}.partial", f"{OLD2}.partial"]


def test_a_wrong_drive_does_not_trigger_a_sweep(tree, monkeypatch):
    job, root = tree
    partial(root, OLD1)
    partial(root, OLD2)
    job["target_volume"] = "some-other-drive"
    monkeypatch.setattr(backup_utils, "volume_id", lambda p: "this-drive")
    result = backup_utils.run_job(job)
    assert result["ok"] is False and "not the same drive" in result["message"]
    assert "swept" not in result
    assert names(root) == [f"{OLD1}.partial", f"{OLD2}.partial"]


def test_a_failed_verification_still_keeps_its_partial_for_inspection(tree, monkeypatch):
    job, root = tree
    partial(root, OLD1)
    monkeypatch.setattr(backup_utils, "_verify_copy",
                        lambda *a, **k: (1, 1, ["a.txt"]))
    result = backup_utils.run_job(job)
    assert result["ok"] is False and result["verified"] is False
    assert "kept for inspection" in result["message"]
    assert os.path.isdir(result["partial"])  # this run's partial is not a leftover
    assert result["swept"] == []  # OLD1 was the newest partial then -> kept
