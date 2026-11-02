# tests/test_migrate.py
# Coverage for utils/migrate.py: the one-time copy of data, scheduled tasks
# and settings from the pre-rename UtilityTool identity to Safekeep.
#
# Every directory is under tmp_path and every scheduler call is an injected
# fake, so the suite never reads the real app-data folder and never runs
# schtasks or launchctl.
import json
import os

import pytest

from utils import migrate


@pytest.fixture
def dirs(tmp_path):
    """An old app-data dir holding a jobs file and an activity log, and an
    empty new one (app_dir() creates it on import, so empty-but-present is
    the real starting state)."""
    old = tmp_path / "UtilityTool"
    new = tmp_path / "Safekeep"
    old.mkdir()
    new.mkdir()
    (old / "backup_jobs.json").write_text(json.dumps([{"id": "j1"}]), encoding="utf-8")
    (old / "activity.json").write_text("[]", encoding="utf-8")
    (old / "icons").mkdir()
    (old / "icons" / "cached.png").write_bytes(b"png")
    return old, new


def _snapshot(directory):
    """Every file under `directory` with its bytes, for before/after checks."""
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in sorted(directory.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def test_data_is_copied_and_the_old_dir_left_untouched(dirs):
    old, new = dirs
    before = _snapshot(old)
    copied = migrate.migrate_data(str(old), str(new))
    assert sorted(copied) == sorted(["backup_jobs.json", "activity.json",
                                     os.path.join("icons", "cached.png")])
    assert json.loads((new / "backup_jobs.json").read_text(encoding="utf-8")) == [{"id": "j1"}]
    assert (new / "icons" / "cached.png").read_bytes() == b"png"
    assert _snapshot(old) == before


def test_second_run_changes_nothing(dirs):
    old, new = dirs
    migrate.migrate_data(str(old), str(new))
    after_first = _snapshot(new)
    # The user then clears the activity log in Safekeep; a re-run must not
    # bring the old one back.
    (new / "activity.json").unlink()
    assert migrate.migrate_data(str(old), str(new)) == []
    assert not (new / "activity.json").exists()
    assert set(_snapshot(new)) == set(after_first) - {"activity.json"}


def test_existing_jobs_file_in_the_new_dir_blocks_the_copy(dirs):
    old, new = dirs
    (new / "backup_jobs.json").write_text("[]", encoding="utf-8")
    assert migrate.migrate_data(str(old), str(new)) == []
    assert (new / "backup_jobs.json").read_text(encoding="utf-8") == "[]"
    assert not (new / "activity.json").exists()


def test_never_overwrites_a_file_already_in_the_new_dir(dirs):
    old, new = dirs
    (new / "activity.json").write_text('["newer"]', encoding="utf-8")
    copied = migrate.migrate_data(str(old), str(new))
    assert "activity.json" not in copied
    assert (new / "activity.json").read_text(encoding="utf-8") == '["newer"]'
    assert (new / "backup_jobs.json").exists()


def test_absent_old_dir_is_a_no_op(tmp_path):
    new = tmp_path / "Safekeep"
    new.mkdir()
    assert migrate.migrate_data(str(tmp_path / "UtilityTool"), str(new)) == []
    assert list(new.iterdir()) == []
    assert not (tmp_path / "UtilityTool").exists()  # never conjured into existence


# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------

class FakeScheduler:
    """Stands in for backup_utils' register/exists/unregister trio."""

    def __init__(self, register_ok=True, old_tasks=("j1",), remove_ok=True):
        self.register_ok = register_ok
        self.old_tasks = set(old_tasks)
        self.remove_ok = remove_ok
        self.registered, self.removed = [], []

    def register(self, job):
        if self.register_ok:
            self.registered.append(job["id"])
            return True, "ok"
        return False, "Access is denied."

    def legacy_exists(self, job_id):
        return job_id in self.old_tasks

    def remove_legacy(self, job_id):
        if not self.remove_ok:
            return False, "locked"
        self.old_tasks.discard(job_id)
        self.removed.append(job_id)
        return True, ""

    def kwargs(self):
        return {"register": self.register, "legacy_exists": self.legacy_exists,
                "remove_legacy": self.remove_legacy}


DAILY = {"id": "j1", "name": "Docs", "schedule": "daily", "at": "20:00"}
MANUAL = {"id": "m1", "name": "Hand", "schedule": "manual"}


def test_register_succeeds_then_old_task_is_deleted(dirs):
    old, new = dirs
    fake = FakeScheduler()
    results = migrate.migrate_tasks([DAILY], str(old), str(new), **fake.kwargs())
    assert fake.registered == ["j1"] and fake.removed == ["j1"]
    assert [r["ok"] for r in results] == [True]
    assert (new / migrate.TASKS_MARKER).exists()


def test_register_fails_so_old_task_stays_and_failure_is_reported(dirs):
    old, new = dirs
    fake = FakeScheduler(register_ok=False)
    results = migrate.migrate_tasks([DAILY], str(old), str(new), **fake.kwargs())
    assert fake.removed == []
    assert fake.old_tasks == {"j1"}  # the old task is still there
    assert results[0]["ok"] is False
    assert "UtilityTool_Backup_j1" in results[0]["message"]
    assert "Access is denied." in results[0]["message"]
    # Not marked done, so the next start tries again.
    assert not (new / migrate.TASKS_MARKER).exists()


def test_old_task_that_will_not_delete_is_reported(dirs):
    old, new = dirs
    fake = FakeScheduler(remove_ok=False)
    results = migrate.migrate_tasks([DAILY], str(old), str(new), **fake.kwargs())
    assert fake.registered == ["j1"]
    assert results[0]["ok"] is False
    assert not (new / migrate.TASKS_MARKER).exists()


def test_old_task_missing_still_registers_the_new_one(dirs):
    old, new = dirs
    fake = FakeScheduler(old_tasks=())
    results = migrate.migrate_tasks([DAILY], str(old), str(new), **fake.kwargs())
    assert fake.registered == ["j1"] and fake.removed == []
    assert results[0]["ok"] is True


def test_manual_jobs_create_no_task(dirs):
    old, new = dirs
    fake = FakeScheduler()
    assert migrate.migrate_tasks([MANUAL], str(old), str(new), **fake.kwargs()) == []
    assert fake.registered == [] and fake.removed == []


def test_tasks_run_once_and_not_at_all_without_an_old_install(dirs, tmp_path):
    old, new = dirs
    fake = FakeScheduler()
    migrate.migrate_tasks([DAILY], str(old), str(new), **fake.kwargs())
    assert migrate.migrate_tasks([DAILY], str(old), str(new), **fake.kwargs()) == []
    assert fake.registered == ["j1"]

    fresh = FakeScheduler()
    other_new = tmp_path / "fresh"
    other_new.mkdir()
    assert migrate.migrate_tasks([DAILY], str(tmp_path / "nope"), str(other_new),
                                 **fresh.kwargs()) == []
    assert fresh.registered == []


def test_launchd_variant_uses_the_same_order_via_backup_utils(dirs, monkeypatch):
    """The default callables route through backup_utils, which branches to
    launchd on macOS. Unverified on a real Mac -- this only pins the order and
    the old/new labels, with launchctl and the plist writes faked out."""
    from utils import backup_utils
    old, new = dirs
    monkeypatch.setattr(backup_utils.sys, "platform", "darwin")
    calls = []
    monkeypatch.setattr(backup_utils, "_launchd_register",
                        lambda job: calls.append(("register", backup_utils.launchd_label(job["id"])))
                        or (True, ""))
    monkeypatch.setattr(backup_utils, "_launchd_exists", lambda job_id, legacy=False: legacy)
    monkeypatch.setattr(backup_utils, "_launchd_unregister",
                        lambda job_id, legacy=False: calls.append(
                            ("remove", backup_utils.launchd_label(job_id, legacy))) or (True, ""))
    results = migrate.migrate_tasks([DAILY], str(old), str(new))
    assert calls == [("register", "com.techcabana.safekeep.backup.j1"),
                     ("remove", "com.techcabana.utilitytool.backup.j1")]
    assert results[0]["ok"] is True


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class FakeSettings:
    """The four QSettings methods migrate_settings uses, over a dict."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.synced = False

    def allKeys(self):
        return list(self.values)

    def value(self, key):
        return self.values[key]

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        self.synced = True


def test_settings_copy_into_an_empty_new_store():
    old = FakeSettings({"ui/theme": "dark", "ui/last_tab": 1})
    new = FakeSettings()
    assert sorted(migrate.migrate_settings(old, new)) == ["ui/last_tab", "ui/theme"]
    assert new.values == {"ui/theme": "dark", "ui/last_tab": 1} and new.synced
    assert old.values == {"ui/theme": "dark", "ui/last_tab": 1}


def test_settings_never_replace_a_value_already_chosen_in_the_new_app():
    old = FakeSettings({"ui/theme": "dark"})
    new = FakeSettings({"ui/theme": "light"})
    assert migrate.migrate_settings(old, new) == []
    assert new.values == {"ui/theme": "light"}
