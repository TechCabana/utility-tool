# tests/test_verify.py
# Coverage for the verified-backup path of utils/backup_utils.run_job: the
# hash-while-copying manifest, the re-read verification before a version is
# promoted, and prune gating on verified versions.
#
# Every expected hash is computed here, independently, from the SOURCE files
# with hashlib -- never read back from the code under test -- so a manifest
# that records the wrong thing cannot agree with itself and pass.
import glob
import hashlib
import json
import os
import time

import pytest

from utils import backup_utils


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_utils, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def tree(tmp_path):
    """A source with nested, binary, empty and mixed-case files; a job on it."""
    source = tmp_path / "src"
    (source / "Nested" / "Deeper").mkdir(parents=True)
    (source / "a.txt").write_text("alpha", encoding="utf-8")
    (source / "Nested" / "B.bin").write_bytes(os.urandom(3 * 1024 * 1024 + 7))
    (source / "Nested" / "Deeper" / "c.txt").write_text("gamma", encoding="utf-8")
    (source / "empty.dat").write_bytes(b"")
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Verify job", str(source), str(target),
                               schedule="manual", retention=1)
    return job, source, target


def sha(path) -> str:
    """Independent SHA-256 of a file on disk."""
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def source_files(source):
    """{relative path with forward slashes: absolute path} for the source."""
    found = {}
    for dirpath, _, names in os.walk(source):
        for name in names:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, source).replace("\\", "/")] = full
    return found


def manifest_lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def partial_dir(job):
    """The one in-flight `.partial` folder of `job`."""
    found = glob.glob(os.path.join(backup_utils.job_root(job), "*.partial"))
    assert len(found) == 1, found
    return found[0]


def corrupt_on_verify(job, rel="Nested/B.bin"):
    """An on_phase hook that flips one byte of a copied file just before the
    verify pass reads it -- the bad copy the card exists to catch."""
    def hook(phase):
        if phase == "verifying":
            path = os.path.join(partial_dir(job), *rel.split("/"))
            with open(path, "r+b") as handle:
                first = handle.read(1)
                handle.seek(0)
                handle.write(bytes([first[0] ^ 0xFF]))
    return hook


def delete_on_verify(job, rel="a.txt"):
    """An on_phase hook that removes one copied file before verification."""
    def hook(phase):
        if phase == "verifying":
            os.remove(os.path.join(partial_dir(job), *rel.split("/")))
    return hook


def fake_version(job, name, verified):
    """A version folder, with a verified manifest beside it if asked."""
    os.makedirs(backup_utils.version_path(job, name))
    if verified:
        with open(backup_utils.manifest_path(job, name), "w", encoding="utf-8") as h:
            h.write(json.dumps({"type": "header", "version": 1, "stamp": name}) + "\n")
            h.write(json.dumps({"type": "end", "files": 0, "bytes": 0,
                                "verified": True}) + "\n")


# ---------------------------------------------------------------------------
# A normal run
# ---------------------------------------------------------------------------

def test_a_run_writes_a_sibling_manifest_matching_independent_hashes(tree):
    job, source, _ = tree
    phases = []
    result = backup_utils.run_job(job, on_phase=phases.append)

    assert result["ok"], result["message"]
    assert result["verified"] is True
    assert phases == ["copying", "verifying"]
    version = backup_utils.version_path(job, result["version"])
    manifest = backup_utils.manifest_path(job, result["version"])
    assert result["manifest"] == manifest
    assert os.path.dirname(manifest) == backup_utils.job_root(job)
    assert not os.path.commonpath([manifest, version]) == version

    lines = manifest_lines(manifest)
    assert lines[0]["type"] == "header" and lines[0]["version"] == 1
    assert lines[0]["stamp"] == result["version"]
    assert lines[-1] == {"type": "end", "files": 4, "bytes": result["bytes"],
                         "verified": True}
    entries = {e["path"]: e for e in lines[1:-1]}
    assert all(e["type"] == "file" for e in entries.values())

    expected = source_files(source)
    assert set(entries) == set(expected)            # case preserved, "/" separated
    for rel, full in expected.items():
        assert entries[rel]["sha256"] == sha(full), rel
        assert entries[rel]["size"] == os.path.getsize(full), rel
        assert abs(entries[rel]["mtime"] - os.path.getmtime(full)) < 1e-3, rel
    assert result["bytes"] == sum(os.path.getsize(p) for p in expected.values())

    assert backup_utils.is_verified(job, result["version"])
    assert backup_utils.verified_versions(job) == [result["version"]]
    # The version folder is exactly the source: no manifest, no tmp inside.
    assert set(source_files(version)) == set(expected)
    # And nothing else is left beside it.
    assert sorted(os.listdir(backup_utils.job_root(job))) == sorted(
        [result["version"], result["version"] + backup_utils.MANIFEST_SUFFIX])
    assert backup_utils.changes_since(job)["changed"] == 0


def test_restore_of_a_manifest_version_matches_the_source_byte_for_byte(tree, tmp_path):
    job, source, _ = tree
    version = backup_utils.run_job(job)["version"]
    out = tmp_path / "out"
    out.mkdir()
    result = backup_utils.restore_version(job, version, str(out))
    assert result["ok"], result["message"]
    restored = source_files(result["path"])
    expected = source_files(source)
    assert set(restored) == set(expected)
    for rel in expected:
        assert open(restored[rel], "rb").read() == open(expected[rel], "rb").read()


# ---------------------------------------------------------------------------
# Verification failure: the Done-when
# ---------------------------------------------------------------------------

def test_positive_control_the_same_flow_without_corruption_verifies(tree):
    # Same two runs, same retention, same on_phase plumbing -- only the byte
    # flip is missing. Proves the flip is what fails the next test.
    job, _, _ = tree
    first = backup_utils.run_job(job)["version"]
    time.sleep(1.1)
    result = backup_utils.run_job(job, on_phase=lambda phase: None)
    assert result["ok"], result["message"]
    assert result["pruned"] == [first]


def test_a_byte_corrupted_mid_run_fails_and_keeps_the_older_verified_version(tree):
    job, source, _ = tree
    first = backup_utils.run_job(job)["version"]
    first_files = source_files(backup_utils.version_path(job, first))
    time.sleep(1.1)

    result = backup_utils.run_job(job, on_phase=corrupt_on_verify(job))

    assert result["ok"] is False
    assert "Nested/B.bin" in result["message"]
    assert "1 file(s)" in result["message"]
    # Nothing promoted, nothing pruned: only the first version exists, and it
    # is still verified and still byte-identical to the source.
    assert backup_utils.version_names(job) == [first]
    assert backup_utils.is_verified(job, first)
    for rel, full in source_files(source).items():
        assert sha(first_files[rel]) == sha(full)
    # The failed copy and its manifest are kept for inspection.
    assert os.path.isdir(result["partial"])
    assert result["partial"].endswith(".partial")
    assert os.path.isfile(result["manifest"])
    assert result["manifest"].endswith(".manifest.jsonl.tmp")
    stamp = os.path.basename(result["partial"])[:-len(".partial")]
    assert not os.path.exists(backup_utils.version_path(job, stamp))
    assert not os.path.exists(backup_utils.manifest_path(job, stamp))


def test_a_failed_run_prunes_nothing_even_a_legacy_version(tree):
    # A legacy (manifest-less) version is not protected by the verified rule,
    # so only "prune nothing on failure" keeps it alive at retention=1.
    job, _, _ = tree
    fake_version(job, "20200101-000000", verified=False)
    result = backup_utils.run_job(job, on_phase=corrupt_on_verify(job))
    assert result["ok"] is False
    assert backup_utils.version_names(job) == ["20200101-000000"]


def test_a_fault_on_the_write_path_during_the_copy_fails_the_run(tree, monkeypatch):
    # The bytes reach the destination wrong while being copied. The manifest
    # must hold the hash of what was READ from the source, so the verify pass
    # notices; a manifest describing the destination would bless the damage.
    job, _, _ = tree
    real_open = open

    class FlippingWriter:
        """A real destination handle whose first write lands one byte off."""

        def __init__(self, handle):
            self.handle, self.flipped = handle, False

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            if not self.flipped and data:
                data = bytes([data[0] ^ 0xFF]) + bytes(data[1:])
                self.flipped = True
            return self.handle.write(data)

    def fake_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if mode == "wb" and str(path).replace("\\", "/").endswith("Nested/B.bin"):
            return FlippingWriter(handle)
        return handle

    monkeypatch.setattr(backup_utils, "open", fake_open, raising=False)
    result = backup_utils.run_job(job)
    assert result["ok"] is False
    assert "Nested/B.bin" in result["message"]
    assert backup_utils.version_names(job) == []


def test_a_file_missing_at_verify_time_fails_the_run(tree):
    job, _, _ = tree
    first = backup_utils.run_job(job)["version"]
    time.sleep(1.1)
    result = backup_utils.run_job(job, on_phase=delete_on_verify(job, "a.txt"))
    assert result["ok"] is False
    assert "a.txt" in result["message"]
    assert backup_utils.version_names(job) == [first]
    assert os.path.isdir(result["partial"]) and os.path.isfile(result["manifest"])


# ---------------------------------------------------------------------------
# Cancelling and unreadable source files
# ---------------------------------------------------------------------------

def test_abort_during_copy_removes_partial_and_temp_manifest(tree):
    job, _, _ = tree
    calls = {"n": 0}

    def abort_after_first_file():
        calls["n"] += 1
        return calls["n"] > 1

    result = backup_utils.run_job(job, should_abort=abort_after_first_file)
    assert result == {"ok": False, "message": "Backup cancelled"}
    assert os.listdir(backup_utils.job_root(job)) == []


def test_abort_during_verify_removes_partial_and_temp_manifest(tree):
    job, _, _ = tree
    state = {"verifying": False}

    def on_phase(phase):
        if phase == "verifying":
            # Positive control: the copy really left both behind to clean up.
            assert glob.glob(os.path.join(backup_utils.job_root(job), "*.partial"))
            assert glob.glob(os.path.join(backup_utils.job_root(job), "*.tmp"))
            state["verifying"] = True

    result = backup_utils.run_job(job, on_phase=on_phase,
                                  should_abort=lambda: state["verifying"])
    assert result["message"] == "Backup cancelled"
    assert os.listdir(backup_utils.job_root(job)) == []


def test_an_unreadable_source_file_is_skipped_reported_and_left_out(tree, monkeypatch):
    job, source, _ = tree
    bad = os.path.join(str(source), "Nested", "Deeper", "c.txt")
    real_open = open
    seen = {"half_written": False}

    class FailingRead:
        """Opens for real, then fails on the first read -- after the copy has
        already created its destination file."""

        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, *args):
            seen["half_written"] = bool(glob.glob(os.path.join(
                backup_utils.job_root(job), "*.partial", "Nested", "Deeper", "c.txt")))
            raise OSError("simulated read failure")

    def fake_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if os.path.abspath(str(path)) == os.path.abspath(bad) and "r" in mode:
            return FailingRead(handle)
        return handle

    monkeypatch.setattr(backup_utils, "open", fake_open, raising=False)
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["verified"] is True
    assert len(result["errors"]) == 1 and "c.txt" in result["errors"][0]
    assert "1 file(s) could not be read and were skipped" in result["message"]
    assert seen["half_written"]  # the destination existed mid-copy...
    version = backup_utils.version_path(job, result["version"])
    assert not os.path.exists(os.path.join(version, "Nested", "Deeper", "c.txt"))  # ...and is gone
    paths = [e["path"] for e in manifest_lines(result["manifest"])[1:-1]]
    assert "Nested/Deeper/c.txt" not in paths and len(paths) == 3
    assert backup_utils.is_verified(job, result["version"])


# ---------------------------------------------------------------------------
# Verified-ness and prune gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    b"",                                                          # empty
    b'{"type":"header","version":1}\n{"type":"file","path":"a"}\n',  # no trailer
    b'{"type":"header","version":1}\n{"type":"end","fi',          # truncated trailer
    b"\x00\xff garbage \x13\n\x88",                               # binary junk
    b'{"type":"end","files":1,"bytes":1,"verified":false}\n',     # not verified
    b"x" * 10000 + b'{"type":"end","files":1,"bytes":1,"verified":true}',  # overlong line
])
def test_a_damaged_manifest_is_not_verified(tree, body):
    job, _, _ = tree
    fake_version(job, "20260101-000000", verified=False)
    with open(backup_utils.manifest_path(job, "20260101-000000"), "wb") as handle:
        handle.write(body)
    assert backup_utils.read_manifest_summary(job, "20260101-000000") is None
    assert not backup_utils.is_verified(job, "20260101-000000")


def test_an_orphan_manifest_or_temp_manifest_is_never_a_version(tree):
    job, _, _ = tree
    fake_version(job, "20260101-000000", verified=True)
    os.rmdir(backup_utils.version_path(job, "20260101-000000"))   # orphan manifest
    open(backup_utils.manifest_path(job, "20260102-000000") + ".tmp", "w").close()
    assert backup_utils.version_names(job) == []
    assert not backup_utils.is_verified(job, "20260101-000000")
    assert backup_utils.verified_versions(job) == []


def test_prune_protects_the_newest_verified_version_behind_legacy_ones(tree):
    job, _, _ = tree
    job["retention"] = 1
    fake_version(job, "20260101-000000", verified=True)
    fake_version(job, "20260102-000000", verified=False)
    fake_version(job, "20260103-000000", verified=False)
    removed = backup_utils.prune_versions(job)
    assert removed == ["20260102-000000"]
    assert backup_utils.version_names(job) == ["20260101-000000", "20260103-000000"]
    assert backup_utils.is_verified(job, "20260101-000000")


def test_prune_deletes_a_pruned_versions_manifest_with_it(tree):
    job, _, _ = tree
    job["retention"] = 1
    for name in ("20260101-000000", "20260102-000000", "20260103-000000"):
        fake_version(job, name, verified=True)
    assert backup_utils.prune_versions(job) == ["20260101-000000", "20260102-000000"]
    assert sorted(os.listdir(backup_utils.job_root(job))) == [
        "20260103-000000", "20260103-000000" + backup_utils.MANIFEST_SUFFIX]


def test_legacy_versions_still_prune_by_retention(tree):
    job, _, _ = tree
    job["retention"] = 2
    for name in ("20260101-000000", "20260102-000000", "20260103-000000"):
        fake_version(job, name, verified=False)
    assert backup_utils.prune_versions(job) == ["20260101-000000"]


def test_a_real_run_after_legacy_versions_prunes_them_and_keeps_itself(tree):
    job, _, _ = tree
    job["retention"] = 1
    fake_version(job, "20200101-000000", verified=False)
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert result["pruned"] == ["20200101-000000"]
    assert backup_utils.verified_versions(job) == [result["version"]]


# ---------------------------------------------------------------------------
# Review-gate fixes: same-second runs, read-only prune, Windows attributes
# ---------------------------------------------------------------------------

class _FrozenClock(backup_utils.datetime):
    """Every run starts in the same second."""

    @classmethod
    def now(cls, tz=None):
        return backup_utils.datetime(2026, 11, 2, 12, 0, 0)


def test_a_second_run_in_the_same_second_never_demotes_the_first(tree, monkeypatch):
    job, _, _ = tree
    job["retention"] = 5
    monkeypatch.setattr(backup_utils, "datetime", _FrozenClock)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    first = backup_utils.run_job(job)
    assert first["ok"], first["message"]
    second = backup_utils.run_job(job)
    assert not second["ok"] and "same moment" in second["message"]
    # The first run's version is still verified and nothing else was left.
    assert backup_utils.verified_versions(job) == [first["version"]]
    assert sorted(os.listdir(backup_utils.job_root(job))) == [
        first["version"], first["version"] + backup_utils.MANIFEST_SUFFIX]


def test_back_to_back_runs_get_distinct_verified_versions(tree):
    job, _, _ = tree
    job["retention"] = 5
    first = backup_utils.run_job(job)
    second = backup_utils.run_job(job)
    assert first["ok"] and second["ok"], (first["message"], second["message"])
    assert first["version"] != second["version"]
    assert backup_utils.verified_versions(job) == [first["version"], second["version"]]


def test_a_read_only_file_does_not_stop_retention(tree):
    job, source, _ = tree
    locked = source / "locked.txt"
    locked.write_text("ro", encoding="utf-8")
    os.chmod(locked, 0o444)
    try:
        first = backup_utils.run_job(job)
        time.sleep(1.1)
        second = backup_utils.run_job(job)
    finally:
        os.chmod(locked, 0o666)
    assert first["ok"] and second["ok"]
    assert second["pruned"] == [first["version"]]
    assert backup_utils.version_names(job) == [second["version"]]


@pytest.mark.skipif(os.name != "nt", reason="Windows file attributes")
def test_hidden_and_system_attributes_survive_the_copy(tree):
    import ctypes
    job, source, _ = tree
    hidden = str(source / "a.txt")
    ctypes.windll.kernel32.SetFileAttributesW(hidden, 0x2 | 0x4)  # HIDDEN | SYSTEM
    try:
        result = backup_utils.run_job(job)
        assert result["ok"], result["message"]
        copied = os.path.join(backup_utils.version_path(job, result["version"]), "a.txt")
        assert os.stat(copied).st_file_attributes & 0x6 == 0x6
        # Positive control: an ordinary file is not made hidden.
        plain = os.path.join(backup_utils.version_path(job, result["version"]),
                             "Nested", "Deeper", "c.txt")
        assert os.stat(plain).st_file_attributes & 0x6 == 0
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(hidden, 0x80)
