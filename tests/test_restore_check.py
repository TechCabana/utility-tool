# tests/test_restore_check.py
# Card 6ajl8vjD: a restore checks every file it restores against the manifest
# hash `_hash_copy` already computes while copying, and says so -- or names the
# files that do not match, or says plainly that it could not check.
#
# Versions are made by real `run_job` runs (so the manifest is the one the
# engine writes), then damaged on disk the way a real fault would: a byte
# flipped in a stored file, a manifest truncated or deleted.
import json
import os
import shutil

import pytest

from utils import backup_utils, jobs_store
from helpers import clear_ro, clear_ro_tree
from test_long_paths import _deep_file, _write, _x, windows_only

CHECKED_EACH = "and checked each against the backup"


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep this file's jobs off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


def make_backup(tmp_path, files=None, **options):
    """A real job with one verified version; returns `(job, stamp, source)`."""
    files = files if files is not None else {
        "a.txt": b"alpha", "nested/b.txt": b"bravo bravo", "nested/deep/c.bin": b"\x00\x01" * 50,
        "d.txt": b"delta", "e.txt": b"echo"}
    source, target = tmp_path / "src", tmp_path / "dst"
    for rel, data in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    target.mkdir()
    job = backup_utils.new_job("Check job", str(source), str(target),
                               schedule="manual", retention=5, **options)
    result = backup_utils.run_job(job)
    assert result["ok"] and result["verified"], result["message"]
    return job, result["version"], source


def restore(job, stamp, tmp_path, name="out", **kwargs):
    """Restore `stamp` into a fresh folder."""
    out = tmp_path / name
    out.mkdir()
    return backup_utils.restore_version(job, stamp, str(out), **kwargs)


def stored(job, stamp, rel):
    """The path of `rel` inside the stored version."""
    return os.path.join(backup_utils.version_path(job, stamp), *rel.split("/"))


def flip_byte(path, offset=0):
    """Change one byte of `path` in place, keeping its size."""
    clear_ro(path)      # a finished version's files are read-only (card fEC32w6P)
    with open(_x(path), "r+b") as handle:
        handle.seek(offset)
        byte = handle.read(1)
        handle.seek(offset)
        handle.write(bytes([byte[0] ^ 0x01]))


def manifest_lines(job, stamp):
    """The manifest's lines, as text."""
    with open(backup_utils.manifest_path(job, stamp), encoding="utf-8") as handle:
        return handle.read().splitlines()


def write_manifest(job, stamp, lines):
    """Replace the manifest with `lines`."""
    clear_ro(backup_utils.manifest_path(job, stamp))
    with open(backup_utils.manifest_path(job, stamp), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# The Done-when
# ---------------------------------------------------------------------------

def test_a_version_with_a_manifest_reports_every_file_checked(tmp_path):
    job, stamp, source = make_backup(tmp_path)

    result = restore(job, stamp, tmp_path)

    assert result["ok"], result["message"]
    assert result["files"] == 5 and result["checked"] == 5
    assert result["mismatched"] == [] and result["not_in_manifest"] == []
    assert result["missing_from_version"] == [] and result["unchecked_reason"] is None
    assert result["verified"] is True
    assert result["message"] == f"Restored 5 files to {result['path']} {CHECKED_EACH}"
    assert (tmp_path / "out" / os.path.basename(result["path"]) / "nested" / "b.txt"
            ).read_bytes() == b"bravo bravo"


def test_one_corrupted_byte_in_the_backup_is_reported_by_name(tmp_path):
    job, stamp, source = make_backup(tmp_path)
    flip_byte(stored(job, stamp, "nested/b.txt"), 3)

    result = restore(job, stamp, tmp_path)

    # The restore itself succeeded and nothing was dropped, but it is not verified.
    assert result["ok"] is True and result["verified"] is False
    assert result["mismatched"] == ["nested/b.txt"]   # manifest form: forward slashes
    assert result["checked"] == 4 and result["files"] == 5
    assert "1 file did not match the backup and may be damaged: nested/b.txt" in result["message"]
    assert CHECKED_EACH not in result["message"]
    restored = os.path.join(result["path"], "nested", "b.txt")
    assert open(restored, "rb").read() != b"bravo bravo"        # kept, not deleted
    assert open(os.path.join(result["path"], "a.txt"), "rb").read() == b"alpha"


def test_a_version_without_a_manifest_restores_and_says_it_could_not_be_checked(tmp_path):
    job, stamp, source = make_backup(tmp_path)
    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(backup_utils.manifest_path(job, stamp))

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is True and result["files"] == 5
    assert result["unchecked_reason"] == "no manifest"
    assert result["verified"] is False and result["checked"] == 0
    assert result["mismatched"] == [] and result["missing_from_version"] == []
    # The files of a finished version are read-only (card fEC32w6P) and nothing
    # says which were read-only in the source, so that is said last.
    assert result["message"].endswith(
        "This backup has no checksum list, so the files could not be checked"
        ". 5 files stayed read-only as they are stored in the backup, since its "
        "checksum list cannot say which were read-only originally (clear the "
        "read-only mark to edit)")
    assert CHECKED_EACH not in result["message"]
    assert open(os.path.join(result["path"], "a.txt"), "rb").read() == b"alpha"


# ---------------------------------------------------------------------------
# A manifest that cannot be trusted is never a pass
# ---------------------------------------------------------------------------

def _truncate_trailer(job, stamp):
    """A run that died before writing its end line."""
    write_manifest(job, stamp, manifest_lines(job, stamp)[:-1])


def _garbage_line(job, stamp):
    """A line that is not JSON in the middle, trailer intact."""
    lines = manifest_lines(job, stamp)
    lines.insert(2, "{not json")
    write_manifest(job, stamp, lines)


def _count_disagrees(job, stamp):
    """An entry removed while the trailer still says 5."""
    lines = manifest_lines(job, stamp)
    del lines[2]
    write_manifest(job, stamp, lines)


def _bad_hash(job, stamp):
    """A file entry whose sha256 is not 64 hex digits."""
    lines = manifest_lines(job, stamp)
    entry = json.loads(lines[1])
    entry["sha256"] = "zz" + entry["sha256"][2:]
    lines[1] = json.dumps(entry)
    write_manifest(job, stamp, lines)


def _duplicate_path(job, stamp):
    """An extra line repeating one path, so the distinct entries still number
    what the trailer says and only the repeat gives it away."""
    lines = manifest_lines(job, stamp)
    lines.insert(2, lines[1])
    write_manifest(job, stamp, lines)


def _empty_manifest(job, stamp):
    """A zero-byte manifest."""
    clear_ro(backup_utils.manifest_path(job, stamp))
    open(backup_utils.manifest_path(job, stamp), "w").close()


@pytest.mark.parametrize("damage", [_truncate_trailer, _garbage_line, _count_disagrees,
                                    _bad_hash, _duplicate_path, _empty_manifest])
def test_a_damaged_manifest_is_could_not_be_checked_not_a_pass(tmp_path, damage):
    job, stamp, source = make_backup(tmp_path)
    damage(job, stamp)

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is True and result["files"] == 5      # still restores
    assert result["unchecked_reason"] == "manifest damaged"
    assert result["verified"] is False and result["checked"] == 0
    assert "checksum list is damaged, so the files could not be checked" in result["message"]
    assert CHECKED_EACH not in result["message"]


# ---------------------------------------------------------------------------
# Disagreements between the manifest and the stored version
# ---------------------------------------------------------------------------

def test_a_manifest_entry_whose_file_is_gone_from_the_version_is_named(tmp_path):
    job, stamp, source = make_backup(tmp_path)
    clear_ro(stored(job, stamp, "d.txt"))
    os.remove(stored(job, stamp, "d.txt"))

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is True and result["verified"] is False
    assert result["missing_from_version"] == ["d.txt"]
    assert result["checked"] == 4 and result["files"] == 4     # not counted as checked
    assert "1 file the checksum list records could not be found in the backup: d.txt" in result["message"]
    assert CHECKED_EACH not in result["message"]


def test_a_file_in_the_version_that_the_manifest_does_not_list_is_named(tmp_path):
    job, stamp, source = make_backup(tmp_path)
    with open(stored(job, stamp, "stray.txt"), "wb") as handle:
        handle.write(b"not in the manifest")

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is True and result["verified"] is False
    assert result["not_in_manifest"] == ["stray.txt"]
    assert result["checked"] == 5 and result["files"] == 6     # 5 matched, 1 unchecked
    assert "1 file not on the backup's checksum list could not be checked: stray.txt" \
        in result["message"]
    assert open(os.path.join(result["path"], "stray.txt"), "rb").read() == b"not in the manifest"


def test_a_byte_flipped_in_the_middle_of_a_large_file_is_caught(tmp_path):
    big = bytes(range(256)) * (3 * 4096)          # 3 MiB: three copy chunks
    job, stamp, source = make_backup(tmp_path, {"big.bin": big, "small.txt": b"s"})
    flip_byte(stored(job, stamp, "big.bin"), 1_500_000)         # inside the second chunk

    result = restore(job, stamp, tmp_path)

    assert result["mismatched"] == ["big.bin"] and result["checked"] == 1
    assert result["verified"] is False


def test_many_mismatches_are_counted_and_only_five_are_named(tmp_path):
    files = {f"f{i}.txt": b"x" * (i + 1) for i in range(8)}
    job, stamp, source = make_backup(tmp_path, files)
    for i in range(7):
        flip_byte(stored(job, stamp, f"f{i}.txt"))

    result = restore(job, stamp, tmp_path)

    assert len(result["mismatched"]) == 7 and result["checked"] == 1
    assert "7 files did not match the backup and may be damaged: " in result["message"]
    assert "f0.txt, f1.txt, f2.txt, f3.txt, f4.txt and 2 more" in result["message"]
    assert "f5.txt" not in result["message"]


def test_a_version_with_verification_off_is_still_checked(tmp_path):
    # An "off" version is never the protected last-good copy, but its manifest
    # still records every file's hash from the copy, so the check works.
    source, target = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    target.mkdir()
    (source / "a.txt").write_bytes(b"alpha")
    job = backup_utils.new_job("Off job", str(source), str(target), schedule="manual", verify="off")
    stamp = backup_utils.run_job(job)["version"]
    flip_byte(stored(job, stamp, "a.txt"))

    result = restore(job, stamp, tmp_path)

    assert result["mismatched"] == ["a.txt"] and result["verified"] is False


# ---------------------------------------------------------------------------
# Behaviour that must not change
# ---------------------------------------------------------------------------

def test_restoring_makes_no_second_read_pass(tmp_path, monkeypatch):
    job, stamp, source = make_backup(tmp_path)
    from utils import copying, verify
    real, calls = copying._hash_copy, []
    monkeypatch.setattr(copying, "_hash_copy", lambda s, d: calls.append(s) or real(s, d))

    def no_reread(path):
        raise AssertionError(f"restore re-read {path}")
    monkeypatch.setattr(verify, "_rehash", no_reread)

    result = restore(job, stamp, tmp_path)

    assert result["verified"] is True and len(calls) == 5      # one read per file, no more


def test_an_unreadable_file_is_still_skipped_and_is_not_also_missing(tmp_path, monkeypatch):
    job, stamp, source = make_backup(tmp_path)
    from utils import copying
    real = copying._hash_copy

    def locked(src, dst):
        if src.endswith("d.txt"):
            raise PermissionError(13, "locked by another process", src)
        return real(src, dst)
    monkeypatch.setattr(copying, "_hash_copy", locked)

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is True and result["files"] == 4
    assert len(result["errors"]) == 1 and "d.txt" in result["errors"][0]
    assert "1 file(s) could not be read from the backup and were skipped" in result["message"]
    assert result["missing_from_version"] == []        # the error already says so
    assert result["checked"] == 4 and result["mismatched"] == []


def test_a_whole_unreadable_folder_is_not_also_missing(tmp_path, monkeypatch):
    job, stamp, source = make_backup(tmp_path)
    from utils import copying
    real = os.scandir

    def scandir(path):
        if str(path).replace("\\", "/").rstrip("/").endswith("/nested"):
            raise PermissionError(13, "access denied", str(path))
        return real(path)
    monkeypatch.setattr(copying.os, "scandir", scandir)

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is True and result["errors"] and "nested" in result["errors"][0]
    assert result["files"] == 3                         # a.txt, d.txt, e.txt
    assert result["missing_from_version"] == []         # nested/b.txt, nested/deep/c.bin explained
    assert result["checked"] == 3


def test_an_empty_version_still_fails_as_before(tmp_path):
    job, stamp, source = make_backup(tmp_path)
    clear_ro_tree(backup_utils.version_path(job, stamp))
    shutil.rmtree(backup_utils.version_path(job, stamp))
    os.makedirs(backup_utils.version_path(job, stamp))

    result = restore(job, stamp, tmp_path)

    assert result["ok"] is False and result["cause"] == "source_incomplete"
    assert "verified" not in result and "checked" not in result   # no claim at all


def test_a_cancelled_restore_reports_what_was_checked_and_claims_nothing_more(tmp_path):
    job, stamp, source = make_backup(tmp_path)
    asks = []

    def abort_on_third():
        asks.append(1)
        return len(asks) >= 3                  # two files restored, then stop

    result = restore(job, stamp, tmp_path, should_abort=abort_on_third)

    assert result["ok"] is False and result["cancelled"] is True
    assert result["checked"] == 2 and result["verified"] is False
    assert result["message"].startswith("Restore cancelled. 2 files had been copied and matched")
    assert CHECKED_EACH not in result["message"]
    # Positive control: the same restore run to the end does say it.
    assert CHECKED_EACH in restore(job, stamp, tmp_path, name="full")["message"]


def test_a_cancelled_restore_still_names_a_mismatch_it_had_found(tmp_path):
    job, stamp, source = make_backup(tmp_path, {"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})
    for rel in ("a.txt", "b.txt", "c.txt"):
        flip_byte(stored(job, stamp, rel))
    asks = []

    result = restore(job, stamp, tmp_path,
                     should_abort=lambda: asks.append(1) or len(asks) >= 3)

    assert result["cancelled"] is True and len(result["mismatched"]) == 2
    assert "2 files did not match" in result["message"]


@windows_only
def test_a_file_past_the_windows_path_limit_is_checked_and_named_in_plain_form(tmp_path):
    job, stamp, source = make_backup(tmp_path, {"short.txt": b"s"})
    deep = _deep_file(tmp_path / "src", 250)
    _write(deep, b"deep file contents")
    assert backup_utils.run_job(job)["ok"]
    stamp = backup_utils.latest_version(job)
    rel = os.path.relpath(deep, tmp_path / "src").replace("\\", "/")

    clean = restore(job, stamp, tmp_path)
    assert clean["verified"] is True and clean["checked"] == 2, clean["message"]

    flip_byte(os.path.join(backup_utils.version_path(job, stamp), *rel.split("/")))
    damaged = restore(job, stamp, tmp_path, name="again")
    assert damaged["mismatched"] == [rel]
    assert "\\\\?\\" not in damaged["message"]


# ---------------------------------------------------------------------------
# The Backup tab shows it
# ---------------------------------------------------------------------------

@pytest.fixture
def qt_app():
    """An offscreen QApplication; no QSettings is constructed by these tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def tab(qt_app, monkeypatch):
    """A real BackupTab with no scheduler and its worker thread captured."""
    from utils import scheduler
    from tabs import backup_tab

    def no_scheduler(args):
        assert args[:2] == ["schtasks", "/query"], f"the scheduler was modified: {args}"
        return True, ""
    monkeypatch.setattr(scheduler, "_run", no_scheduler)
    widget = backup_tab.BackupTab()
    widget.started = []
    monkeypatch.setattr(widget, "_start_backup",
                        lambda status, work, done, phases=None: widget.started.append((work, done)))
    return widget


def press_restore(tab, monkeypatch, job, stamp, destination):
    """Click through restore_backup_job as the user would and run its worker
    on this thread, handing the result to the handler the tab chose."""
    from PySide6 import QtWidgets
    monkeypatch.setattr(tab, "_choose_restore_version", lambda _job: stamp)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: str(destination)))
    tab.restore_backup_job(job)
    (work, done), = tab.started
    tab.started.clear()
    done(work(lambda d, t: None, lambda: False, lambda phase: None))
    return tab.backup_status


def test_the_tab_says_each_file_was_checked_in_plain_style(tab, tmp_path, monkeypatch):
    job, stamp, source = make_backup(tmp_path)
    (tmp_path / "out").mkdir()

    status = press_restore(tab, monkeypatch, job, stamp, tmp_path / "out")

    assert not status.isHidden() and status.objectName() == "Hint"
    assert status.text().startswith("Restored 5 files to ") and CHECKED_EACH in status.text()


def test_the_tab_shows_a_mismatch_as_a_warning_naming_the_file(tab, tmp_path, monkeypatch):
    job, stamp, source = make_backup(tmp_path)
    (tmp_path / "out").mkdir()
    flip_byte(stored(job, stamp, "a.txt"))

    status = press_restore(tab, monkeypatch, job, stamp, tmp_path / "out")

    assert not status.isHidden() and status.objectName() == "StatusWarn"
    assert "1 file did not match the backup and may be damaged: a.txt" in status.text()
    assert CHECKED_EACH not in status.text()


def test_the_tab_shows_could_not_be_checked_as_plain_news(tab, tmp_path, monkeypatch):
    job, stamp, source = make_backup(tmp_path)
    (tmp_path / "out").mkdir()
    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(backup_utils.manifest_path(job, stamp))

    status = press_restore(tab, monkeypatch, job, stamp, tmp_path / "out")

    assert status.objectName() == "Hint"            # not a warning, not an error
    assert "no checksum list, so the files could not be checked" in status.text()
    assert CHECKED_EACH not in status.text()


def test_the_tab_still_shows_a_failed_restore_as_an_error(tab):
    tab._finish_restore({"ok": False, "message": "Restore failed: The destination drive is full"})
    assert tab.backup_status.objectName() == "StatusError"
    assert "destination drive is full" in tab.backup_status.text()
