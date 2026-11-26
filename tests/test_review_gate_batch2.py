# tests/test_review_gate_batch2.py
# Gaps the independent review gate found in batch 2 (run lock, sweep, verify
# modes, target write failures, vanished source). Each test here failed
# against the batch as written and passes with the gate's fix:
#
# - a second restore into the same folder aborted on the first read-only or
#   hidden file the first restore had put there
# - a full drive that refused a MANIFEST line was reported as a bare errno,
#   with no `cause`
# - an unplugged backup drive was told "the incomplete copy was removed"
# - a wrong drive that was also short of space was swept before the
#   drive-identity guard had run
# - the headless run died with a traceback when the jobs-file lock timed out
#
# Faults are injected at the real call underneath the code under test
# (`backup_utils.open`, a real junction, a real held lock), never by patching
# the function being tested. Hashes are computed here, independently.
import errno
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import app_data_dir
from utils import backup_utils, copying, jobs_store, locks, preflight, runner
from utils.fs_utils import plain_path

REPO = Path(__file__).resolve().parent.parent
FILES = 30
OLD1, OLD2 = "20200101-000000", "20200102-000000"


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep every test off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture
def tree(tmp_path):
    """30 source files (20 at the top, 10 in `sub`), retention 1."""
    source = tmp_path / "src"
    (source / "sub").mkdir(parents=True)
    for i in range(FILES):
        folder = source if i < 20 else source / "sub"
        (folder / f"file{i:02d}.bin").write_bytes(os.urandom(2000 + i))
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Gate job", str(source), str(target),
                               schedule="manual", retention=1)
    return job, source, target


def tree_hashes(root) -> dict:
    """{relative path: sha256} for every file under `root`."""
    found = {}
    for dirpath, _, names in os.walk(root):
        for name in names:
            full = os.path.join(dirpath, name)
            with open(full, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            found[os.path.relpath(full, root).replace("\\", "/")] = digest
    return found


def set_attributes(path, attributes: int) -> None:
    """Set Windows file attributes (hidden 0x2, system 0x4, normal 0x80)."""
    import ctypes
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes)


# ---------------------------------------------------------------------------
# Restore twice into the same folder
# ---------------------------------------------------------------------------

def test_a_second_restore_with_read_only_and_hidden_files_keeps_the_first_and_succeeds(tree, tmp_path):
    """Windows refuses to truncate a read-only file (every git repo has them)
    or a hidden one (desktop.ini). Through `_hash_copy` that refusal was a
    destination failure, so the whole second restore aborted with "No
    permission to write to the destination drive". Since card 5AJTFrRx the
    second restore does not write into the first folder at all."""
    job, source, _ = tree
    os.chmod(source / "file00.bin", 0o444)
    if os.name == "nt":
        set_attributes(source / "file01.bin", 0x2 | 0x4)
    version = backup_utils.run_job(job)["version"]
    out = tmp_path / "out"
    out.mkdir()
    first = backup_utils.restore_version(job, version, str(out))
    assert first["ok"], first["message"]

    # Change both restored copies, so "ok" can only mean they were rewritten,
    # then put the awkward attributes back.
    for name in ("file00.bin", "file01.bin"):
        copy = os.path.join(first["path"], name)
        if os.name == "nt":
            set_attributes(copy, 0x80)
        os.chmod(copy, 0o666)
        with open(copy, "wb") as handle:
            handle.write(b"edited since the first restore")
    os.chmod(os.path.join(first["path"], "file00.bin"), 0o444)
    if os.name == "nt":
        set_attributes(os.path.join(first["path"], "file01.bin"), 0x2 | 0x4)

    again = backup_utils.restore_version(job, version, str(out))

    # Card 5AJTFrRx: the second restore no longer rewrites the first folder (the
    # edits above survive); it lands in a folder of its own, with the source's
    # attributes on its copies.
    assert again["ok"], again["message"]
    assert again["path"] == first["path"] + "-2"
    assert again["files"] == FILES and again["errors"] == []
    assert tree_hashes(again["path"]) == tree_hashes(source)
    for name in ("file00.bin", "file01.bin"):
        with open(os.path.join(first["path"], name), "rb") as handle:
            assert handle.read() == b"edited since the first restore"
    assert not os.access(os.path.join(again["path"], "file00.bin"), os.W_OK)
    if os.name == "nt":
        attributes = os.stat(os.path.join(again["path"], "file01.bin")).st_file_attributes
        assert attributes & 0x6 == 0x6


def test_a_copy_over_a_read_only_hidden_file_still_works_for_the_backup_side(tmp_path):
    """`copying._open_for_write` no longer serves restore (a restore folder is
    always new), but the backup's re-copy of a file that changed while it was
    read does overwrite its own first copy, which `copystat` made read-only and
    hidden. That path is still needed, so it is still covered."""
    from utils import copying
    src, dst = tmp_path / "src.bin", tmp_path / "dst.bin"
    src.write_bytes(b"new bytes")
    dst.write_bytes(b"old bytes")
    os.chmod(dst, 0o444)
    if os.name == "nt":
        set_attributes(dst, 0x1 | 0x2 | 0x4)
    copying._hash_copy(str(src), str(dst))
    assert dst.read_bytes() == b"new bytes"


# ---------------------------------------------------------------------------
# A full drive that refuses the manifest instead of a data file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("at", ["open", "write"])
def test_a_full_drive_hitting_the_manifest_fails_with_the_same_cause(
        tree, monkeypatch, at):
    """Which write a full drive refuses first is chance. Refusing a manifest
    line must read the same as refusing a data file: a `cause`, the plain
    words, and the "nothing was saved or pruned" line -- not a bare errno."""
    job, source, _ = tree
    first = backup_utils.run_job(job)
    assert first["ok"] and first["files"] == FILES
    first_hashes = tree_hashes(backup_utils.version_path(job, first["version"]))
    real_open = open
    error = OSError(errno.ENOSPC, "No space left on device")

    class FullManifest:
        """The real manifest handle; its 4th write fails."""

        def __init__(self, handle):
            self.handle, self.writes = handle, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def write(self, data):
            self.writes += 1
            if self.writes > 3:
                raise error
            return self.handle.write(data)

        def flush(self):
            return self.handle.flush()

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "w" and str(path).endswith(".manifest.jsonl.tmp"):
            if at == "open":
                raise error
            return FullManifest(real_open(path, mode, *args, **kwargs))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(runner, "open", fake_open, raising=False)
    time.sleep(1.1)  # a distinct version stamp
    result = backup_utils.run_job(job)

    assert result["ok"] is False, result["message"]
    assert result.get("cause") == "no_space", result["message"]
    assert "The backup drive is full" in result["message"]
    assert "Nothing was saved or pruned" in result["message"]
    # Nothing promoted, pruned or left behind; the complete version is intact.
    stamp = first["version"]
    assert sorted(os.listdir(backup_utils.job_root(job))) == sorted(
        [stamp, stamp + backup_utils.MANIFEST_SUFFIX])
    assert backup_utils.is_verified(job, stamp)
    assert tree_hashes(backup_utils.version_path(job, stamp)) == first_hashes


# ---------------------------------------------------------------------------
# An unplugged backup drive
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_an_unplugged_target_does_not_claim_the_copy_was_removed(tree, tmp_path):
    """When the backup drive goes away mid-copy the half-written copy cannot
    be deleted -- it is still on the drive -- so the message must not say it
    was. The "drive" is a junction removed between two files."""
    import _winapi
    job, _, _ = tree
    drive = tmp_path / "drive"
    drive.mkdir()
    mount = tmp_path / "mount"
    try:
        _winapi.CreateJunction(str(drive), str(mount))
    except OSError:
        pytest.skip("could not create a junction here")
    job["target"] = str(mount)

    def unplug(done, _total):
        """Remove the junction only; the "drive" keeps its files."""
        if done == 3 and os.path.lexists(mount):
            os.rmdir(mount)

    result = backup_utils.run_job(job, on_progress=unplug)

    assert result["ok"] is False and result["cause"] == "target_unavailable"
    left = sorted(os.listdir(drive / job["id"]))
    assert len(left) == 2 and left[1].endswith(".partial"), left  # it IS still there
    assert "was removed" not in result["message"], result["message"]
    assert "could not be removed" in result["message"]


# ---------------------------------------------------------------------------
# A run that copies nothing
# ---------------------------------------------------------------------------

def test_a_run_that_copies_nothing_never_becomes_the_newest_backup(tmp_path, monkeypatch):
    """A two-file source whose files are both locked (an open mail store and
    its lock file) is under the 3-file shortfall floor, so the run was "ok"
    with 0 files, counted as verified, and pruned the last real copy."""
    source = tmp_path / "src"
    source.mkdir()
    for name in ("mail.pst", "mail.lock"):
        (source / name).write_bytes(os.urandom(3000))
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("tiny", str(source), str(target),
                               schedule="manual", retention=1)
    first = backup_utils.run_job(job)
    assert first["ok"] and first["files"] == 2
    stamp = first["version"]
    before = tree_hashes(backup_utils.version_path(job, stamp))
    assert before == tree_hashes(source)  # positive control: a complete copy
    real_open = open
    source_root = os.path.normcase(str(source))

    def locked(path, mode="r", *args, **kwargs):
        """Every source file is held open by another program."""
        if mode == "rb" and os.path.normcase(plain_path(str(path))).startswith(source_root):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", locked, raising=False)
    time.sleep(1.1)  # a distinct version stamp
    result = backup_utils.run_job(job)

    assert result["ok"] is False, result["message"]
    assert result["cause"] == "source_incomplete"
    assert "0 of 2 file(s)" in result["message"]
    assert "pruned" not in result
    assert sorted(os.listdir(backup_utils.job_root(job))) == sorted(
        [stamp, stamp + backup_utils.MANIFEST_SUFFIX])
    assert backup_utils.is_verified(job, stamp)
    assert tree_hashes(backup_utils.version_path(job, stamp)) == before


# ---------------------------------------------------------------------------
# Sweep versus the drive-identity guard
# ---------------------------------------------------------------------------

def test_a_full_wrong_drive_is_reported_as_the_wrong_drive_and_not_swept(
        tree, monkeypatch):
    """A different drive mounted at the target that is also short of space
    was reported as "not enough free space" and swept, before the
    drive-identity guard had run. Leftovers on a drive this job does not
    recognise are not this run's to delete."""
    job, _, target = tree
    root = target / job["id"]
    for stamp in (OLD1, OLD2):
        (root / f"{stamp}.partial").mkdir(parents=True)
        (root / f"{stamp}.partial" / "data.bin").write_text("x", encoding="utf-8")
    job["target_volume"] = "some-other-drive"
    monkeypatch.setattr(preflight, "volume_id", lambda p: "this-drive")
    monkeypatch.setattr(preflight, "_free_bytes", lambda p: 0)

    # Positive control: on the RIGHT drive this very state is swept.
    control = dict(job, target_volume="this-drive")
    assert backup_utils.preflight(control).startswith(backup_utils.LOW_SPACE_PREFIX)

    result = backup_utils.run_job(job)

    assert result["ok"] is False and "not the same drive" in result["message"]
    assert "swept" not in result
    assert sorted(p.name for p in root.iterdir()) == [f"{OLD1}.partial",
                                                      f"{OLD2}.partial"]
    swept = backup_utils.run_job(control)
    assert swept["swept"] == [f"{OLD1}.partial"]  # the sweep could have deleted


# ---------------------------------------------------------------------------
# Headless run: a result that cannot be recorded
# ---------------------------------------------------------------------------

def test_cli_reports_a_result_it_could_not_record_instead_of_a_traceback(tmp_path):
    """`record_run` raises OSError when the jobs-file lock cannot be taken
    (documented, and handled in the GUI). The headless path caught only
    JobsFileCorrupt, so the scheduled task died with a traceback and never
    printed the run's own outcome."""
    appdata = tmp_path / "appdata"
    data = app_data_dir(appdata)
    data.mkdir(parents=True)
    job = backup_utils.new_job("j", str(tmp_path / "nosuch-src"), str(tmp_path / "dst"),
                               schedule="manual")
    jobs_file = data / "backup_jobs.json"
    jobs_file.write_text(json.dumps([job]), encoding="utf-8")
    before = jobs_file.read_bytes()
    lock_dir = data / "locks"
    lock_dir.mkdir()
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")
    # Another Safekeep process is "stuck" holding the jobs-file lock.
    with locks.acquire(str(lock_dir / "jobs-file.lock"), "test holder"):
        proc = subprocess.run([sys.executable, str(REPO / "main.py"),
                               "--run-backup-job", job["id"]],
                              cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=120)

    assert "Traceback" not in proc.stderr, proc.stderr
    assert proc.returncode == 1, (proc.stdout, proc.stderr)  # the run itself failed
    assert "Source folder not found" in proc.stderr          # its outcome is still said
    assert "could not be recorded" in proc.stderr
    assert jobs_file.read_bytes() == before
