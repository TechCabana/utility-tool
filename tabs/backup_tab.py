# tabs/backup_tab.py
"""The Backup page: configured jobs above, a separate Restore table below.

Moved out of the old Disk tab, where it was the third of three sub-tabs, when
the app was cut down to backup only. The widgets, the worker and the job flow
are the same code as before; only the host changed, from a QTabWidget page to
a sidebar page of its own.
"""
import datetime
import os
import re
from pathlib import Path
from typing import List, Optional

from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtCore import Qt

from utils import backup_utils
from utils.fs_utils import human_size
from widgets.common import (ConfirmDialog, ElidedLabel, EmptyState, PageHeader,
                            divider, icon_button, section, table_header)


class BackupWorker(QtCore.QObject):
    """Runs one long backup-related filesystem call off the UI thread.

    One worker rather than three near-identical ones. Taking a backup,
    restoring a version and measuring what has changed since the last backup
    are all "walk and copy through utils/backup_utils, report (done, total),
    hand back a result dict" -- they differ only in which function is called,
    so the work is passed in as a callable taking `(on_progress,
    should_abort, on_phase)` instead of being three copies of this class.
    Only a backup run reports phases; the other two ignore `on_phase`.

    A plain QObject moved onto a QThread, started by `thread.started`,
    reporting only through signals.
    Copying a real backup source is minutes of disk I/O, which is exactly the
    kind of work CLAUDE.md §3 forbids on the UI thread.
    """
    progress = QtCore.Signal(int, int)   # done, total
    phase = QtCore.Signal(str)           # "copying", "verifying"
    finished = QtCore.Signal(dict)       # the result dict from backup_utils

    def __init__(self, work):
        super().__init__()
        self.work = work
        self._abort = False

    @QtCore.Slot()
    def run(self):
        try:
            result = self.work(self.progress.emit, lambda: self._abort,
                               self.phase.emit)
        except Exception as exc:
            # backup_utils turns every filesystem failure it expects into an
            # ok=False result, so reaching here means something genuinely
            # unforeseen. It still must not take the app down mid-batch.
            result = {"ok": False, "message": f"Unexpected failure: {exc}"}
        self.finished.emit(result)

    def abort(self):
        self._abort = True


class BackupJobDialog(QtWidgets.QDialog):
    """The Add / Edit form for one backup job.

    Source and Target are two separately labelled fields, never one combined
    "source -> target" string -- a named requirement from DESIGN.md v4 rule 4.

    Validation happens here, on the UI thread, before anything is written:
    a job pointing at a folder that does not exist would register a scheduled
    task that fails silently every night, which is worse than refusing to save
    it. The target-inside-source check is the one non-obvious rule -- backing a
    folder up into itself grows without limit, each run copying the previous
    run's copies.
    """

    def __init__(self, parent, job=None):
        super().__init__(parent)
        self.setWindowTitle("Edit backup job" if job else "Add backup job")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.job = dict(job) if job else None

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        outer.addWidget(card)

        v = QtWidgets.QVBoxLayout(card)
        v.setSpacing(10)

        heading = QtWidgets.QLabel("Edit backup job" if job else "Add backup job")
        heading.setObjectName("H2")
        v.addWidget(heading)

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft)

        self.name_edit = QtWidgets.QLineEdit((job or {}).get("name", ""))
        self.name_edit.setPlaceholderText("Documents nightly")
        form.addRow(self._label("Name"), self.name_edit)

        self.source_edit = QtWidgets.QLineEdit((job or {}).get("source", ""))
        form.addRow(self._label("Source"),
                    self._picker(self.source_edit, "Choose the folder to back up"))

        self.target_edit = QtWidgets.QLineEdit((job or {}).get("target", ""))
        form.addRow(self._label("Target"),
                    self._picker(self.target_edit, "Choose where backups are stored"))

        self.schedule = QtWidgets.QComboBox()
        self.schedule.addItems(["Manual only", "Daily", "Weekly (Monday)"])
        self.schedule.setCurrentIndex(
            {"manual": 0, "daily": 1, "weekly": 2}.get(
                (job or {}).get("schedule", "daily"), 1))
        form.addRow(self._label("Schedule"), self.schedule)

        # A plain masked QLineEdit rather than a QTimeEdit: the QSS styles
        # QLineEdit and QSpinBox, and a QTimeEdit is neither, so it would be
        # the one unthemed control on the screen.
        self.at_edit = QtWidgets.QLineEdit()
        self.at_edit.setInputMask("99:99")
        self.at_edit.setText((job or {}).get("at", "20:00"))
        self.at_edit.setMaximumWidth(90)
        form.addRow(self._label("Run at"), self.at_edit)

        self.retention = QtWidgets.QSpinBox()
        self.retention.setRange(1, 50)
        self.retention.setValue(int((job or {}).get(
            "retention", backup_utils.DEFAULT_RETENTION)))
        self.retention.setMaximumWidth(90)
        form.addRow(self._label("Versions to keep"), self.retention)

        v.addLayout(form)

        hint = QtWidgets.QLabel(
            "Each run copies the source into a new timestamped folder under "
            "the target. Older versions beyond the number kept are deleted. A "
            f"scheduled job is registered with {backup_utils.scheduler_name()} "
            "and runs without opening this app.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.error = QtWidgets.QLabel()
        self.error.setObjectName("StatusError")
        self.error.setWordWrap(True)
        self.error.setVisible(False)
        v.addWidget(self.error)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setObjectName("Secondary")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QtWidgets.QPushButton("Save job")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self.on_save)
        row.addWidget(save)
        v.addLayout(row)

        shadow = QtWidgets.QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 2)
        shadow.setColor(QtGui.QColor(24, 24, 27, 28))
        card.setGraphicsEffect(shadow)

    def _label(self, text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setObjectName("FieldLabel")
        return label

    def _picker(self, edit: QtWidgets.QLineEdit, caption: str) -> QtWidgets.QWidget:
        host = QtWidgets.QWidget()
        host.setObjectName("CardBody")
        h = QtWidgets.QHBoxLayout(host)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(edit, 1)
        browse = QtWidgets.QPushButton("Browse...")
        browse.setObjectName("Secondary")
        browse.clicked.connect(lambda: self._browse(edit, caption))
        h.addWidget(browse)
        return host

    def _browse(self, edit: QtWidgets.QLineEdit, caption: str):
        chosen = QtWidgets.QFileDialog.getExistingDirectory(
            self, caption, edit.text() or str(Path.home()))
        if chosen:
            edit.setText(os.path.normpath(chosen))

    def _fail(self, message: str) -> None:
        self.error.setText(message)
        self.error.setVisible(True)

    def on_save(self):
        name = self.name_edit.text().strip()
        source = self.source_edit.text().strip()
        target = self.target_edit.text().strip()
        at = self.at_edit.text().strip()

        if not name:
            return self._fail("Give the job a name.")
        if not os.path.isdir(source):
            return self._fail("The source folder does not exist.")
        if not target:
            return self._fail("Choose a target folder for the backups.")
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            return self._fail("Run at must be a 24-hour time, e.g. 20:00.")

        # The rule lives in backup_utils so run_job enforces the same one.
        if backup_utils.target_inside_source(source, target):
            return self._fail("The target cannot be inside the source folder.")

        # Record which drive the target is on, so a later run can tell if a
        # different one is now mounted there. Kept as-is when only the name or
        # schedule changed, so editing never re-trusts a swapped drive.
        if (self.job is None or self.job.get("target") != target
                or not self.job.get("target_volume")):
            volume = backup_utils.target_volume(target)
        else:
            volume = self.job["target_volume"]

        schedule = ("manual", "daily", "weekly")[self.schedule.currentIndex()]
        if self.job is None:
            self.job = backup_utils.new_job(name, source, target, schedule, at,
                                            self.retention.value(), volume)
        else:
            self.job.update({"name": name, "source": source, "target": target,
                             "schedule": schedule, "at": at,
                             "retention": self.retention.value(),
                             "target_volume": volume})
        self.accept()


# The Backup tables' columns, defined once as (label, stretch, minimum width)
# and used to build both the header row and every data row. Two copies of a
# column list drift the moment one of them is edited, and a header whose
# columns no longer line up with its rows is exactly the complaint DESIGN.md
# v4 rule 3 was raised about.
#
# The minimum widths are what stop a long path from crushing the other columns
# to a single elided character. The page's QScrollArea keeps its horizontal
# scrollbar policy at the Qt default (as-needed) rather than switching it off
# outright, because at the app's documented minimum
# window (940x620) this table's real minimum still runs a few px past the
# viewport even with four columns and tightened row spacing (measured
# 2026-09-12: ~8px). Closing that outright means shrinking a column below
# this table, or the shared Card/IconButton padding used everywhere in the
# app; both are real tradeoffs an owner should make, not a layout tweak.
#
# Four columns, not six. Source and Target used to be columns of their own,
# and their minimum widths plus three text buttons summed wider than the app's
# own minimum window: the page grew a horizontal scrollbar, the last button
# was cut off, and the prose above the table was clipped mid-sentence. The two
# paths now sit on a second line inside the Job cell, where they have the full
# row width to elide into, and the row's three actions are icon buttons.
JOB_COLUMNS = (
    ("Job", 5, 180), ("Schedule", 3, 92), ("Status", 2, 62), ("Last run", 3, 96),
)
JOB_ACTION_WIDTH = 108

RESTORE_COLUMNS = (
    ("Job", 3, 120), ("Last backup", 3, 104), ("Changes since", 6, 190),
)
RESTORE_ACTION_WIDTH = 110


def _tail(path: str, segments: int = 2) -> str:
    """The last few segments of a path, for a narrow table column.

    A backup path is identified by its leaf and its parent, not by the
    fifteen directories above them, and those are what survive at this column
    width. The full path is always the row's tooltip.
    """
    parts = [part for part in os.path.normpath(path).split(os.sep) if part]
    if len(parts) <= segments:
        return path
    return "..." + os.sep + os.sep.join(parts[-segments:])


class JobRow(QtWidgets.QWidget):
    """One configured backup job: the six columns plus its three actions.

    Source and Target elide rather than wrap -- see `ElidedLabel` for why a
    wrapped path breaks this particular layout and no other in the app.
    """
    run_requested = QtCore.Signal(dict)
    edit_requested = QtCore.Signal(dict)
    remove_requested = QtCore.Signal(dict)

    def __init__(self, job: dict):
        super().__init__()
        self.setObjectName("CardBody")
        self.job = job

        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 10, 0, 10)
        # 4, not the app's usual 12, for a row like this: at 940px (the
        # app's minimum) four columns plus three action buttons plus six
        # gaps doesn't fit at 12 -- see the JOB_COLUMNS comment above. This
        # narrows the horizontal-scroll overflow from 32px to ~8px but does
        # not close it outright; the rest would mean shrinking a column
        # below its documented minimum or the shared Card/IconButton
        # padding, both real tradeoffs, not a layout-only fix.
        h.setSpacing(4)

        status, style = self._status(job)
        cells = [
            (self._identity(job), None),
            (self._hint(backup_utils.schedule_label(job)), None),
            (self._hint(status), style),
            (self._data(self._last_run(job)), None),
        ]
        for (widget, style_name), (_, stretch, minimum) in zip(cells, JOB_COLUMNS):
            if style_name:
                widget.setObjectName(style_name)
            widget.setMinimumWidth(minimum)
            h.addWidget(widget, stretch)

        # Icon buttons, so three per-row actions cost 108px instead of 240 --
        # each keeps a tooltip and an accessible name, which is what a text
        # label was carrying.
        for icon_name, tooltip, signal in (
            ("play", "Run this job now", self.run_requested),
            ("pencil", "Edit this job", self.edit_requested),
            ("trash", "Remove this job", self.remove_requested),
        ):
            name = job.get("name", "this job")
            button = icon_button(icon_name, tooltip, f"{tooltip}: {name}")
            if icon_name == "trash":
                button.setObjectName("GhostDanger")
            button.clicked.connect(lambda _=False, s=signal: s.emit(self.job))
            h.addWidget(button)

    def _text(self, value: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(value)
        label.setWordWrap(True)
        return label

    def _data(self, value: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(value)
        label.setObjectName("DataMuted")
        return label

    def _identity(self, job: dict) -> QtWidgets.QWidget:
        """The Job cell: the name, and under it where it reads and writes.

        Source and Target keep their own labels rather than being joined into
        one arrow string -- DESIGN.md v4 rule 4 records the owner asking for
        two labelled values, and an arrow between two elided paths is
        unreadable anyway.
        """
        host = QtWidgets.QWidget()
        host.setObjectName("CardBody")
        column = QtWidgets.QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)

        # Elided, not wrapped: a job name is user-supplied, so it can be one
        # long unbroken string and blow the column out exactly as a path does.
        name = ElidedLabel(job.get("name", "(unnamed)"))
        name.setToolTip(job.get("name", ""))
        column.addWidget(name)

        for caption, value in (("From", job.get("source", "")), ("To", job.get("target", ""))):
            line = QtWidgets.QWidget()
            line.setObjectName("CardBody")
            row = QtWidgets.QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            label = QtWidgets.QLabel(caption)
            label.setObjectName("Hint")
            label.setMinimumWidth(30)
            row.addWidget(label)
            row.addWidget(self._path(value), 1)
            column.addWidget(line)
        return host

    def _hint(self, value: str) -> QtWidgets.QLabel:
        label = self._text(value)
        label.setObjectName("Hint")
        return label

    def _path(self, value: str) -> QtWidgets.QLabel:
        label = ElidedLabel(_tail(value) if value else "(not set)")
        label.setObjectName("DataMuted")
        # The full path, never the tail -- the column is a glance, the tooltip
        # is the answer to "which folder exactly".
        label.setToolTip(value or "(not set)")
        return label

    def _status(self, job: dict):
        """The Status column: what the last run did, or that none has run."""
        result = job.get("last_result", "")
        if not job.get("last_run"):
            return "Never run", "Hint"
        if result.startswith("ok"):
            return "OK", "StatusOk"
        return "Failed", "StatusError"

    def _last_run(self, job: dict) -> str:
        """When this job last ran, short enough to fit its column.

        Stored as an ISO timestamp so it sorts and parses. Shown as
        "09 Sep 08:49" rather than "2026-09-09 08:49": the year is almost
        never the thing being read here, and the sixteen-character form set
        the column's minimum width, which was what pushed the whole table
        past the app's 940px minimum window. Dropping it is the one saving
        available that costs no information anyone is actually using and
        touches no shared token.

        A run from a previous year keeps its year, since that is exactly the
        case where the year IS the point.
        """
        stamp = job.get("last_run", "")
        if not stamp:
            return "-"
        try:
            moment = datetime.datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            # Never let an unparseable stored value break the row.
            return stamp.replace("T", " ")[:16]
        if moment.year != datetime.datetime.now().year:
            return moment.strftime("%d %b %Y")
        return moment.strftime("%d %b %H:%M")


class RestoreRow(QtWidgets.QWidget):
    """One job's restore row: last backup, what has changed since, Restore.

    One row per job, never a per-snapshot history -- DESIGN.md v4 rule 5
    records the owner removing exactly that. Version browsing, if it is ever
    wanted, is an action off this row rather than a second table.
    """
    restore_requested = QtCore.Signal(dict)
    edit_requested = QtCore.Signal(dict)

    def __init__(self, job: dict, changes: dict):
        super().__init__()
        self.setObjectName("CardBody")
        self.job = job

        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 10, 0, 10)
        h.setSpacing(4)  # matches JobRow -- see the note there

        when = changes.get("when") if changes.get("ok") else None
        last = QtWidgets.QLabel(when or "Never backed up")
        last.setObjectName("Hint")

        text, style = self._changes(changes)
        changed = QtWidgets.QLabel(text)
        changed.setObjectName(style)
        changed.setWordWrap(True)

        cells = (ElidedLabel(job.get("name", "(unnamed)")), last, changed)
        for widget, (_, stretch, minimum) in zip(cells, RESTORE_COLUMNS):
            widget.setMinimumWidth(minimum)
            h.addWidget(widget, stretch)

        # A row whose folders cannot be read offers the thing that fixes it -
        # the job's own Edit dialog - instead of a Restore button that is
        # disabled for a reason the row does not explain. Where the message is
        # merely "never backed up" there is nothing to fix, so the ordinary
        # (disabled) Restore stays.
        if self._is_broken(changes):
            self.fix_btn = QtWidgets.QPushButton("Fix job...")
            self.fix_btn.setObjectName("Secondary")
            self.fix_btn.setToolTip(
                "Open this job and correct its Source or Target folder")
            self.fix_btn.setAccessibleName(
                f"Fix the folders for {job.get('name', 'this job')}")
            self.fix_btn.clicked.connect(lambda: self.edit_requested.emit(self.job))
            h.addWidget(self.fix_btn)
            self.restore_btn = self.fix_btn
            return

        self.restore_btn = QtWidgets.QPushButton("Restore...")
        self.restore_btn.setObjectName("Secondary")
        # Nothing to restore from until a version exists on disk.
        self.restore_btn.setEnabled(bool(changes.get("ok")))
        self.restore_btn.clicked.connect(lambda: self.restore_requested.emit(self.job))
        h.addWidget(self.restore_btn)

    @staticmethod
    def _is_broken(changes: dict) -> bool:
        """True when the failure is something editing the job would fix.

        Reads the flag `changes_since` sets rather than matching on message
        text. The first version of this asked "is the message anything other
        than 'Never backed up'", which also caught "Cancelled" - so a healthy
        job whose comparison had simply not finished was offered a Fix button
        for a problem it did not have.
        """
        return bool(changes.get("fixable"))

    def _changes(self, changes: dict):
        """The changes-since column, coloured by what it means.

        Green for "nothing has changed", warn for real drift, error for a
        source or a backup this app could not read -- colour as data, per
        DESIGN.md, not decoration.
        """
        if not changes.get("ok"):
            message = changes.get("message", "Not measured")
            return message, ("Hint" if message == "Never backed up"
                             else "StatusError")
        count = changes.get("changed", 0)
        if count == 0:
            return "Up to date", "StatusOk"
        parts = []
        for key, word in (("added", "added"), ("modified", "modified"),
                          ("removed", "removed")):
            if changes.get(key):
                parts.append(f"{changes[key]} {word}")
        detail = ", ".join(parts)
        size = human_size(changes.get("bytes", 0))
        return f"{count} file(s) changed ({detail}) - {size} to copy", "StatusWarn"


class BackupTab(QtWidgets.QWidget):
    """The Backup screen: the jobs table and, below it, the Restore table.

    Restore is a table of its own (DESIGN.md v4 rule 5), not a history
    nested in each job row.
    """

    def __init__(self):
        super().__init__()
        self.home = str(Path.home())

        # One backup operation at a time -- taking a backup, restoring a
        # version and measuring changes all read or write the same trees, and
        # running two of them together would report against a moving target.
        self.backup_thread: Optional[QtCore.QThread] = None
        self.backup_worker: Optional[BackupWorker] = None
        self.jobs: List[dict] = []
        self._changes: dict = {}
        # True while the jobs file is damaged: the tables are replaced by a
        # notice, and nothing may be written until it is restored.
        self._jobs_damaged = False
        self._can_restore_jobs = False   # a usable .bak copy exists
        self._backup_loaded = False
        self._backup_done = None      # the handler for the run in flight
        self._remeasure = False       # a backup just ran; its drift is stale
        # Carries a run's result line across the remeasure that follows it,
        # so the remeasure's own outcome does not erase what just happened.
        self._backup_notice = ""

        v = QtWidgets.QVBoxLayout(self)
        # Margins leave room for the card drop shadows to render un-clipped
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(10)

        v.addWidget(PageHeader(
            "Backup",
            "Copy folders to another drive on a schedule, keep the last few "
            "versions, and get files back. Nothing is overwritten on restore."))
        v.addWidget(self._build_backup(), 1)

        # Stop a running backup cleanly if the app quits mid-run, rather than
        # destroying a QThread that is still copying.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    def showEvent(self, event):
        """Load the jobs and measure their drift the first time Backup opens.

        Measuring drift walks every job's source and its last backup, so it
        waits until the page is actually shown rather than running on every
        app launch. QStackedWidget hides non-current pages, so this fires on
        first selection.
        """
        super().showEvent(event)
        if not self._backup_loaded:
            self._backup_loaded = True
            self.refresh_backup()
            self.refresh_changes()

    def shutdown(self):
        """Abort and join the backup worker. Wired to QApplication.aboutToQuit."""
        if self.backup_worker:
            self.backup_worker.abort()
        if self.backup_thread:
            self.backup_thread.quit()
            self.backup_thread.wait()
        self.backup_thread = self.backup_worker = None

    def _build_backup(self) -> QtWidgets.QWidget:
        """The Backup screen: one surface, Jobs then Restore as its sections.

        Restore stays a table of its own rather than an expandable history
        inside each job row: managing jobs and getting a file back are two
        different tasks, and DESIGN.md v4 rule 5 records the owner rejecting
        the nested snapshot list explicitly. Two sections of one surface is
        that separation without giving each its own bordered card.
        """
        page = QtWidgets.QWidget()
        page.setObjectName("CardBody")
        pv = QtWidgets.QVBoxLayout(page)
        # Margins so the surface's drop shadow has room inside the viewport.
        pv.setContentsMargins(2, 2, 8, 8)
        pv.setSpacing(0)

        surface = QtWidgets.QFrame()
        surface.setObjectName("Card")
        jv = QtWidgets.QVBoxLayout(surface)
        jv.setSpacing(8)
        pv.addWidget(surface)

        # --- jobs ---------------------------------------------------------
        self.job_add_btn = QtWidgets.QPushButton("Add job")
        self.job_add_btn.setObjectName("Primary")
        self.job_add_btn.clicked.connect(self.add_backup_job)
        jv.addWidget(section("Backup jobs", self.job_add_btn))

        note = QtWidgets.QLabel(
            "A job copies its source folder into a new timestamped folder "
            "under its target every time it runs, keeping the most recent "
            "versions and deleting the rest. Scheduled jobs are handed to "
            f"{backup_utils.scheduler_name()} and run without this app "
            "being open.")
        note.setObjectName("Hint")
        note.setWordWrap(True)
        jv.addWidget(note)

        self.job_header = self._table_header(JOB_COLUMNS, JOB_ACTION_WIDTH)
        jv.addWidget(self.job_header)

        self.jobs_host = QtWidgets.QWidget()
        self.jobs_host.setObjectName("CardBody")
        self.jobs_layout = QtWidgets.QVBoxLayout(self.jobs_host)
        self.jobs_layout.setContentsMargins(0, 0, 0, 0)
        # Row separation is each row's own margins plus the divider() between
        # them, so the layout adds nothing on top.
        self.jobs_layout.setSpacing(0)
        jv.addWidget(self.jobs_host)

        self.jobs_empty = EmptyState(
            title="No backup jobs yet",
            hint="Add a job to copy a folder to another drive on a schedule, "
                 "keeping the last few versions.",
        )
        jv.addWidget(self.jobs_empty)

        # Shown instead of the tables when backup_jobs.json is damaged. Same
        # EmptyState the empty case uses, with its one action: bring back the
        # last good copy. Hidden until refresh_backup() meets a bad file.
        self.jobs_damaged = EmptyState(
            title="The backup jobs file is damaged",
            hint="Your jobs could not be read, so nothing has been changed or "
                 "run. Restoring brings back the last good copy, which may "
                 "not include your most recent edit; the damaged file is "
                 "kept beside it, not deleted.",
            cta_text="Restore from backup copy",
        )
        self.jobs_damaged.cta.clicked.connect(self.restore_jobs_file)
        self.jobs_damaged.setVisible(False)
        jv.addWidget(self.jobs_damaged)

        self.backup_bar = QtWidgets.QProgressBar()
        self.backup_bar.setObjectName("WithText")
        self.backup_bar.setVisible(False)
        jv.addWidget(self.backup_bar)

        self.backup_status = QtWidgets.QLabel()
        self.backup_status.setObjectName("Hint")
        self.backup_status.setWordWrap(True)
        self.backup_status.setVisible(False)
        jv.addWidget(self.backup_status)

        # --- restore ------------------------------------------------------
        # Same surface, second section. `rv` stays a separate name so the two
        # halves of this method remain easy to read apart.
        rv = jv
        self.restore_refresh_btn = QtWidgets.QPushButton("Refresh")
        self.restore_refresh_btn.setObjectName("Secondary")
        self.restore_refresh_btn.clicked.connect(self.refresh_changes)
        rv.addWidget(section("Restore", self.restore_refresh_btn))

        rnote = QtWidgets.QLabel(
            "One row per job: when it was last backed up, and how much of the "
            "source has changed since. Restoring copies a stored version into "
            "a new folder you choose -- it never writes over the live source.")
        rnote.setObjectName("Hint")
        rnote.setWordWrap(True)
        rv.addWidget(rnote)

        self.restore_header = self._table_header(RESTORE_COLUMNS,
                                                 RESTORE_ACTION_WIDTH)
        rv.addWidget(self.restore_header)

        self.restore_host = QtWidgets.QWidget()
        self.restore_host.setObjectName("CardBody")
        self.restore_layout = QtWidgets.QVBoxLayout(self.restore_host)
        self.restore_layout.setContentsMargins(0, 0, 0, 0)
        # Same as the jobs table above: rows carry their own margins and a
        # divider() sits between them.
        self.restore_layout.setSpacing(0)
        rv.addWidget(self.restore_host)

        self.restore_empty = EmptyState(
            title="Nothing to restore yet",
            hint="Once a job has run at least once, its last backup and what "
                 "has changed since show up here.",
        )
        rv.addWidget(self.restore_empty)

        pv.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setAccessibleName("Backup content")
        # No horizontal scrollbar here either, as of the table reshape: four
        # columns with the paths on a second line fit inside the app's own
        # 940px minimum, so nothing needs to scroll sideways. The column
        # minimums still exist, and still stop one deep path from eliding
        # every other cell to a single character - they are just small enough
        # now to fit. If a future column pushes the total back over the
        # window, reshape the table again rather than restoring the scrollbar.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    def _table_header(self, columns, action_width: int) -> QtWidgets.QWidget:
        """A labelled header row over a repeated-row table.

        DESIGN.md v4 rule 3 requires one over every such table, and was a
        named complaint rather than a preference. It takes the same column
        spec the rows do -- same stretch, same minimum width -- so the two
        cannot drift apart, and `action_width` reserves the row's buttons.
        """
        # The labels and the rule that underlines them are one widget: the
        # callers show and hide the header as a unit, and a rule left behind
        # over an empty table reads as a table with a missing first row.
        header = QtWidgets.QWidget()
        header.setObjectName("CardBody")
        v = QtWidgets.QVBoxLayout(header)
        v.setContentsMargins(0, 8, 0, 0)
        v.setSpacing(8)

        row = QtWidgets.QWidget()
        row.setObjectName("CardBody")
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)  # must match JobRow/RestoreRow or columns misalign
        for text, stretch, minimum in columns:
            label = table_header(text)
            label.setMinimumWidth(minimum)
            h.addWidget(label, stretch)
        spacer = QtWidgets.QLabel("")
        spacer.setMinimumWidth(action_width)
        h.addWidget(spacer)

        v.addWidget(row)
        v.addWidget(divider())
        return header

    def refresh_backup(self):
        """Re-read the jobs file and rebuild both tables.

        A damaged file is not "no jobs": the tables give way to a notice with
        a restore action instead, and nothing is written.
        """
        try:
            self.jobs = backup_utils.load_jobs()
        except backup_utils.JobsFileCorrupt as exc:
            self._show_jobs_damaged(exc)
            return
        self._jobs_damaged = False
        self.jobs_damaged.setVisible(False)
        self._clear_layout(self.jobs_layout)
        self._clear_layout(self.restore_layout)

        self.jobs_empty.setVisible(not self.jobs)
        self.job_header.setVisible(bool(self.jobs))
        for index, job in enumerate(self.jobs):
            # A hairline between rows but not after the last one.
            if index:
                self.jobs_layout.addWidget(divider())
            row = JobRow(job)
            row.run_requested.connect(self.run_backup_job)
            row.edit_requested.connect(self.edit_backup_job)
            row.remove_requested.connect(self.remove_backup_job)
            self.jobs_layout.addWidget(row)

        self.restore_empty.setVisible(not self.jobs)
        self.restore_header.setVisible(bool(self.jobs))
        for index, job in enumerate(self.jobs):
            if index:
                self.restore_layout.addWidget(divider())
            row = RestoreRow(job, self._changes.get(job["id"], {}))
            row.restore_requested.connect(self.restore_backup_job)
            # A broken row offers Fix job..., which is the same Edit dialog
            # the jobs table above opens.
            row.edit_requested.connect(self.edit_backup_job)
            self.restore_layout.addWidget(row)
        self._set_backup_controls(enabled=self.backup_thread is None)

    def _show_jobs_damaged(self, exc: "backup_utils.JobsFileCorrupt"):
        """Swap both tables for the damaged-file notice and say why."""
        self.jobs = []
        self._changes = {}
        self._jobs_damaged = True
        self._can_restore_jobs = exc.has_backup
        self._clear_layout(self.jobs_layout)
        self._clear_layout(self.restore_layout)
        for widget in (self.jobs_empty, self.job_header,
                       self.restore_empty, self.restore_header):
            widget.setVisible(False)
        self.jobs_damaged.setVisible(True)
        self._set_backup_controls(enabled=self.backup_thread is None)
        self._say(str(exc), ok=False)

    def restore_jobs_file(self):
        """Put the last good copy of the jobs file back, then reload."""
        if self.backup_thread is not None:
            return
        try:
            count = backup_utils.restore_jobs_from_backup()
        except (backup_utils.JobsFileCorrupt, OSError) as exc:
            self._say(f"{exc} Nothing was changed.", ok=False)
            return
        message = (f"Restored {count} job(s) from the backup copy. The damaged "
                   "file was kept beside it with a .corrupt- name.")
        self._jobs_damaged = False
        self._backup_notice = message
        self.refresh_backup()
        self.refresh_changes()
        if self.backup_thread is None:   # no worker started to show it later
            self._backup_notice = ""
            self._say(message)

    def _clear_layout(self, layout: QtWidgets.QLayout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _set_backup_controls(self, enabled: bool):
        """Lock the whole Backup screen while one operation is in flight.

        Re-enabling is deliberately *not* the mirror of disabling: a Restore
        button for a job that has never run is disabled on its own merits, and
        a blanket re-enable would offer a restore from a backup that does not
        exist. `refresh_backup()` rebuilds every row in its correct state
        after each operation, so unlocking only has to release the two
        screen-level buttons.
        """
        self.job_add_btn.setEnabled(enabled and not self._jobs_damaged)
        self.restore_refresh_btn.setEnabled(enabled)
        self.jobs_damaged.cta.setEnabled(enabled and self._can_restore_jobs)
        if enabled:
            return
        for host in (self.jobs_host, self.restore_host):
            for button in host.findChildren(QtWidgets.QPushButton):
                button.setEnabled(False)

    def _say(self, message: str, ok: bool = True):
        self.backup_status.setObjectName("Hint" if ok else "StatusError")
        # An objectName change after the sheet was applied needs the style
        # re-polished, or the widget keeps the rules it was first matched by.
        self.backup_status.style().unpolish(self.backup_status)
        self.backup_status.style().polish(self.backup_status)
        self.backup_status.setText(message)
        self.backup_status.setVisible(True)

    def add_backup_job(self):
        dialog = BackupJobDialog(self)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self._save_and_schedule(dialog.job, "Added")

    def edit_backup_job(self, job: dict):
        dialog = BackupJobDialog(self, job)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        self._save_and_schedule(dialog.job, "Updated")

    def _save_and_schedule(self, job: dict, verb: str):
        """Store the job, then make the OS scheduler match it.

        Two steps reported separately on purpose: a job that saved but could
        not be scheduled is still a usable job (Run Now works), and saying so
        is more useful than one combined "failed".
        """
        try:
            backup_utils.save_job(job)
        except backup_utils.JobsFileCorrupt as exc:
            # Nothing was written; the job is not stored or scheduled.
            self.refresh_backup()
            self._say(f"\"{job['name']}\" was not saved. {exc}", ok=False)
            return
        except OSError as exc:
            self._say(f"\"{job['name']}\" was not saved: {exc}", ok=False)
            return
        ok, detail = backup_utils.sync_task(job)
        message = f"{verb} job \"{job['name']}\"."
        if job.get("schedule") == "manual":
            message += " It runs only when you press Run Now."
        elif ok:
            message += f" Registered with {backup_utils.scheduler_name()}."
        else:
            message += f" It could not be scheduled: {detail}"
        self._say(message, ok=ok or job.get("schedule") == "manual")
        self.refresh_backup()
        self.refresh_changes()

    def remove_backup_job(self, job: dict):
        """Confirm, then drop the job and its scheduled task together.

        Leaving the task behind would run a backup for a job the user
        believes they deleted, so the two are removed in one action.
        """
        try:
            backup_utils.load_jobs()   # before anything is unscheduled
        except backup_utils.JobsFileCorrupt:
            self.refresh_backup()
            return
        if not ConfirmDialog.ask(
            self,
            "Remove this backup job?",
            f"\"{job.get('name')}\" and its Windows scheduled task are "
            "removed. Backups already written to the target folder are left "
            "exactly where they are, so nothing you have backed up is lost.",
            confirm_text="Remove job",
        ):
            return
        task_ok, task_detail = backup_utils.unregister_task(job["id"])
        try:
            backup_utils.remove_job(job["id"])
        except (backup_utils.JobsFileCorrupt, OSError) as exc:
            self.refresh_backup()
            self._say(f"\"{job.get('name')}\" was not removed from the "
                      f"list: {exc}", ok=False)
            return
        self._changes.pop(job["id"], None)
        name = job.get("name")
        if job.get("schedule") == "manual":
            # A manual job never had a scheduled task to begin with, so
            # unregister_task's result says nothing about a real failure --
            # don't claim a task was removed that was never registered.
            self._say(f"Removed job \"{name}\".")
        elif task_ok:
            self._say(f"Removed job \"{name}\" and its scheduled task.")
        else:
            # unregister_task's own "nothing to remove" case would also
            # report False, but a scheduled job (checked above) should have
            # had a task -- surface the failure instead of quietly claiming
            # removal, so a stray Task Scheduler entry doesn't go unnoticed.
            self._say(
                f"Removed job \"{name}\", but its Windows scheduled task "
                f"could not be removed: {task_detail}. Check Task Scheduler.",
                ok=False,
            )
        self.refresh_backup()

    def run_backup_job(self, job: dict):
        if self.backup_thread is not None:
            return
        self._start_backup(
            f"Backing up \"{job.get('name')}\"...",
            lambda progress, abort, phase: backup_utils.run_job(
                job, progress, abort, phase),
            lambda result: self._finish_run(job, result),
            phases={"verifying": f"Verifying backup \"{job.get('name')}\"..."},
        )

    def restore_backup_job(self, job: dict):
        """Copy the job's latest version into a folder the user picks.

        Restoring to a new location rather than over the live source: an
        overwrite would silently replace every edit made since the backup and
        there is no undo for it, and nothing in this feature asked for that.
        The restored folder is named for the job and the version so it is
        obvious what it is once it lands.
        """
        if self.backup_thread is not None:
            return
        version = backup_utils.latest_version(job)
        if version is None:
            self._say(f"\"{job.get('name')}\" has no backup to restore yet.",
                      ok=False)
            return
        destination = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Restore into which folder?", self.home)
        if not destination:
            return
        self._start_backup(
            f"Restoring \"{job.get('name')}\" from {version}...",
            lambda progress, abort, _phase: backup_utils.restore_version(
                job, version, destination, progress, abort),
            lambda result: self._say(result.get("message", ""),
                                     ok=result.get("ok", False)),
        )

    def refresh_changes(self):
        """Measure every job's drift since its last backup, off the UI thread.

        One worker for all jobs rather than one each: each is a walk of two
        whole trees, and running them together would mean several threads
        competing for the same disk to fill in one table.
        """
        if self.backup_thread is not None:
            return
        try:
            jobs = backup_utils.load_jobs()
        except backup_utils.JobsFileCorrupt:
            self.refresh_backup()   # shows the damaged-file notice
            return
        if not jobs:
            self.refresh_backup()
            return

        def work(progress, abort, _phase):
            measured = {}
            for index, job in enumerate(jobs, 1):
                if abort():
                    break
                progress(index, len(jobs))
                measured[job["id"]] = backup_utils.changes_since(job, None, abort)
            return {"ok": True, "message": "", "changes": measured}

        self._start_backup("Checking what has changed since each backup...",
                           work, self._finish_changes)

    def _start_backup(self, status: str, work, done, phases=None):
        """Run one backup-related call on a QThread, locking the screen.

        `phases` maps a phase name the work reports to the status line shown
        while it lasts; a phase not in it leaves the status line alone.
        """
        self._say(status)
        self.backup_bar.setRange(0, 0)  # indeterminate until a total arrives
        self.backup_bar.setValue(0)
        self.backup_bar.setVisible(True)
        self._set_backup_controls(enabled=False)

        self._backup_done = done
        self._backup_phases = phases or {}
        self.backup_thread = QtCore.QThread(self)
        self.backup_worker = BackupWorker(work)
        self.backup_worker.moveToThread(self.backup_thread)
        self.backup_worker.progress.connect(self.on_backup_progress)
        self.backup_worker.phase.connect(self.on_backup_phase)
        self.backup_worker.finished.connect(self.on_backup_finished)
        self.backup_thread.started.connect(self.backup_worker.run)
        self.backup_thread.start()

    def on_backup_progress(self, done: int, total: int):
        self.backup_bar.setRange(0, total)
        self.backup_bar.setValue(done)

    def on_backup_phase(self, phase: str):
        """Show the status line for the phase the worker just entered.

        A bound method, not a lambda, so the queued connection delivers it on
        the UI thread rather than running it on the worker's.
        """
        status = self._backup_phases.get(phase)
        if status:
            self._say(status)

    def on_backup_finished(self, result: dict):
        self.backup_bar.setVisible(False)
        if self.backup_thread:
            self.backup_thread.quit()
            self.backup_thread.wait()
            self.backup_thread = None
            self.backup_worker = None
        handler, self._backup_done = self._backup_done, None
        if handler is not None:
            handler(result)
        self.refresh_backup()
        if self._remeasure:
            self._remeasure = False
            self.refresh_changes()

    def _finish_run(self, job: dict, result: dict):
        """Record the run, then queue a remeasure of what changed since it.

        The remeasure is a second threaded pass, never a `changes_since` call
        from here: this runs on the UI thread, and walking both trees on it is
        the frozen window CLAUDE.md §3 exists to prevent. Its outcome rides in
        `_backup_notice` so the pass that follows does not overwrite the
        result line the user is reading.
        """
        message = result.get("message", "")
        try:
            backup_utils.record_run(job["id"], result)
        except (backup_utils.JobsFileCorrupt, OSError) as exc:
            self._say(f"{message} The result could not be recorded: {exc}",
                      ok=False)
            return
        self._say(message, ok=result.get("ok", False))
        if result.get("ok"):
            self._backup_notice = message
            self._remeasure = True

    def _finish_changes(self, result: dict):
        self._changes.update(result.get("changes", {}))
        notice, self._backup_notice = self._backup_notice, ""
        if notice:
            self._say(notice)
        else:
            self.backup_status.setVisible(False)
