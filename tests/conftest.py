# tests/conftest.py
# Keep every test off the real %APPDATA%: the lock files (utils/locks.py) live
# in `<app data>/locks`, and run_job / save_job / record_run all take one.
# Each test gets a fresh, empty locks folder OUTSIDE tmp_path (several tests
# list tmp_path and would otherwise see a stray "locks" directory).
import pytest

from utils import jobs_store, runlog


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
