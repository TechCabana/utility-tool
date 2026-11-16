# tests/test_runlog.py
# Card 52: every run appends ONE line to a per-job log (utils/runlog.py), the
# log is capped, and a logging failure can never change a run. Real trees under
# tmp_path; faults are patched on the module that owns them (`utils.copying`,
# `utils.runner`, `utils.runlog`), never on the backup_utils facade. The logs
# folder itself is redirected for every test by tests/conftest.py.
import errno
import inspect
import json
import os
import subprocess
import sys
from datetime import datetime as real_datetime, timedelta
from pathlib import Path

import pytest

from utils import backup_utils, copying, jobs_store, locks, runlog, runner

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(tmp_path / "backup_jobs.json"))
    yield


@pytest.fixture(autouse=True)
def ticking_clock(monkeypatch):
    """A `datetime.now()` in runner that advances one second a call, so runs get
    distinct version stamps without the claim loop sleeping."""
    start = real_datetime.now().replace(microsecond=0)
    ticks = iter(range(10 ** 6))

    class Tick(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=next(ticks))

    monkeypatch.setattr(runner, "datetime", Tick)


def set_files(source, count):
    """Make `source` hold exactly `count` small files."""
    source.mkdir(parents=True, exist_ok=True)
    for old in source.iterdir():
        if old.is_file():
            old.unlink()
    for i in range(count):
        (source / f"file{i}.txt").write_text(f"secret contents {i}", encoding="utf-8")


@pytest.fixture
def make_job(tmp_path):
    """Build a saved job over `tmp_path/src` (10 files, plus a `sub` folder)."""
    def build(files=10, retention=2):
        source = tmp_path / "src"
        set_files(source, files)
        (source / "sub").mkdir(exist_ok=True)
        (source / "sub" / "deep.txt").write_text("deep secret", encoding="utf-8")
        target = tmp_path / "dst"
        target.mkdir(exist_ok=True)
        job = backup_utils.new_job("Log job", str(source), str(target),
                                   schedule="manual", retention=retention)
        backup_utils.save_job(job)
        return job, source
    return build


def raw_lines(job_id):
    """The log's lines exactly as written ([] if there is no log yet)."""
    path = runlog.log_path(job_id)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return handle.read().splitlines()


def only_line(job_id) -> dict:
    """Assert the job's log holds exactly one well-formed line; return it."""
    lines = raw_lines(job_id)
    assert len(lines) == 1, lines
    entry = json.loads(lines[0])
    assert tuple(entry) == runlog.LINE_KEYS      # exact keys, in the documented order
    assert entry["v"] == 1 and entry["job"] == job_id
    for stamp in (entry["start"], entry["end"]):
        real_datetime.fromisoformat(stamp)       # ISO-8601, local, seconds
    return entry


def lock_files(monkeypatch, names):
    """Make opening any source file called one of `names` fail for reading."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "rb" and os.path.basename(str(path)) in names:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)


def fail_target_writes(monkeypatch, root):
    """Make every destination file opened for writing under `root` raise ENOSPC."""
    real_open = open
    root = os.path.normcase(os.path.abspath(str(root)))

    def fake_open(path, mode="r", *args, **kwargs):
        if mode == "wb" and os.path.normcase(os.path.abspath(str(path))).startswith(root):
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(copying, "open", fake_open, raising=False)


# --- one line per run, whatever the outcome -------------------------------------

def test_a_successful_run_appends_one_line(make_job):
    job, _ = make_job()
    result = backup_utils.run_job(job)
    assert result["ok"]

    entry = only_line(job["id"])
    assert entry["outcome"] == "ok" and entry["trigger"] == "manual"
    assert entry["name"] == "Log job"
    assert entry["files"] == result["files"] == 11
    assert entry["bytes"] == result["bytes"] > 0
    assert (entry["skipped"], entry["skipped_paths"], entry["skipped_more"]) == (0, [], 0)
    assert entry["pruned"] == 0 and entry["version"] == result["version"]
    assert entry["verify_mode"] == "full" and entry["cause"] is None
    assert entry["message"] == result["message"] and entry["pruning_held"] is False


def test_a_failed_run_appends_one_line_with_its_cause(make_job, tmp_path, monkeypatch):
    job, _ = make_job()
    fail_target_writes(monkeypatch, tmp_path / "dst")
    result = backup_utils.run_job(job)
    assert not result["ok"]

    entry = only_line(job["id"])
    assert entry["outcome"] == "failed" and entry["cause"] == result["cause"] == "no_space"
    assert entry["version"] is None and entry["message"] == result["message"]
    assert (entry["files"], entry["bytes"], entry["skipped"]) == (0, 0, 0)


def test_a_preflight_refusal_is_logged_as_failed(make_job, tmp_path):
    job, source = make_job()
    job["source"] = str(tmp_path / "gone")
    result = backup_utils.run_job(job)
    assert not result["ok"]
    entry = only_line(job["id"])
    assert entry["outcome"] == "failed" and entry["message"] == result["message"]


def test_a_run_with_skipped_files_logs_their_relative_paths(make_job, monkeypatch):
    job, _ = make_job()
    lock_files(monkeypatch, {"file3.txt", "deep.txt"})
    result = backup_utils.run_job(job)
    assert result["ok"] and result["not_copied"] == 2

    entry = only_line(job["id"])
    assert entry["outcome"] == "ok_skipped"
    assert entry["skipped"] == 2 and entry["skipped_more"] == 0
    assert sorted(entry["skipped_paths"]) == ["file3.txt", "sub/deep.txt"]
    assert entry["files"] == 9 and entry["cause"] is None
    assert "Permission denied" not in json.dumps(entry["skipped_paths"])


def test_a_run_that_held_pruning_logs_it(make_job):
    job, source = make_job(files=10)
    assert backup_utils.run_job(job)["ok"]
    set_files(source, 3)                          # far more than 30% fewer files
    result = backup_utils.run_job(job)
    assert result["ok"] and result["pruning_held"]

    first, second = [json.loads(line) for line in raw_lines(job["id"])]
    assert first["outcome"] == "ok" and second["outcome"] == "ok_pruning_held"
    assert second["pruning_held"] is True and second["pruned"] == 0
    assert second["files"] == result["files"]


def test_pruning_held_wins_over_skipped_but_keeps_the_skipped_count(make_job, monkeypatch):
    job, _ = make_job()
    monkeypatch.setattr(runner, "_locked_run", lambda *a: {
        "ok": True, "message": "m", "files": 3, "bytes": 9, "pruned": [],
        "errors": ["a.txt: denied"], "not_copied": 1, "pruning_held": True})
    backup_utils.run_job(job)
    entry = only_line(job["id"])
    assert entry["outcome"] == "ok_pruning_held" and entry["skipped"] == 1
    assert entry["skipped_paths"] == ["a.txt"]


def test_a_cancelled_run_appends_one_line(make_job):
    job, _ = make_job()
    result = backup_utils.run_job(job, should_abort=lambda: True)
    assert not result["ok"] and result["message"] == "Backup cancelled"
    entry = only_line(job["id"])
    assert entry["outcome"] == "cancelled" and entry["message"] == "Backup cancelled"


def test_a_busy_run_appends_one_line(make_job):
    job, _ = make_job()
    held = locks.acquire(runner._job_lock_path(job), "test holder")
    try:
        result = backup_utils.run_job(job)
    finally:
        held.release()
    assert result.get("busy")
    entry = only_line(job["id"])
    assert entry["outcome"] == "busy" and entry["message"] == result["message"]
    assert entry["files"] == 0 and entry["version"] is None


def test_a_job_without_an_id_is_logged_to_the_app_log(make_job):
    backup_utils.run_job({"name": "No id", "source": "x", "target": "y"})
    lines = raw_lines(None)
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["job"] is None and entry["outcome"] == "failed"


def test_a_run_that_raises_is_logged_as_failed_and_still_raises(make_job, monkeypatch):
    job, _ = make_job()

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(runner, "_locked_run", boom)
    with pytest.raises(RuntimeError, match="boom"):
        backup_utils.run_job(job)
    entry = only_line(job["id"])
    assert entry["outcome"] == "failed" and entry["message"] == "RuntimeError: boom"


# --- skipped paths, whitelist ---------------------------------------------------

def test_skipped_paths_are_capped_at_fifty_and_the_rest_counted(make_job, monkeypatch):
    job, _ = make_job()
    errors = [f"dir/f{i}.txt: [Errno 13] Permission denied: 'C:\\\\secret\\\\f{i}'"
              for i in range(60)]
    monkeypatch.setattr(runner, "_locked_run", lambda *a: {
        "ok": True, "message": "m", "files": 1, "bytes": 1, "pruned": [],
        "errors": errors, "not_copied": 60})
    backup_utils.run_job(job)
    entry = only_line(job["id"])
    assert entry["skipped"] == 60 and len(entry["skipped_paths"]) == 50
    assert entry["skipped_more"] == 10
    assert entry["skipped_paths"][0] == "dir/f0.txt" and entry["skipped_paths"][-1] == "dir/f49.txt"
    assert "Permission denied" not in raw_lines(job["id"])[0]


def test_only_whitelisted_fields_reach_the_log(make_job, monkeypatch):
    job, _ = make_job()
    monkeypatch.setattr(runner, "_locked_run", lambda *a: {
        "ok": True, "message": "Backed up", "files": 2, "bytes": 5, "pruned": [],
        "password": "hunter2", "api_secret_token": "s3cr3t", "manifest": "/m",
        "target_volume": "vol-123", "swept": ["x"], "contents": "FILE BODY"})
    backup_utils.run_job(job)
    text = raw_lines(job["id"])[0]
    assert set(json.loads(text)) == set(runlog.LINE_KEYS)
    for leaked in ("hunter2", "s3cr3t", "vol-123", "FILE BODY", "password"):
        assert leaked not in text


def test_the_gui_path_is_manual_by_default():
    assert inspect.signature(runner.run_job).parameters["trigger"].default == "manual"


# --- cap, corruption, failures that must not matter -----------------------------

def test_the_log_keeps_only_the_newest_runs(make_job):
    job, _ = make_job()
    now = real_datetime.now()
    for i in range(250):
        assert runlog.append_run(job, {"ok": True, "message": str(i), "files": i},
                                 "manual", now)
    lines = [json.loads(line) for line in raw_lines(job["id"])]
    assert len(lines) == runlog.RUNLOG_MAX_RUNS == 200
    assert [line["message"] for line in lines] == [str(i) for i in range(50, 250)]


def test_the_app_log_is_capped_too(tmp_path):
    now = real_datetime.now()
    for i in range(205):
        runlog.append_run(None, {"ok": False, "message": str(i)}, "scheduled", now)
    lines = [json.loads(line) for line in raw_lines(None)]
    assert len(lines) == 200 and lines[0]["message"] == "5"


def test_a_corrupt_line_does_not_stop_appending_or_trimming(make_job):
    job, _ = make_job()
    path = runlog.log_path(job["id"])
    now = real_datetime.now()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    assert runlog.append_run(job, {"ok": True, "message": "first"}, "manual", now)
    lines = raw_lines(job["id"])
    assert lines[0] == "{not json at all" and json.loads(lines[1])["message"] == "first"

    # Over the cap: the rewrite drops the corrupt lines and keeps the newest runs.
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("garbage one\n\x00\x01 garbage two\n")
        for i in range(runlog.RUNLOG_MAX_RUNS):
            handle.write(json.dumps({"message": str(i)}) + "\n")
    assert runlog.append_run(job, {"ok": True, "message": "last"}, "manual", now)
    lines = [json.loads(line) for line in raw_lines(job["id"])]
    assert len(lines) == 200 and lines[-1]["message"] == "last"
    assert lines[0]["message"] == "1"


def test_a_name_utf8_cannot_encode_is_still_logged_as_one_valid_line(make_job):
    """Review gate: a Windows file name can hold an unpaired surrogate. UTF-8
    refuses to encode it, so the append raised, was swallowed, and the run left
    no line at all."""
    job, _ = make_job()
    now = real_datetime.now()
    skipped = {"ok": True, "message": "m", "files": 2,
               "errors": ["dir/\udc80odd.txt: [WinError 32] in use"]}
    failed = {"ok": False, "message": "Backup failed (failed on bad\ud800name.txt)"}

    assert runlog.append_run(job, skipped, "manual", now) is True
    assert runlog.append_run(job, failed, "scheduled", now) is True

    with open(runlog.log_path(job["id"]), "rb") as handle:
        raw = handle.read()
    raw.decode("utf-8")                                  # strict: the file is valid UTF-8
    first, second = [json.loads(line) for line in raw.splitlines()]
    assert first["skipped_paths"] == ["dir/\udc80odd.txt"]      # round-trips exactly
    assert second["message"] == failed["message"]
    assert [run["outcome"] for run in runlog.read_runs(job["id"])] == ["failed", "ok_skipped"]


def test_an_append_after_a_torn_line_starts_a_fresh_line(make_job):
    """Review gate: a crash or a full disk mid-append leaves a fragment with no
    newline; the next run's line was glued onto it and lost as well."""
    job, _ = make_job()
    path = runlog.log_path(job["id"])
    now = real_datetime.now()
    assert runlog.append_run(job, {"ok": True, "message": "before"}, "manual", now)
    with open(path, "ab") as handle:
        handle.write(b'{"v": 1, "start": "2026-01-01T00:00:00", "mess')   # torn, no newline

    assert runlog.append_run(job, {"ok": True, "message": "after"}, "manual", now)

    assert [run["message"] for run in runlog.read_runs(job["id"])] == ["after", "before"]
    lines = raw_lines(job["id"])
    assert len(lines) == 3 and lines[1].endswith('"mess')    # the fragment stays alone
    # ...and a healthy log gets no blank lines.
    assert runlog.append_run(job, {"ok": True, "message": "third"}, "manual", now)
    assert "" not in raw_lines(job["id"]) and len(raw_lines(job["id"])) == 4


def test_a_huge_message_is_capped(make_job):
    job, _ = make_job()
    assert runlog.append_run(job, {"ok": False, "message": "x" * 1_000_000},
                             "manual", real_datetime.now())
    assert len(only_line(job["id"])["message"]) == runlog.MESSAGE_MAX == 2000
    assert os.path.getsize(runlog.log_path(job["id"])) < 4096


def test_an_unwritable_log_folder_does_not_change_the_run(make_job, tmp_path, monkeypatch):
    job, _ = make_job()
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("a file where the logs folder should be", encoding="utf-8")
    monkeypatch.setattr(runlog, "_logs_dir", lambda: str(blocker))

    result = backup_utils.run_job(job)
    assert result["ok"] and result["files"] == 11
    assert runlog.append_run(job, result, "manual", real_datetime.now()) is False


def test_a_log_lock_timeout_does_not_change_the_run(make_job, monkeypatch):
    job, _ = make_job()

    def timeout(*_a, **_k):
        raise OSError("Timed out waiting")

    monkeypatch.setattr(runlog.locks, "acquire_wait", timeout)
    result = backup_utils.run_job(job)
    assert result["ok"] and raw_lines(job["id"]) == []


def test_a_logging_bug_cannot_fail_a_run(make_job, monkeypatch):
    job, _ = make_job()
    monkeypatch.setattr(runlog, "build_line", lambda *a: 1 / 0)
    assert backup_utils.run_job(job)["ok"]


def test_the_log_lock_is_released_and_the_logs_folder_holds_only_logs(make_job, tmp_path):
    job, _ = make_job()
    backup_utils.run_job(job)
    logs = Path(runlog._logs_dir())
    assert [p.name for p in logs.iterdir()] == [job["id"] + ".jsonl"]
    leftovers = [p.name for p in Path(jobs_store._locks_dir()).iterdir()]
    assert leftovers == []


@pytest.mark.skipif(os.name != "nt", reason="app_dir() follows %APPDATA% on Windows only")
def test_the_unpatched_app_data_folder_is_not_the_real_one():
    """Review gate: tests/conftest.py points %APPDATA% at a throwaway folder for
    the whole session, so a test that forgets a redirect cannot write the real
    jobs file, locks or logs. `app_dir()` is what all three are built from
    (`app_dir_for` is the same path without creating it)."""
    from utils import fs_utils
    real_roaming = os.path.normcase(os.path.expanduser("~\\AppData\\Roaming"))
    here = os.path.normcase(fs_utils.app_dir_for(fs_utils.APP_NAME))
    assert not here.startswith(real_roaming), here


def test_a_job_id_cannot_point_the_log_outside_its_folder():
    path = runlog.log_path("../../evil")
    assert os.path.dirname(path) == runlog._logs_dir()
    assert os.path.basename(path).startswith("id-")


# --- reading --------------------------------------------------------------------

def test_read_runs_is_newest_first_and_skips_corrupt_lines(make_job):
    job, _ = make_job()
    now = real_datetime.now()
    for i in range(4):
        runlog.append_run(job, {"ok": True, "message": str(i)}, "manual", now)
    with open(runlog.log_path(job["id"]), "a", encoding="utf-8") as handle:
        handle.write("{broken\n[1, 2]\n")
    runlog.append_run(job, {"ok": True, "message": "4"}, "manual", now)

    runs = runlog.read_runs(job["id"])
    assert [r["message"] for r in runs] == ["4", "3", "2", "1", "0"]
    assert [r["message"] for r in runlog.read_runs(job["id"], limit=2)] == ["4", "3"]
    assert runlog.read_runs("no-such-job") == []


# --- headless -------------------------------------------------------------------

def run_cli(appdata, *args):
    """Run `main.py` as the scheduled task does, with a fresh app-data folder."""
    env = dict(os.environ, APPDATA=str(appdata), HOME=str(appdata),
               USERPROFILE=str(appdata), QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, str(REPO / "main.py"), *args],
                          cwd=str(REPO), env=env, capture_output=True,
                          text=True, timeout=120)


def app_log(appdata):
    """The parsed lines of `<appdata>/Safekeep/logs/app.jsonl`."""
    path = Path(appdata) / "Safekeep" / "logs" / "app.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_headless_unknown_job_logs_to_the_app_log_and_still_exits_2(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    (appdata / "Safekeep" / "backup_jobs.json").write_text("[]", encoding="utf-8")

    proc = run_cli(appdata, "--run-backup-job", "does-not-exist")

    assert proc.returncode == 2, proc.stderr
    assert proc.stdout.strip() == "[ERROR] No backup job with id 'does-not-exist'"
    (entry,) = app_log(appdata)
    assert tuple(entry) == runlog.LINE_KEYS
    assert (entry["trigger"], entry["outcome"], entry["cause"], entry["job"]) == (
        "scheduled", "failed", "unknown_job", None)
    assert "does-not-exist" in entry["message"]


def test_headless_damaged_jobs_file_logs_to_the_app_log_and_still_exits_3(tmp_path):
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    jobs = appdata / "Safekeep" / "backup_jobs.json"
    jobs.write_text('[{"id": "abc"', encoding="utf-8")
    before = jobs.read_bytes()

    proc = run_cli(appdata, "--run-backup-job", "abc")

    assert proc.returncode == 3, proc.stderr
    assert "[ERROR]" in proc.stderr and "damaged" in proc.stderr
    assert jobs.read_bytes() == before
    (entry,) = app_log(appdata)
    assert (entry["trigger"], entry["outcome"], entry["cause"], entry["job"]) == (
        "scheduled", "failed", "jobs_file_damaged", None)
    assert "damaged" in entry["message"]


def test_a_headless_real_run_logs_trigger_scheduled(tmp_path, make_job):
    job, _ = make_job()
    appdata = tmp_path / "appdata"
    (appdata / "Safekeep").mkdir(parents=True)
    (appdata / "Safekeep" / "backup_jobs.json").write_text(
        json.dumps([job]), encoding="utf-8")

    proc = run_cli(appdata, "--run-backup-job", job["id"])

    assert proc.returncode == 0, proc.stderr
    path = appdata / "Safekeep" / "logs" / (job["id"] + ".jsonl")
    (entry,) = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert entry["trigger"] == "scheduled" and entry["outcome"] == "ok"
    assert entry["job"] == job["id"] and entry["files"] == 11
    assert not (appdata / "Safekeep" / "logs" / "app.jsonl").exists()
