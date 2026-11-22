# tests/test_precheck.py
# Card 104 (RQ8FygoS): `utils.precheck.precheck(job)` lists every file the
# target cannot hold -- path too long, too large for the filesystem, a name it
# cannot store, names that differ only by case, files that will be skipped --
# with a fix for each, and never writes anything.
#
# Injection points, all on the owning module (`utils.precheck`, never the
# facade): `volume_info` (the target's filesystem), `_list_dir` (sizes and
# listings that a real disk cannot easily produce: a 5 GB file, a name with a
# colon, two names differing only by case) and `_can_open`. Each injection is
# checked to have fired, so a test cannot pass because its fault never
# reached the code. Real files are used wherever they can be: the locked file
# is a real exclusive lock (CreateFileW, share mode 0).
#
# The card's 270-vs-250 path scenario predates card 56 (extended-length
# paths): the engine's real limit is now ~32,750, so the scenario runs with an
# injected `max_path=260`, and a separate test shows the default does NOT
# report a 270-character path.
import errno
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from utils import backup_utils, copying, jobs_store, precheck, runlog
from utils.copying import TargetWriteFailed

REPO = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows filesystem rules")
GiB = 1024 ** 3


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    """Keep this file's jobs off the real jobs file."""
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))


def _x(path) -> str:
    """`path` in extended-length form, built here rather than by the code
    under test, so a fixture can create names past 260 characters."""
    full = os.path.abspath(str(path))
    return "\\\\?\\" + full if os.name == "nt" else full


@pytest.fixture
def fs(monkeypatch):
    """Inject a target filesystem: `fs("FAT32")`. Returns the list of paths
    `volume_info` was asked about, so a test can prove the injection fired."""
    calls = []

    def use(name, max_component=255):
        def fake(path):
            calls.append(path)
            return {"fs": name, "max_component": max_component}
        monkeypatch.setattr(precheck, "volume_info", fake)
        return calls
    return use


def make_job(tmp_path, files=None, target=None):
    """A job over `tmp_path/src` holding `files` ({relative path: bytes})."""
    source = tmp_path / "src"
    source.mkdir(exist_ok=True)
    files = {"a.txt": b"a", "sub/b.txt": b"bb"} if files is None else files
    for rel, data in files.items():
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    target = target or tmp_path / "dst"
    return backup_utils.new_job("Pre", str(source), str(target), schedule="manual")


def fake_sizes(monkeypatch, sizes):
    """Wrap the real `_list_dir`, overriding the size of files named in
    `sizes`; returns the set of names actually overridden."""
    real = precheck._list_dir
    hit = set()

    def listing(path):
        out = []
        for name, is_dir, size in real(path):
            if name in sizes:
                hit.add(name)
                size = sizes[name]
            out.append((name, is_dir, size))
        return out
    monkeypatch.setattr(precheck, "_list_dir", listing)
    return hit


def fake_tree(monkeypatch, root, tree):
    """Replace the walk with an invented `tree` under real folder `root`.

    `tree` maps a name to a size (a file) or to a dict (a folder). Opening is
    stubbed to succeed. Returns the folders listed, to prove the walk used it.
    """
    listings = {}

    def build(folder, node):
        listings[folder] = sorted((name, isinstance(child, dict),
                                   0 if isinstance(child, dict) else child)
                                  for name, child in node.items())
        for name, child in node.items():
            if isinstance(child, dict):
                build(os.path.join(folder, name), child)
    build(str(root), tree)
    listed = []

    def listing(path):
        listed.append(path)
        return listings[path]
    monkeypatch.setattr(precheck, "_list_dir", listing)
    monkeypatch.setattr(precheck, "_can_open", lambda path: None)
    return listed


def kinds(report):
    """`[(kind, rel)]` of a report's problems."""
    return [(p["kind"], p["rel"]) for p in report["problems"]]


def snapshot(root):
    """Every path and file's bytes under `root` (empty if it does not exist)."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            found[os.path.relpath(os.path.join(dirpath, name), root)] = None
        for name in filenames:
            full = os.path.join(dirpath, name)
            found[os.path.relpath(full, root)] = Path(full).read_bytes()
    return found


# --- file too large ---------------------------------------------------------------

def test_a_5gb_file_is_too_large_for_fat32_and_clean_on_ntfs(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {"big.iso": b"x", "small.txt": b"y"})
    hit = fake_sizes(monkeypatch, {"big.iso": 5 * GiB})

    calls = fs("FAT32")
    fat = precheck.precheck(job)
    assert calls and "big.iso" in hit                     # both injections fired
    assert fat["ok"] is False and fat["fs"] == "FAT32"
    (problem,) = fat["problems"]
    assert (problem["kind"], problem["rel"], problem["blocking"]) == (
        "file_too_large", "big.iso", True)
    assert problem["path"] == os.path.join(job["source"], "big.iso")
    assert "NTFS or exFAT" in problem["suggestion"]
    assert "5,368,709,120 bytes" in problem["detail"]

    fs("NTFS")
    ntfs = precheck.precheck(job)
    assert ntfs["ok"] is True and ntfs["problems"] == [] and ntfs["scanned"] == 2
    fs("exFAT")
    assert precheck.precheck(job)["problems"] == []


def test_the_fat32_limit_is_4_gib_minus_one_byte(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {"at_limit.bin": b"x", "over.bin": b"y"})
    fake_sizes(monkeypatch, {"at_limit.bin": 4 * GiB - 1, "over.bin": 4 * GiB})
    fs("FAT32")
    report = precheck.precheck(job)
    assert kinds(report) == [("file_too_large", "over.bin")]


def test_an_unknown_filesystem_applies_no_size_or_name_rules(tmp_path, monkeypatch, fs):
    fake_tree(monkeypatch, make_job(tmp_path, {})["source"],
              {"big.iso": 5 * GiB, "a:b.txt": 1, "Same": 1, "same": 2})
    fs("SOMEFS")
    report = precheck.precheck(make_job(tmp_path, {}))
    assert report["fs"] == "unknown" and report["problems"] == []


# --- path too long ----------------------------------------------------------------

def copy_path_name(job, length, char="n"):
    """A file name whose copy path (`<target>\\<id>\\<stamp>.partial\\name`)
    is exactly `length` characters."""
    base = os.path.join(os.path.abspath(job["target"]), job["id"],
                        "20000101-000000.partial")
    size = length - len(base) - 1
    assert 0 < size <= 255, "tmp_path is too long for this geometry"
    return char * (size - 4) + ".txt"


def test_a_270_character_copy_path_is_reported_and_250_is_not_at_a_260_limit(tmp_path):
    job = make_job(tmp_path, {})
    long_name, short_name = copy_path_name(job, 270), copy_path_name(job, 250)
    exact, over = copy_path_name(job, 260), copy_path_name(job, 261)
    for name in (long_name, short_name, exact, over):
        with open(_x(Path(job["source"]) / name), "wb") as handle:
            handle.write(b"x")

    report = precheck.precheck(job, max_path=260)
    assert sorted(kinds(report)) == sorted([("path_too_long", long_name),
                                            ("path_too_long", over)])
    problem = next(p for p in report["problems"] if p["rel"] == long_name)
    assert "would be 270 characters" in problem["detail"]
    assert "move the folder nearer the drive root" in problem["suggestion"]
    assert report["ok"] is False and report["scanned"] == 4


def test_the_default_limit_is_the_extended_length_one_not_260(tmp_path):
    # Card 56: the engine writes through `\\?\` paths, so 270 is fine now.
    job = make_job(tmp_path, {})
    name = copy_path_name(job, 270)
    with open(_x(Path(job["source"]) / name), "wb") as handle:
        handle.write(b"x")
    if os.name == "nt":
        assert precheck.MAX_COPY_PATH == 32_750
    assert precheck.precheck(job)["problems"] == []


@windows_only
def test_past_the_extended_limit_a_folder_is_reported_once_not_per_file(tmp_path, monkeypatch):
    job = make_job(tmp_path, {})
    # Nested 250-character folders until the copy path passes 32,750; the
    # deepest holds two files, which must not be reported again.
    node = {"f1.txt": 1, "f2.txt": 1}
    for level in range(140):
        node = {f"{level:03d}" + "d" * 247: node}
    fake_tree(monkeypatch, job["source"], node)
    report = precheck.precheck(job)
    (problem,) = report["problems"]
    assert problem["kind"] == "path_too_long"
    assert problem["rel"].count("/") < 139          # a folder, not one of the files
    assert int(problem["detail"].split("would be ")[1].split(" ")[0]) > 32_750
    assert report["scanned"] == 2


# --- names --------------------------------------------------------------------------

def test_a_name_over_255_characters_is_reported_and_255_is_not(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {})
    fake_tree(monkeypatch, job["source"], {"a" * 255: 1, "b" * 256: 1})
    fs("NTFS")
    report = precheck.precheck(job)
    assert kinds(report) == [("bad_name", "b" * 256)]
    assert "256 characters" in report["problems"][0]["detail"]


@pytest.mark.parametrize("name, offender", [
    ("report.", "ends in a dot"), ("draft ", "ends in a space"),
    ("a:b.txt", "contains :"), ('what?.txt', "contains ?"), ("tab\x01.txt", "\\x01"),
])
def test_names_a_fat_drive_cannot_store_are_reported_for_fat_not_ntfs(
        tmp_path, monkeypatch, fs, name, offender):
    job = make_job(tmp_path, {})
    listed = fake_tree(monkeypatch, job["source"], {name: 1, "fine.txt": 1})
    for fat in ("FAT32", "exFAT", "FAT"):
        fs(fat)
        report = precheck.precheck(job)
        assert kinds(report) == [("bad_name", name)], fat
        assert offender in report["problems"][0]["detail"]
        assert report["ok"] is False
    fs("NTFS")
    assert precheck.precheck(job)["problems"] == []
    assert listed                                           # the fake walk was used


@windows_only
def test_a_reserved_device_name_is_a_warning_not_a_failure(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {})
    fake_tree(monkeypatch, job["source"], {"CON": 1, "nul.txt": 1, "console.txt": 1})
    fs("NTFS")
    report = precheck.precheck(job)
    assert kinds(report) == [("bad_name", "CON"), ("bad_name", "nul.txt")]
    assert all(p["blocking"] is False for p in report["problems"])
    assert report["ok"] is True


def test_names_differing_only_by_case_collide_on_a_case_insensitive_target(
        tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {})
    fake_tree(monkeypatch, job["source"], {
        "Report.txt": 1, "a.txt": 1, "report.TXT": 2,
        "one": {"same.txt": 1}, "two": {"same.txt": 1},     # different folders: fine
    })
    fs("NTFS")
    report = precheck.precheck(job)
    assert kinds(report) == [("case_collision", "report.TXT")]
    assert '"report.TXT" and "Report.txt"' in report["problems"][0]["detail"]
    assert report["ok"] is False
    fs("FAT32")
    assert kinds(precheck.precheck(job)) == [("case_collision", "report.TXT")]


# --- unreadable ---------------------------------------------------------------------

@windows_only
def test_a_locked_file_is_reported_as_will_be_skipped_not_as_a_failure(tmp_path):
    import ctypes
    from ctypes import wintypes
    job = make_job(tmp_path, {"locked.db": b"data", "free.txt": b"ok"})
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE)
    handle = kernel32.CreateFileW(str(Path(job["source"]) / "locked.db"),
                                  0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share 0
    if handle in (None, wintypes.HANDLE(-1).value):
        pytest.skip(f"could not take an exclusive lock: {ctypes.get_last_error()}")
    try:
        with pytest.raises(PermissionError):                # positive control
            open(Path(job["source"]) / "locked.db", "rb")
        report = precheck.precheck(job)
    finally:
        kernel32.CloseHandle(handle)
    (problem,) = report["problems"]
    assert (problem["kind"], problem["rel"], problem["blocking"]) == (
        "unreadable", "locked.db", False)
    assert "will be skipped" in problem["detail"]
    assert report["ok"] is True and report["blocking"] == 0
    assert precheck.precheck(job)["problems"] == []         # released: clean


def test_check_open_false_skips_the_open(tmp_path, monkeypatch):
    job = make_job(tmp_path, {"a.txt": b"a"})
    monkeypatch.setattr(precheck, "_can_open", lambda path: PermissionError(13, "locked"))
    assert kinds(precheck.precheck(job)) == [("unreadable", "a.txt")]
    assert precheck.precheck(job, check_open=False)["problems"] == []


# --- clean tree, writes nothing -------------------------------------------------

def test_a_clean_tree_is_ok_and_writes_nothing_to_an_existing_target(tmp_path):
    job = make_job(tmp_path)
    target = Path(job["target"])
    (target / job["id"] / "20260101-000000").mkdir(parents=True)
    (target / "other.txt").write_bytes(b"keep")
    before_target, before_source = snapshot(target), snapshot(job["source"])

    report = precheck.precheck(job)

    assert report["ok"] is True and report["problems"] == []
    assert report["scanned"] == 2 and report["aborted"] is False
    assert isinstance(report["took"], float)
    assert set(report) >= {"ok", "scanned", "problems", "took"}
    assert snapshot(target) == before_target
    assert snapshot(job["source"]) == before_source


def test_a_target_that_does_not_exist_uses_its_nearest_ancestor_and_is_not_created(
        tmp_path, fs):
    job = make_job(tmp_path, target=tmp_path / "drive" / "not" / "yet")
    (tmp_path / "drive").mkdir()
    calls = fs("FAT32")
    report = precheck.precheck(job)
    assert calls == [str(tmp_path / "drive")]
    assert report["fs"] == "FAT32" and report["ok"] is True
    assert not (tmp_path / "drive" / "not").exists()


def test_a_missing_target_drive_and_a_missing_source_are_blocking(tmp_path, monkeypatch):
    job = make_job(tmp_path)
    monkeypatch.setattr(precheck, "nearest_existing_ancestor", lambda path: None)
    report = precheck.precheck(job)
    assert kinds(report) == [("target_unavailable", "")]
    assert report["ok"] is False

    job["source"] = str(tmp_path / "gone")
    report = precheck.precheck(job)
    assert [p["kind"] for p in report["problems"]] == ["target_unavailable",
                                                       "source_unavailable"]


@windows_only
def test_volume_info_reads_the_real_volume():
    info = precheck.volume_info(os.getcwd())
    assert info["fs"] != "unknown" and info["max_component"] == 255


# --- progress and abort ---------------------------------------------------------

def test_progress_counts_every_file_once(tmp_path):
    job = make_job(tmp_path, {f"d{i % 3}/f{i}.txt": b"x" for i in range(10)})
    seen = []
    report = precheck.precheck(job, progress=seen.append)
    assert seen == list(range(1, 11)) and report["scanned"] == 10


def test_an_abort_stops_the_walk_and_is_never_ok(tmp_path):
    job = make_job(tmp_path, {f"f{i}.txt": b"x" for i in range(50)})
    calls = []

    def abort():
        calls.append(1)
        return len(calls) > 5
    report = precheck.precheck(job, should_abort=abort)
    assert report["aborted"] is True and report["ok"] is False
    assert 0 < report["scanned"] < 50


def test_the_report_caps_its_list_but_counts_every_problem(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {})
    fake_tree(monkeypatch, job["source"], {f"bad{i}.": 1 for i in range(precheck.PROBLEMS_MAX + 7)})
    fs("FAT32")
    report = precheck.precheck(job)
    assert len(report["problems"]) == precheck.PROBLEMS_MAX
    assert report["problems_more"] == 7
    assert report["counts"] == {"bad_name": precheck.PROBLEMS_MAX + 7}


# --- last_precheck on the job ---------------------------------------------------

def test_last_precheck_round_trips_and_keeps_every_other_field(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {f"f{i}.iso": b"x" for i in range(7)})
    job["future_field"] = {"kept": True}
    backup_utils.save_job(job)
    fake_sizes(monkeypatch, {f"f{i}.iso": 5 * GiB for i in range(7)})
    fs("FAT32")
    report = precheck.precheck(job)

    when = datetime(2026, 11, 22, 9, 30)
    stored = backup_utils.record_precheck(job["id"], precheck.summary(report, when))

    loaded = backup_utils.get_job(job["id"])
    assert loaded == stored
    assert loaded["future_field"] == {"kept": True}
    last = loaded["last_precheck"]
    assert last["time"] == "2026-11-22T09:30:00"
    assert (last["scanned"], last["problems"], last["blocking"]) == (7, 7, 7)
    assert last["counts"] == {"file_too_large": 7} and last["fs"] == "FAT32"
    assert last["sample"] == [f"f{i}.iso" for i in range(5)]     # first five only

    # A run recorded afterwards keeps it; an older file without it loads fine.
    backup_utils.record_run(job["id"], {"ok": True, "message": "done", "files": 7})
    assert backup_utils.get_job(job["id"])["last_precheck"] == last
    assert backup_utils.record_precheck("no-such-id", last) is None
    old = make_job(tmp_path)
    assert "last_precheck" not in old
    backup_utils.save_job(old)
    assert "last_precheck" not in backup_utils.get_job(old["id"])


# --- (d) a failed run names every offender ----------------------------------------

def test_a_path_too_long_failure_lists_every_file_at_least_that_long(tmp_path, monkeypatch):
    job = make_job(tmp_path, {})
    long_a, long_b = copy_path_name(job, 240, "a"), copy_path_name(job, 245, "b")
    short = copy_path_name(job, 200, "s")
    for name in (long_a, long_b, short):
        with open(_x(Path(job["source"]) / name), "wb") as handle:
            handle.write(b"x")
    real = copying._hash_copy
    refused = []

    def refuse_long(src, dst):
        """The drive refuses any copy path of 240 characters or more."""
        if len(dst) >= 240:
            refused.append(dst)
            raise TargetWriteFailed(dst, OSError(errno.ENAMETOOLONG, "File name too long"))
        return real(src, dst)
    monkeypatch.setattr(copying, "_hash_copy", refuse_long)

    result = backup_utils.run_job(job)

    assert refused and result["ok"] is False and result["cause"] == "path_too_long"
    assert sorted(result["offenders"]) == sorted([long_a, long_b])
    assert long_a in result["message"] and long_b in result["message"]
    assert short not in result["message"]
    (line,) = runlog.read_runs(job["id"])
    assert sorted(line["offenders"]) == sorted([long_a, long_b])
    assert line["cause"] == "path_too_long"


def test_a_file_too_large_failure_names_the_file(tmp_path, monkeypatch, fs):
    job = make_job(tmp_path, {"movie.mkv": b"x" * 10, "notes.txt": b"n"})
    fake_sizes(monkeypatch, {"movie.mkv": 6 * GiB})
    fs("FAT32")

    def too_large(src, dst):
        raise TargetWriteFailed(dst, OSError(errno.EFBIG, "File too large"))
    monkeypatch.setattr(copying, "_hash_copy", too_large)

    result = backup_utils.run_job(job)
    assert result["cause"] == "file_too_large"
    assert result["offenders"] == ["movie.mkv"]
    assert "Files the backup drive cannot hold: movie.mkv." in result["message"]


def test_other_failures_carry_no_offenders(tmp_path, monkeypatch):
    job = make_job(tmp_path)

    def full(src, dst):
        raise TargetWriteFailed(dst, OSError(errno.ENOSPC, "No space left"))
    monkeypatch.setattr(copying, "_hash_copy", full)
    result = backup_utils.run_job(job)
    assert result["cause"] == "no_space" and "offenders" not in result
    assert runlog.read_runs(job["id"])[0]["offenders"] == []


# --- headless entry points --------------------------------------------------------

@pytest.fixture
def cli():
    """main.py's functions (imports PySide6 but builds no QApplication)."""
    import main
    return main


def test_precheck_job_cli_exits_0_for_a_clean_job_and_records_it(tmp_path, cli, capsys):
    job = make_job(tmp_path)
    backup_utils.save_job(job)
    assert cli.precheck_job_cli(job["id"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("[OK] Checked 2 file(s)")
    last = backup_utils.get_job(job["id"])["last_precheck"]
    assert last["problems"] == 0 and last["ok"] is True
    assert not Path(job["target"]).exists()


def test_precheck_job_cli_exits_5_and_lists_the_offenders(tmp_path, cli, capsys,
                                                         monkeypatch, fs):
    job = make_job(tmp_path, {"big.iso": b"x", "bad.": b"y", "ok.txt": b"z"})
    backup_utils.save_job(job)
    fake_sizes(monkeypatch, {"big.iso": 5 * GiB})
    fs("FAT32")
    # "bad." cannot be made on NTFS through Win32, so it is injected too.
    real = precheck._list_dir
    monkeypatch.setattr(precheck, "_list_dir", lambda path: [
        ("bad." if name == "bad" else name, d, s) for name, d, s in real(path)])
    monkeypatch.setattr(precheck, "_can_open", lambda path: None)

    assert cli.precheck_job_cli(job["id"]) == 5
    out = capsys.readouterr().out
    assert out.startswith("[PROBLEMS] Checked 3 file(s): 2 problem(s), 2 of which")
    assert os.path.join(job["source"], "big.iso") in out
    assert os.path.join(job["source"], "bad.") in out
    assert "Fix: Leave this file out, or back up to an NTFS or exFAT drive." in out
    last = backup_utils.get_job(job["id"])["last_precheck"]
    assert last["counts"] == {"file_too_large": 1, "bad_name": 1}


def test_precheck_job_cli_exits_5_for_a_skip_only_report(tmp_path, cli, capsys, monkeypatch):
    job = make_job(tmp_path, {"a.txt": b"a"})
    backup_utils.save_job(job)
    monkeypatch.setattr(precheck, "_can_open", lambda path: PermissionError(13, "locked"))
    assert cli.precheck_job_cli(job["id"]) == 5
    assert "will be skipped, not a failure" in capsys.readouterr().out
    assert backup_utils.get_job(job["id"])["last_precheck"]["ok"] is True


def test_precheck_job_cli_exits_2_for_an_unknown_job_and_3_for_a_damaged_file(
        tmp_path, cli, capsys):
    backup_utils.save_jobs([])
    assert cli.precheck_job_cli("nope") == 2
    Path(jobs_store.JOBS_FILE).write_text('[{"id": ', encoding="utf-8")
    before = Path(jobs_store.JOBS_FILE).read_bytes()
    assert cli.precheck_job_cli("nope") == 3
    assert "damaged" in capsys.readouterr().err
    assert Path(jobs_store.JOBS_FILE).read_bytes() == before


def test_a_first_scheduled_run_is_prechecked_but_never_blocked(tmp_path, cli, capsys,
                                                               monkeypatch, fs):
    job = make_job(tmp_path, {"big.iso": b"x", "ok.txt": b"y"})
    backup_utils.save_job(job)
    fake_sizes(monkeypatch, {"big.iso": 5 * GiB})
    fs("FAT32")

    assert cli.run_backup_job_cli(job["id"]) == 0          # the real copy is tiny
    err = capsys.readouterr().err
    assert "[WARN] The pre-check found 1 problem(s)" in err and "big.iso" in err
    stored = backup_utils.get_job(job["id"])
    assert stored["last_precheck"]["counts"] == {"file_too_large": 1}
    assert stored["last_result"] == "ok"

    # Only the first run: with last_precheck present no second check runs.
    monkeypatch.setattr(precheck, "precheck", lambda *a, **k: pytest.fail("rechecked"))
    assert cli.run_backup_job_cli(job["id"]) == 0


def test_the_real_cli_exits_5_for_a_locked_file_and_writes_nothing_to_the_target(tmp_path):
    """End to end: `python main.py --precheck-job <id>` in its own process,
    with APPDATA pointed at a fresh folder."""
    if os.name != "nt":
        pytest.skip("exclusive lock via CreateFileW")
    import ctypes
    from ctypes import wintypes
    appdata = tmp_path / "appdata"
    data = appdata / "Safekeep"
    data.mkdir(parents=True)
    job = make_job(tmp_path, {"locked.db": b"x", "free.txt": b"y"})
    (data / "backup_jobs.json").write_text(json.dumps([job]), encoding="utf-8")
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE)
    handle = kernel32.CreateFileW(str(Path(job["source"]) / "locked.db"),
                                  0x80000000, 0, None, 3, 0x80, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        pytest.skip("could not take an exclusive lock")
    try:
        proc = subprocess.run([sys.executable, str(REPO / "main.py"), "--precheck-job",
                               job["id"]], cwd=str(REPO), env=env, capture_output=True,
                              text=True, timeout=120)
    finally:
        kernel32.CloseHandle(handle)

    assert proc.returncode == 5, (proc.stdout, proc.stderr)
    assert "[PROBLEMS] Checked 2 file(s): 1 problem(s), 0 of which" in proc.stdout
    assert str(Path(job["source"]) / "locked.db") in proc.stdout
    assert "will be skipped" in proc.stdout
    stored = json.loads((data / "backup_jobs.json").read_text(encoding="utf-8"))[0]
    assert stored["last_precheck"]["counts"] == {"unreadable": 1}
    assert not Path(job["target"]).exists()

    usage = subprocess.run([sys.executable, str(REPO / "main.py"), "--precheck-job"],
                           cwd=str(REPO), env=env, capture_output=True, text=True,
                           timeout=120)
    assert usage.returncode == 2 and "--precheck-job <job id>" in usage.stdout


# --- performance -------------------------------------------------------------------

def test_3000_files_are_checked_in_a_few_seconds(tmp_path):
    job = make_job(tmp_path, {f"d{i % 30}/f{i}.txt": b"x" for i in range(3000)})
    report = precheck.precheck(job)
    assert report["scanned"] == 3000 and report["problems"] == []
    assert report["took"] < 10, report["took"]
    print(f"\n3,000-file pre-check took {report['took']:.2f} s")


# --- the UI ---------------------------------------------------------------------------

@pytest.fixture
def qt_app():
    """An offscreen QApplication; no QSettings is constructed by these tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def a_report(tmp_path):
    """A report with one blocking and one skip-only problem in real folders."""
    (tmp_path / "src" / "sub").mkdir(parents=True, exist_ok=True)
    return {"ok": False, "scanned": 9, "fs": "FAT32", "blocking": 1, "problems_more": 0,
            "counts": {"file_too_large": 1, "unreadable": 1},
            "problems": [
                {"path": str(tmp_path / "src" / "sub" / "big.iso"), "rel": "sub/big.iso",
                 "kind": "file_too_large", "detail": "It is big.",
                 "suggestion": precheck.SUGGESTIONS["file_too_large"], "blocking": True},
                {"path": str(tmp_path / "src" / "locked.db"), "rel": "locked.db",
                 "kind": "unreadable", "detail": "Locked.",
                 "suggestion": precheck.SUGGESTIONS["unreadable"], "blocking": False}]}


def test_the_report_dialog_lists_each_problem_with_an_open_folder_button(
        tmp_path, qt_app, monkeypatch):
    from PySide6 import QtCore, QtWidgets
    from tabs.backup_tab import PrecheckDialog
    opened = []
    monkeypatch.setattr(PrecheckDialog, "open_url", staticmethod(opened.append))

    dialog = PrecheckDialog(None, {"name": "Photos"}, a_report(tmp_path), "Back up anyway")

    labels = {w.text(): w.objectName() for w in dialog.findChildren(QtWidgets.QLabel)}
    assert labels["File too large for this drive"] == "StatusError"
    assert labels["Cannot be read right now - will be skipped"] == "StatusWarn"
    assert len(dialog.folder_buttons) == 2
    for button in dialog.folder_buttons:
        assert button.isEnabled() and button.focusPolicy() != QtCore.Qt.NoFocus
        assert button.accessibleName().startswith("Open the folder holding")
    dialog.folder_buttons[0].click()
    assert opened == [QtCore.QUrl.fromLocalFile(str(tmp_path / "src" / "sub"))]
    # Asking mode: the safe choice is the default, "Back up anyway" accepts.
    assert dialog.close_button.text() == "Cancel" and dialog.close_button.isDefault()
    dialog.confirm_button.click()
    assert dialog.result() == QtWidgets.QDialog.Accepted

    info = PrecheckDialog(None, {"name": "Photos"}, a_report(tmp_path))
    assert info.confirm_button is None and info.close_button.text() == "Close"


@pytest.fixture
def tab(qt_app, monkeypatch):
    """A BackupTab whose dialog and recording are observed, not real."""
    from tabs import backup_tab
    shown, recorded = [], []

    class FakeDialog:
        """Stands in for PrecheckDialog; `answer` is what exec() returns."""
        answer = None

        def __init__(self, parent, job, report, run_text=""):
            shown.append(run_text)

        def exec(self):
            return FakeDialog.answer
    monkeypatch.setattr(backup_tab, "PrecheckDialog", FakeDialog)
    monkeypatch.setattr(backup_utils, "record_precheck",
                        lambda job_id, summary: recorded.append(job_id))
    widget = backup_tab.BackupTab()
    widget.shown, widget.recorded, widget.FakeDialog = shown, recorded, FakeDialog
    return widget


def test_a_clean_precheck_goes_straight_on_with_no_dialog(tab):
    went_on = []
    tab._finish_precheck({"id": "j1", "name": "J"},
                         {"ok": True, "scanned": 3, "problems": [], "counts": {},
                          "blocking": 0, "aborted": False, "fs": "NTFS"},
                         went_on.append, "Back up anyway")
    assert len(went_on) == 1 and tab.shown == [] and tab.recorded == ["j1"]


def test_problems_ask_before_back_up_now_and_never_block_it(tab, tmp_path):
    from PySide6 import QtWidgets
    report = dict(a_report(tmp_path), aborted=False)
    went_on = []

    tab.FakeDialog.answer = QtWidgets.QDialog.Rejected
    tab._finish_precheck({"id": "j1", "name": "J"}, report, went_on.append, "Back up anyway")
    assert went_on == [] and tab.shown == ["Back up anyway"]
    assert "not started" in tab.backup_status.text()

    tab.FakeDialog.answer = QtWidgets.QDialog.Accepted
    tab._finish_precheck({"id": "j1", "name": "J"}, report, went_on.append, "Back up anyway")
    assert len(went_on) == 1

    # After Add/Edit (no run_text) the dialog only informs; closing goes on.
    tab.FakeDialog.answer = QtWidgets.QDialog.Rejected
    tab._finish_precheck({"id": "j1", "name": "J"}, report, went_on.append, "")
    assert len(went_on) == 2


def test_a_precheck_that_could_not_run_does_not_stop_the_backup(tab):
    went_on = []
    tab._finish_precheck({"id": "j1", "name": "J"},
                         {"ok": False, "message": "Unexpected failure: boom"},
                         went_on.append, "Back up anyway")
    assert len(went_on) == 1 and tab.recorded == []
    assert "could not run" in tab.backup_status.text()


def capture_workers(tab, monkeypatch):
    """Record `_start_backup` calls instead of starting threads."""
    started = []
    monkeypatch.setattr(tab, "_start_backup",
                        lambda status, work, done, phases=None: started.append(
                            (status, work, done)))
    return started


def run_worker(work):
    """Run a captured worker's callable on this thread, as the worker would."""
    return work(lambda done, total: None, lambda: False, lambda phase: None)


def test_back_up_now_prechecks_off_the_ui_thread_then_runs(tab, tmp_path, monkeypatch):
    job = make_job(tmp_path)
    started = capture_workers(tab, monkeypatch)
    tab.run_backup_job(job)
    (status, work, done), = started
    assert status.startswith('Checking "Pre" against its backup drive')
    report = run_worker(work)                  # what the worker thread runs
    assert report["ok"] is True and report["scanned"] == 2
    done(report)
    assert started[1][0].startswith('Backing up "Pre"')   # clean: straight on
    assert tab.shown == []


def test_saving_a_job_prechecks_it_then_remeasures(tab, tmp_path, monkeypatch):
    from utils import scheduler

    def no_scheduler(*args, **kwargs):
        raise AssertionError("the real scheduler must not run in a test")
    monkeypatch.setattr(scheduler, "_run", no_scheduler)
    monkeypatch.setattr(backup_utils, "sync_task", lambda job: (True, ""))
    remeasured = []
    monkeypatch.setattr(tab, "refresh_changes", lambda: remeasured.append(1))
    job = make_job(tmp_path)
    started = capture_workers(tab, monkeypatch)

    tab._save_and_schedule(job, "Added")

    (status, work, done), = started
    assert status.startswith("Checking")
    assert tab._backup_notice.startswith('Added job "Pre".')   # carried across
    done(run_worker(work))
    assert remeasured == [1] and tab.recorded == [job["id"]] and tab.shown == []
