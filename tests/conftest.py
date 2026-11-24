# tests/conftest.py
# Keep every test off the real %APPDATA%: the lock files (utils/locks.py) live
# in `<app data>/locks`, and run_job / save_job / record_run all take one.
# Each test gets a fresh, empty locks folder OUTSIDE tmp_path (several tests
# list tmp_path and would otherwise see a stray "locks" directory).
import atexit
import os
import shutil
import tempfile

import pytest

# Fail closed (review gate, batch 3). The fixtures below redirect two names, and
# each test file redirects JOBS_FILE itself; a test that forgets one would write
# the REAL %APPDATA%/Safekeep (jobs file, locks, and since card 52 a log line per
# run). So the whole session's app-data root is a throwaway folder, set BEFORE
# any repo module is imported (jobs_store reads it at import). Windows only:
# elsewhere app_dir() is built from the home folder and the fixtures stay the
# only guard.
_SESSION_APPDATA = tempfile.mkdtemp(prefix="safekeep-tests-")
os.environ["APPDATA"] = _SESSION_APPDATA
atexit.register(shutil.rmtree, _SESSION_APPDATA, ignore_errors=True)

from utils import copying, jobs_store, runlog  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_locks_dir(tmp_path_factory, monkeypatch):
    """Point backup_utils at a throwaway locks folder and return its path."""
    path = tmp_path_factory.mktemp("locks")
    monkeypatch.setattr(jobs_store, "_locks_dir", lambda: str(path))
    return path


@pytest.fixture(autouse=True)
def isolated_logs_dir(tmp_path_factory, monkeypatch):
    """Point the run log (utils/runlog.py) at a throwaway folder.

    run_job appends a line per run to `<app data>/logs`; without this every test
    that runs a job would write into the REAL %APPDATA%/Safekeep/logs.
    """
    path = tmp_path_factory.mktemp("logs")
    monkeypatch.setattr(runlog, "_logs_dir", lambda: str(path))
    return path


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    """Skip the half-second wait before the retry pass (utils/copying.py).

    Tests that skip files would each sleep it; the ones that care about the
    pass replace `copying._retry_pause` with a hook of their own instead.
    """
    monkeypatch.setattr(copying, "RETRY_PAUSE_SECONDS", 0)


@pytest.fixture(scope="session", autouse=True)
def clear_read_only_debris(tmp_path_factory):
    """At session end, clear the read-only mark on every file under this run's
    temp folder (card fEC32w6P).

    Finished versions are read-only, so a test's `tmp_path` ends the session
    full of read-only files, and a folder like that can make the NEXT run's
    temp-folder rotation fail loudly or leave debris. Clearing the mark lets
    pytest delete it like any other folder. Links and junctions are never
    followed, so nothing outside the temp folder is touched.
    """
    yield
    import stat
    from utils.fs_utils import _is_link_like, long_path
    stack = [str(tmp_path_factory.getbasetemp())]
    while stack:
        try:
            with os.scandir(long_path(stack.pop())) as entries:
                listing = list(entries)
        except OSError:
            continue
        for entry in listing:
            try:
                if _is_link_like(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                else:
                    mode = stat.S_IMODE(entry.stat(follow_symlinks=False).st_mode)
                    os.chmod(entry.path, mode | stat.S_IWUSR)
            except OSError:
                pass
