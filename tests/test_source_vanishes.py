# tests/test_source_vanishes.py
# Card 49: a source that goes away (or loses most of its files) part-way
# through the copy fails the run. Pre-flight counts the source once and
# nothing used to compare that count with what was copied, so a run whose
# source was unplugged after the first file verified that one file, promoted
# it as the newest verified backup and PRUNED a complete one.
#
# The source is made to vanish from inside the real run -- from the
# `on_progress` callback, or by wrapping the real `_hash_copy` -- never by
# patching the function under test. Expected hashes are computed here with
# hashlib, independently of the code under test.
import errno
import hashlib
import os

import pytest

from utils import backup_utils, copying, jobs_store

TOP, SUB = 24, 16          # 40 files; `sub` is 40% of them
FILES = TOP + SUB


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


def make_tree(tmp_path, top=TOP, sub=SUB, retention=2):
    """A source with `top` files at the root and `sub` in a subfolder."""
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    for i in range(top + sub):
        folder = source if i < top else source / "sub"
        (folder / f"file{i:02d}.bin").write_bytes(os.urandom(1500 + i))
    target = tmp_path / "dst"
    target.mkdir()
    return backup_utils.new_job("Vanish job", str(source), str(target),
                                schedule="manual", retention=retention)


@pytest.fixture
def job(tmp_path):
    return make_tree(tmp_path)


def sha(path) -> str:
    """Independent SHA-256 of a file on disk."""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def tree_hashes(root) -> dict:
    """{relative path: sha256} for every file under `root`."""
    found = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, root).replace("\\", "/")] = sha(full)
    return found


def two_good_versions(job):
    """Two real verified versions; returns ([stamps], {stamp: hashes})."""
    stamps = []
    for _ in range(2):
        result = backup_utils.run_job(job)
        assert result["ok"] and result["files"] == FILES, result["message"]
        stamps.append(result["version"])
    hashes = {s: tree_hashes(backup_utils.version_path(job, s)) for s in stamps}
    for stamp in stamps:  # positive control: both really are complete
        assert hashes[stamp] == tree_hashes(job["source"])
        assert backup_utils.is_verified(job, stamp)
    return stamps, hashes


def move(path, dest):
    """Rename `path` to `dest` (the folder 'goes away')."""
    os.rename(str(path), str(dest))


def when_copied(count, action):
    """An `on_progress` that runs `action()` once, when `count` files have
    been copied (the first call to reach it is always the copy phase)."""
    state = {"fired": False}

    def progress(done, total):
        if not state["fired"] and done == count:
            state["fired"] = True
            action()
    return progress


def counting_hash_copy(monkeypatch):
    """Wrap the real `_hash_copy`, counting calls; returns the counter."""
    real = backup_utils._hash_copy
    calls = {"n": 0}

    def wrapper(src, dst):
        calls["n"] += 1
        return real(src, dst)

    monkeypatch.setattr(copying, "_hash_copy", wrapper)
    return calls


def assert_failed_cleanly(job, result, stamps, hashes):
    """Nothing promoted, pruned or left behind; earlier backups untouched."""
    assert result["ok"] is False, result["message"]
    assert "pruned" not in result and "version" not in result
    expected = sorted(stamps + [s + backup_utils.MANIFEST_SUFFIX for s in stamps])
    assert sorted(os.listdir(backup_utils.job_root(job))) == expected
    assert backup_utils.version_names(job) == stamps
    for stamp in stamps:
        assert backup_utils.is_verified(job, stamp)
        assert tree_hashes(backup_utils.version_path(job, stamp)) == hashes[stamp]


# ---------------------------------------------------------------------------
# The Done-when: the source disappears after the first file is copied
# ---------------------------------------------------------------------------

def test_a_source_that_vanishes_after_the_first_file_fails_the_run(job, tmp_path, monkeypatch):
    stamps, hashes = two_good_versions(job)  # retention 2: a good run prunes one
    source = job["source"]
    calls = counting_hash_copy(monkeypatch)

    result = backup_utils.run_job(
        job, on_progress=when_copied(1, lambda: move(source, tmp_path / "away")))

    assert (tmp_path / "away").is_dir() and not os.path.exists(source)  # it really vanished
    assert_failed_cleanly(job, result, stamps, hashes)
    assert result["cause"] == "source_unavailable"
    assert "The source folder became unavailable during the backup" in result["message"]
    assert "only 1 of 40 file(s) were copied" in result["message"]
    assert "skipped" not in result["message"]
    # It stopped at the first sign of trouble instead of grinding through the
    # 38 remaining files as 38 skips.
    assert calls["n"] == 2


def test_a_vanished_source_prunes_nothing_when_retention_would(job, tmp_path):
    # With retention 2 and two versions a successful run WOULD prune one, but
    # a prune that wrongly ran after the failure would still find two versions
    # and delete nothing. Retention 1 over two versions makes "nothing was
    # pruned" a statement that could have been false.
    stamps, hashes = two_good_versions(job)
    job["retention"] = 1
    source = job["source"]

    result = backup_utils.run_job(
        job, on_progress=when_copied(1, lambda: move(source, tmp_path / "away")))

    assert_failed_cleanly(job, result, stamps, hashes)
    # Positive control: reconnect the source and the same job prunes normally.
    move(tmp_path / "away", source)
    again = backup_utils.run_job(job)
    assert again["ok"], again["message"]
    assert again["pruned"] == stamps


def test_a_subfolder_that_vanishes_beyond_tolerance_fails_as_incomplete(job, tmp_path):
    stamps, hashes = two_good_versions(job)
    sub = os.path.join(job["source"], "sub")

    result = backup_utils.run_job(
        job, on_progress=when_copied(1, lambda: move(sub, tmp_path / "away")))

    assert os.path.isdir(job["source"])  # the root is alive: only `sub` went
    assert_failed_cleanly(job, result, stamps, hashes)
    assert result["cause"] == "source_incomplete"
    assert f"only {TOP} of {FILES} file(s) were copied" in result["message"]


# ---------------------------------------------------------------------------
# Positive controls: a few skips are normal and the run still succeeds
# ---------------------------------------------------------------------------

def lock_files(monkeypatch, names):
    """Make opening these source files for reading raise PermissionError."""
    real_open = open
    bad = {n.lower() for n in names}

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)).lower() in bad:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)


def test_two_locked_files_of_forty_stay_a_skip_and_prune_still_runs(job, monkeypatch):
    stamps, _ = two_good_versions(job)
    lock_files(monkeypatch, ["file03.bin", "file30.bin"])

    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["files"] == FILES - 2 and len(result["errors"]) == 2
    assert result["not_copied"] == 2
    assert "2 file(s) could not be read and were skipped" in result["message"]
    assert result["pruned"] == [stamps[0]]  # 3 versions, retention 2
    assert backup_utils.is_verified(job, result["version"])


@pytest.mark.parametrize("locked, ok", [(2, True), (3, False)])
def test_the_floor_protects_a_tiny_tree_but_not_a_mostly_missing_one(
        tmp_path, monkeypatch, locked, ok):
    # 5 files: 2 locked is 40% (> 30%) but under the 3-file floor, so it is a
    # skip; 3 locked is both, so it fails.
    job = make_tree(tmp_path, top=5, sub=0, retention=2)
    lock_files(monkeypatch, [f"file{i:02d}.bin" for i in range(locked)])

    result = backup_utils.run_job(job)

    assert result["ok"] is ok, result["message"]
    if ok:
        assert result["files"] == 5 - locked
    else:
        assert result["cause"] == "source_incomplete"
        assert backup_utils.version_names(job) == []
        assert os.listdir(backup_utils.job_root(job)) == []


def test_a_vanished_folder_is_counted_by_its_files_not_as_one_skip(tmp_path):
    # 100 files, 20 of them in a folder that disappears: 20% is within
    # tolerance, so the run is ok -- but it used to say "1 file(s) could not be
    # read" because a whole unlistable folder is a single entry in `errors`.
    job = make_tree(tmp_path, top=80, sub=20)
    sub = os.path.join(job["source"], "sub")

    result = backup_utils.run_job(
        job, on_progress=when_copied(1, lambda: move(sub, tmp_path / "away")))

    assert result["ok"], result["message"]
    assert result["files"] == 80
    assert len(result["errors"]) == 1 and result["errors"][0].startswith("sub:")
    assert result["not_copied"] == 20
    assert "20 file(s) could not be read and were skipped" in result["message"]


# ---------------------------------------------------------------------------
# When exactly the source goes away
# ---------------------------------------------------------------------------

def test_a_source_gone_when_the_last_file_lands_still_fails(job, tmp_path):
    # The check is made when the copy ends, whatever the counts: here every
    # file was copied and no per-file error ever fired, so only the end-of-copy
    # root check can catch it.
    stamps, hashes = two_good_versions(job)
    source = job["source"]

    result = backup_utils.run_job(
        job, on_progress=when_copied(FILES, lambda: move(source, tmp_path / "away")))

    assert_failed_cleanly(job, result, stamps, hashes)
    assert result["cause"] == "source_unavailable"
    assert f"only {FILES} of {FILES} file(s) were copied" in result["message"]


def test_a_source_that_goes_away_during_verification_does_not_matter(job, tmp_path):
    # Chosen behaviour: the copy was complete and verification reads the COPY,
    # so a source unplugged afterwards changes nothing. Positive control for the
    # tests above: the same removal, one phase later, is a success.
    source = job["source"]
    gone = {"done": False}

    def on_phase(phase):
        if phase == "verifying" and not gone["done"]:
            gone["done"] = True
            move(source, tmp_path / "away")

    result = backup_utils.run_job(job, on_phase=on_phase)

    assert gone["done"] and not os.path.exists(source)
    assert result["ok"], result["message"]
    assert result["files"] == FILES and result["verified"] is True
    assert backup_utils.is_verified(job, result["version"])


@pytest.mark.parametrize("blips,recovered", [
    ({2}, True),                 # back for the retry (card b5CfTkL8): copied after all
    ({2, FILES + 1}, False),     # gone for the retry too: one skipped file
])
def test_a_source_that_blips_away_for_one_file_is_just_a_skip(job, tmp_path, monkeypatch,
                                                              blips, recovered):
    # Gone for exactly one file's read, back before the next look: the run
    # sees a live root and one unreadable file, not an unavailable source. The
    # single retry after the walk is the file's second look.
    real = backup_utils._hash_copy
    state = {"n": 0}

    def wrapper(src, dst):
        state["n"] += 1
        if state["n"] not in blips:
            return real(src, dst)
        move(job["source"], tmp_path / "away")
        try:
            return real(src, dst)  # the real copy: the file is not there
        finally:
            move(tmp_path / "away", job["source"])

    monkeypatch.setattr(copying, "_hash_copy", wrapper)

    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    if recovered:
        assert result["files"] == FILES and result["errors"] == []
        assert result["retried"] == 1
    else:
        assert result["files"] == FILES - 1 and len(result["errors"]) == 1
        assert result["retried"] == 0


def test_a_root_replaced_by_an_empty_folder_fails_as_incomplete(job, tmp_path):
    # The drive comes back empty (or the folder is recreated): the root exists
    # and lists fine, so only the shortfall can catch it.
    stamps, hashes = two_good_versions(job)
    source = job["source"]

    def swap_for_empty():
        move(source, tmp_path / "away")
        os.mkdir(source)

    result = backup_utils.run_job(job, on_progress=when_copied(1, swap_for_empty))

    assert os.listdir(source) == []
    assert_failed_cleanly(job, result, stamps, hashes)
    assert result["cause"] == "source_incomplete"


def test_cancelling_still_works_and_is_not_called_a_vanished_source(job):
    stamps, hashes = two_good_versions(job)
    state = {"n": 0}

    def abort():
        state["n"] += 1
        return state["n"] > 3

    result = backup_utils.run_job(job, should_abort=abort)

    assert_failed_cleanly(job, result, stamps, hashes)
    assert result["message"] == "Backup cancelled" and "cause" not in result


# ---------------------------------------------------------------------------
# Restore: the stored version is the source
# ---------------------------------------------------------------------------

def test_a_restore_whose_version_vanishes_fails_and_removes_its_folder(job, tmp_path):
    stamps, hashes = two_good_versions(job)
    version = backup_utils.version_path(job, stamps[1])
    out = tmp_path / "out"
    out.mkdir()

    result = backup_utils.restore_version(
        job, stamps[1], str(out),
        on_progress=when_copied(1, lambda: move(version, tmp_path / "away")))

    assert result["ok"] is False, result["message"]
    assert result["cause"] == "source_unavailable"
    assert "The backup version became unavailable during the restore" in result["message"]
    assert "The incomplete restore folder was removed" in result["message"]
    assert os.listdir(out) == []
    # The stored version is exactly as it was once it is back.
    move(tmp_path / "away", version)
    assert tree_hashes(version) == hashes[stamps[1]]
    assert backup_utils.is_verified(job, stamps[1])


def test_a_restore_into_an_existing_folder_leaves_it_when_the_version_vanishes(job, tmp_path):
    stamps, _ = two_good_versions(job)
    version = backup_utils.version_path(job, stamps[1])
    out = tmp_path / "out"
    out.mkdir()
    folder = backup_utils.restore_version(job, stamps[1], str(out))["path"]
    (out / os.path.basename(folder) / "my notes.txt").write_text("mine", encoding="utf-8")

    result = backup_utils.restore_version(
        job, stamps[1], str(out),
        on_progress=when_copied(1, lambda: move(version, tmp_path / "away")))

    assert result["ok"] is False and result["cause"] == "source_unavailable"
    assert "may now be incomplete" in result["message"]
    assert (out / os.path.basename(folder) / "my notes.txt").read_text(
        encoding="utf-8") == "mine"
