# tests/test_changed_during_copy.py
# Card RnuRsA2V: a file that changed while it was being copied is detected,
# copied once more, and, if it changes again, kept and flagged "changed during
# backup" instead of being presented as verified.
#
# The deterministic faults go through the one hook `copying._after_read`, which
# runs between the last read of the source and the post-copy look at it: it
# rewrites the file at exactly the moment the check is for (patched on the
# module that owns it, never on the facade). One test uses a REAL concurrent
# writer thread on a 64 MB file instead; that one depends on the writer being
# scheduled while the copy runs, so it asserts only what is guaranteed.
import hashlib
import json
import os
import threading

import pytest

from helpers import clear_ro
from utils import backup_utils, copying, jobs_store, runlog
from utils.fs_utils import long_path

windows_only = pytest.mark.skipif(os.name != "nt", reason="needs Windows path rules")

TOP = [f"a{i:02d}.txt" for i in range(6)]
SUB = ["b0.txt", "b1.txt"]
TOTAL = len(TOP) + len(SUB)
VICTIM = "a03.txt"          # the file most tests change under the copy


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


def make_source(root):
    """Eight small files with distinct bytes: six at the root, two in `sub/`."""
    (root / "sub").mkdir(parents=True)
    for name in TOP:
        (root / name).write_bytes(f"contents of {name}".encode() * 50)
    for name in SUB:
        (root / "sub" / name).write_bytes(f"deep contents of {name}".encode() * 50)
    return root


def make_job(tmp_path, **options):
    """A saved job over `make_source`, retention 3, target empty."""
    source = make_source(tmp_path / "src")
    target = tmp_path / "dst"
    target.mkdir()
    job = backup_utils.new_job("Changing", str(source), str(target), schedule="manual",
                               retention=3, **options)
    backup_utils.save_job(job)
    return job


@pytest.fixture
def job(tmp_path):
    return make_job(tmp_path)


# --- ways of changing a file --------------------------------------------------------

def _bump_mtime(path, seconds=2):
    """Move `path`'s modified time forward without touching its bytes."""
    info = os.stat(long_path(path))
    os.utime(long_path(path), ns=(info.st_atime_ns, info.st_mtime_ns + seconds * 10 ** 9))


def change_size(path):
    """One byte longer, and (as any real write does) a new mtime."""
    with open(long_path(path), "ab") as handle:
        handle.write(b"+")


def change_mtime_only(path):
    """Same size, same bytes, later mtime: `touch`."""
    _bump_mtime(path)


def change_size_keeping_mtime(path):
    """One byte longer, but the modified time put back (a tool that preserves it):
    only the size gives it away."""
    info = os.stat(long_path(path))
    with open(long_path(path), "ab") as handle:
        handle.write(b"+")
    os.utime(long_path(path), ns=(info.st_atime_ns, info.st_mtime_ns))


def change_mtime_one_tick(path):
    """Same size, same bytes, mtime one NTFS tick (100 ns) later. The float
    `st_mtime` cannot tell this from the original; `st_mtime_ns` can."""
    info = os.stat(long_path(path))
    os.utime(long_path(path), ns=(info.st_atime_ns, info.st_mtime_ns + 100))


def change_content_same_size(path):
    """Same size, different bytes, later mtime: an in-place rewrite."""
    with open(long_path(path), "r+b") as handle:
        first = handle.read(1)
        handle.seek(0)
        handle.write(b"Z" if first != b"Z" else b"Y")
    _bump_mtime(path)


def change_by_rename(path):
    """Replace the file by renaming a same-size, different-mtime file over it
    (an editor's save): the open-handle view and the path now name different files."""
    temp = path + ".swap"
    with open(long_path(path), "rb") as handle:
        data = handle.read()
    with open(long_path(temp), "wb") as handle:
        handle.write(data[::-1])
    _bump_mtime(temp)
    os.replace(long_path(temp), long_path(path))


CHANGES = {"size": change_size, "mtime": change_mtime_only, "tick": change_mtime_one_tick,
           "size_keep_mtime": change_size_keeping_mtime,
           "content": change_content_same_size, "rename": change_by_rename}


class Meddler:
    """Change files at the moment the check is for.

    `plan` maps a file name to how many of ITS copy attempts get changed
    (1: only the first; 99: every one). Records every `_hash_copy` attempt.
    """

    def __init__(self, monkeypatch, plan, how=change_size, after=None):
        self.attempts = {}
        self.hooked = {}
        real = copying._hash_copy

        def watched(src, dst):
            name = os.path.basename(src)
            self.attempts[name] = self.attempts.get(name, 0) + 1
            return real(src, dst)

        def hook(src):
            name = os.path.basename(src)
            self.hooked[name] = self.hooked.get(name, 0) + 1
            if self.hooked[name] <= plan.get(name, 0):
                how(src)
                if after is not None:
                    after(name, self.hooked[name])

        monkeypatch.setattr(copying, "_hash_copy", watched)
        monkeypatch.setattr(copying, "_after_read", hook)

    @property
    def total_attempts(self):
        return sum(self.attempts.values())


def run_and_record(job, **kwargs):
    result = backup_utils.run_job(job, **kwargs)
    backup_utils.record_run(job["id"], result)
    return result, backup_utils.get_job(job["id"])


def sha_of(data: bytes) -> str:
    """Independent SHA-256, not computed by the code under test."""
    return hashlib.sha256(data).hexdigest()


def manifest_lines(result):
    with open(result["manifest"], encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def manifest_files(result):
    """`{path: entry}`; asserts no path has two lines."""
    entries = [e for e in manifest_lines(result) if e["type"] == "file"]
    paths = [e["path"] for e in entries]
    assert len(paths) == len(set(paths)), "a file has two manifest lines"
    return {e["path"]: e for e in entries}


def stored_bytes(job, result, rel):
    """The bytes of `rel` inside the promoted version."""
    path = os.path.join(backup_utils.version_path(job, result["version"]), *rel.split("/"))
    with open(long_path(path), "rb") as handle:
        return handle.read()


def source_bytes(job, rel):
    with open(long_path(os.path.join(job["source"], *rel.split("/"))), "rb") as handle:
        return handle.read()


def run_log(job):
    """The job's run-log lines, oldest first."""
    return list(reversed(runlog.read_runs(job["id"])))


# --- the Done-when ---------------------------------------------------------------------

def test_a_file_changed_between_the_read_and_the_check_is_copied_again_and_ends_clean(job, monkeypatch):
    meddler = Meddler(monkeypatch, {VICTIM: 1})              # changed on its first attempt only
    result, stored = run_and_record(job)

    assert result["ok"], result["message"]
    assert meddler.attempts[VICTIM] == 2                      # retried, once
    assert meddler.total_attempts == TOTAL + 1                # nothing else was copied twice
    assert result["recopied"] == 1 and result["changed"] == 0 and result["changed_sample"] == []
    assert "copied again" in result["message"] and "changed during backup" not in result["message"]
    # The version holds the file as it is NOW (the second copy), once.
    assert stored_bytes(job, result, VICTIM) == source_bytes(job, VICTIM)
    entry = manifest_files(result)[VICTIM]
    assert "changed" not in entry                             # clean on the retry: an ordinary file
    assert entry["sha256"] == sha_of(source_bytes(job, VICTIM))
    assert entry["size"] == len(source_bytes(job, VICTIM))
    assert stored["last_changed"] == 0 and stored["last_changed_sample"] == []
    line = run_log(job)[-1]
    assert (line["outcome"], line["recopied"], line["changed"]) == ("ok", 1, 0)


def test_a_file_that_changes_on_the_retry_too_is_named_as_changed_during_the_backup(job, monkeypatch):
    meddler = Meddler(monkeypatch, {VICTIM: 99})              # changed on every attempt
    result, stored = run_and_record(job)

    assert result["ok"], result["message"]
    assert meddler.attempts[VICTIM] == 2                      # ONE extra attempt, never a loop
    assert result["changed"] == 1 and result["changed_sample"] == [VICTIM]
    assert result["recopied"] == 0
    assert "changed during backup" in result["message"]
    # Kept in the version, flagged in its manifest line, counted as a copied file.
    assert result["files"] == TOTAL
    entry = manifest_files(result)[VICTIM]
    assert entry["changed"] is True
    assert entry["sha256"] == sha_of(stored_bytes(job, result, VICTIM))   # the bytes that were read
    assert [e for e in manifest_files(result).values() if "changed" in e] == [entry]
    # Not a plain ok anywhere it is stored: the job carries the flag and its names ...
    assert stored["last_result"] == "ok_changed"              # the real outcome word (card C5HuzNVK)
    assert stored["last_changed"] == 1 and stored["last_changed_sample"] == [VICTIM]
    # ... and the run log says ok_changed, with counts and no path.
    line = run_log(job)[-1]
    assert (line["outcome"], line["changed"], line["recopied"]) == ("ok_changed", 1, 0)
    assert VICTIM not in json.dumps(line)                     # counts only: no file name


def test_the_verify_pass_still_passes_a_flagged_file_but_the_run_carries_the_flag(job, monkeypatch):
    Meddler(monkeypatch, {VICTIM: 99})
    result = backup_utils.run_job(job)

    assert result["verified"] is True and result["verify_mode"] == "full"
    assert "verified every copied file" in result["message"]   # what was read WAS verified ...
    assert result["changed"] == 1                              # ... and the flag says what that is worth
    trailer = manifest_lines(result)[-1]
    assert trailer["type"] == "end" and trailer["files"] == TOTAL and trailer["verified"] is True


@pytest.mark.parametrize("how", sorted(CHANGES))
def test_every_kind_of_change_is_detected(job, monkeypatch, how):
    # Size only, mtime only (same bytes), same-size rewrite, and a file replaced
    # by rename: each one retried once, then flagged when it keeps happening.
    meddler = Meddler(monkeypatch, {VICTIM: 99}, how=CHANGES[how])
    result = backup_utils.run_job(job)

    assert result["ok"] and result["changed"] == 1, (how, result["message"])
    assert meddler.attempts[VICTIM] == 2
    assert manifest_files(result)[VICTIM]["changed"] is True


@pytest.mark.parametrize("how", sorted(CHANGES))
def test_every_kind_of_change_that_settles_on_the_retry_is_an_ordinary_file(job, monkeypatch, how):
    Meddler(monkeypatch, {VICTIM: 1}, how=CHANGES[how])
    result = backup_utils.run_job(job)

    assert result["ok"] and result["recopied"] == 1 and result["changed"] == 0, how
    assert "changed" not in manifest_files(result)[VICTIM]
    assert stored_bytes(job, result, VICTIM) == source_bytes(job, VICTIM)


def test_a_stable_file_is_never_flagged_and_the_same_path_can_say_both(tmp_path, monkeypatch):
    src, dst = tmp_path / "f.bin", tmp_path / "out.bin"
    src.write_bytes(b"x" * 5000)

    clean = copying._hash_copy(str(src), str(dst))             # positive control: stable
    size, mtime, sha = clean                                   # still the three values it always was
    assert clean.changed is False and size == 5000 and sha == sha_of(b"x" * 5000)

    for how in CHANGES.values():                               # the very same code path, now changed
        monkeypatch.setattr(copying, "_after_read", lambda p, how=how: how(p))
        moved = copying._hash_copy(str(src), str(dst))
        assert moved.changed is True
        monkeypatch.undo()

    # A whole run over files nothing touches flags nothing and copies each file once.
    job = make_job(tmp_path / "j")
    meddler = Meddler(monkeypatch, {})
    result = backup_utils.run_job(job)
    assert result["ok"] and result["changed"] == 0 and result["recopied"] == 0
    assert meddler.total_attempts == TOTAL
    assert all("changed" not in e for e in manifest_files(result).values())


def test_a_plain_tuple_from_a_stand_in_reads_as_not_changed(job, monkeypatch):
    # Tests (and anything else) that replace `_hash_copy` with a three-tuple keep working.
    real = copying._hash_copy
    monkeypatch.setattr(copying, "_hash_copy", lambda s, d: tuple(real(s, d)))
    monkeypatch.setattr(copying, "_after_read", change_size)    # would flag every file
    result = backup_utils.run_job(job)
    assert result["ok"] and result["changed"] == 0 and result["recopied"] == 0


# --- counts: one line, one count per file ---------------------------------------------

def test_a_recopied_file_has_one_manifest_line_and_is_counted_once(job, monkeypatch):
    Meddler(monkeypatch, {VICTIM: 1, "b1.txt": 99})
    seen = []
    result = backup_utils.run_job(job, on_progress=lambda done, total: seen.append((done, total)))

    assert result["ok"] and result["files"] == TOTAL
    files = manifest_files(result)                              # asserts no duplicate line
    assert len(files) == TOTAL
    trailer = manifest_lines(result)[-1]
    assert trailer["files"] == TOTAL
    # The bytes of the files as stored (each file's LAST copy, once), not twice.
    on_disk = sum(len(stored_bytes(job, result, rel)) for rel in files)
    assert result["bytes"] == trailer["bytes"] == on_disk == sum(e["size"] for e in files.values())
    assert files[VICTIM]["size"] == len(source_bytes(job, VICTIM))   # settled: matches the source now
    assert seen[:TOTAL] == [(i, TOTAL) for i in range(1, TOTAL + 1)]   # one progress step per file


# --- the one-extra-attempt cap ---------------------------------------------------------

def test_a_file_recovered_by_the_unreadable_retry_is_not_copied_a_third_time(job, monkeypatch):
    # Unreadable on its first open, readable on the retry, and then changing:
    # the retry WAS its one extra attempt, so it is flagged straight away.
    real_open, opens = open, {"n": 0}

    def flaky(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) == VICTIM:
            opens["n"] += 1
            if opens["n"] == 1:
                raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", flaky, raising=False)
    meddler = Meddler(monkeypatch, {VICTIM: 99})
    result = backup_utils.run_job(job)

    assert result["ok"] and result["retried"] == 1
    assert meddler.attempts[VICTIM] == 2                        # failed open + one real copy: the copy ran ONCE
    assert opens["n"] == 2                                      # no third open
    assert result["changed"] == 1 and result["recopied"] == 0
    assert manifest_files(result)[VICTIM]["changed"] is True
    assert result["errors"] == [] and result["not_copied"] == 0


def test_a_recopy_that_cannot_be_read_is_skipped_and_not_retried_again(job, monkeypatch):
    # First attempt changes; the re-copy then cannot open the file. That file's
    # one extra attempt is spent, so the unreadable-file retry leaves it alone.
    real_open, opens = open, {"n": 0}

    def flaky(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) == VICTIM:
            opens["n"] += 1
            if opens["n"] >= 2:
                raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", flaky, raising=False)
    Meddler(monkeypatch, {VICTIM: 1})
    result = backup_utils.run_job(job)

    assert result["ok"] and opens["n"] == 2                     # no retry-pass open
    assert result["not_copied"] == 1 and len(result["errors"]) == 1
    assert result["errors"][0].startswith(VICTIM)
    assert VICTIM not in manifest_files(result)                 # not in the version, not in the manifest
    assert not os.path.exists(os.path.join(backup_utils.version_path(job, result["version"]), VICTIM))
    assert result["changed"] == 0 and result["files"] == TOTAL - 1


def test_a_file_that_vanishes_before_the_post_copy_look_is_a_skip(job, monkeypatch):
    # The stat after the copy fails: handled as any other unreadable source file.
    def vanish(src):
        if os.path.basename(src) == VICTIM and os.path.exists(long_path(src)):
            os.remove(long_path(src))

    monkeypatch.setattr(copying, "_after_read", vanish)
    result = backup_utils.run_job(job)

    assert result["ok"] and result["not_copied"] == 1 and result["changed"] == 0
    assert result["errors"][0].startswith(VICTIM)
    assert VICTIM not in manifest_files(result) and result["files"] == TOTAL - 1


# --- cancel and target failure during the re-copy --------------------------------------

def test_cancelling_during_the_recopy_ends_the_run_cancelled(job, monkeypatch):
    cancel = {"now": False}
    meddler = Meddler(monkeypatch, {VICTIM: 1}, after=lambda name, n: cancel.update(now=True))
    result, stored = run_and_record(job, should_abort=lambda: cancel["now"])

    assert result["ok"] is False and result["message"] == "Backup cancelled"
    assert meddler.attempts[VICTIM] == 1                        # cancelled BEFORE the re-copy began
    root = backup_utils.job_root(job)
    assert not [n for n in os.listdir(root) if n.endswith((".partial", ".tmp"))]
    assert backup_utils.latest_version(job) is None             # nothing was promoted
    assert stored["last_changed"] == 0


def test_a_target_write_failure_during_the_recopy_fails_the_run_as_on_the_first_pass(job, monkeypatch):
    real = copying._hash_copy
    calls = {"victim": 0}

    def full_on_the_recopy(src, dst):
        if os.path.basename(src) == VICTIM:
            calls["victim"] += 1
            if calls["victim"] == 2:
                raise copying.TargetWriteFailed(dst, OSError(28, "No space left on device"))
        return real(src, dst)

    monkeypatch.setattr(copying, "_hash_copy", full_on_the_recopy)
    monkeypatch.setattr(copying, "_after_read", lambda src: change_size(src)
                        if os.path.basename(src) == VICTIM else None)
    result = backup_utils.run_job(job)

    assert result["ok"] is False and result["cause"] == "no_space"
    assert backup_utils.latest_version(job) is None
    root = backup_utils.job_root(job)
    assert not [n for n in os.listdir(root) if n.endswith((".partial", ".tmp"))]


# --- the stored status: combined with skipped, capped, relative --------------------------

def lock_files(monkeypatch, names):
    """Make opening a source file called one of `names` fail for reading."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) in names:
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)


def test_skipped_and_changed_in_one_run_are_both_stored_logged_and_said(job, monkeypatch):
    lock_files(monkeypatch, {"a01.txt"})
    Meddler(monkeypatch, {VICTIM: 99})
    result, stored = run_and_record(job)

    assert result["ok"] and result["not_copied"] == 1 and result["changed"] == 1
    assert "could not be read and were skipped" in result["message"]
    assert "changed during backup" in result["message"]
    assert stored["last_result"] == "ok_skipped"              # skipped outranks changed (card C5HuzNVK)
    assert stored["last_skipped"] == 1 and stored["last_changed"] == 1
    assert stored["last_skipped_sample"] == ["a01.txt"] and stored["last_changed_sample"] == [VICTIM]
    line = run_log(job)[-1]
    assert (line["outcome"], line["skipped"], line["changed"]) == ("ok_skipped", 1, 1)

    pytest.importorskip("PySide6")
    from tabs.backup_tab import JobRow
    assert JobRow._status(None, stored) == (
        "OK, 1 skipped, 1 changed during backup", "StatusWarn")
    detail = JobRow._warning_detail(stored)
    assert "1 file(s) could not be read" in detail and "may mix old and new parts" in detail


def test_the_stored_sample_is_capped_at_five_relative_paths(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    plan = {name: 99 for name in TOP[:4] + SUB}                 # six files, one in each folder
    Meddler(monkeypatch, plan)
    result, stored = run_and_record(job)

    assert result["changed"] == 6 and stored["last_changed"] == 6
    assert len(stored["last_changed_sample"]) == jobs_store.SKIPPED_SAMPLE_MAX == 5
    assert len(result["changed_sample"]) == 5
    for name in stored["last_changed_sample"]:
        assert not os.path.isabs(name) and ":" not in name and "\\" not in name
        assert name in TOP or name.startswith("sub/")
    # The sample field holds names only. (Not the whole jobs file: it also stores the
    # job's own source and target, which sit under tmp_path -- and on Windows the file
    # escapes every backslash, so a raw `str(tmp_path) in file text` never matched.)
    assert not any(str(tmp_path) in name
                   for name in stored["last_changed_sample"] + result["changed_sample"])


def test_a_failed_run_stores_no_changed_files(job, monkeypatch):
    stored = backup_utils.get_job(job["id"])
    stored["last_changed"], stored["last_changed_sample"] = 3, ["x"]
    backup_utils.save_job(stored)
    backup_utils.record_run(job["id"], {"ok": False, "message": "boom", "changed": 2,
                                        "changed_sample": ["a"]})
    after = backup_utils.get_job(job["id"])
    assert after["last_changed"] == 0 and after["last_changed_sample"] == []


@pytest.mark.parametrize("junk", ["3", -2, None, True, 1.5, [1]])
def test_a_hand_edited_changed_count_never_crashes_the_reader(junk):
    assert backup_utils.job_changed({"last_changed": junk}) == 0
    assert backup_utils.job_changed_sample({"last_changed_sample": junk}) == []


def test_an_old_jobs_file_without_the_fields_reads_as_zero():
    old = {"id": "abc12345", "name": "Old", "last_run": "2026-01-01T00:00:00", "last_result": "ok"}
    assert backup_utils.job_changed(old) == 0 and backup_utils.job_changed_sample(old) == []
    pytest.importorskip("PySide6")
    from tabs.backup_tab import JobRow
    assert JobRow._status(None, old) == ("OK", "StatusOk")      # positive control: plain OK stays plain


def test_the_run_log_line_carries_counts_only(job, monkeypatch):
    Meddler(monkeypatch, {VICTIM: 99, "a00.txt": 1})
    backup_utils.run_job(job)
    line = run_log(job)[-1]

    assert set(line) == set(runlog.LINE_KEYS)
    assert line["changed"] == 1 and line["recopied"] == 1
    assert isinstance(line["changed"], int) and isinstance(line["recopied"], int)
    assert "ok_changed" in runlog.OUTCOMES and line["outcome"] == "ok_changed"
    assert VICTIM not in json.dumps(line)                       # no path anywhere in the line


# --- the screen and the headless run ---------------------------------------------------

def status_label(row):
    """The Status cell of a JobRow: the label with a Status* object name."""
    from PySide6 import QtWidgets
    found = [label for label in row.findChildren(QtWidgets.QLabel)
             if label.objectName().startswith("Status")]
    assert len(found) == 1, [(l.objectName(), l.text()) for l in found]
    return found[0]


@pytest.fixture
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_status_cell_says_changed_during_backup_in_the_warning_style(qt_app, job, monkeypatch):
    from tabs.backup_tab import JobRow
    plain = status_label(JobRow(backup_utils.get_job(job["id"]) | {
        "last_run": "2026-11-20T02:00:00", "last_result": "ok"}))
    assert (plain.objectName(), plain.text(), plain.toolTip()) == ("StatusOk", "OK", "")   # control

    Meddler(monkeypatch, {VICTIM: 99, "a00.txt": 99, "a01.txt": 99, "a02.txt": 99,
                          "a04.txt": 99, "a05.txt": 99})
    _, stored = run_and_record(job)
    label = status_label(JobRow(stored))

    assert label.objectName() == "StatusWarn"                   # the skipped warning's object name
    assert label.text() == "OK, 6 changed during backup"
    tip = label.toolTip()
    assert ("These files were being changed while they were backed up, so the copy may "
            "mix old and new parts. Close the program using them and back up again.") in tip
    listed = [line for line in tip.splitlines()[1:]]
    assert len(listed) == 5 + 1 and listed[-1] == "and 1 more"   # five names, then the rest
    assert label.accessibleDescription() == tip


def test_the_tab_reads_a_changed_run_as_a_warning(qt_app, job, monkeypatch):
    from utils import scheduler
    from tabs import backup_tab
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))
    tab = backup_tab.BackupTab()
    result = {"ok": True, "message": "Backed up 8 file(s) and verified", "changed": 1,
              "changed_sample": [VICTIM], "files": 8, "version": "20261120-020000"}
    tab._finish_run(job, result)
    assert tab.backup_status.objectName() == "StatusWarn"
    tab._finish_run(job, dict(result, changed=0, changed_sample=[]))
    assert tab.backup_status.objectName() == "Hint"             # positive control: no flag, plain news


def test_the_headless_run_warns_on_stderr_and_exits_zero(job, monkeypatch, capsys):
    import main
    job = dict(job, last_precheck={"problems": 0})              # skip the first-run pre-check
    backup_utils.save_job(job)
    Meddler(monkeypatch, {VICTIM: 99, "b1.txt": 99})
    code = main.run_backup_job_cli(job["id"])
    out, err = capsys.readouterr()

    assert code == 0 and "[OK] " in out
    line = [l for l in err.splitlines() if "changed while they were being copied" in l]
    assert len(line) == 1 and line[0].startswith("[WARN] 2 file(s) changed while they were being copied: ")
    assert VICTIM in line[0] and "sub/b1.txt" in line[0]
    assert "were not copied" not in err                         # not reported as skipped as well


def test_a_clean_headless_run_prints_no_changed_warning(job, capsys):
    import main
    backup_utils.save_job(dict(job, last_precheck={"problems": 0}))
    assert main.run_backup_job_cli(job["id"]) == 0
    assert "changed while they were being copied" not in capsys.readouterr().err


# --- verify modes, restore and old manifests ---------------------------------------------

@pytest.mark.parametrize("mode", ["full", "quick", "off"])
def test_every_verify_mode_treats_a_flagged_file_normally(tmp_path, monkeypatch, mode):
    job = make_job(tmp_path, verify=mode)
    Meddler(monkeypatch, {VICTIM: 99})
    result, stored = run_and_record(job)

    assert result["ok"] and result["verify_mode"] == mode, result["message"]
    assert result["verified"] is (mode != "off")
    assert result["changed"] == 1 and stored["last_changed"] == 1
    assert manifest_files(result)[VICTIM]["changed"] is True
    assert result["files"] == TOTAL


def test_a_flagged_version_restores_and_checks_like_any_other(job, tmp_path, monkeypatch):
    Meddler(monkeypatch, {VICTIM: 99})
    result = backup_utils.run_job(job)
    monkeypatch.undo()
    out = tmp_path / "out"
    out.mkdir()

    restored = backup_utils.restore_version(job, result["version"], str(out))

    assert restored["ok"] and restored["verified"] is True       # the strict manifest check accepts the key
    assert restored["checked"] == TOTAL and restored["mismatched"] == []
    assert restored["unchecked_reason"] is None and restored["errors"] == []
    with open(long_path(os.path.join(restored["path"], VICTIM)), "rb") as handle:
        assert handle.read() == stored_bytes(job, result, VICTIM)


def test_a_manifest_without_the_key_reads_exactly_like_one_with_it(job, monkeypatch):
    from utils import versions
    Meddler(monkeypatch, {VICTIM: 99})
    result = backup_utils.run_job(job)
    with_key, reason = versions._read_manifest_hashes(job, result["version"])
    assert reason is None and len(with_key) == TOTAL

    # An older manifest: the same file lines without the extra key.
    path = result["manifest"]
    clear_ro(path)      # a finished version's manifest is read-only (card fEC32w6P)
    lines = [json.loads(l) for l in open(path, encoding="utf-8")]
    for entry in lines:
        entry.pop("changed", None)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(json.dumps(e) + "\n" for e in lines))

    without_key, reason = versions._read_manifest_hashes(job, result["version"])
    assert reason is None and without_key == with_key
    assert backup_utils.is_verified(job, result["version"]) is True
    assert versions.read_manifest_summary(job, result["version"])["files"] == TOTAL


# --- awkward names ---------------------------------------------------------------------

@windows_only
def test_a_path_over_260_characters_and_a_trailing_dot_name_are_checked_too(tmp_path, monkeypatch):
    from test_long_paths import _deep_file, _write, _x, _raw_x
    source, target = tmp_path / "s", tmp_path / "t"
    target.mkdir()
    deep = _deep_file(source, 300, leaf="long.bin")
    _write(deep, b"deep" * 100)
    os.makedirs(_raw_x(source), exist_ok=True)
    with open(_raw_x(source) + "\\notes.", "wb") as handle:
        handle.write(b"dot" * 100)
    _write(source / "plain.txt", b"plain")
    job = backup_utils.new_job("Awkward", str(source), str(target), schedule="manual", retention=3)
    backup_utils.save_job(job)

    meddler = Meddler(monkeypatch, {"long.bin": 99, "notes.": 1})
    result = backup_utils.run_job(job)

    assert result["ok"], result["message"]
    assert result["files"] == 3 and result["errors"] == []
    assert meddler.attempts == {"long.bin": 2, "notes.": 2, "plain.txt": 1}
    assert result["changed"] == 1 and result["recopied"] == 1
    flagged = [e["path"] for e in manifest_files(result).values() if e.get("changed")]
    assert len(flagged) == 1 and flagged[0].endswith("long.bin") and len(flagged[0]) > 260 - len(str(source))


# --- a real writer, running while the real engine copies --------------------------------

def test_a_real_concurrent_writer_is_never_reported_as_a_clean_verified_copy(tmp_path):
    # ponytail: timing based by nature -- the writer must be scheduled while the
    # copy reads. It is started before the run and stopped after it, over a
    # 64 MB file whose first and last 8 bytes it rewrites together in a loop
    # (the state the card's audit captured as head 57 / tail 16910), so the
    # copy cannot finish inside a quiet moment unless the writer was starved
    # for the whole run. If the writer did make progress, the file MUST be flagged.
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    big = source / "mail.pst"
    size = 64 * 1024 * 1024
    with open(big, "wb") as handle:
        handle.truncate(size)
    (source / "small1.txt").write_bytes(b"one" * 100)
    (source / "small2.txt").write_bytes(b"two" * 100)
    (source / "small3.txt").write_bytes(b"three" * 100)
    job = backup_utils.new_job("Live", str(source), str(target), schedule="manual", retention=2)
    backup_utils.save_job(job)

    stop, progress = threading.Event(), {"n": 0, "error": None}

    def writer():
        try:
            with open(big, "r+b") as handle:
                while not stop.is_set():
                    progress["n"] += 1
                    pair = progress["n"].to_bytes(8, "little")
                    handle.seek(0)
                    handle.write(pair)
                    handle.seek(size - 8)
                    handle.write(pair)
                    handle.flush()
        except Exception as exc:        # a failed writer must fail the test, not hang it
            progress["error"] = exc

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        started = progress["n"]
        result = backup_utils.run_job(job)
        wrote = progress["n"] - started
    finally:
        stop.set()
        thread.join(timeout=30)
    assert not thread.is_alive() and progress["error"] is None

    assert result["ok"], result["message"]
    assert wrote > 0                                             # the writer really ran during the copy
    # Never "verified" with no flag while the writer ran throughout.
    assert result["changed"] == 1 and result["changed_sample"] == ["mail.pst"]
    assert manifest_files(result)["mail.pst"]["changed"] is True

    # Positive control: the same file with the writer stopped is not flagged.
    again = backup_utils.run_job(job)
    assert again["ok"] and again["changed"] == 0 and again["recopied"] == 0
    assert "changed" not in manifest_files(again)["mail.pst"]
