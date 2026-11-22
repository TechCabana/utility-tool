# tests/test_empty_job_list.py
# Card OsE7AgPP: when backup_jobs.json is damaged and there is no usable .bak,
# the user can "Start with an empty list". The damaged file is renamed (never
# copied, never deleted) to backup_jobs.json.corrupt-<timestamp>, the Backup
# tab shows its normal empty state with Add enabled, and the scheduled tasks
# left in the OS scheduler are listed (read-only).
#
# Injection points, all on the OWNING modules: utils.jobs_store.JOBS_FILE (and
# its datetime / os.rename for the clock and the failing rename) and
# utils.scheduler._run (so no real schtasks ever runs; the stub also fails the
# test if anything but a /query is attempted). The GUI tests drive a real
# BackupTab and a real ConfirmDialog (only exec() is replaced, to press a
# button), offscreen, with no QSettings. Absence assertions on widgets use
# isHidden() -- an explicit flag -- and each is paired with a case where the same
# widget is shown, so they could have failed.
import os
import re
import sys
from datetime import datetime

import pytest

from utils import backup_utils, jobs_store, scheduler

DAMAGED = [
    pytest.param(b'[{"id": "a1", "name": "half', id="truncated-json"),
    pytest.param(b"\xff\xfe\x00{\x80\x81\x00\x01binary\xc3\x28", id="binary-invalid-utf8"),
    pytest.param(b"", id="empty-file"),
    pytest.param(b'{"not": "a list"}', id="valid-json-wrong-shape"),
]
VALID = b'[{"id": "ab12cd34", "name": "Photos"}]'
KEPT_NAME = re.compile(r"^backup_jobs\.json\.corrupt-\d{8}-\d{6}$")


@pytest.fixture(autouse=True)
def jobs_file(tmp_path, monkeypatch):
    """Point the jobs file at tmp_path (conftest already redirects locks)."""
    path = tmp_path / "backup_jobs.json"
    monkeypatch.setattr(jobs_store, "JOBS_FILE", str(path))
    assert str(path).startswith(str(tmp_path))
    return path


def kept_files(directory):
    """Names in `directory` that are quarantined files."""
    return sorted(p.name for p in directory.iterdir() if ".corrupt-" in p.name)


def fixed_clock(monkeypatch, moment=datetime(2026, 11, 22, 18, 30, 5)):
    """Make jobs_store's clock stand still, so a name can be predicted."""
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment
    monkeypatch.setattr(jobs_store, "datetime", Fixed)
    return "20261122-183005"


# --- the engine function ------------------------------------------------------------

@pytest.mark.parametrize("damaged", DAMAGED)
def test_the_damaged_file_is_kept_byte_for_byte_under_a_corrupt_name(jobs_file, damaged):
    jobs_file.write_bytes(damaged)
    before_id = os.stat(jobs_file).st_ino
    with pytest.raises(backup_utils.JobsFileCorrupt) as caught:
        backup_utils.load_jobs()
    assert caught.value.has_backup is False      # the state the action is for

    kept = backup_utils.quarantine_jobs_file()

    assert KEPT_NAME.match(os.path.basename(kept)), kept
    assert os.path.dirname(kept) == str(jobs_file.parent)
    with open(kept, "rb") as handle:
        assert handle.read() == damaged            # not a byte changed
    assert not jobs_file.exists()                  # moved, not copied
    assert os.stat(kept).st_ino == before_id != 0  # the very same file, renamed
    assert kept_files(jobs_file.parent) == [os.path.basename(kept)]


def test_afterwards_the_list_is_empty_and_adding_a_job_works(jobs_file):
    jobs_file.write_bytes(b"\x00\x01 not json")
    kept = backup_utils.quarantine_jobs_file()

    assert backup_utils.load_jobs() == []
    job = backup_utils.new_job("Docs", "src", "dst", schedule="manual")
    backup_utils.save_job(job)

    assert [j["id"] for j in backup_utils.load_jobs()] == [job["id"]]
    with open(kept, "rb") as handle:
        assert handle.read() == b"\x00\x01 not json"   # the new save left it alone


def test_the_name_comes_from_the_clock_and_a_taken_name_gets_a_suffix(jobs_file, monkeypatch):
    stamp = fixed_clock(monkeypatch)
    taken = jobs_file.parent / f"backup_jobs.json.corrupt-{stamp}"
    taken.write_bytes(b"an earlier quarantine")

    jobs_file.write_bytes(b"damaged one")
    first = backup_utils.quarantine_jobs_file()
    jobs_file.write_bytes(b"damaged two")
    second = backup_utils.quarantine_jobs_file()

    assert os.path.basename(first) == f"backup_jobs.json.corrupt-{stamp}-2"
    assert os.path.basename(second) == f"backup_jobs.json.corrupt-{stamp}-3"
    assert taken.read_bytes() == b"an earlier quarantine"   # never overwritten
    assert open(first, "rb").read() == b"damaged one"
    assert open(second, "rb").read() == b"damaged two"


def test_an_unused_name_has_no_suffix(jobs_file, monkeypatch):
    stamp = fixed_clock(monkeypatch)
    jobs_file.write_bytes(b"damaged")
    assert os.path.basename(backup_utils.quarantine_jobs_file()) == \
        f"backup_jobs.json.corrupt-{stamp}"


def test_it_refuses_when_the_jobs_file_is_valid(jobs_file):
    jobs_file.write_bytes(VALID)
    with pytest.raises(backup_utils.QuarantineRefused, match="left alone"):
        backup_utils.quarantine_jobs_file()
    assert jobs_file.read_bytes() == VALID and kept_files(jobs_file.parent) == []


def test_it_refuses_when_a_usable_backup_copy_exists(jobs_file):
    jobs_file.write_bytes(b"damaged")
    bak = jobs_file.parent / "backup_jobs.json.bak"
    bak.write_bytes(VALID)
    with pytest.raises(backup_utils.QuarantineRefused, match="usable backup copy"):
        backup_utils.quarantine_jobs_file()
    assert jobs_file.read_bytes() == b"damaged" and bak.read_bytes() == VALID
    assert kept_files(jobs_file.parent) == []


def test_it_refuses_when_there_is_no_jobs_file(jobs_file):
    with pytest.raises(backup_utils.QuarantineRefused, match="no jobs file"):
        backup_utils.quarantine_jobs_file()
    assert list(jobs_file.parent.iterdir()) == []


def test_an_unusable_backup_copy_is_set_aside_too_and_never_deleted(jobs_file, monkeypatch):
    stamp = fixed_clock(monkeypatch)
    jobs_file.write_bytes(b"damaged main")
    bak = jobs_file.parent / "backup_jobs.json.bak"
    bak.write_bytes(b"\xff damaged bak")

    kept = backup_utils.quarantine_jobs_file()

    assert open(kept, "rb").read() == b"damaged main"
    kept_bak = jobs_file.parent / f"backup_jobs.json.bak.corrupt-{stamp}"
    assert kept_bak.read_bytes() == b"\xff damaged bak"
    assert not bak.exists() and not jobs_file.exists()


def failing_rename(monkeypatch, fail_for):
    """Make os.rename raise PermissionError for the paths in `fail_for`."""
    real = os.rename
    calls = []

    def rename(src, dst, *args, **kwargs):
        calls.append(str(src))
        if str(src) in fail_for:
            raise PermissionError(13, "Access is denied", str(src))
        return real(src, dst, *args, **kwargs)
    monkeypatch.setattr(jobs_store.os, "rename", rename)
    return calls


def test_a_failed_rename_raises_and_leaves_the_original_where_it_was(jobs_file, monkeypatch):
    jobs_file.write_bytes(b"damaged\xff")
    calls = failing_rename(monkeypatch, {str(jobs_file)})

    with pytest.raises(OSError, match="Could not set aside"):
        backup_utils.quarantine_jobs_file()

    assert calls == [str(jobs_file)]                   # the failure really fired
    assert jobs_file.read_bytes() == b"damaged\xff"
    assert kept_files(jobs_file.parent) == []


def test_if_the_jobs_file_cannot_move_the_backup_copy_is_put_back(jobs_file, monkeypatch):
    jobs_file.write_bytes(b"damaged main")
    bak = jobs_file.parent / "backup_jobs.json.bak"
    bak.write_bytes(b"damaged bak")
    calls = failing_rename(monkeypatch, {str(jobs_file)})

    with pytest.raises(OSError):
        backup_utils.quarantine_jobs_file()

    assert len(calls) == 3        # bak aside, jobs file (fails), bak put back
    assert jobs_file.read_bytes() == b"damaged main"
    assert bak.read_bytes() == b"damaged bak"
    assert kept_files(jobs_file.parent) == []


def test_it_is_exported_by_the_facade():
    assert backup_utils.quarantine_jobs_file is jobs_store.quarantine_jobs_file
    assert backup_utils.QuarantineRefused is jobs_store.QuarantineRefused
    assert backup_utils.list_scheduled_tasks is scheduler.list_scheduled_tasks


# --- listing what the OS scheduler still holds ---------------------------------------

SCHTASKS_CSV = "\r\n".join([
    '"\\Safekeep_Backup_ab12cd34","11/23/2026 8:00:00 PM","Ready"',
    '"\\Safekeep_Backup_ab12cd34","11/24/2026 8:00:00 PM","Ready"',   # a second trigger
    '"\\UtilityTool_Backup_99ff00aa","N/A","Disabled"',
    '"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","N/A","Ready"',
    '"\\OtherApp_Safekeep_Backup_x","N/A","Ready"',
])


def stub_run(monkeypatch, result):
    """Replace scheduler._run; return the list of argument lists it received."""
    calls = []

    def fake(args):
        calls.append(list(args))
        return result
    monkeypatch.setattr(scheduler, "_run", fake)
    return calls


def test_the_listing_returns_owned_tasks_by_prefix_without_duplicates(monkeypatch):
    calls = stub_run(monkeypatch, (True, SCHTASKS_CSV))

    names, error = scheduler.list_scheduled_tasks()

    assert error == ""
    assert names == ["Safekeep_Backup_ab12cd34", "UtilityTool_Backup_99ff00aa"]
    assert calls == [["schtasks", "/query", "/fo", "csv", "/nh"]]   # a query, nothing else


def test_a_scheduler_with_no_tasks_is_none_not_an_error(monkeypatch):
    stub_run(monkeypatch, (True, "INFO: There are no scheduled tasks presently available."))
    assert scheduler.list_scheduled_tasks() == ([], "")


def test_a_failing_schtasks_is_reported_not_raised(monkeypatch):
    stub_run(monkeypatch, (False, "ERROR: Access is denied."))
    names, error = scheduler.list_scheduled_tasks()
    assert names == []
    assert error == "Could not list scheduled tasks: ERROR: Access is denied."


def test_a_failing_schtasks_with_no_output_still_says_why(monkeypatch):
    stub_run(monkeypatch, (False, ""))
    assert scheduler.list_scheduled_tasks()[1].startswith("Could not list scheduled tasks")


def test_the_macos_listing_reads_launch_agents_by_prefix(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    for name in ("com.techcabana.safekeep.backup.ab12cd34.plist",
                 "com.techcabana.utilitytool.backup.99ff00aa.plist",
                 "com.apple.something.plist", "com.techcabana.safekeep.backup.x.txt"):
        (agents / name).write_text("x")
    monkeypatch.setattr(scheduler.sys, "platform", "darwin")
    monkeypatch.setattr(scheduler.os.path, "expanduser", lambda p: str(tmp_path))
    stub_run(monkeypatch, (False, "the macOS branch must not call schtasks"))

    names, error = scheduler.list_scheduled_tasks()

    assert error == "" and names == ["com.techcabana.safekeep.backup.ab12cd34",
                                     "com.techcabana.utilitytool.backup.99ff00aa"]
    assert sorted(os.listdir(agents))[0] == "com.apple.something.plist"   # read-only


# --- the Backup tab ---------------------------------------------------------------------

@pytest.fixture
def qt_app():
    """An offscreen QApplication; no QSettings is constructed by these tests."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def scheduler_calls(monkeypatch):
    """No real scheduler: only a /query is allowed, and it answers SCHTASKS_CSV."""
    calls = []

    def fake(args):
        calls.append(list(args))
        assert args[:2] == ["schtasks", "/query"], f"the scheduler was modified: {args}"
        return True, SCHTASKS_CSV
    monkeypatch.setattr(scheduler, "_run", fake)
    return calls


@pytest.fixture
def tab(qt_app, scheduler_calls, monkeypatch):
    """A real BackupTab; its confirmation dialog is real but answered by `answer`."""
    from PySide6 import QtWidgets
    from tabs import backup_tab
    shown, opened = [], []

    def exec_(self):
        shown.append(" ".join(label.text() for label in self.findChildren(QtWidgets.QLabel)))
        (self.confirm_button if widget.answer == "confirm" else self.cancel_button).click()
        return self.result()
    monkeypatch.setattr(backup_tab.ConfirmDialog, "exec", exec_)
    monkeypatch.setattr(backup_tab.BackupTab, "open_url", staticmethod(opened.append))
    widget = backup_tab.BackupTab()
    widget.answer = "confirm"
    widget.shown, widget.opened = shown, opened
    return widget


def test_the_button_shows_only_for_a_damaged_file_with_no_backup_copy(tab, jobs_file):
    # Positive control: the damaged, no-backup state shows it, enabled, keyboard reachable.
    jobs_file.write_bytes(b"damaged")
    tab.refresh_backup()
    assert not tab.start_empty_btn.isHidden() and tab.start_empty_btn.isEnabled()
    assert tab.start_empty_btn.focusPolicy() != 0 and tab.start_empty_btn.text() == "Start with an empty list"
    assert not tab.jobs_damaged.isHidden()
    assert not tab.jobs_damaged.cta.isEnabled()        # Restore has nothing to restore
    assert not tab.job_add_btn.isEnabled()

    # Damaged but a usable .bak exists: Restore is offered, this is not.
    (jobs_file.parent / "backup_jobs.json.bak").write_bytes(VALID)
    tab.refresh_backup()
    assert not tab.jobs_damaged.isHidden() and tab.jobs_damaged.cta.isEnabled()
    assert tab.start_empty_btn.isHidden()

    # Healthy file: neither.
    jobs_file.write_bytes(VALID)
    tab.refresh_backup()
    assert tab.start_empty_btn.isHidden() and tab.jobs_damaged.isHidden()
    assert tab.job_add_btn.isEnabled()


def test_confirming_keeps_the_file_and_shows_the_empty_state_with_add_enabled(
        tab, jobs_file, scheduler_calls):
    damaged = b"\xff\xfe damaged \x00 jobs"
    jobs_file.write_bytes(damaged)
    tab.refresh_backup()
    assert not tab.start_empty_btn.isHidden() and tab.job_add_btn.isEnabled() is False

    tab.start_empty_btn.click()

    (kept,) = kept_files(jobs_file.parent)
    assert KEPT_NAME.match(kept)
    assert (jobs_file.parent / kept).read_bytes() == damaged     # byte for byte
    assert not jobs_file.exists()
    assert tab.jobs_damaged.isHidden() and tab.start_empty_btn.isHidden()
    assert not tab.jobs_empty.isHidden()                         # the normal empty state
    assert tab.job_add_btn.isEnabled()
    status = tab.backup_status.text()
    assert kept in status and "empty job list" in status
    assert "Safekeep_Backup_ab12cd34" in status and "UtilityTool_Backup_99ff00aa" in status
    assert not tab.open_kept_btn.isHidden() and tab.open_kept_btn.focusPolicy() != 0
    assert all(call[:2] == ["schtasks", "/query"] for call in scheduler_calls)

    # The confirmation said what would happen, and listed the tasks, before anything moved.
    (message,) = tab.shown
    assert "not deleted" in message and "backup_jobs.json.corrupt-" in message
    assert "Safekeep_Backup_ab12cd34" in message

    # Add really works now.
    job = backup_utils.new_job("Docs", "src", "dst", schedule="manual")
    backup_utils.save_job(job)
    tab.refresh_backup()
    assert [j["id"] for j in tab.jobs] == [job["id"]] and tab.job_add_btn.isEnabled()
    assert (jobs_file.parent / kept).read_bytes() == damaged


def test_open_folder_opens_the_folder_holding_the_kept_file(tab, jobs_file):
    from PySide6 import QtCore
    jobs_file.write_bytes(b"damaged")
    tab.refresh_backup()
    tab.start_empty_btn.click()
    tab.open_kept_btn.click()
    assert tab.opened == [QtCore.QUrl.fromLocalFile(str(jobs_file.parent))]
    tab._say("something else")                    # any later notice drops the button
    assert tab.open_kept_btn.isHidden()


def test_cancelling_changes_nothing(tab, jobs_file):
    jobs_file.write_bytes(b"damaged\xff")
    tab.refresh_backup()
    tab.answer = "cancel"

    tab.start_empty_btn.click()

    assert len(tab.shown) == 1                    # the question really was asked
    assert jobs_file.read_bytes() == b"damaged\xff"
    assert kept_files(jobs_file.parent) == []
    assert not tab.jobs_damaged.isHidden() and not tab.start_empty_btn.isHidden()
    assert not tab.job_add_btn.isEnabled()


def test_a_scheduler_that_cannot_be_listed_is_said_so_and_does_not_stop_the_action(
        tab, jobs_file, monkeypatch):
    monkeypatch.setattr(scheduler, "_run", lambda args: (False, "ERROR: Access is denied."))
    jobs_file.write_bytes(b"damaged")
    tab.refresh_backup()

    tab.start_empty_btn.click()

    assert "Could not list scheduled tasks" in tab.shown[0]
    assert "Could not list scheduled tasks" in tab.backup_status.text()
    assert len(kept_files(jobs_file.parent)) == 1 and tab.job_add_btn.isEnabled()


def test_a_file_repaired_meanwhile_is_left_alone(tab, jobs_file):
    jobs_file.write_bytes(b"damaged")
    tab.refresh_backup()
    jobs_file.write_bytes(VALID)                  # fixed by hand while the tab was open

    tab.start_empty_btn.click()

    assert jobs_file.read_bytes() == VALID and kept_files(jobs_file.parent) == []
    assert "left alone" in tab.backup_status.text() and "Nothing was changed" in tab.backup_status.text()
    assert [j["id"] for j in tab.jobs] == ["ab12cd34"]
    assert tab.start_empty_btn.isHidden() and tab.job_add_btn.isEnabled()


def test_a_failed_rename_is_shown_and_the_damaged_state_stays(tab, jobs_file, monkeypatch):
    jobs_file.write_bytes(b"damaged")
    tab.refresh_backup()
    failing_rename(monkeypatch, {str(jobs_file)})

    tab.start_empty_btn.click()

    assert "Could not set aside" in tab.backup_status.text()
    assert jobs_file.read_bytes() == b"damaged"
    assert not tab.start_empty_btn.isHidden() and not tab.job_add_btn.isEnabled()
