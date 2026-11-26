# tests/test_read_only_versions.py
# Card fEC32w6P (spec safety item 9): a finished version's files and its manifest
# are marked read-only, so an accidental save, a drag in Explorer or a careless
# script is refused instead of changing a backup with no sign. The mark must not
# break retention (older versions still delete), restore (the restored copy is a
# normal writable file, EXCEPT where the SOURCE file was read-only, which the
# manifest records as `ro`), or the verified state (still "the manifest says so").
#
# Versions are made by real `run_job` runs. Every attribute check reads the real
# OS attribute (`tests/helpers.py`), and every "refused" check has a positive
# control: the same open succeeds once the mark is cleared.
import contextlib
import json
import os
import stat
import sys
from datetime import datetime

import pytest

from helpers import (_long, clear_ro, clear_ro_tree, file_attributes, is_read_only,
                     set_attributes)
from test_long_paths import _deep_file, _write, _x, windows_only
from utils import backup_utils, copying, fs_utils, jobs_store, versions

HIDDEN, SYSTEM, ARCHIVE, READONLY = 0x2, 0x4, 0x20, 0x1


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep this file's jobs off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


FILES = {"a.txt": b"alpha", "nested/b.txt": b"bravo bravo", "nested/deep/c.bin": b"\x00\x01" * 50,
         "d.txt": b"delta"}


def make_job(tmp_path, files=None, retention=5, read_only=(), **options):
    """A job over a fresh source (`files`, with `read_only` names made read-only)."""
    files = FILES if files is None else files
    source, target = tmp_path / "src", tmp_path / "dst"
    for rel, data in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for rel in read_only:
        os.chmod(source / rel, stat.S_IREAD)
    target.mkdir(exist_ok=True)
    return backup_utils.new_job("Ro job", str(source), str(target), schedule="manual",
                                retention=retention, **options), source


def run_ok(job):
    """Run `job` once and return its (ok) result."""
    result = backup_utils.run_job(job)
    assert result["ok"], result["message"]
    return result


def stored(job, stamp, rel):
    """The path of `rel` inside the stored version."""
    return os.path.join(backup_utils.version_path(job, stamp), *rel.split("/"))


def stored_files(job, stamp):
    """Every file path inside the stored version."""
    root = _x(backup_utils.version_path(job, stamp))
    return [os.path.join(cur, name) for cur, _d, names in os.walk(root) for name in names]


def entries(job, stamp):
    """The manifest's lines, parsed."""
    with open(_x(backup_utils.manifest_path(job, stamp)), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def restore(job, stamp, tmp_path, name="out"):
    """Restore `stamp` into a fresh folder `name`; returns the result."""
    out = tmp_path / name
    out.mkdir(exist_ok=True)
    return backup_utils.restore_version(job, stamp, str(out))


def restored(result, rel):
    """The path of `rel` inside a restore's folder."""
    return os.path.join(result["path"], *rel.split("/"))


def assert_refuses_write(path):
    """Opening `path` for append and for write fails; reading works. Then the
    positive control: once the mark is cleared the same open succeeds."""
    for mode in ("ab", "wb", "r+b"):
        with pytest.raises(PermissionError):
            open(_x(path), mode).close()
    with open(_x(path), "rb") as handle:
        handle.read(1)
    clear_ro(path)
    with open(_x(path), "ab") as handle:       # the check could have said "writable"
        handle.write(b"")


def attributes_of_folder(path) -> int:
    """Windows attribute bits of a folder."""
    return os.stat(_x(path)).st_file_attributes


@contextlib.contextmanager
def locked_exclusively(path):
    """Hold `path` open with no sharing at all (Windows), so it can be neither
    deleted nor opened for writing by anyone else."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    handle = kernel.CreateFileW(_x(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value, "could not lock the file"
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


posix_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                reason="root ignores write bits")


# ---------------------------------------------------------------------------
# The Done-when, part (a): a finished version refuses writes
# ---------------------------------------------------------------------------

@posix_root
def test_a_stored_file_cannot_be_opened_for_writing(tmp_path):
    job, _ = make_job(tmp_path)
    result = run_ok(job)
    assert result["protected"] is True and result["protect_failed"] == 0
    for rel in FILES:
        assert is_read_only(stored(job, result["version"], rel)), rel
    assert_refuses_write(stored(job, result["version"], "nested/b.txt"))


@posix_root
def test_the_manifest_cannot_be_opened_for_writing(tmp_path):
    job, _ = make_job(tmp_path)
    result = run_ok(job)
    assert_refuses_write(backup_utils.manifest_path(job, result["version"]))


@posix_root
def test_an_edit_in_place_is_refused_and_the_file_is_unchanged(tmp_path):
    """The exact accident the card names: overwrite a file inside the version."""
    job, _ = make_job(tmp_path)
    result = run_ok(job)
    path = stored(job, result["version"], "a.txt")
    with pytest.raises(PermissionError):
        with open(_x(path), "wb") as handle:
            handle.write(b"oops")
    with open(_x(path), "rb") as handle:
        assert handle.read() == b"alpha"


def test_is_verified_still_means_only_the_manifest_says_so(tmp_path):
    """Documented non-goal: an edit made after clearing the mark is not noticed
    by `is_verified` (that is the deep-verify card), so the rule is unchanged."""
    job, _ = make_job(tmp_path)
    stamp = run_ok(job)["version"]
    clear_ro(stored(job, stamp, "a.txt"))
    with open(_x(stored(job, stamp, "a.txt")), "wb") as handle:
        handle.write(b"edited on purpose")
    assert backup_utils.is_verified(job, stamp) is True


@windows_only
def test_folders_are_not_marked_on_windows(tmp_path):
    job, _ = make_job(tmp_path)
    stamp = run_ok(job)["version"]
    version = backup_utils.version_path(job, stamp)
    for folder in (version, os.path.join(version, "nested"), os.path.join(version, "nested", "deep")):
        assert not attributes_of_folder(folder) & READONLY, folder


@windows_only
def test_hidden_system_archive_bits_are_kept_on_the_stored_and_restored_file(tmp_path):
    job, source = make_job(tmp_path)
    set_attributes(source / "a.txt", HIDDEN | SYSTEM | ARCHIVE)
    stamp = run_ok(job)["version"]
    kept = file_attributes(stored(job, stamp, "a.txt"))
    assert kept & (HIDDEN | SYSTEM | ARCHIVE) == HIDDEN | SYSTEM | ARCHIVE
    assert kept & READONLY                                      # marked
    assert not file_attributes(stored(job, stamp, "d.txt")) & HIDDEN   # control: others plain
    out = restore(job, stamp, tmp_path)
    again = file_attributes(restored(out, "a.txt"))
    assert again & (HIDDEN | SYSTEM | ARCHIVE) == HIDDEN | SYSTEM | ARCHIVE
    assert not again & READONLY                                 # normal, writable
    for path in (source / "a.txt",):
        set_attributes(path, 0x80)                              # let pytest clean up


# ---------------------------------------------------------------------------
# Part (b): retention still deletes a protected version
# ---------------------------------------------------------------------------

def test_retention_deletes_the_older_protected_version_and_its_manifest(tmp_path):
    job, source = make_job(tmp_path, retention=1)
    first = run_ok(job)["version"]
    old_folder = backup_utils.version_path(job, first)
    old_manifest = backup_utils.manifest_path(job, first)
    assert all(is_read_only(p) for p in stored_files(job, first)) and is_read_only(old_manifest)
    second = run_ok(job)
    assert second["pruned"] == [first]
    assert not os.path.exists(_x(old_folder)) and not os.path.exists(_x(old_manifest))
    assert backup_utils.version_names(job) == [second["version"]]
    assert all(is_read_only(p) for p in stored_files(job, second["version"]))
    assert backup_utils.is_verified(job, second["version"])


@windows_only
def test_retention_deletes_nested_hidden_system_read_only_files(tmp_path):
    job, source = make_job(tmp_path, retention=1)
    set_attributes(source / "nested" / "b.txt", HIDDEN | SYSTEM)
    set_attributes(source / "nested" / "deep" / "c.bin", HIDDEN | SYSTEM | ARCHIVE)
    first = run_ok(job)["version"]
    assert file_attributes(stored(job, first, "nested/b.txt")) & (HIDDEN | SYSTEM | READONLY) \
        == HIDDEN | SYSTEM | READONLY
    set_attributes(source / "nested" / "b.txt", 0x80)           # source plain again
    set_attributes(source / "nested" / "deep" / "c.bin", 0x80)
    second = run_ok(job)
    assert second["pruned"] == [first]
    assert not os.path.exists(_x(backup_utils.version_path(job, first)))
    assert [n for n in os.listdir(_x(backup_utils.job_root(job)))
            if not n.startswith(second["version"])] == []


def test_prune_of_a_protected_version_whose_manifest_is_missing(tmp_path):
    job, _ = make_job(tmp_path, retention=1)
    first = run_ok(job)["version"]
    manifest = backup_utils.manifest_path(job, first)
    clear_ro(manifest)
    os.remove(_x(manifest))
    second = run_ok(job)
    assert second["pruned"] == [first]
    assert not os.path.exists(_x(backup_utils.version_path(job, first)))


def test_the_rmtree_hook_clears_read_only_on_every_file(tmp_path):
    tree = tmp_path / "t"
    for rel in ("a", "n/b", "n/m/c"):
        _write(tree / rel, b"x")
        os.chmod(_x(tree / rel), stat.S_IREAD)
    versions._rmtree(str(tree))
    assert not os.path.exists(_x(tree))


@windows_only
def test_trailing_dot_and_over_260_character_names_are_marked_pruned_and_restored(tmp_path):
    job, source = make_job(tmp_path, files={"plain.txt": b"p"}, retention=1)
    with open(os.path.join(_long(source), "notes."), "wb") as handle:   # no normalising: keeps the dot
        handle.write(b"dot")
    deep = _deep_file(source, 300)
    _write(deep, b"deep bytes")
    first = run_ok(job)
    stamp = first["version"]
    marked = stored_files(job, stamp)
    assert any(p.endswith("notes.") for p in marked)
    assert any(len(p) > 280 for p in marked)
    assert all(is_read_only(p) for p in marked), [p for p in marked if not is_read_only(p)]
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["verified"], out["message"]
    restored_files = [os.path.join(c, n) for c, _d, ns in os.walk(_x(out["path"])) for n in ns]
    assert restored_files and not any(is_read_only(p) for p in restored_files)
    second = run_ok(job)
    assert second["pruned"] == [stamp]
    assert not os.path.exists(_x(backup_utils.version_path(job, stamp)))
    assert not os.path.exists(_x(backup_utils.manifest_path(job, stamp)))


# ---------------------------------------------------------------------------
# Part (c): restore
# ---------------------------------------------------------------------------

def test_a_restored_file_is_writable_unless_the_source_file_was_read_only(tmp_path):
    job, source = make_job(tmp_path, read_only=["nested/b.txt"])
    stamp = run_ok(job)["version"]
    assert is_read_only(stored(job, stamp, "d.txt"))            # marked, though the source was not
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["verified"], out["message"]
    assert is_read_only(restored(out, "nested/b.txt"))          # the source's own bit comes back
    for rel in ("a.txt", "nested/deep/c.bin", "d.txt"):
        assert not is_read_only(restored(out, rel)), rel
        with open(_x(restored(out, rel)), "ab"):                # genuinely writable
            pass
    with open(_x(restored(out, "nested/b.txt")), "rb") as handle:
        assert handle.read() == b"bravo bravo"


def test_the_manifest_records_ro_only_for_source_read_only_files_and_protected_in_the_trailer(tmp_path):
    job, _ = make_job(tmp_path, read_only=["a.txt"])
    stamp = run_ok(job)["version"]
    lines = entries(job, stamp)
    ro = {line["path"] for line in lines if line["type"] == "file" and line.get("ro")}
    assert ro == {"a.txt"}
    assert all(line.get("ro") is True for line in lines if line.get("ro") is not None)
    assert lines[-1]["type"] == "end" and lines[-1]["protected"] is True
    assert lines[0].get("protected") is None and "ro" not in lines[0]
    assert backup_utils.read_manifest_summary(job, stamp)["protected"] is True


def make_old_version(job, stamp, drop_ro_keys=True):
    """Turn a freshly made version into what an earlier build would have left:
    no `ro` and no `protected` in the manifest, and the stored files carry the
    SOURCE's read-only bit (the old rule) rather than the protective one."""
    manifest = backup_utils.manifest_path(job, stamp)
    lines = entries(job, stamp)
    read_only_in_source = {l["path"] for l in lines if l.get("ro")}
    clear_ro_tree(backup_utils.version_path(job, stamp))
    for rel in read_only_in_source:
        os.chmod(_x(stored(job, stamp, rel)), stat.S_IREAD)
    clear_ro(manifest)
    out = []
    for line in lines:
        line.pop("protected", None)
        if drop_ro_keys:
            line.pop("ro", None)
        out.append(json.dumps(line))
    with open(_x(manifest), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")
    return read_only_in_source


def test_an_old_unprotected_version_restores_by_the_old_rule(tmp_path):
    """Made before this change: the stored file's own read-only bit IS the source's."""
    job, _ = make_job(tmp_path, read_only=["nested/b.txt"])
    stamp = run_ok(job)["version"]
    assert make_old_version(job, stamp) == {"nested/b.txt"}
    assert backup_utils.read_manifest_summary(job, stamp).get("protected") is None
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["verified"], out["message"]
    assert is_read_only(restored(out, "nested/b.txt"))
    for rel in ("a.txt", "d.txt", "nested/deep/c.bin"):
        assert not is_read_only(restored(out, rel)), rel


def test_the_ro_key_is_ignored_for_a_version_that_is_not_protected(tmp_path):
    job, _ = make_job(tmp_path)
    stamp = run_ok(job)["version"]
    make_old_version(job, stamp)
    manifest = backup_utils.manifest_path(job, stamp)
    lines = entries(job, stamp)
    for line in lines:
        if line.get("path") == "a.txt":
            line["ro"] = True                       # a stray key, no `protected` in the trailer
    with open(_x(manifest), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(json.dumps(l) for l in lines) + "\n")
    out = restore(job, stamp, tmp_path)
    assert out["ok"], out["message"]
    assert not is_read_only(restored(out, "a.txt"))


def test_a_protected_version_whose_marking_partly_failed_still_restores_correctly(tmp_path, monkeypatch):
    job, _ = make_job(tmp_path, read_only=["a.txt"])
    real = fs_utils.set_read_only

    def flaky(path, read_only):
        """Refuse to mark b.txt and a.txt (the source-read-only one)."""
        if read_only and os.path.basename(path) in ("b.txt", "a.txt"):
            raise PermissionError(13, "denied", path)
        real(path, read_only)

    with monkeypatch.context() as patch:
        patch.setattr(versions, "set_read_only", flaky)
        result = run_ok(job)
    stamp = result["version"]
    assert result["ok"] and result["protected"] is False and result["protect_failed"] == 2
    assert "2 file(s) could not be marked read-only" in result["message"]
    assert backup_utils.is_verified(job, stamp)
    assert not is_read_only(stored(job, stamp, "nested/b.txt"))     # still writable: not marked
    assert is_read_only(stored(job, stamp, "nested/deep/c.bin"))    # the others were
    assert is_read_only(stored(job, stamp, "d.txt")) and is_read_only(backup_utils.manifest_path(job, stamp))
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["verified"], out["message"]
    assert is_read_only(restored(out, "a.txt"))                     # `ro`, though never marked
    for rel in ("nested/b.txt", "nested/deep/c.bin", "d.txt"):
        assert not is_read_only(restored(out, rel)), rel


def test_a_protected_version_with_a_damaged_manifest_keeps_the_stored_modes(tmp_path):
    """A manifest that cannot be trusted says nothing about `ro`: the copy's
    attributes are left as the copy made them, and the restore says it could
    not check (existing behaviour). Review gate: it also SAYS the files stayed
    read-only, so a user is not left with uneditable files and no reason."""
    job, _ = make_job(tmp_path)
    stamp = run_ok(job)["version"]
    manifest = backup_utils.manifest_path(job, stamp)
    lines = entries(job, stamp)
    clear_ro(manifest)
    with open(_x(manifest), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(json.dumps(l) for l in lines[:-2] + [lines[-1]]) + "\n")  # files != trailer
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["unchecked_reason"] == "manifest damaged"
    assert is_read_only(restored(out, "a.txt"))
    assert out["read_only_kept"] == len(FILES)
    assert (f"{len(FILES)} files stayed read-only as they are stored in the backup"
            in out["message"]), out["message"]


def test_a_missing_manifest_says_how_many_restored_files_stayed_read_only(tmp_path):
    """Same for a protected version whose manifest is gone: nothing can say which
    files were read-only in the source, and the restore says so."""
    job, _ = make_job(tmp_path)
    stamp = run_ok(job)["version"]
    clear_ro(backup_utils.manifest_path(job, stamp))
    os.remove(_x(backup_utils.manifest_path(job, stamp)))
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["read_only_kept"] == len(FILES), out["message"]
    assert "clear the read-only mark to edit" in out["message"]


def test_a_trusted_restore_says_nothing_about_read_only_files(tmp_path):
    """Control: a good protected version restores its files writable (one source
    read-only file stays so, by its `ro` line) and adds no such sentence."""
    job, _ = make_job(tmp_path, read_only=["a.txt"])
    out = restore(job, run_ok(job)["version"], tmp_path)
    assert out["ok"] and out["verified"] and "read_only_kept" not in out
    assert "stayed read-only" not in out["message"]


def test_a_second_restore_leaves_a_read_only_restored_file_alone_and_goes_to_a_new_folder(tmp_path):
    # Card 5AJTFrRx: this used to overwrite the first restore's read-only file.
    job, _ = make_job(tmp_path, read_only=["a.txt"])
    stamp = run_ok(job)["version"]
    first = restore(job, stamp, tmp_path)
    assert is_read_only(restored(first, "a.txt"))
    second = restore(job, stamp, tmp_path)                      # same destination, same version
    assert second["ok"] and second["verified"], second["message"]
    assert second["path"] == first["path"] + "-2"
    assert is_read_only(restored(first, "a.txt")) and is_read_only(restored(second, "a.txt"))
    for result in (first, second):
        with open(_x(restored(result, "a.txt")), "rb") as handle:
            assert handle.read() == b"alpha"


@windows_only
def test_a_file_held_open_in_an_earlier_restore_does_not_stop_a_second_restore(tmp_path):
    # Card 5AJTFrRx: a restore no longer needs to replace anything in the earlier
    # folder, so a file there that cannot be written (locked, read-only) is
    # irrelevant. It used to fail with `no_permission` naming the file.
    job, _ = make_job(tmp_path, read_only=["a.txt"])
    stamp = run_ok(job)["version"]
    first = restore(job, stamp, tmp_path)
    with locked_exclusively(restored(first, "a.txt")):
        second = restore(job, stamp, tmp_path)
    assert second["ok"] is True and second["path"] == first["path"] + "-2", second["message"]


# ---------------------------------------------------------------------------
# Marking failures are never fatal
# ---------------------------------------------------------------------------

def test_a_failure_to_mark_one_file_does_not_fail_the_run(tmp_path, monkeypatch):
    job, _ = make_job(tmp_path)
    real = fs_utils.set_read_only
    seen = []

    def one_refusal(path, read_only):
        if read_only and os.path.basename(path) == "d.txt":
            seen.append(path)
            raise PermissionError(13, "denied", path)
        real(path, read_only)

    with monkeypatch.context() as patch:
        patch.setattr(versions, "set_read_only", one_refusal)
        result = run_ok(job)
    stamp = result["version"]
    assert seen and result["protected"] is False and result["protect_failed"] == 1
    assert result["verified"] and backup_utils.is_verified(job, stamp)
    assert is_read_only(stored(job, stamp, "a.txt"))             # the rest still marked
    assert not is_read_only(stored(job, stamp, "d.txt"))
    out = restore(job, stamp, tmp_path)
    assert out["ok"] and out["verified"]


def test_a_failure_to_mark_the_manifest_is_counted_and_not_fatal(tmp_path, monkeypatch):
    job, _ = make_job(tmp_path)
    real = fs_utils.set_read_only

    def refuse_manifest(path, read_only):
        if str(path).endswith(backup_utils.MANIFEST_SUFFIX):
            raise OSError("no")
        real(path, read_only)

    with monkeypatch.context() as patch:
        patch.setattr(versions, "set_read_only", refuse_manifest)
        result = run_ok(job)
    assert result["protected"] is False and result["protect_failed"] == 1
    assert not is_read_only(backup_utils.manifest_path(job, result["version"]))


def test_an_unexpected_error_while_marking_does_not_fail_the_run(tmp_path, monkeypatch):
    job, _ = make_job(tmp_path)
    from utils import runner

    def boom(job, stamp):
        raise RuntimeError("bug")

    monkeypatch.setattr(runner, "protect_version", boom)
    result = run_ok(job)
    assert result["protected"] is False and result["protect_failed"] > 0
    assert backup_utils.is_verified(job, result["version"])


def test_the_message_says_nothing_about_marking_when_it_worked(tmp_path):
    job, _ = make_job(tmp_path)
    result = run_ok(job)
    assert "read-only" not in result["message"]


def test_marking_leaves_the_jobs_file_and_run_log_shape_unchanged(tmp_path):
    from utils import runlog
    job, _ = make_job(tmp_path)
    jobs_store.save_job(job)
    result = run_ok(job)
    jobs_store.record_run(job["id"], result)
    stored_job = jobs_store.get_job(job["id"])
    assert not any("protect" in key for key in stored_job)
    line = runlog.read_runs(job["id"])[-1] if hasattr(runlog, "read_runs") else None
    if line is not None:
        assert tuple(line) == runlog.LINE_KEYS


# ---------------------------------------------------------------------------
# The copy itself, with read-only sources
# ---------------------------------------------------------------------------

def test_a_read_only_source_that_changes_during_the_copy_is_still_recopied(tmp_path, monkeypatch):
    """Card 55's second copy overwrites the first, which copystat made read-only."""
    job, source = make_job(tmp_path, read_only=["a.txt"])
    target = source / "a.txt"
    calls = {"n": 0}

    def change_once(src):
        if os.path.basename(src) == "a.txt" and calls["n"] == 0:
            calls["n"] += 1
            clear_ro(target)
            with open(_x(target), "ab") as handle:
                handle.write(b"+more")
            os.chmod(_x(target), stat.S_IREAD)

    monkeypatch.setattr(copying, "_after_read", change_once)
    result = run_ok(job)
    stamp = result["version"]
    assert calls["n"] == 1 and result["recopied"] == 1 and result["changed"] == 0
    with open(_x(stored(job, stamp, "a.txt")), "rb") as handle:
        assert handle.read() == b"alpha+more"
    assert is_read_only(stored(job, stamp, "a.txt"))
    assert [l for l in entries(job, stamp) if l.get("path") == "a.txt"][0].get("ro") is True
    out = restore(job, stamp, tmp_path)
    assert is_read_only(restored(out, "a.txt"))


def test_a_failed_copy_of_a_read_only_source_leaves_no_file_behind(tmp_path):
    """`_hash_copy` has already made the half-copy read-only (copystat) when
    the manifest callback fails; the clean-up must still delete it."""
    source = tmp_path / "s"
    source.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (source / name).write_bytes(b"x")
        os.chmod(source / name, stat.S_IREAD)

    def refuse(rel, size, mtime, sha, **flags):
        raise RuntimeError("manifest line refused")

    dst = tmp_path / "d"
    with pytest.raises(RuntimeError):
        copying.copy_tree(str(source), str(dst), on_copied=refuse)
    assert not [n for n in os.listdir(_x(dst))]


def test_on_copied_gets_ro_only_for_a_read_only_source(tmp_path):
    source = tmp_path / "s"
    source.mkdir()
    (source / "plain.txt").write_bytes(b"x")
    (source / "locked.txt").write_bytes(b"y")
    os.chmod(source / "locked.txt", stat.S_IREAD)
    seen = {}

    def callback(rel, size, mtime, sha, **flags):
        seen[rel] = flags

    copying.copy_tree(str(source), str(tmp_path / "d"), on_copied=callback)
    assert seen == {"plain.txt": {}, "locked.txt": {"ro": True}}
    plain_only = tmp_path / "p"
    plain_only.mkdir()
    (plain_only / "x.txt").write_bytes(b"x")
    four = []
    copying.copy_tree(str(plain_only), str(tmp_path / "d2"),     # a 4-argument callback still works
                      on_copied=lambda rel, size, mtime, sha: four.append(rel))
    assert four == ["x.txt"]


# ---------------------------------------------------------------------------
# Clean-up paths
# ---------------------------------------------------------------------------

def old_stamp_manifest(root, stamp, read_only=True):
    """An orphan manifest (no folder) for `stamp`, read-only as a protected one would be."""
    path = root / f"{stamp}{backup_utils.MANIFEST_SUFFIX}"
    path.write_text("{}\n", encoding="utf-8")
    if read_only:
        os.chmod(path, stat.S_IREAD)
    return path


def test_sweep_removes_a_marked_orphan_manifest(tmp_path):
    job, _ = make_job(tmp_path)
    root = tmp_path / "dst" / job["id"]
    root.mkdir(parents=True)
    orphan = old_stamp_manifest(root, "20200102-000000")
    result = backup_utils.sweep_stale(job, now=datetime(2026, 11, 13))
    assert result["removed"] == [orphan.name] and result["errors"] == []
    assert not orphan.exists()


def test_discard_removes_a_read_only_partial_and_manifest_tmp(tmp_path):
    partial = tmp_path / "p.partial"
    _write(partial / "x" / "y.txt", b"1")
    os.chmod(_x(partial / "x" / "y.txt"), stat.S_IREAD)
    tmp = tmp_path / "m.tmp"
    tmp.write_text("{}", encoding="utf-8")
    os.chmod(tmp, stat.S_IREAD)
    backup_utils._discard(str(partial), str(tmp))
    assert not partial.exists() and not tmp.exists()


def test_remove_file_clears_the_mark_and_propagates_other_failures(tmp_path):
    marked = tmp_path / "m.txt"
    marked.write_text("x", encoding="utf-8")
    os.chmod(marked, stat.S_IREAD)
    fs_utils.remove_file(str(marked))
    assert not marked.exists()
    with pytest.raises(FileNotFoundError):
        fs_utils.remove_file(str(tmp_path / "nope.txt"))
    folder = tmp_path / "dir"
    folder.mkdir()
    with pytest.raises(OSError):
        fs_utils.remove_file(str(folder))
    assert folder.is_dir()


@windows_only
def test_remove_file_puts_the_mark_back_when_the_delete_still_fails(tmp_path):
    path = tmp_path / "m.txt"
    path.write_text("x", encoding="utf-8")
    os.chmod(path, stat.S_IREAD)
    with locked_exclusively(path):
        with pytest.raises(OSError):
            fs_utils.remove_file(str(path))
    assert path.exists() and is_read_only(path)
    fs_utils.remove_file(str(path))                              # lock gone: deleted
    assert not path.exists()


@windows_only
def test_set_read_only_keeps_every_other_attribute(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")
    set_attributes(path, HIDDEN | SYSTEM | ARCHIVE)
    fs_utils.set_read_only(str(path), True)
    assert file_attributes(path) == HIDDEN | SYSTEM | ARCHIVE | READONLY
    fs_utils.set_read_only(str(path), False)
    assert file_attributes(path) == HIDDEN | SYSTEM | ARCHIVE
    set_attributes(path, 0x80)


def test_posix_mode_arithmetic_drops_every_write_bit_and_restores_only_the_owner_s():
    """The POSIX half, on any platform: the pure rule `set_read_only` applies."""
    assert fs_utils.mode_with_write(0o664, False) == 0o444
    assert fs_utils.mode_with_write(0o755, False) == 0o555
    assert fs_utils.mode_with_write(0o444, True) == 0o644
    assert fs_utils.mode_with_write(0o555, True) == 0o755
    assert fs_utils.mode_with_write(0o4755, False) == 0o4555      # other bits untouched


@windows_only
def test_a_failed_prune_of_a_locked_protected_version_leaves_everything_consistent(tmp_path):
    job, _ = make_job(tmp_path, retention=1)
    first = run_ok(job)["version"]
    second = run_ok(job)               # prunes `first`, nothing is locked yet
    assert second["pruned"] == [first]
    third_job = job
    # Lock a file in the NOW-oldest version, then run again: its deletion must
    # fail without touching the newest verified version or failing the run.
    older = second["version"]
    with locked_exclusively(stored(job, older, "nested/b.txt")):
        third = run_ok(third_job)
        assert third["ok"] and third["pruned"] == []
        newest = third["version"]
        assert os.path.isdir(_x(backup_utils.version_path(job, older)))   # not half-pretending to be gone
        # The failed delete must not leave a file in the surviving version writable.
        assert is_read_only(stored(job, older, "nested/b.txt"))
        assert all(is_read_only(p) for p in stored_files(job, older))
        assert backup_utils.is_verified(job, newest)
        assert all(is_read_only(p) for p in stored_files(job, newest))
        assert is_read_only(backup_utils.manifest_path(job, newest))
    fourth = run_ok(job)               # lock released: the leftover is pruned now
    assert older in fourth["pruned"]
    assert not os.path.exists(_x(backup_utils.version_path(job, older)))


# ---------------------------------------------------------------------------
# Neighbours: holds, readers, ordering
# ---------------------------------------------------------------------------

def tree_state(job, stamp):
    """`{relative path: (bytes, is_read_only, attributes)}` of a version."""
    root = backup_utils.version_path(job, stamp)
    state = {}
    for path in stored_files(job, stamp):
        with open(path, "rb") as handle:
            state[os.path.relpath(path, _x(root))] = (
                handle.read(), is_read_only(path),
                os.stat(path).st_file_attributes if os.name == "nt" else 0)
    return state


def test_the_pruning_hold_and_release_leave_protected_versions_untouched(tmp_path):
    big = {f"f{i}.txt": b"x" * (i + 1) for i in range(10)}
    job, source = make_job(tmp_path, files=big, retention=1)
    jobs_store.save_job(job)
    first = run_ok(job)["version"]
    for i in range(1, 10):
        os.remove(source / f"f{i}.txt")
    result = run_ok(job)
    assert result["pruning_held"] is True and result["baseline_stamp"] == first
    assert result["protected"] is True
    before = tree_state(job, first)
    jobs_store.record_run(job["id"], result)
    released = backup_utils.release_hold(job["id"])
    assert released["ok"] and released["released"]
    assert tree_state(job, first) == before
    assert all(v[1] for v in before.values())                  # and they are still marked


def test_readers_accept_manifests_with_and_without_the_new_keys(tmp_path):
    from utils import verify
    job, _ = make_job(tmp_path, read_only=["a.txt"])
    stamp = run_ok(job)["version"]
    summary = backup_utils.read_manifest_summary(job, stamp)
    assert summary["verified"] is True and summary["protected"] is True
    folder = backup_utils.version_path(job, stamp)
    checked, bad, _named = verify._verify_copy(folder, backup_utils.manifest_path(job, stamp),
                                               None, None, summary["files"])
    assert (checked, bad) == (summary["files"], 0)
    hashes, reason = versions._read_manifest_hashes(job, stamp)
    assert reason is None and len(hashes) == summary["files"]
    ro_paths = set()
    versions._read_manifest_hashes(job, stamp, ro_paths)
    assert ro_paths == {"a.txt"}
    make_old_version(job, stamp)
    old = backup_utils.read_manifest_summary(job, stamp)
    assert old["verified"] is True and "protected" not in old
    assert versions._read_manifest_hashes(job, stamp)[1] is None
    manifest = backup_utils.manifest_path(job, stamp)
    lines = entries(job, stamp)
    lines[-1]["protected"] = "yes"                              # junk is the same as absent
    with open(_x(manifest), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(json.dumps(l) for l in lines) + "\n")
    assert "protected" not in backup_utils.read_manifest_summary(job, stamp)


def test_version_ordering_is_unchanged_by_the_new_trailer_key(tmp_path):
    job, _ = make_job(tmp_path, retention=5)
    first = run_ok(job)["version"]
    second = run_ok(job)["version"]
    assert backup_utils.ordered_versions(job) == [first, second]
    assert backup_utils.latest_version(job) == second
    assert backup_utils.restore_candidates(job)["preferred"] == second


def test_an_off_mode_version_is_marked_too_and_restores(tmp_path):
    job, _ = make_job(tmp_path, read_only=["d.txt"], verify="off")
    result = run_ok(job)
    assert result["protected"] is True and result["verified"] is False
    assert all(is_read_only(p) for p in stored_files(job, result["version"]))
    out = restore(job, result["version"], tmp_path)
    assert is_read_only(restored(out, "d.txt")) and not is_read_only(restored(out, "a.txt"))


def test_a_quick_mode_version_is_marked_too(tmp_path):
    job, _ = make_job(tmp_path, verify="quick")
    result = run_ok(job)
    assert result["protected"] is True
    assert all(is_read_only(p) for p in stored_files(job, result["version"]))


def test_protect_version_marks_files_and_manifest_and_counts_them(tmp_path):
    job, _ = make_job(tmp_path)
    stamp = run_ok(job)["version"]
    clear_ro_tree(backup_utils.version_path(job, stamp))
    clear_ro(backup_utils.manifest_path(job, stamp))
    counts = versions.protect_version(job, stamp)
    assert counts == {"marked": len(FILES) + 1, "failed": 0}
    assert versions.protect_version(job, stamp) == counts       # idempotent


def test_protect_version_on_a_missing_version_counts_failures_and_does_not_raise(tmp_path):
    job, _ = make_job(tmp_path)
    counts = versions.protect_version(job, "20200101-000000")
    assert counts["marked"] == 0 and counts["failed"] >= 1
