# tests/test_verify_modes.py
# Card 42: verification is optional per job -- "full" (default), "quick"
# (every size, a sample of hashes) and "off" (no re-read, never verified).
#
# Re-reads are counted by wrapping backup_utils._rehash, the one function the
# verify passes read copied content through. The full-mode test is the
# positive control for that instrument: it must see every file, so a count of
# zero elsewhere means "not read", not "not counted". Expected hashes are
# computed here with hashlib from the source files, never read back from the
# code under test.
import glob
import hashlib
import json
import os
import time

import pytest

from utils import backup_utils, jobs_store, verify

FILES = 60          # 5% of 60 -> a sample of 3
LARGEST = "big/largest.bin"
NEWEST = "newest.txt"
SMALL = "small/f_10.txt"   # neither largest nor newest


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


@pytest.fixture
def rehashed(monkeypatch):
    """Record every path the verify passes re-read the content of."""
    seen = []
    real = backup_utils._rehash

    def counting(path):
        seen.append(os.path.relpath(path, partial_root(path)).replace("\\", "/"))
        return real(path)

    monkeypatch.setattr(verify, "_rehash", counting)
    return seen


def partial_root(path):
    """The `.partial` folder a copied file sits in."""
    while not path.endswith(".partial"):
        path = os.path.dirname(path)
    return path


def make_job(tmp_path, mode, retention=5):
    """A 60-file source with one obvious largest and one obvious newest file."""
    source = tmp_path / "src"
    (source / "small").mkdir(parents=True)
    (source / "big").mkdir()
    for index in range(FILES - 2):
        (source / "small" / f"f_{index}.txt").write_bytes(os.urandom(100 + index))
    (source / "big" / "largest.bin").write_bytes(os.urandom(200_000))
    (source / "newest.txt").write_bytes(b"newest")
    old = time.time() - 3600
    for path in source.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))
    os.utime(source / "newest.txt", (old + 1800, old + 1800))
    target = tmp_path / "dst"
    target.mkdir(exist_ok=True)
    job = backup_utils.new_job("Mode job", str(source), str(target),
                               schedule="manual", retention=retention, verify=mode)
    return job, source


def sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def manifest_lines(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def partial_dir(job):
    found = glob.glob(os.path.join(backup_utils.job_root(job), "*.partial"))
    assert len(found) == 1, found
    return found[0]


def on_verify(job, action):
    """An on_phase hook running `action(partial folder)` as verification starts."""
    def hook(phase):
        if phase == "verifying":
            action(partial_dir(job))
    return hook


def flip_first_byte(rel):
    def action(partial):
        with open(os.path.join(partial, *rel.split("/")), "r+b") as handle:
            first = handle.read(1)
            handle.seek(0)
            handle.write(bytes([first[0] ^ 0xFF]))
    return action


def assert_failed_and_kept(job, result, rel):
    """A failed check: named file, .partial and tmp manifest kept, nothing promoted."""
    assert result["ok"] is False
    assert result["verified"] is False
    assert rel in result["message"]
    assert os.path.isdir(result["partial"])
    assert result["manifest"].endswith(".manifest.jsonl.tmp")
    assert os.path.isfile(result["manifest"])
    assert backup_utils.version_names(job) == []


def write_version(job, name, mode):
    """A version folder with a complete manifest made in `mode` (None: legacy)."""
    os.makedirs(backup_utils.version_path(job, name))
    if mode is None:
        return
    with open(backup_utils.manifest_path(job, name), "w", encoding="utf-8") as h:
        h.write(json.dumps({"type": "header", "version": 1, "stamp": name}) + "\n")
        h.write(json.dumps({"type": "end", "files": 0, "bytes": 0, "verify": mode,
                            "verified": mode != "off"}) + "\n")


# ---------------------------------------------------------------------------
# full: unchanged, and the positive control for the read counter
# ---------------------------------------------------------------------------

def test_full_rereads_every_file(tmp_path, rehashed):
    job, source = make_job(tmp_path, "full")
    phases = []
    result = backup_utils.run_job(job, on_phase=phases.append)
    assert result["ok"], result["message"]
    assert phases == ["copying", "verifying"]
    assert len(rehashed) == FILES and len(set(rehashed)) == FILES
    assert result["verified"] is True and result["verify_mode"] == "full"
    assert result["message"].endswith("and verified every copied file")
    assert backup_utils.verify_mode(job, result["version"]) == "full"


# ---------------------------------------------------------------------------
# off
# ---------------------------------------------------------------------------

def test_off_skips_the_reread_but_writes_hashes_and_an_unverified_manifest(tmp_path, rehashed):
    job, source = make_job(tmp_path, "off")
    phases = []
    result = backup_utils.run_job(job, on_phase=phases.append)

    assert result["ok"], result["message"]
    assert rehashed == []                       # not one file re-read
    assert phases == ["copying"]                # no "verifying" announced
    assert result["verified"] is False and result["verify_mode"] == "off"
    assert "NOT verified: verification is turned off for this job" in result["message"]

    # Promoted: a real version folder, no .partial, manifest in place.
    stamp = result["version"]
    assert backup_utils.version_names(job) == [stamp]
    assert not glob.glob(os.path.join(backup_utils.job_root(job), "*.partial"))
    lines = manifest_lines(backup_utils.manifest_path(job, stamp))
    assert lines[-1] == {"type": "end", "files": FILES, "bytes": result["bytes"],
                         "verify": "off", "verified": False,
                         "finished_utc": result["finished_utc"], "protected": True}
    entries = {e["path"]: e for e in lines[1:-1]}
    assert len(entries) == FILES
    for rel, entry in entries.items():
        assert entry["sha256"] == sha(os.path.join(source, *rel.split("/"))), rel

    assert backup_utils.is_verified(job, stamp) is False
    assert backup_utils.verify_mode(job, stamp) == "off"
    assert backup_utils.read_manifest_summary(job, stamp)["verified"] is False
    assert backup_utils.verified_versions(job) == []
    assert backup_utils.changes_since(job)["verified"] is False
    # Restore still works from an unverified version.
    out = tmp_path / "out"
    out.mkdir()
    assert backup_utils.restore_version(job, stamp, str(out))["files"] == FILES


# ---------------------------------------------------------------------------
# quick
# ---------------------------------------------------------------------------

def test_quick_hashes_only_a_sample_including_largest_and_newest(tmp_path, rehashed):
    job, _ = make_job(tmp_path, "quick")
    phases, progress = [], []
    result = backup_utils.run_job(job, on_phase=phases.append,
                                  on_progress=lambda d, t: progress.append((d, t)))
    assert result["ok"], result["message"]
    assert phases == ["copying", "verifying"]
    # 5% of 60 = 3, within [1, cap], and always the largest and the newest.
    assert len(rehashed) == 3 and len(set(rehashed)) == 3
    assert LARGEST in rehashed and NEWEST in rehashed
    assert result["verified"] is True and result["verify_mode"] == "quick"
    assert (f"and quick-checked 3 of {FILES} file(s) (sizes of all, hashes of a "
            "sample)") in result["message"]
    assert backup_utils.is_verified(job, result["version"])
    assert backup_utils.verify_mode(job, result["version"]) == "quick"
    assert manifest_lines(result["manifest"])[-1]["verify"] == "quick"
    # Progress for the verify pass runs over the sample, ending at 3 of 3.
    assert progress[-1] == (3, 3)


def test_quick_catches_a_corrupted_sampled_file(tmp_path):
    job, _ = make_job(tmp_path, "quick")
    result = backup_utils.run_job(job, on_phase=on_verify(job, flip_first_byte(LARGEST)))
    assert_failed_and_kept(job, result, LARGEST)
    assert result["verify_mode"] == "quick"


@pytest.mark.parametrize("damage", ["missing", "grown"])
def test_quick_catches_a_missing_or_resized_unsampled_file(tmp_path, monkeypatch,
                                                          rehashed, damage):
    # Sample shrunk to exactly {largest, newest}, so SMALL is certainly unsampled.
    monkeypatch.setattr(verify, "QUICK_SAMPLE_FRACTION", 0.0001)
    job, _ = make_job(tmp_path, "quick")

    def action(partial):
        path = os.path.join(partial, *SMALL.split("/"))
        if damage == "missing":
            os.remove(path)
        else:
            with open(path, "ab") as handle:
                handle.write(b"!")

    result = backup_utils.run_job(job, on_phase=on_verify(job, action))
    assert_failed_and_kept(job, result, SMALL)
    assert SMALL not in rehashed


def test_quick_does_not_catch_a_same_size_corruption_outside_the_sample(
        tmp_path, monkeypatch, rehashed):
    # The honest limit of quick mode, pinned so nobody mistakes it for full:
    # a byte flipped (size unchanged) in a file the sample skipped passes.
    monkeypatch.setattr(verify, "QUICK_SAMPLE_FRACTION", 0.0001)
    job, source = make_job(tmp_path, "quick")
    result = backup_utils.run_job(job, on_phase=on_verify(job, flip_first_byte(SMALL)))
    assert result["ok"], result["message"]
    assert sorted(rehashed) == sorted([LARGEST, NEWEST])
    copied = os.path.join(backup_utils.version_path(job, result["version"]),
                          *SMALL.split("/"))
    assert sha(copied) != sha(os.path.join(source, *SMALL.split("/")))


def test_abort_during_quick_verify_removes_partial_and_temp_manifest(tmp_path):
    job, _ = make_job(tmp_path, "quick")
    state = {"verifying": False}
    result = backup_utils.run_job(
        job, on_phase=lambda p: state.update(verifying=p == "verifying"),
        should_abort=lambda: state["verifying"])
    assert result["ok"] is False and result["message"] == "Backup cancelled"
    assert os.listdir(backup_utils.job_root(job)) == []


def test_quick_sample_bounds_and_reproducibility():
    sample = backup_utils._quick_sample
    assert sample(0, 0, 0, "s") == set()
    assert sample(1, 0, 0, "s") == {0}
    assert sample(10, 3, 7, "s") == {3, 7}          # want 1, forced pair kept
    assert len(sample(100, 5, 5, "s")) == 5         # 5%, forced overlap
    big = sample(100_000, 0, 99_999, "20261113-120000")
    assert len(big) == backup_utils.QUICK_SAMPLE_CAP
    assert {0, 99_999} <= big
    assert big == sample(100_000, 0, 99_999, "20261113-120000")
    assert big != sample(100_000, 0, 99_999, "20261113-120001")


# ---------------------------------------------------------------------------
# Reading modes back
# ---------------------------------------------------------------------------

def test_a_legacy_trailer_without_verify_reads_as_full(tmp_path):
    job, _ = make_job(tmp_path, "full")
    os.makedirs(backup_utils.version_path(job, "20260101-000000"))
    with open(backup_utils.manifest_path(job, "20260101-000000"), "w") as h:
        h.write('{"type":"header","version":1}\n'
                '{"type":"end","files":0,"bytes":0,"verified":true}\n')
    assert backup_utils.verify_mode(job, "20260101-000000") == "full"
    assert backup_utils.is_verified(job, "20260101-000000")


@pytest.mark.parametrize("trailer", [
    {"verify": "off", "verified": True},     # off can never be verified
    {"verify": "quick", "verified": False},  # nor can full/quick be unverified
    {"verify": "turbo", "verified": True},   # unknown mode
    {"verify": "off"},                       # no flag at all
])
def test_an_inconsistent_trailer_is_damaged_not_verified(tmp_path, trailer):
    job, _ = make_job(tmp_path, "full")
    os.makedirs(backup_utils.version_path(job, "20260101-000000"))
    with open(backup_utils.manifest_path(job, "20260101-000000"), "w") as h:
        h.write(json.dumps(dict({"type": "end", "files": 0, "bytes": 0}, **trailer)) + "\n")
    assert backup_utils.read_manifest_summary(job, "20260101-000000") is None
    assert backup_utils.verify_mode(job, "20260101-000000") is None
    assert not backup_utils.is_verified(job, "20260101-000000")


def test_no_manifest_has_no_mode(tmp_path):
    job, _ = make_job(tmp_path, "full")
    write_version(job, "20260101-000000", None)
    assert backup_utils.verify_mode(job, "20260101-000000") is None


@pytest.mark.parametrize("stored", ["turbo", None, 7, ""])
def test_an_unknown_or_missing_verify_value_runs_as_full(tmp_path, rehashed, stored):
    job, _ = make_job(tmp_path, "full")
    if stored is None:
        del job["verify"]
    else:
        job["verify"] = stored
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    assert result["verify_mode"] == "full"
    assert len(rehashed) == FILES


def test_new_job_validates_verify():
    assert backup_utils.new_job("n", "s", "t")["verify"] == "full"
    assert backup_utils.new_job("n", "s", "t", verify="quick")["verify"] == "quick"
    assert backup_utils.new_job("n", "s", "t", verify="off")["verify"] == "off"
    assert backup_utils.new_job("n", "s", "t", verify="bogus")["verify"] == "full"


def test_every_mode_keeps_the_existing_result_keys(tmp_path):
    keys = {"ok", "message", "version", "files", "bytes", "pruned", "errors",
            "verified", "manifest", "target_volume"}
    for mode in backup_utils.VERIFY_MODES:
        job, _ = make_job(tmp_path / mode, mode)
        result = backup_utils.run_job(job)
        assert keys <= set(result), mode
        assert result["verify_mode"] == mode
        assert result["verified"] is (mode != "off")


# ---------------------------------------------------------------------------
# Prune: an "off" version is never the protected last-good copy
# ---------------------------------------------------------------------------

def test_off_runs_never_displace_the_last_verified_version(tmp_path):
    job, _ = make_job(tmp_path, "full", retention=1)
    first = backup_utils.run_job(job)
    assert first["ok"], first["message"]
    job["verify"] = "off"
    offs = []
    for _ in range(2):
        time.sleep(1.1)
        result = backup_utils.run_job(job)
        assert result["ok"], result["message"]
        offs.append(result["version"])
        # The verified full version survives every off run at retention 1.
        assert first["version"] in backup_utils.version_names(job)
        assert backup_utils.is_verified(job, first["version"])
    # The first off version was pruned by retention like any other.
    assert backup_utils.version_names(job) == [first["version"], offs[1]]
    assert not os.path.exists(backup_utils.manifest_path(job, offs[0]))


def test_prune_protects_verified_over_newer_off_and_prunes_off_by_retention(tmp_path):
    job, _ = make_job(tmp_path, "full", retention=2)
    write_version(job, "20260101-000000", "full")
    write_version(job, "20260102-000000", "quick")
    write_version(job, "20260103-000000", "off")
    write_version(job, "20260104-000000", "off")
    write_version(job, "20260105-000000", "off")
    removed = backup_utils.prune_versions(job)
    # Newest verified is the quick one; everything else past retention goes.
    assert removed == ["20260101-000000", "20260103-000000"]
    assert backup_utils.version_names(job) == [
        "20260102-000000", "20260104-000000", "20260105-000000"]
