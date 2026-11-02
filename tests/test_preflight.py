# tests/test_preflight.py
# Coverage for the pre-flight stage of utils/backup_utils.run_job: every guard
# must refuse BEFORE anything is created in the target, and a normal run must
# pass all of them.
#
# Free space and volume identity are injected (preflight's parameters, or a
# monkeypatch of the module-level defaults when going through run_job) so no
# test depends on a real drive's state.
import os

import pytest

from utils import backup_utils

GB = 1024 ** 3


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_utils, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def tree(tmp_path):
    """A 2-file source and a job whose target folder does NOT exist yet, so
    "nothing was created" is observable as the folder still being absent."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "b.txt").write_text("beta", encoding="utf-8")
    target = tmp_path / "dst"
    job = backup_utils.new_job("j", str(source), str(target), schedule="manual")
    return job, source, target


def roomy(_path):
    return 10 * GB


def test_normal_job_passes_every_guard_and_backs_up(tree):
    job, _, target = tree
    assert backup_utils.preflight(job, free_space=roomy) is None
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert (target / job["id"] / result["version"] / "a.txt").exists()


# --- guard 1: source ---------------------------------------------------------

def test_empty_source_is_blocked_before_anything_is_written(tree):
    job, source, target = tree
    for child in source.iterdir():
        child.unlink()
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "empty" in result["message"] and "nothing was backed up" in result["message"]
    assert not target.exists()


def test_source_holding_only_empty_folders_counts_as_empty(tree):
    job, source, target = tree
    for child in source.iterdir():
        child.unlink()
    (source / "sub").mkdir()
    assert "empty" in backup_utils.preflight(job, free_space=roomy)
    assert not target.exists()


# --- guard 2: target vs source ----------------------------------------------

def test_target_inside_source_is_blocked_before_anything_is_written(tree):
    job, source, _ = tree
    inside = source / "backups"
    job["target"] = str(inside)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "inside the source" in result["message"]
    assert not inside.exists()


def test_target_equal_to_source_is_blocked(tree):
    job, source, _ = tree
    job["target"] = str(source)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert not (source / job["id"]).exists()


def test_target_case_and_trailing_separator_variants_are_still_inside(tree):
    job, source, _ = tree
    job["target"] = str(source / "x") + os.sep
    assert "inside" in backup_utils.preflight(job, free_space=roomy)
    if os.name == "nt":
        job["target"] = str(source).upper()
        assert "inside" in backup_utils.preflight(job, free_space=roomy)


def test_sibling_whose_name_merely_starts_with_the_source_is_not_inside(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "f.txt").write_text("x", encoding="utf-8")
    sibling = tmp_path / "data2"
    assert backup_utils.target_inside_source(str(data), str(sibling)) is False
    job = backup_utils.new_job("j", str(data), str(sibling))
    assert backup_utils.preflight(job, free_space=roomy) is None


# --- guard 3: free space -----------------------------------------------------

def test_insufficient_space_is_blocked_before_anything_is_written(tree, monkeypatch):
    job, _, target = tree
    # Source is 9 bytes; report 5 free. Both amounts must appear.
    monkeypatch.setattr(backup_utils, "_free_bytes", lambda path: 5)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "Not enough free space" in result["message"]
    assert "needs about 9 B" in result["message"]
    assert "only 5 B is free" in result["message"]
    assert not target.exists()


def test_space_check_applies_the_safety_margin(tree):
    job, _, _ = tree   # add a bigger file so the 10% margin is visible
    (tree[1] / "big.bin").write_bytes(b"\0" * 1000)
    need = int((9 + 1000) * backup_utils.SPACE_MARGIN)
    assert backup_utils.preflight(job, free_space=lambda p: need) is None
    assert "Not enough free space" in backup_utils.preflight(job, free_space=lambda p: need - 1)


def test_space_is_measured_on_the_nearest_existing_ancestor(tree):
    job, _, target = tree
    job["target"] = str(target / "deep" / "er")   # none of it exists
    asked = []
    backup_utils.preflight(job, free_space=lambda p: asked.append(p) or 10 * GB)
    assert asked == [str(target.parent)]


def test_unreadable_free_space_is_reported_not_raised(tree):
    job, _, _ = tree

    def boom(_path):
        raise OSError("device not ready")

    assert "free space" in backup_utils.preflight(job, free_space=boom)


# --- guard 4: volume identity ------------------------------------------------

def test_changed_volume_is_blocked_before_anything_is_written(tree, monkeypatch):
    job, _, target = tree
    job["target_volume"] = "111"
    monkeypatch.setattr(backup_utils, "volume_id", lambda path: "222")
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "not the same drive" in result["message"]
    assert not target.exists()


def test_matching_recorded_volume_passes(tree, monkeypatch):
    job, _, _ = tree
    job["target_volume"] = "111"
    monkeypatch.setattr(backup_utils, "volume_id", lambda path: "111")
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]


def test_missing_recorded_volume_passes_and_is_recorded_on_success(tree, monkeypatch):
    job, _, _ = tree
    monkeypatch.setattr(backup_utils, "volume_id", lambda path: "333")
    backup_utils.save_job(job)
    assert not backup_utils.get_job(job["id"]).get("target_volume")

    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    backup_utils.record_run(job["id"], result)
    assert backup_utils.get_job(job["id"])["target_volume"] == "333"


def test_a_failed_run_does_not_record_a_volume(tree, monkeypatch):
    job, _, _ = tree
    monkeypatch.setattr(backup_utils, "volume_id", lambda path: "333")
    backup_utils.save_job(job)
    backup_utils.record_run(job["id"], {"ok": False, "message": "x", "target_volume": "333"})
    assert not backup_utils.get_job(job["id"]).get("target_volume")


def test_record_run_never_overwrites_an_existing_volume(tree):
    job, _, _ = tree
    job["target_volume"] = "111"
    backup_utils.save_job(job)
    backup_utils.record_run(job["id"], {"ok": True, "message": "x", "target_volume": "999"})
    assert backup_utils.get_job(job["id"])["target_volume"] == "111"


def test_new_job_stores_the_volume_it_is_given():
    assert backup_utils.new_job("n", "s", "t", target_volume="42")["target_volume"] == "42"
    assert backup_utils.new_job("n", "s", "t")["target_volume"] == ""


def test_real_volume_id_is_stable_for_one_folder(tmp_path):
    assert backup_utils.volume_id(str(tmp_path)) == backup_utils.volume_id(str(tmp_path))
    assert backup_utils.target_volume(str(tmp_path / "not" / "yet")) == \
        backup_utils.volume_id(str(tmp_path))


# --- unavailable drive -------------------------------------------------------

def _unused_drive_target():
    """A path on a drive letter that is not mounted, or None off Windows."""
    if os.name != "nt":
        return None
    for letter in "ZYXWVUTSRQPONM":
        if not os.path.exists(f"{letter}:\\"):
            return f"{letter}:\\backups"
    return None


def test_unavailable_drive_is_reported_and_terminates(tree):
    target = _unused_drive_target()
    if target is None:
        pytest.skip("needs an unmounted Windows drive letter")
    job, _, _ = tree
    job["target"] = target
    assert backup_utils.nearest_existing_ancestor(target) is None
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "drive is not available" in result["message"]
    assert backup_utils.target_volume(target) == ""


def test_nearest_existing_ancestor_walks_up_to_a_real_folder(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    assert backup_utils.nearest_existing_ancestor(str(deep)) == str(tmp_path)
    assert backup_utils.nearest_existing_ancestor(str(tmp_path)) == str(tmp_path)
