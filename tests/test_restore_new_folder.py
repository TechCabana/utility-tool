# tests/test_restore_new_folder.py
# Card 5AJTFrRx: a second restore of the same version must not silently overwrite
# the first. `restore_version` used to write into `<destination>/<job name>-
# restored-<version>` whether or not it existed, so restoring again replaced every
# file in it -- including edits the user had made in the first restored folder.
# Now the folder is CREATED, exclusively (`os.mkdir`, which fails on any existing
# name), and a taken name moves the restore to `...-2`, `...-3` and so on.
#
# Versions are made by real `run_job` runs. Every "untouched" check hashes the
# whole earlier folder (names, bytes, mtimes) before and after, and the checks that
# say "this could not have been a different folder" have a control that can fail.
import hashlib
import os
import stat
import threading

import pytest

from helpers import clear_ro_tree, is_read_only
from test_long_paths import _x, windows_only
from utils import backup_utils, copying, jobs_store, versions

FILES = {"a.txt": b"alpha", "nested/b.txt": b"bravo bravo", "nested/deep/c.bin": b"\x00\x01" * 50,
         "d.txt": b"delta"}
SAME_NAME = "A folder with that name already existed"


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep this file's jobs off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


def make_backup(tmp_path, name="Photos", files=None, read_only=()):
    """A real job with one verified version: `(job, stamp, source)`."""
    files = FILES if files is None else files
    source, target = tmp_path / "src", tmp_path / "dst"
    for rel, data in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for rel in read_only:
        os.chmod(source / rel, stat.S_IREAD)
    target.mkdir(exist_ok=True)
    job = backup_utils.new_job(name, str(source), str(target), schedule="manual", retention=5)
    result = backup_utils.run_job(job)
    assert result["ok"] and result["verified"], result["message"]
    return job, result["version"], source


def destination(tmp_path, name="out"):
    """A fresh, empty folder to restore into."""
    folder = tmp_path / name
    folder.mkdir(exist_ok=True)
    return str(folder)


def snapshot(root):
    """Everything under `root`: each folder, and each file's bytes hash and mtime.
    Independent of utils, so it means the same whatever the code under test does."""
    found = {}
    for current, dirs, names in os.walk(root):
        for name in dirs:
            found[os.path.relpath(os.path.join(current, name), root)] = "dir"
        for name in names:
            full = os.path.join(current, name)
            with open(full, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
            found[os.path.relpath(full, root)] = (digest, os.stat(full).st_mtime_ns)
    return found


def contents(root):
    """{relative path with forward slashes: bytes} for every file under `root`."""
    found = {}
    for current, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(current, name)
            with open(full, "rb") as handle:
                found[os.path.relpath(full, root).replace(os.sep, "/")] = handle.read()
    return found


def base_of(result):
    """The restore folder's own name."""
    return os.path.basename(result["path"])


# ---------------------------------------------------------------------------
# The Done-when
# ---------------------------------------------------------------------------

def test_a_second_restore_of_the_same_version_goes_into_a_new_folder(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    first = backup_utils.restore_version(job, stamp, out)
    assert first["ok"], first["message"]
    edited = os.path.join(first["path"], "a.txt")
    with open(edited, "wb") as handle:
        handle.write(b"my own work since the first restore")
    before = snapshot(first["path"])
    assert before["a.txt"][0] == hashlib.sha256(b"my own work since the first restore").hexdigest()

    second = backup_utils.restore_version(job, stamp, out)

    assert second["ok"] and second["verified"], second["message"]
    assert second["path"] != first["path"]
    assert base_of(second) == base_of(first) + "-2"
    assert sorted(os.listdir(out)) == sorted([base_of(first), base_of(second)])
    assert snapshot(first["path"]) == before                    # bytes AND mtimes, whole folder
    with open(edited, "rb") as handle:
        assert handle.read() == b"my own work since the first restore"
    assert contents(second["path"]) == {k: v for k, v in FILES.items()}   # the new one is the backup
    assert f"to {second['path']}" in second["message"]
    assert SAME_NAME in second["message"]
    assert f"went into {base_of(second)} and left the earlier one untouched" in second["message"]


def test_the_first_restore_keeps_the_plain_name_and_the_old_message(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)

    first = backup_utils.restore_version(job, stamp, out)

    assert base_of(first) == f"Photos-restored-{stamp}"
    assert first["path"] == os.path.join(out, f"Photos-restored-{stamp}")
    assert first["message"] == (f"Restored {len(FILES)} files to {first['path']} "
                                "and checked each against the backup")
    assert SAME_NAME not in first["message"]


def test_three_restores_make_three_folders_and_leave_each_one_alone(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    first = backup_utils.restore_version(job, stamp, out)
    second = backup_utils.restore_version(job, stamp, out)
    for result in (first, second):
        with open(os.path.join(result["path"], "d.txt"), "wb") as handle:
            handle.write(b"edit in " + base_of(result).encode())
    seen = {r["path"]: snapshot(r["path"]) for r in (first, second)}

    third = backup_utils.restore_version(job, stamp, out)

    assert [base_of(r) for r in (first, second, third)] == [
        f"Photos-restored-{stamp}", f"Photos-restored-{stamp}-2", f"Photos-restored-{stamp}-3"]
    assert third["message"].endswith(f"went into {base_of(third)} and left the earlier one untouched")
    for path, before in seen.items():
        assert snapshot(path) == before


def test_a_different_version_is_not_a_clash(tmp_path):
    """Control: the number is added only for the SAME version; another version's
    restore keeps its own plain name beside the first."""
    job, first_stamp, _source = make_backup(tmp_path)
    (tmp_path / "src" / "d.txt").write_bytes(b"changed")
    second_stamp = backup_utils.run_job(job)["version"]
    assert second_stamp != first_stamp
    out = destination(tmp_path)
    one = backup_utils.restore_version(job, first_stamp, out)
    two = backup_utils.restore_version(job, second_stamp, out)
    assert base_of(one) == f"Photos-restored-{first_stamp}"
    assert base_of(two) == f"Photos-restored-{second_stamp}"
    assert SAME_NAME not in two["message"]


# ---------------------------------------------------------------------------
# A taken name is skipped, whatever took it
# ---------------------------------------------------------------------------

def test_a_file_with_the_folder_name_is_skipped_not_overwritten(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    squatter = os.path.join(out, f"Photos-restored-{stamp}")
    with open(squatter, "wb") as handle:
        handle.write(b"I am a file, not a folder")

    result = backup_utils.restore_version(job, stamp, out)

    assert result["ok"], result["message"]
    assert base_of(result) == f"Photos-restored-{stamp}-2"
    assert os.path.isfile(squatter)
    with open(squatter, "rb") as handle:
        assert handle.read() == b"I am a file, not a folder"
    assert contents(result["path"]) == FILES


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_a_junction_with_the_folder_name_is_skipped_and_its_target_is_untouched(tmp_path):
    import _winapi
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_bytes(b"not for the restore to touch")
    before = snapshot(str(elsewhere))
    link = os.path.join(out, f"Photos-restored-{stamp}")
    _winapi.CreateJunction(str(elsewhere), link)
    try:
        result = backup_utils.restore_version(job, stamp, out)

        assert result["ok"], result["message"]
        assert base_of(result) == f"Photos-restored-{stamp}-2"
        assert snapshot(str(elsewhere)) == before               # nothing written through the link
        assert sorted(os.listdir(elsewhere)) == ["precious.txt"]
        assert contents(result["path"]) == FILES
    finally:
        os.rmdir(link)                                          # the link only, never its target
    assert not os.path.lexists(link)
    assert snapshot(str(elsewhere)) == before


def test_an_existing_folder_of_that_name_is_untouched_even_when_it_holds_something_else(tmp_path):
    """A folder the USER made with that name (not a restore) is as protected as
    a restored one."""
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    theirs = os.path.join(out, f"Photos-restored-{stamp}")
    os.makedirs(os.path.join(theirs, "mine"))
    with open(os.path.join(theirs, "a.txt"), "wb") as handle:     # same name as a backed-up file
        handle.write(b"theirs")
    before = snapshot(theirs)

    result = backup_utils.restore_version(job, stamp, out)

    assert result["ok"] and base_of(result).endswith("-2")
    assert snapshot(theirs) == before


# ---------------------------------------------------------------------------
# Names that are awkward on their own
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Trip 2024", "Year 2024.", "v1.2 final ", "Backup-2", "2024", "a b  c",
                                  "Dots..."])
def test_the_number_goes_after_the_version_for_any_job_name(tmp_path, name):
    job, stamp, _source = make_backup(tmp_path, name=name)
    out = destination(tmp_path)
    first = backup_utils.restore_version(job, stamp, out)
    second = backup_utils.restore_version(job, stamp, out)
    assert first["ok"] and second["ok"], (first["message"], second["message"])
    assert base_of(first).endswith(f"-restored-{stamp}")
    assert base_of(second) == base_of(first) + "-2"
    assert os.path.isdir(first["path"]) and os.path.isdir(second["path"])
    assert contents(second["path"]) == FILES


@windows_only
def test_a_destination_past_260_characters_works_for_both_restores(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    deep = str(tmp_path)
    while len(deep) < 280:
        deep = os.path.join(deep, "a-folder-name-of-some-length-" + "x" * 40)
    os.makedirs(_x(deep))
    assert len(deep) > 260
    first = backup_utils.restore_version(job, stamp, deep)
    second = backup_utils.restore_version(job, stamp, deep)
    assert first["ok"] and second["ok"], (first["message"], second["message"])
    assert base_of(second) == base_of(first) + "-2"
    assert len(second["path"]) > 260
    for result in (first, second):
        with open(_x(os.path.join(result["path"], "a.txt")), "rb") as handle:
            assert handle.read() == b"alpha"


def test_a_destination_that_does_not_exist_yet_is_still_created(tmp_path):
    """Unchanged behaviour: the copy always made a missing destination."""
    job, stamp, _source = make_backup(tmp_path)
    out = str(tmp_path / "not" / "there" / "yet")
    result = backup_utils.restore_version(job, stamp, out)
    assert result["ok"], result["message"]
    assert result["path"] == os.path.join(out, f"Photos-restored-{stamp}")
    assert contents(result["path"]) == FILES


def test_a_destination_that_is_a_file_fails_plainly_and_writes_nothing(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"a file where a folder was meant")
    result = backup_utils.restore_version(job, stamp, str(blocker))
    assert result["ok"] is False
    assert result["message"].startswith("Restore failed:") and "Nothing was restored" in result["message"]
    assert blocker.read_bytes() == b"a file where a folder was meant"


# ---------------------------------------------------------------------------
# A restore that does not finish
# ---------------------------------------------------------------------------

def test_a_cancelled_restore_leaves_its_folder_and_the_next_restore_uses_the_next_name(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    calls = []

    def stop_after_two_files():
        calls.append(1)
        return len(calls) > 2

    cancelled = backup_utils.restore_version(job, stamp, out, should_abort=stop_after_two_files)
    assert cancelled["ok"] is False and cancelled.get("cancelled")
    half = os.path.join(out, f"Photos-restored-{stamp}")
    assert os.path.isdir(half)                                  # card 102 owns cleaning this up
    held = snapshot(half)
    kept_files = [entry for entry in held.values() if entry != "dir"]
    assert 0 < len(kept_files) < len(FILES)                     # really half full, not empty

    nxt = backup_utils.restore_version(job, stamp, out)

    assert nxt["ok"], nxt["message"]
    assert base_of(nxt) == f"Photos-restored-{stamp}-2"
    assert snapshot(half) == held                               # the half-filled one was not reused
    assert contents(nxt["path"]) == FILES


def test_a_restore_that_fails_on_the_drive_removes_only_its_own_folder_and_names_are_free_again(
        tmp_path, monkeypatch):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    first = backup_utils.restore_version(job, stamp, out)       # an earlier restore, left alone
    held = snapshot(first["path"])

    def full_drive(src, dst):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(copying, "_hash_copy", full_drive)
        failed = backup_utils.restore_version(job, stamp, out)
    assert failed["ok"] is False and failed["cause"] == "no_space"
    assert "The incomplete restore folder was removed" in failed["message"]
    assert os.listdir(out) == [base_of(first)]                  # its own folder gone, the first kept
    assert snapshot(first["path"]) == held

    again = backup_utils.restore_version(job, stamp, out)
    assert again["ok"] and base_of(again) == base_of(first) + "-2"


def test_a_restore_never_writes_over_a_file_that_is_already_there(tmp_path, monkeypatch):
    """The folder is empty when the copy starts, so no destination file exists at
    the moment any file is written: the read-only overwrite in
    `copying._open_for_write` is never reached by a restore."""
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    real, met = copying._hash_copy, []

    def spy(src, dst):
        met.append(os.path.lexists(dst))
        return real(src, dst)

    monkeypatch.setattr(copying, "_hash_copy", spy)
    backup_utils.restore_version(job, stamp, out)
    backup_utils.restore_version(job, stamp, out)
    backup_utils.restore_version(job, stamp, out)

    assert len(met) == 3 * len(FILES)
    assert not any(met)
    # Control: the spy can see a pre-existing file (a backup re-copy over one).
    probe_src, probe_dst = tmp_path / "src" / "a.txt", tmp_path / "probe.txt"
    probe_dst.write_bytes(b"already here")
    spy(str(probe_src), str(probe_dst))
    assert met[-1] is True


def test_a_folder_that_appears_between_the_check_and_the_create_is_not_written_into(
        tmp_path, monkeypatch):
    """Someone else (another restore, the user) makes the folder at the last
    moment. The create-or-fail mkdir loses the race cleanly and the restore moves
    on; a check-then-create would walk into their folder."""
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    plain = os.path.join(out, f"Photos-restored-{stamp}")
    real_mkdir, real_makedirs, fired = os.mkdir, os.makedirs, []

    def sneak(path):
        """The other creator wins, once, the instant before this call."""
        if not fired and os.path.basename(os.fspath(path)) == os.path.basename(plain):
            fired.append(1)
            real_mkdir(os.fspath(path))
            with open(os.path.join(os.fspath(path), "theirs.txt"), "wb") as handle:
                handle.write(b"theirs")

    def racing_mkdir(path, *args, **kwargs):
        sneak(path)
        return real_mkdir(path, *args, **kwargs)

    def racing_makedirs(path, *args, **kwargs):
        sneak(path)
        return real_makedirs(path, *args, **kwargs)

    with monkeypatch.context() as patch:        # scoped: never `monkeypatch.undo()`, which would
        patch.setattr(versions.os, "mkdir", racing_mkdir)    # also drop the autouse isolation
        patch.setattr(versions.os, "makedirs", racing_makedirs)
        result = backup_utils.restore_version(job, stamp, out)

    assert fired, "the race was never staged"
    assert result["ok"], result["message"]
    assert base_of(result) == f"Photos-restored-{stamp}-2"
    assert os.listdir(plain) == ["theirs.txt"]                  # nothing restored into it
    assert contents(result["path"]) == FILES


def test_two_restores_racing_for_the_same_name_end_in_two_folders_holding_everything(tmp_path):
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    runners = 4
    barrier = threading.Barrier(runners)
    results, errors = [], []

    def go():
        try:
            barrier.wait(10)
            results.append(backup_utils.restore_version(job, stamp, out))
        except BaseException as exc:         # surfaced by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(runners)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert errors == [] and len(results) == runners
    assert all(r["ok"] and r["verified"] for r in results), [r["message"] for r in results]
    names = sorted(base_of(r) for r in results)
    base = f"Photos-restored-{stamp}"
    assert names == sorted([base, base + "-2", base + "-3", base + "-4"])
    assert sorted(os.listdir(out)) == names
    for result in results:
        assert contents(result["path"]) == FILES                # every file, in every folder


# ---------------------------------------------------------------------------
# The cap
# ---------------------------------------------------------------------------

def test_when_every_name_is_taken_the_restore_fails_plainly_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(versions, "RESTORE_FOLDER_ATTEMPTS", 3)
    job, stamp, _source = make_backup(tmp_path)
    out = destination(tmp_path)
    made = [backup_utils.restore_version(job, stamp, out) for _ in range(3)]
    assert [base_of(r) for r in made] == [f"Photos-restored-{stamp}", f"Photos-restored-{stamp}-2",
                                          f"Photos-restored-{stamp}-3"]
    before = snapshot(out)

    fourth = backup_utils.restore_version(job, stamp, out)

    assert fourth["ok"] is False and "path" not in fourth
    assert "Could not find a free folder name for the restore" in fourth["message"]
    assert "Nothing was restored" in fourth["message"]
    assert snapshot(out) == before                              # no -4, nothing overwritten


# ---------------------------------------------------------------------------
# Read-only files and the test restore
# ---------------------------------------------------------------------------

def test_a_read_only_source_file_comes_back_read_only_in_every_folder(tmp_path):
    job, stamp, _source = make_backup(tmp_path, read_only=["a.txt"])
    out = destination(tmp_path)
    first = backup_utils.restore_version(job, stamp, out)
    second = backup_utils.restore_version(job, stamp, out)
    assert first["ok"] and second["ok"] and base_of(second).endswith("-2")
    for result in (first, second):
        assert is_read_only(os.path.join(result["path"], "a.txt"))
        assert not is_read_only(os.path.join(result["path"], "d.txt"))     # control: only that one
        assert contents(result["path"]) == FILES
    clear_ro_tree(out)


def test_the_test_restore_path_still_restores_into_its_own_folder_with_the_plain_name(tmp_path):
    """Card 0Bv7mvB3 restores a sample into an empty `mkdtemp` folder through
    `restore_version(only=...)`; nothing is there, so the name has no number."""
    import tempfile
    job, stamp, _source = make_backup(tmp_path)
    folder = tempfile.mkdtemp(prefix="safekeep-test-restore-", dir=str(tmp_path))
    result = backup_utils.restore_version(job, stamp, folder, only={"a.txt", "nested/b.txt"})
    assert result["ok"], result["message"]
    assert result["path"] == os.path.join(folder, f"Photos-restored-{stamp}")
    assert contents(result["path"]) == {"a.txt": b"alpha", "nested/b.txt": b"bravo bravo"}
    assert SAME_NAME not in result["message"]


# ---------------------------------------------------------------------------
# What the Backup tab tells the user
# ---------------------------------------------------------------------------

@pytest.fixture
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_backup_tab_shows_the_folder_a_second_restore_really_used(qt_app, tmp_path, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from tabs import backup_tab
    from utils import scheduler
    monkeypatch.setattr(scheduler, "_run", lambda args: (True, ""))      # no real schtasks
    for name in ("warning", "critical", "information", "question"):      # no dialog may show
        monkeypatch.setattr(QtWidgets.QMessageBox, name, staticmethod(lambda *a, **k: 0))
    job, stamp, _source = make_backup(tmp_path)
    backup_utils.save_job(job)
    out = destination(tmp_path)
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: out))
    tab = backup_tab.BackupTab()

    def restore_on_tab():
        """One Restore press on the real tab, worker thread and Qt event loop."""
        tab.restore_backup_job(backup_utils.get_job(job["id"]))
        assert tab.backup_thread is not None
        loop, poll = QtCore.QEventLoop(), QtCore.QTimer()
        poll.setInterval(20)
        poll.timeout.connect(lambda: loop.quit() if tab.backup_thread is None else None)
        QtCore.QTimer.singleShot(60_000, loop.quit)
        poll.start()
        loop.exec()
        poll.stop()
        assert tab.backup_thread is None, "the restore did not finish in the real event loop"
        return tab.backup_status.text()

    first = restore_on_tab()
    second = restore_on_tab()

    plain = os.path.join(out, f"Photos-restored-{stamp}")
    assert first.startswith(f"Restored {len(FILES)} files to {plain} ")
    assert SAME_NAME not in first
    assert second.startswith(f"Restored {len(FILES)} files to {plain}-2 ")
    assert f"went into Photos-restored-{stamp}-2 and left the earlier one untouched" in second
    assert tab.backup_status.objectName() != "StatusError"
