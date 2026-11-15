# tests/conftest.py
# Keep every test off the real %APPDATA%: the lock files (utils/locks.py) live
# in `<app data>/locks`, and run_job / save_job / record_run all take one.
# Each test gets a fresh, empty locks folder OUTSIDE tmp_path (several tests
# list tmp_path and would otherwise see a stray "locks" directory).
import pytest

from utils import jobs_store


@pytest.fixture(autouse=True)
def isolated_locks_dir(tmp_path_factory, monkeypatch):
    """Point backup_utils at a throwaway locks folder and return its path."""
    path = tmp_path_factory.mktemp("locks")
    monkeypatch.setattr(jobs_store, "_locks_dir", lambda: str(path))
    return path
