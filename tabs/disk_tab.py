# tabs/disk_tab.py
import datetime
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6 import QtWidgets, QtCore, QtGui
from PySide6.QtCore import Qt

from utils import backup_utils
from utils.disk_utils import (
    CleanupCategory, DuplicateGroup, cleanup_scan, duplicate_roots,
    find_duplicate_files, find_duplicate_images, human_size,
    top_level_breakdown,
)
from utils.file_utils import delete_file
from widgets.common import (ConfirmDialog, ElidedLabel, EmptyState, PageHeader,
                            divider, icon_button, section, table_header)


class ScanWorker(QtCore.QObject):
    """Measures the home directory's top-level folders off the UI thread.

    Same shape as ImageWorker/FileWorker (CLAUDE.md §3): a plain QObject
    moved onto a QThread, kicked off by `thread.started`, reporting only
    through signals. Walking a home directory can take tens of seconds, so
    it must never touch the UI thread.

    It emits one sorted result set at the end rather than a row at a time --
    the breakdown is only meaningful once every folder has been measured and
    ranked, and `scanning` keeps the UI honest in the meantime.
    """
    scanning = QtCore.Signal(str)   # folder name currently being measured
    finished = QtCore.Signal(list)  # [(name, size_bytes, skipped), ...]

    def __init__(self, root: str):
        super().__init__()
        self.root = root
        self._abort = False

    @QtCore.Slot()
    def run(self):
        results = top_level_breakdown(
            self.root,
            should_abort=lambda: self._abort,
            on_folder=self.scanning.emit,
        )
        self.finished.emit(results)

    def abort(self):
        self._abort = True


def _shorten(path: str, home: str) -> str:
    """Show a path under the home directory as "~\\...", full path otherwise.

    The drill-down lists real paths so the user can see exactly what would be
    binned; dropping the repeated home prefix keeps them readable without
    hiding which file is which.
    """
    if home and path.lower().startswith(home.lower() + os.sep):
        return "~" + path[len(home):]
    return path


class CleanupScanWorker(QtCore.QObject):
    """Runs the eight-category Cleanup quick scan off the UI thread.

    Same shape as ScanWorker above -- a plain QObject moved onto a QThread,
    started by `thread.started`, reporting only through signals. The scan
    walks %TEMP%, AppData, the browser caches and three home folders, which
    takes seconds at best, so it cannot run on the UI thread.
    """
    scanning = QtCore.Signal(str)   # category name currently being measured
    finished = QtCore.Signal(list)  # [CleanupCategory, ...]

    def __init__(self, home: str):
        super().__init__()
        self.home = home
        self._abort = False

    @QtCore.Slot()
    def run(self):
        results = cleanup_scan(
            home=self.home,
            should_abort=lambda: self._abort,
            on_category=self.scanning.emit,
        )
        self.finished.emit(results)

    def abort(self):
        self._abort = True


class CleanWorker(QtCore.QObject):
    """Moves the selected paths to the Recycle Bin, one at a time.

    Reuses `utils.file_utils.delete_file` -- the same send2trash call the File
    Manager's Delete operation makes -- rather than a second deletion path.
    Nothing here is ever a permanent delete.

    The worker never prompts: the confirm dialog is shown on the UI thread
    before the thread starts, and this only applies the already-made decision.
    A per-item dialog from a worker thread is not safe in Qt.
    """
    progress = QtCore.Signal(int, int)      # done, total
    finished = QtCore.Signal(int, int, list)  # removed, freed_bytes, errors

    def __init__(self, items: List[Tuple[str, int]]):
        super().__init__()
        self.items = items
        self._abort = False

    @QtCore.Slot()
    def run(self):
        removed = 0
        freed = 0
        errors: List[str] = []
        total = len(self.items)
        for index, (path, size) in enumerate(self.items, 1):
            if self._abort:
                break
            try:
                delete_file(path)
                removed += 1
                freed += size
            except Exception as exc:
                # A file an app currently holds open cannot be moved. That is
                # normal for a live cache, so it is reported, never raised --
                # one locked file must not abandon the rest of the batch.
                errors.append(f"{os.path.basename(path)}: {exc}")
            self.progress.emit(index, total)
        self.finished.emit(removed, freed, errors)

    def abort(self):
        self._abort = True


class CategorySection(QtWidgets.QWidget):
    """One collapsible Cleanup category: checkbox, totals, expandable detail.

    Generic over the category it is given, so the same widget renders all
    eight quick-scan categories -- and would render a future Duplicates
    section too, since nothing here knows what a category contains beyond
    (path, size) pairs.

    Checking the box selects every item the category found; the drill-down is
    a read-only listing, per DESIGN.md's "transparency before deleting
    anything beats a category-level trust default".
    """
    toggled = QtCore.Signal()

    # The detail list is one QLabel, not one widget per file: %TEMP% alone can
    # hold thousands of entries, and 8 categories of those would cost more to
    # lay out than the screen can ever show. A cap plus a "and N more" line is
    # what the panel is for -- the full list is the checkbox's job.
    MAX_DETAIL_ROWS = 50

    def __init__(self, category: CleanupCategory):
        super().__init__()
        self.setObjectName("CardBody")
        self.category = category
        self._detail_built = False

        v = QtWidgets.QVBoxLayout(self)
        # Asymmetric on purpose. Before this the gap from a row to its own
        # description was almost the same as the gap to the next row, so a
        # description read as floating between two categories rather than
        # belonging to the one above it. Tight to its own hint, generous to
        # the next row -- and a divider() between rows in _render_sections.
        v.setContentsMargins(0, 10, 0, 12)
        v.setSpacing(2)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)

        self.check = QtWidgets.QCheckBox(category.name)
        self.check.setEnabled(bool(category.items) and category.removable)
        self.check.toggled.connect(self.toggled)
        head.addWidget(self.check, 4)

        if category.items:
            found = f"{len(category.items)} item(s)"
        elif not category.removable:
            found = "reported only"
        else:
            found = "nothing found"
        found_label = QtWidgets.QLabel(found)
        found_label.setObjectName("Hint")
        head.addWidget(found_label, 2)

        size_label = QtWidgets.QLabel(human_size(category.size))
        size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_label.setMinimumWidth(90)
        head.addWidget(size_label, 1)

        self.expand_btn = QtWidgets.QPushButton("Show files")
        self.expand_btn.setObjectName("Secondary")
        self.expand_btn.setEnabled(bool(category.items))
        self.expand_btn.clicked.connect(self.toggle_detail)
        head.addWidget(self.expand_btn)

        v.addLayout(head)

        hint = category.hint
        if category.note:
            hint = f"{hint}. {category.note}"
        if category.skipped:
            hint = (f"{hint}. {category.skipped} item(s) could not be read "
                    "and are not counted")
        hint = hint.rstrip(".") + "."
        hint_label = QtWidgets.QLabel(hint)
        hint_label.setObjectName("Hint")
        hint_label.setWordWrap(True)
        # Indented to start under the category name rather than under the
        # checkbox, so it hangs off its own row instead of sitting flush with
        # the column header above it.
        hint_label.setContentsMargins(26, 0, 0, 0)
        v.addWidget(hint_label)

        self.detail = QtWidgets.QLabel()
        self.detail.setObjectName("Hint")
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setContentsMargins(24, 4, 0, 4)
        # Without wrapping, one deep browser-cache path sets the label's width
        # and the whole Cleanup page grows a horizontal scrollbar.
        self.detail.setWordWrap(True)
        self.detail.setVisible(False)
        v.addWidget(self.detail)

    def toggle_detail(self):
        """Show or hide the file listing, building it the first time only."""
        if not self._detail_built:
            shown = self.category.items[:self.MAX_DETAIL_ROWS]
            home = str(Path.home())
            lines = [f"{human_size(size):>10}   {_shorten(path, home)}"
                     for path, size in shown]
            extra = len(self.category.items) - len(shown)
            if extra > 0:
                lines.append(f"... and {extra} more, all included when checked")
            self.detail.setText("\n".join(lines))
            self._detail_built = True
        visible = not self.detail.isVisible()
        self.detail.setVisible(visible)
        self.expand_btn.setText("Hide files" if visible else "Show files")

    def selected_items(self) -> List[Tuple[str, int]]:
        """The (path, size) pairs this category contributes to Clean Selected."""
        return list(self.category.items) if self.check.isChecked() else []


class DuplicateScanWorker(QtCore.QObject):
    """Runs one Duplicates scan -- Files or Images mode -- off the UI thread.

    Same shape as the two scan workers above: a plain QObject moved onto a
    QThread, started by `thread.started`, reporting only through signals.
    Hashing every same-sized file in Downloads/Documents/Desktop, or opening
    every image in them, is minutes of work in the worst case, so this is the
    one part of the tab that absolutely cannot run on the UI thread.
    """
    progress = QtCore.Signal(int, int)     # files fingerprinted, total
    finished = QtCore.Signal(list, int)    # [DuplicateGroup, ...], skipped

    # One signal per file would queue thousands of cross-thread events for a
    # scan whose whole point is that it is long. The bar does not need every
    # step -- it needs to move.
    REPORT_EVERY = 25

    def __init__(self, mode: str, home: str):
        super().__init__()
        self.mode = mode            # "files" | "images"
        self.home = home
        self._abort = False

    @QtCore.Slot()
    def run(self):
        finder = (find_duplicate_images if self.mode == "images"
                  else find_duplicate_files)
        groups, skipped = finder(
            duplicate_roots(self.home),
            should_abort=lambda: self._abort,
            on_progress=self._report,
        )
        self.finished.emit(groups, skipped)

    def _report(self, done: int, total: int):
        if done == total or done % self.REPORT_EVERY == 0:
            self.progress.emit(done, total)

    def abort(self):
        self._abort = True


class DuplicateGroupSection(QtWidgets.QWidget):
    """One duplicate group: a summary row expanding to its individual files.

    Deliberately *not* a `CategorySection`. That widget carries one checkbox
    for a whole category and renders its detail as a read-only label, which is
    exactly right for "delete this pile of cache" and exactly wrong here: a
    duplicate group is a per-file decision, one file in it must never be
    removable, and the copies need to arrive already checked. The two share
    the collapsible-summary shape, not an implementation.

    Selection is held in `_checked`, not in the checkbox widgets, because the
    file rows are built lazily on first expand -- a group the user never opens
    still has to know its copies are selected.
    """
    toggled = QtCore.Signal()

    def __init__(self, group: DuplicateGroup, number: int, show_similarity: bool):
        super().__init__()
        self.setObjectName("CardBody")
        self.group = group
        self._detail_built = False
        self._checked = {path: True for path, _ in group.duplicates}

        home = str(Path.home())
        v = QtWidgets.QVBoxLayout(self)
        # Same asymmetric rhythm as CategorySection: tight to the group's own
        # detail, generous to the next group.
        v.setContentsMargins(0, 10, 0, 12)
        v.setSpacing(2)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)

        # The similarity badge lives inside the Group cell, not as a column of
        # its own: an extra header-level widget in Images mode and not in
        # Files mode would put the size out of line with its own column
        # header, which is the exact complaint DESIGN.md v4 rule 3 records.
        copies = len(group.duplicates)
        group_cell = QtWidgets.QWidget()
        group_cell.setObjectName("CardBody")
        gh = QtWidgets.QHBoxLayout(group_cell)
        gh.setContentsMargins(0, 0, 0, 0)
        gh.setSpacing(8)
        title = QtWidgets.QLabel(f"Group {number} - {copies + 1} matching file(s)")
        gh.addWidget(title)
        if show_similarity:
            badge = QtWidgets.QLabel(f"{group.similarity}% match")
            badge.setObjectName("Tag")
            badge.setToolTip(
                "How closely the least similar copy matches the file being "
                "kept, from a perceptual hash -- these files are visually "
                "alike, not byte-identical.")
            gh.addWidget(badge)
        gh.addStretch(1)
        head.addWidget(group_cell, 3)

        keeping = QtWidgets.QLabel(os.path.basename(group.keep[0]))
        keeping.setObjectName("Hint")
        keeping.setToolTip(_shorten(group.keep[0], home))
        head.addWidget(keeping, 3)

        size_label = QtWidgets.QLabel(human_size(group.size))
        size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_label.setMinimumWidth(90)
        head.addWidget(size_label, 1)

        self.expand_btn = QtWidgets.QPushButton("Show files")
        self.expand_btn.setObjectName("Secondary")
        self.expand_btn.clicked.connect(self.toggle_detail)
        head.addWidget(self.expand_btn)

        v.addLayout(head)

        self.detail = QtWidgets.QWidget()
        self.detail.setObjectName("CardBody")
        self.detail_layout = QtWidgets.QVBoxLayout(self.detail)
        self.detail_layout.setContentsMargins(24, 4, 0, 4)
        self.detail_layout.setSpacing(2)
        self.detail.setVisible(False)
        v.addWidget(self.detail)

    def toggle_detail(self):
        """Show or hide the group's files, building the rows the first time."""
        if not self._detail_built:
            home = str(Path.home())
            self.detail_layout.addWidget(self._keep_row(home))
            for path, size in self.group.duplicates:
                self.detail_layout.addWidget(self._copy_row(path, size, home))
            self._detail_built = True
        visible = not self.detail.isVisible()
        self.detail.setVisible(visible)
        self.expand_btn.setText("Hide files" if visible else "Show files")

    def _row(self, lead: QtWidgets.QWidget, path: str, size: int, home: str):
        row = QtWidgets.QWidget()
        row.setObjectName("CardBody")
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        h.addWidget(lead)
        label = QtWidgets.QLabel(_shorten(path, home))
        label.setObjectName("Hint")
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # One deep path would otherwise set the width of the whole Cleanup
        # page; the card denies a horizontal scrollbar, so it has to wrap.
        label.setWordWrap(True)
        h.addWidget(label, 1)
        size_label = QtWidgets.QLabel(human_size(size))
        size_label.setObjectName("Hint")
        size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_label.setMinimumWidth(80)
        h.addWidget(size_label)
        return row

    def _keep_row(self, home: str) -> QtWidgets.QWidget:
        """The kept file: a KEEP tag where the other rows have a checkbox.

        No disabled checkbox, on purpose -- a checkbox says "you may change
        this", and this one is not yet changeable (choosing the keeper is a
        tracked follow-up). A tag says what is true instead of offering a
        control that does nothing.
        """
        tag = QtWidgets.QLabel("KEEP")
        tag.setObjectName("Tag")
        tag.setToolTip(
            "Kept automatically: the oldest copy in Files mode, the highest "
            "resolution one in Images mode. Choosing it yourself is not "
            "supported yet.")
        return self._row(tag, self.group.keep[0], self.group.keep[1], home)

    def _copy_row(self, path: str, size: int, home: str) -> QtWidgets.QWidget:
        check = QtWidgets.QCheckBox()
        check.setChecked(self._checked[path])
        check.toggled.connect(lambda on, p=path: self._set_checked(p, on))
        return self._row(check, path, size, home)

    def _set_checked(self, path: str, on: bool):
        self._checked[path] = on
        self.toggled.emit()

    def selected_items(self) -> List[Tuple[str, int]]:
        """The (path, size) copies this group contributes to Remove Selected."""
        return [(path, size) for path, size in self.group.duplicates
                if self._checked.get(path)]


class BackupWorker(QtCore.QObject):
    """Runs one long backup-related filesystem call off the UI thread.

    One worker rather than three near-identical ones. Taking a backup,
    restoring a version and measuring what has changed since the last backup
    are all "walk and copy through utils/backup_utils, report (done, total),
    hand back a result dict" -- they differ only in which function is called,
    so the work is passed in as a callable taking `(on_progress,
    should_abort)` instead of being three copies of this class.

    Same shape as the scan workers above otherwise: a plain QObject moved onto
    a QThread, started by `thread.started`, reporting only through signals.
    Copying a real backup source is minutes of disk I/O, which is exactly the
    kind of work CLAUDE.md §3 forbids on the UI thread.
    """
    progress = QtCore.Signal(int, int)   # done, total
    finished = QtCore.Signal(dict)       # the result dict from backup_utils

    def __init__(self, work):
        super().__init__()
        self.work = work
        self._abort = False

    @QtCore.Slot()
    def run(self):
        try:
            result = self.work(self.progress.emit, lambda: self._abort)
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
            "scheduled job is registered with Windows Task Scheduler and runs "
            "without opening this app.")
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

        # Backing a folder up into itself copies the previous run's copies on
        # every run, so the target grows without bound and no error is ever
        # raised. Compared with normcase/abspath because Windows paths differ
        # in case and separators without differing at all.
        source_norm = os.path.normcase(os.path.abspath(source))
        target_norm = os.path.normcase(os.path.abspath(target))
        if target_norm == source_norm or target_norm.startswith(source_norm + os.sep):
            return self._fail("The target cannot be inside the source folder.")

        schedule = ("manual", "daily", "weekly")[self.schedule.currentIndex()]
        if self.job is None:
            self.job = backup_utils.new_job(name, source, target, schedule, at,
                                            self.retention.value())
        else:
            self.job.update({"name": name, "source": source, "target": target,
                             "schedule": schedule, "at": at,
                             "retention": self.retention.value()})
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
# outright -- unlike Cleanup, where the wide content is a wrapping label that
# should reflow rather than scroll -- because at the app's documented minimum
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

        self.restore_btn = QtWidgets.QPushButton("Restore...")
        self.restore_btn.setObjectName("Secondary")
        # Nothing to restore from until a version exists on disk.
        self.restore_btn.setEnabled(bool(changes.get("ok")))
        self.restore_btn.clicked.connect(lambda: self.restore_requested.emit(self.job))
        h.addWidget(self.restore_btn)

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


class BreakdownRow(QtWidgets.QWidget):
    """One folder in the Overview breakdown: name, proportional bar, size.

    The bar is the app's ordinary QProgressBar (styled once in
    styles/theme.qss) filled to this folder's share of the largest folder --
    same visual vocabulary as the per-file batch bars in Image Tools and
    File Manager, rather than a second bar style invented for this screen.
    """

    def __init__(self, name: str, size_bytes: int, largest: int, skipped: int):
        super().__init__()
        # Plain container widgets inside a #Card need the transparent
        # background rule, or the base QWidget colour paints the page ground
        # over the card's white surface.
        self.setObjectName("CardBody")

        h = QtWidgets.QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(12)

        label = QtWidgets.QLabel(name)
        label.setMinimumWidth(150)
        h.addWidget(label, 2)

        bar = QtWidgets.QProgressBar()
        bar.setObjectName("DataBar")
        bar.setRange(0, 1000)
        bar.setValue(int(1000 * size_bytes / largest) if largest else 0)
        bar.setTextVisible(False)
        h.addWidget(bar, 5)

        size_label = QtWidgets.QLabel(human_size(size_bytes))
        size_label.setObjectName("Hint")
        size_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        size_label.setMinimumWidth(90)
        h.addWidget(size_label, 1)

        if skipped:
            self.setToolTip(
                f"{skipped} item(s) in {name} could not be read "
                "(permission denied) and are not counted in this total."
            )


class DiskTab(QtWidgets.QWidget):
    """The Disk pillar: Overview / Cleanup / Backup.

    Duplicates is a section inside Cleanup (DESIGN.md v4), not a fourth
    sub-tab; Backup carries two tables of its own -- the jobs, and a separate
    Restore table below them (DESIGN.md v4 rule 5), not a history nested in
    each job.
    """

    # A messy Documents folder can produce hundreds of duplicate groups, and
    # each one is several widgets. Render the biggest wins and say how many
    # were held back -- laying out a thousand rows to show a 3 KB group at
    # the bottom is how this screen would come to feel broken.
    MAX_DUPLICATE_GROUPS = 200

    def __init__(self):
        super().__init__()
        self.home = str(Path.home())
        self.thread: Optional[QtCore.QThread] = None
        self.worker: Optional[ScanWorker] = None
        self._scanned = False

        self.cleanup_thread: Optional[QtCore.QThread] = None
        self.cleanup_worker: Optional[CleanupScanWorker] = None
        self.clean_thread: Optional[QtCore.QThread] = None
        self.clean_worker: Optional[CleanWorker] = None
        self.sections: List[CategorySection] = []
        self._cleanup_scanned = False

        self.dup_thread: Optional[QtCore.QThread] = None
        self.dup_worker: Optional[DuplicateScanWorker] = None
        self.dup_clean_thread: Optional[QtCore.QThread] = None
        self.dup_clean_worker: Optional[CleanWorker] = None
        self.dup_sections: List[DuplicateGroupSection] = []
        # Carries a removal outcome across the rescan that follows it, so the
        # rescan's own result line does not erase what just happened.
        self._dup_notice = ""

        # One backup operation at a time -- taking a backup, restoring a
        # version and measuring changes all read or write the same trees, and
        # running two of them together would report against a moving target.
        self.backup_thread: Optional[QtCore.QThread] = None
        self.backup_worker: Optional[BackupWorker] = None
        self.jobs: List[dict] = []
        self._changes: dict = {}
        self._backup_loaded = False
        self._backup_done = None      # the handler for the run in flight
        self._remeasure = False       # a backup just ran; its drift is stale
        # Carries a run's result line across the remeasure that follows it,
        # exactly as `_dup_notice` does for the Duplicates rescan.
        self._backup_notice = ""

        v = QtWidgets.QVBoxLayout(self)
        # Margins leave room for the card drop shadows to render un-clipped
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(10)

        v.addWidget(PageHeader(
            "Disk",
            "See what is using space, reclaim what you do not need, and keep "
            "folders backed up. Nothing is deleted without asking first."))

        # Sub-navigation is a plain QTabWidget. QTabBar is already fully
        # themed in styles/theme.qss (accent-filled selected tab) and unused
        # anywhere else in the app, so the three Disk areas get the app's own
        # chrome for free -- no new QSS, and no custom segmented control to
        # maintain alongside the sidebar's existing nav paradigm.
        self.sub = QtWidgets.QTabWidget()
        self.sub.addTab(self._build_overview(), "Overview")
        self.sub.addTab(self._build_cleanup(), "Cleanup")
        self.sub.addTab(self._build_backup(), "Backup")
        self.sub.currentChanged.connect(self.on_sub_changed)
        v.addWidget(self.sub, 1)

        # A scan started from __init__ would walk the home directory on every
        # app launch even for a user who never opens this tab. QStackedWidget
        # hides non-current pages, so showEvent fires the first time Disk is
        # actually selected -- which is when the data is first wanted.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ------------------------------
    # Overview
    # ------------------------------
    def _build_overview(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setObjectName("CardBody")
        pv = QtWidgets.QVBoxLayout(page)
        pv.setContentsMargins(2, 8, 2, 2)
        pv.setSpacing(12)

        # --- drive usage -----------------------------------------------
        usage_card = QtWidgets.QFrame()
        usage_card.setObjectName("Card")
        uv = QtWidgets.QVBoxLayout(usage_card)
        uv.setSpacing(8)

        self.drive_title = QtWidgets.QLabel("Drive")
        self.drive_title.setObjectName("H2")
        uv.addWidget(self.drive_title)

        # A measurement, not an interaction: how full a drive is has nothing
        # to do with the accent colour, and a 44%-full drive drawn in the same
        # red as the primary button reads as an alarm. The object name is
        # reassigned by _refresh_usage() as the number crosses its thresholds.
        self.usage_bar = QtWidgets.QProgressBar()
        self.usage_bar.setObjectName("DataBar")
        self.usage_bar.setRange(0, 100)
        # The percentage reads in the line below, not inside an 8px bar: a
        # measurement bar is a shape to compare, not a place to put text.
        self.usage_bar.setTextVisible(False)
        uv.addWidget(self.usage_bar)

        self.usage_label = QtWidgets.QLabel()
        self.usage_label.setObjectName("Hint")
        uv.addWidget(self.usage_label)

        pv.addWidget(usage_card)
        self._refresh_usage()

        # --- per-folder breakdown --------------------------------------
        breakdown_card = QtWidgets.QFrame()
        breakdown_card.setObjectName("Card")
        bv = QtWidgets.QVBoxLayout(breakdown_card)
        bv.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Folders in your home directory")
        title.setObjectName("H2")
        head.addWidget(title)
        head.addStretch(1)
        self.rescan_btn = QtWidgets.QPushButton("Rescan")
        self.rescan_btn.setObjectName("Secondary")
        self.rescan_btn.clicked.connect(self.start_scan)
        head.addWidget(self.rescan_btn)
        bv.addLayout(head)

        path_label = QtWidgets.QLabel(self.home)
        path_label.setObjectName("Hint")
        bv.addWidget(path_label)

        # Scanning indicator. EmptyState is the app's "there is no data"
        # pattern; a scan in flight is a running operation, so it gets an
        # indeterminate progress bar plus the folder currently being measured.
        self.scan_box = QtWidgets.QWidget()
        self.scan_box.setObjectName("CardBody")
        sv = QtWidgets.QVBoxLayout(self.scan_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(6)
        self.scan_bar = QtWidgets.QProgressBar()
        # Indeterminate: muted, so "working" never looks like progress
        # that has reached the end.
        self.scan_bar.setObjectName("BusyBar")
        self.scan_bar.setRange(0, 0)  # indeterminate: no total is known upfront
        self.scan_bar.setTextVisible(False)
        sv.addWidget(self.scan_bar)
        self.scan_status = QtWidgets.QLabel("Scanning your home directory...")
        self.scan_status.setObjectName("Hint")
        sv.addWidget(self.scan_status)
        self.scan_box.setVisible(False)
        bv.addWidget(self.scan_box)

        # The real breakdown rows, and the "nothing to show" fallback for a
        # home directory with no readable subfolders at all.
        self.rows_host = QtWidgets.QWidget()
        self.rows_host.setObjectName("CardBody")
        self.rows_layout = QtWidgets.QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(6)
        bv.addWidget(self.rows_host)

        self.empty_state = EmptyState(
            title="No folders to show",
            hint="Nothing readable was found directly under your home directory.",
        )
        self.empty_state.setVisible(False)
        bv.addWidget(self.empty_state)

        # Permission errors are reported once, in aggregate, rather than as a
        # per-row error state -- a partially unreadable AppData is normal on
        # Windows, not a failure the user needs to act on.
        self.skipped_notice = QtWidgets.QLabel()
        self.skipped_notice.setObjectName("Hint")
        self.skipped_notice.setWordWrap(True)
        self.skipped_notice.setVisible(False)
        bv.addWidget(self.skipped_notice)

        pv.addWidget(breakdown_card)
        pv.addStretch(1)

        # A home directory can hold twenty-odd folders; the page scrolls
        # rather than squashing the rows.
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setWidget(page)
        return scroll

    # ------------------------------
    # Cleanup
    # ------------------------------
    def _build_cleanup(self) -> QtWidgets.QWidget:
        # The footer sits outside the scroll area: how much is selected and
        # the button that acts on it must stay on screen while the category
        # list is scrolled, or the running total is invisible exactly when it
        # is being changed.
        page = QtWidgets.QWidget()
        page.setObjectName("CardBody")
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        body = QtWidgets.QWidget()
        body.setObjectName("CardBody")
        bv = QtWidgets.QVBoxLayout(body)
        # Margins so the surface's drop shadow has room inside the viewport.
        bv.setContentsMargins(2, 2, 8, 8)
        bv.setSpacing(0)

        # One surface for the whole screen. Quick scan and Duplicates are
        # sections of it, divided by a labelled hairline, rather than two
        # cards stacked in a column: a card inside a card column gives the
        # page, its sections and its rows all the same visual weight.
        surface = QtWidgets.QFrame()
        surface.setObjectName("Card")
        cv = QtWidgets.QVBoxLayout(surface)
        cv.setSpacing(8)
        bv.addWidget(surface)

        self.cleanup_rescan_btn = QtWidgets.QPushButton("Rescan")
        self.cleanup_rescan_btn.setObjectName("Secondary")
        self.cleanup_rescan_btn.clicked.connect(self.start_cleanup_scan)
        cv.addWidget(section("Quick scan", self.cleanup_rescan_btn))

        safety = QtWidgets.QLabel(
            "Checked items are moved to the Recycle Bin, never deleted "
            "permanently, and only after you confirm. Expand a category to "
            "see exactly what it found."
        )
        safety.setObjectName("Hint")
        safety.setWordWrap(True)
        cv.addWidget(safety)

        self.cleanup_scan_box = QtWidgets.QWidget()
        self.cleanup_scan_box.setObjectName("CardBody")
        sv = QtWidgets.QVBoxLayout(self.cleanup_scan_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(6)
        self.cleanup_bar = QtWidgets.QProgressBar()
        # Indeterminate: muted, so "working" never looks like progress
        # that has reached the end.
        self.cleanup_bar.setObjectName("BusyBar")
        self.cleanup_bar.setRange(0, 0)  # indeterminate: no file total upfront
        self.cleanup_bar.setTextVisible(False)
        sv.addWidget(self.cleanup_bar)
        self.cleanup_status = QtWidgets.QLabel("Scanning...")
        self.cleanup_status.setObjectName("Hint")
        sv.addWidget(self.cleanup_status)
        self.cleanup_scan_box.setVisible(False)
        cv.addWidget(self.cleanup_scan_box)

        # A header row over every repeated-row table (DESIGN.md v4 rule 3), so
        # the three columns are labelled rather than inferred. The extra space
        # above it separates the table from the safety note; the rule under it
        # ties it to the rows it labels.
        cv.addSpacing(8)
        header = QtWidgets.QWidget()
        header.setObjectName("CardBody")
        hh = QtWidgets.QHBoxLayout(header)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(12)
        for text, stretch, align in (
            ("Category", 4, Qt.AlignLeft | Qt.AlignVCenter),
            ("Found", 2, Qt.AlignLeft | Qt.AlignVCenter),
            ("Size", 1, Qt.AlignRight | Qt.AlignVCenter),
        ):
            label = QtWidgets.QLabel(text)
            label.setObjectName("FieldLabel")
            label.setAlignment(align)
            hh.addWidget(label, stretch)
        # A spacer matching the per-row expand button, so the columns line up.
        spacer = QtWidgets.QLabel("")
        spacer.setMinimumWidth(96)
        hh.addWidget(spacer)
        cv.addWidget(header)
        cv.addWidget(divider())

        self.sections_host = QtWidgets.QWidget()
        self.sections_host.setObjectName("CardBody")
        self.sections_layout = QtWidgets.QVBoxLayout(self.sections_host)
        self.sections_layout.setContentsMargins(0, 0, 0, 0)
        # Row separation is the divider() between rows plus each row's own
        # margins, so the layout adds nothing on top of it.
        self.sections_layout.setSpacing(0)
        cv.addWidget(self.sections_host)

        self._build_duplicates(cv)
        cv.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # The drill-down lists real paths, some of them very deep. Denying the
        # horizontal scrollbar holds the categories to the window width and
        # makes the wrapped detail labels wrap instead of widening the page.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- action bar -------------------------------------------------
        # A bar, not a card: it holds a count, one button and a result line,
        # and a card's padding made the footer taller than it needs to be.
        # Same component the two tool screens use for their run controls.
        footer = QtWidgets.QFrame()
        footer.setObjectName("ActionBar")
        fv = QtWidgets.QVBoxLayout(footer)
        fv.setContentsMargins(14, 10, 14, 10)
        fv.setSpacing(8)

        row = QtWidgets.QHBoxLayout()
        self.selection_label = QtWidgets.QLabel("Nothing selected")
        row.addWidget(self.selection_label)
        row.addStretch(1)
        self.clean_btn = QtWidgets.QPushButton("Clean Selected")
        self.clean_btn.setObjectName("Danger")
        self.clean_btn.setEnabled(False)
        self.clean_btn.clicked.connect(self.clean_selected)
        row.addWidget(self.clean_btn)
        fv.addLayout(row)

        self.clean_bar = QtWidgets.QProgressBar()
        self.clean_bar.setObjectName("WithText")
        self.clean_bar.setVisible(False)
        fv.addWidget(self.clean_bar)

        self.clean_result = QtWidgets.QLabel()
        self.clean_result.setObjectName("Hint")
        self.clean_result.setWordWrap(True)
        self.clean_result.setVisible(False)
        fv.addWidget(self.clean_result)

        outer.addWidget(footer)
        return page

    # ------------------------------
    # Duplicates (a section inside Cleanup, not a fourth sub-tab)
    # ------------------------------
    def _build_duplicates(self, cv: QtWidgets.QVBoxLayout) -> None:
        """Duplicates, as the second section of the Cleanup surface.

        Appends into the caller's layout rather than returning a card of its
        own: Quick scan and Duplicates are two sections of one screen, and
        giving each its own bordered surface was what made this page read as
        three stacked boxes.

        Its own action row stays inside the section rather than moving to the
        screen's action bar: the two scans are separate decisions over
        separate files, and one "Clean Selected" button that mixed browser
        cache with the user's photographs would be a bad thing to click by
        accident.
        """
        mode_label = QtWidgets.QLabel("Mode")
        mode_label.setObjectName("FieldLabel")
        # A QComboBox, matching File Manager's operation picker -- the app
        # already has one mode-picker idiom and does not need a second.
        self.dup_mode = QtWidgets.QComboBox()
        self.dup_mode.addItems(["Files (exact match)", "Images (visual match)"])
        self.dup_mode.currentIndexChanged.connect(self.on_dup_mode_changed)

        self.dup_scan_btn = QtWidgets.QPushButton("Scan")
        self.dup_scan_btn.setObjectName("Secondary")
        self.dup_scan_btn.clicked.connect(self.start_duplicate_scan)
        cv.addWidget(section("Duplicates", mode_label, self.dup_mode, self.dup_scan_btn))

        self.dup_hint = QtWidgets.QLabel()
        self.dup_hint.setObjectName("Hint")
        self.dup_hint.setWordWrap(True)
        cv.addWidget(self.dup_hint)

        self.dup_scan_box = QtWidgets.QWidget()
        self.dup_scan_box.setObjectName("CardBody")
        sv = QtWidgets.QVBoxLayout(self.dup_scan_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(6)
        self.dup_bar = QtWidgets.QProgressBar()
        # Indeterminate: muted, so "working" never looks like progress
        # that has reached the end.
        self.dup_bar.setObjectName("BusyBar")
        self.dup_bar.setRange(0, 0)  # indeterminate until the walk finishes
        self.dup_bar.setTextVisible(False)
        sv.addWidget(self.dup_bar)
        self.dup_status = QtWidgets.QLabel("Scanning...")
        self.dup_status.setObjectName("Hint")
        sv.addWidget(self.dup_status)
        self.dup_scan_box.setVisible(False)
        cv.addWidget(self.dup_scan_box)

        # A header row over the group table, same rule as Quick scan's. Header
        # and its underline live in one container so the three places that
        # show/hide the header cannot leave a stray rule behind.
        header = QtWidgets.QWidget()
        header.setObjectName("CardBody")
        header_v = QtWidgets.QVBoxLayout(header)
        header_v.setContentsMargins(0, 8, 0, 0)
        header_v.setSpacing(8)
        header_row = QtWidgets.QWidget()
        header_row.setObjectName("CardBody")
        hh = QtWidgets.QHBoxLayout(header_row)
        hh.setContentsMargins(0, 0, 0, 0)
        hh.setSpacing(12)
        for text, stretch, align in (
            ("Group", 3, Qt.AlignLeft | Qt.AlignVCenter),
            ("Keeping", 3, Qt.AlignLeft | Qt.AlignVCenter),
            ("Reclaimable", 1, Qt.AlignRight | Qt.AlignVCenter),
        ):
            label = QtWidgets.QLabel(text)
            label.setObjectName("FieldLabel")
            label.setAlignment(align)
            hh.addWidget(label, stretch)
        spacer = QtWidgets.QLabel("")
        spacer.setMinimumWidth(96)
        hh.addWidget(spacer)
        header_v.addWidget(header_row)
        header_v.addWidget(divider())
        self.dup_header = header
        self.dup_header.setVisible(False)
        cv.addWidget(header)

        self.dup_host = QtWidgets.QWidget()
        self.dup_host.setObjectName("CardBody")
        self.dup_layout = QtWidgets.QVBoxLayout(self.dup_host)
        self.dup_layout.setContentsMargins(0, 0, 0, 0)
        # Row separation is each row's own margins plus the divider() between
        # them, so the layout adds nothing on top.
        self.dup_layout.setSpacing(0)
        cv.addWidget(self.dup_host)

        self.dup_empty = EmptyState(
            title="No duplicate scan yet",
            hint="Duplicate detection reads every candidate file, so it runs "
                 "only when you ask for it. Press Scan.",
        )
        cv.addWidget(self.dup_empty)

        footer = QtWidgets.QHBoxLayout()
        self.dup_selection_label = QtWidgets.QLabel("Nothing selected")
        footer.addWidget(self.dup_selection_label)
        footer.addStretch(1)
        self.dup_remove_btn = QtWidgets.QPushButton("Remove Selected")
        self.dup_remove_btn.setObjectName("Danger")
        self.dup_remove_btn.setEnabled(False)
        self.dup_remove_btn.clicked.connect(self.remove_duplicates)
        footer.addWidget(self.dup_remove_btn)
        cv.addLayout(footer)

        self.dup_clean_bar = QtWidgets.QProgressBar()
        self.dup_clean_bar.setObjectName("WithText")
        self.dup_clean_bar.setVisible(False)
        cv.addWidget(self.dup_clean_bar)

        self.dup_result = QtWidgets.QLabel()
        self.dup_result.setObjectName("Hint")
        self.dup_result.setWordWrap(True)
        self.dup_result.setVisible(False)
        cv.addWidget(self.dup_result)

        self._update_dup_hint()

    # ------------------------------
    # Backup (jobs above, Restore below -- two tables, not one nested one)
    # ------------------------------
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
            "Windows Task Scheduler and run without this app being open.")
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
        """Re-read the jobs file and rebuild both tables."""
        self.jobs = backup_utils.load_jobs()
        self._clear_layout(self.jobs_layout)
        self._clear_layout(self.restore_layout)

        self.jobs_empty.setVisible(not self.jobs)
        self.job_header.setVisible(bool(self.jobs))
        for index, job in enumerate(self.jobs):
            # A hairline between rows but not after the last one, same rhythm
            # as the Cleanup categories -- both are repeated-row tables here.
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
            self.restore_layout.addWidget(row)
        self._set_backup_controls(enabled=self.backup_thread is None)

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
        self.job_add_btn.setEnabled(enabled)
        self.restore_refresh_btn.setEnabled(enabled)
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
        backup_utils.save_job(job)
        ok, detail = backup_utils.sync_task(job)
        message = f"{verb} job \"{job['name']}\"."
        if job.get("schedule") == "manual":
            message += " It runs only when you press Run Now."
        elif ok:
            message += (" Registered with Windows Task Scheduler as "
                        f"{backup_utils.task_name(job['id'])}.")
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
        backup_utils.remove_job(job["id"])
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
            lambda progress, abort: backup_utils.run_job(job, progress, abort),
            lambda result: self._finish_run(job, result),
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
            lambda progress, abort: backup_utils.restore_version(
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
        jobs = backup_utils.load_jobs()
        if not jobs:
            self.refresh_backup()
            return

        def work(progress, abort):
            measured = {}
            for index, job in enumerate(jobs, 1):
                if abort():
                    break
                progress(index, len(jobs))
                measured[job["id"]] = backup_utils.changes_since(job, None, abort)
            return {"ok": True, "message": "", "changes": measured}

        self._start_backup("Checking what has changed since each backup...",
                           work, self._finish_changes)

    def _start_backup(self, status: str, work, done):
        """Run one backup-related call on a QThread, locking the screen."""
        self._say(status)
        self.backup_bar.setRange(0, 0)  # indeterminate until a total arrives
        self.backup_bar.setValue(0)
        self.backup_bar.setVisible(True)
        self._set_backup_controls(enabled=False)

        self._backup_done = done
        self.backup_thread = QtCore.QThread(self)
        self.backup_worker = BackupWorker(work)
        self.backup_worker.moveToThread(self.backup_thread)
        self.backup_worker.progress.connect(self.on_backup_progress)
        self.backup_worker.finished.connect(self.on_backup_finished)
        self.backup_thread.started.connect(self.backup_worker.run)
        self.backup_thread.start()

    def on_backup_progress(self, done: int, total: int):
        self.backup_bar.setRange(0, total)
        self.backup_bar.setValue(done)

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
        result line the user is reading -- same reason the Duplicates removal
        carries `_dup_notice` across its rescan.
        """
        backup_utils.record_run(job["id"], result)
        message = result.get("message", "")
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

    def dup_mode_key(self) -> str:
        return "images" if self.dup_mode.currentIndex() == 1 else "files"

    def _update_dup_hint(self):
        if self.dup_mode_key() == "images":
            self.dup_hint.setText(
                "Pictures that look alike -- the same photo resaved, resized "
                "or recompressed -- matched by a perceptual hash, not by "
                "their bytes. The highest-resolution copy is kept, and each "
                "group shows how closely the rest match it. Checked copies "
                "are moved to the Recycle Bin after you confirm.")
        else:
            self.dup_hint.setText(
                "Files with byte-identical contents in Downloads, Documents "
                "and Desktop, matched by a SHA-256 of every candidate. The "
                "oldest copy is kept. Checked copies are moved to the "
                "Recycle Bin after you confirm.")

    def on_dup_mode_changed(self):
        """Switching mode invalidates the results, so clear rather than mix.

        No automatic rescan: the two scans read different files and both are
        expensive, so changing the picker must not silently start minutes of
        work the user did not ask for.
        """
        self._update_dup_hint()
        if self.dup_thread is not None or self.dup_clean_thread is not None:
            return
        self._clear_dup_sections()
        self.dup_header.setVisible(False)
        self.dup_result.setVisible(False)
        self.dup_empty.setVisible(True)
        self.update_dup_selection()

    def start_duplicate_scan(self):
        if self.dup_thread is not None or self.dup_clean_thread is not None:
            return
        self._clear_dup_sections()
        self.dup_header.setVisible(False)
        self.dup_empty.setVisible(False)
        self.dup_result.setVisible(False)
        self.dup_bar.setRange(0, 0)
        self.dup_status.setText("Looking for candidates...")
        self.dup_scan_box.setVisible(True)
        self.dup_scan_btn.setEnabled(False)
        self.dup_mode.setEnabled(False)
        self.dup_remove_btn.setEnabled(False)
        self.dup_selection_label.setText("Nothing selected")

        self.dup_thread = QtCore.QThread(self)
        self.dup_worker = DuplicateScanWorker(self.dup_mode_key(), self.home)
        self.dup_worker.moveToThread(self.dup_thread)
        self.dup_worker.progress.connect(self.on_dup_progress)
        self.dup_worker.finished.connect(self.on_dup_finished)
        self.dup_thread.started.connect(self.dup_worker.run)
        self.dup_thread.start()

    def on_dup_progress(self, done: int, total: int):
        # The total is only known once the walk is done, so the bar starts
        # indeterminate and becomes a real count here.
        self.dup_bar.setRange(0, total)
        self.dup_bar.setValue(done)
        word = "image" if self.dup_mode_key() == "images" else "file"
        self.dup_status.setText(f"Fingerprinting {done} of {total} {word}(s)...")

    def on_dup_finished(self, groups: List[DuplicateGroup], skipped: int):
        self.dup_scan_box.setVisible(False)
        self.dup_scan_btn.setEnabled(True)
        self.dup_mode.setEnabled(True)
        if self.dup_thread:
            self.dup_thread.quit()
            self.dup_thread.wait()
            self.dup_thread = None
            self.dup_worker = None
        self._render_dup_sections(groups, skipped)

    def _clear_dup_sections(self):
        self.dup_sections = []
        while self.dup_layout.count():
            item = self.dup_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _render_dup_sections(self, groups: List[DuplicateGroup], skipped: int):
        self._clear_dup_sections()
        notice, self._dup_notice = self._dup_notice, ""
        if not groups:
            self.dup_empty.setVisible(True)
            self.dup_result.setText(
                (notice + " " if notice else "")
                + "No duplicates found in Downloads, Documents and Desktop."
                + (f" {skipped} file(s) could not be read." if skipped else ""))
            self.dup_result.setVisible(True)
            self.update_dup_selection()
            return

        self.dup_empty.setVisible(False)
        self.dup_header.setVisible(True)
        show_similarity = self.dup_mode_key() == "images"
        shown = groups[:self.MAX_DUPLICATE_GROUPS]
        for number, group in enumerate(shown, 1):
            # Same row rhythm as the Cleanup categories above -- both are
            # repeated-row tables on the one screen, so they read as one.
            if number > 1:
                self.dup_layout.addWidget(divider())
            section = DuplicateGroupSection(group, number, show_similarity)
            section.toggled.connect(self.update_dup_selection)
            self.dup_sections.append(section)
            self.dup_layout.addWidget(section)

        notes = [notice] if notice else []
        if len(groups) > len(shown):
            notes.append(
                f"Showing the {len(shown)} largest of {len(groups)} groups; "
                "clean these up and scan again for the rest")
        if skipped:
            notes.append(f"{skipped} file(s) could not be read and were skipped")
        if notes:
            self.dup_result.setText(
                ". ".join(note.rstrip(".") for note in notes) + ".")
            self.dup_result.setVisible(True)
        self.update_dup_selection()

    def selected_duplicates(self) -> List[Tuple[str, int]]:
        """Every checked copy across the rendered groups, de-duplicated.

        A path cannot legitimately appear in two groups, but the same guard
        the Cleanup footer uses costs nothing and keeps a double delete --
        which fails on the second attempt -- structurally impossible.
        """
        seen = {}
        for group in self.dup_sections:
            for path, size in group.selected_items():
                seen.setdefault(os.path.normcase(path), (path, size))
        return list(seen.values())

    def update_dup_selection(self):
        items = self.selected_duplicates()
        total = sum(size for _, size in items)
        if items:
            self.dup_selection_label.setText(
                f"{len(items)} copy(ies) selected - {human_size(total)}")
        else:
            self.dup_selection_label.setText("Nothing selected")
        self.dup_remove_btn.setEnabled(
            bool(items) and self.dup_clean_thread is None)

    def remove_duplicates(self):
        """Confirm, then move every checked copy to the Recycle Bin.

        Reuses CleanWorker and ConfirmDialog rather than a second deletion
        path -- the kept file is simply never in `items`, so it cannot be
        touched by this.
        """
        items = self.selected_duplicates()
        if not items or self.dup_clean_thread is not None:
            return
        total = sum(size for _, size in items)
        kept = len(self.dup_sections)
        if not ConfirmDialog.ask(
            self,
            "Move duplicate copies to Recycle Bin?",
            f"{len(items)} copy(ies) totalling {human_size(total)} will be "
            f"moved to the Recycle Bin. The {kept} file(s) marked KEEP are "
            "left exactly where they are. You can restore anything from the "
            "Recycle Bin if you change your mind.",
            confirm_text="Move to Recycle Bin",
        ):
            return

        self.dup_remove_btn.setEnabled(False)
        self.dup_scan_btn.setEnabled(False)
        self.dup_mode.setEnabled(False)
        self.dup_result.setVisible(False)
        self.dup_clean_bar.setRange(0, len(items))
        self.dup_clean_bar.setValue(0)
        self.dup_clean_bar.setVisible(True)

        self.dup_clean_thread = QtCore.QThread(self)
        self.dup_clean_worker = CleanWorker(items)
        self.dup_clean_worker.moveToThread(self.dup_clean_thread)
        self.dup_clean_worker.progress.connect(self.on_dup_clean_progress)
        self.dup_clean_worker.finished.connect(self.on_dup_clean_finished)
        self.dup_clean_thread.started.connect(self.dup_clean_worker.run)
        self.dup_clean_thread.start()

    def on_dup_clean_progress(self, done: int, total: int):
        self.dup_clean_bar.setValue(done)

    def on_dup_clean_finished(self, removed: int, freed: int, errors: List[str]):
        self.dup_clean_bar.setVisible(False)
        message = (f"Moved {removed} duplicate copy(ies) to the Recycle Bin, "
                   f"reclaiming {human_size(freed)}.")
        if errors:
            message += (f" {len(errors)} could not be moved: "
                        + "; ".join(errors[:3]))
            if len(errors) > 3:
                message += f"; and {len(errors) - 3} more"

        if self.dup_clean_thread:
            self.dup_clean_thread.quit()
            self.dup_clean_thread.wait()
            self.dup_clean_thread = None
            self.dup_clean_worker = None
        self.dup_scan_btn.setEnabled(True)
        self.dup_mode.setEnabled(True)
        # Rescan so the groups reflect what is actually left on disk. The
        # outcome rides along in `_dup_notice` rather than being written here:
        # the rescan finishes later and would otherwise overwrite it with its
        # own result line.
        self._dup_notice = message
        self.start_duplicate_scan()

    def on_sub_changed(self, index: int):
        """Scan for junk the first time Cleanup is actually opened.

        Same reasoning as Overview's showEvent: the scan walks AppData and
        three home folders, so it must not run for a user who never looks at
        this screen. QTabWidget hides non-current pages, so a page's own
        showEvent is the signal that it has been selected.
        """
        if index == 1 and not self._cleanup_scanned:
            self._cleanup_scanned = True
            self.start_cleanup_scan()
        elif index == 2 and not self._backup_loaded:
            # Measuring drift walks every job's source and its last backup, so
            # it waits until Backup is actually opened -- same reasoning.
            self._backup_loaded = True
            self.refresh_backup()
            self.refresh_changes()

    def start_cleanup_scan(self):
        if self.cleanup_thread is not None or self.clean_thread is not None:
            return  # a scan or a clean is already running
        self._clear_sections()
        self.clean_result.setVisible(False)
        self.cleanup_status.setText("Scanning...")
        self.cleanup_scan_box.setVisible(True)
        self.cleanup_rescan_btn.setEnabled(False)
        self.clean_btn.setEnabled(False)
        self.selection_label.setText("Nothing selected")

        self.cleanup_thread = QtCore.QThread(self)
        self.cleanup_worker = CleanupScanWorker(self.home)
        self.cleanup_worker.moveToThread(self.cleanup_thread)
        self.cleanup_worker.scanning.connect(self.on_cleanup_scanning)
        self.cleanup_worker.finished.connect(self.on_cleanup_finished)
        self.cleanup_thread.started.connect(self.cleanup_worker.run)
        self.cleanup_thread.start()

    def on_cleanup_scanning(self, name: str):
        self.cleanup_status.setText(f"Checking {name}...")

    def on_cleanup_finished(self, categories: List[CleanupCategory]):
        self.cleanup_scan_box.setVisible(False)
        self.cleanup_rescan_btn.setEnabled(True)
        self._render_sections(categories)
        if self.cleanup_thread:
            self.cleanup_thread.quit()
            self.cleanup_thread.wait()
            self.cleanup_thread = None
            self.cleanup_worker = None

    def _clear_sections(self):
        self.sections = []
        while self.sections_layout.count():
            item = self.sections_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _render_sections(self, categories: List[CleanupCategory]):
        self._clear_sections()
        for index, category in enumerate(categories):
            # A hairline between rows, not after the last one: a rule sitting
            # just above the card's own edge reads as an unfinished table.
            if index:
                self.sections_layout.addWidget(divider())
            section = CategorySection(category)
            section.toggled.connect(self.update_selection)
            self.sections.append(section)
            self.sections_layout.addWidget(section)
        self.update_selection()

    def selected_items(self) -> List[Tuple[str, int]]:
        """Every (path, size) pair the checked categories contribute, once.

        Categories can legitimately overlap -- a five-year-old 2 GB installer
        in Downloads is both an old installer and a large-and-old file -- so
        the selection is de-duplicated by path. Without this the total reads
        double and the second delete of the same path fails on a file the
        first one already moved.
        """
        seen = {}
        for group in self.sections:
            for path, size in group.selected_items():
                seen.setdefault(os.path.normcase(path), (path, size))
        return list(seen.values())

    def update_selection(self):
        items = self.selected_items()
        total = sum(size for _, size in items)
        if items:
            self.selection_label.setText(
                f"{len(items)} item(s) selected - {human_size(total)}")
        else:
            self.selection_label.setText("Nothing selected")
        self.clean_btn.setEnabled(bool(items) and self.clean_thread is None)

    def clean_selected(self):
        """Confirm, then move every selected item to the Recycle Bin.

        The confirm happens here, on the UI thread, before the worker starts:
        a modal dialog raised from inside a QThread is not safe in Qt, and one
        decision for the batch is what "confirm before deleting" means anyway.
        """
        items = self.selected_items()
        if not items or self.clean_thread is not None:
            return
        total = sum(size for _, size in items)
        confirmed = ConfirmDialog.ask(
            self,
            "Move to Recycle Bin?",
            f"{len(items)} item(s) totalling {human_size(total)} will be "
            "moved to the Recycle Bin. You can restore them from there if "
            "you change your mind.",
            confirm_text="Move to Recycle Bin",
        )
        if not confirmed:
            return

        self.clean_btn.setEnabled(False)
        self.cleanup_rescan_btn.setEnabled(False)
        self.clean_result.setVisible(False)
        self.clean_bar.setRange(0, len(items))
        self.clean_bar.setValue(0)
        self.clean_bar.setVisible(True)

        self.clean_thread = QtCore.QThread(self)
        self.clean_worker = CleanWorker(items)
        self.clean_worker.moveToThread(self.clean_thread)
        self.clean_worker.progress.connect(self.on_clean_progress)
        self.clean_worker.finished.connect(self.on_clean_finished)
        self.clean_thread.started.connect(self.clean_worker.run)
        self.clean_thread.start()

    def on_clean_progress(self, done: int, total: int):
        self.clean_bar.setValue(done)

    def on_clean_finished(self, removed: int, freed: int, errors: List[str]):
        self.clean_bar.setVisible(False)
        message = (f"Moved {removed} item(s) to the Recycle Bin, "
                   f"reclaiming {human_size(freed)}.")
        if errors:
            # Locked files are the normal case here (a running browser holds
            # its cache open), so they are reported plainly rather than as a
            # failure -- and only the first few, since a live cache can leave
            # hundreds and the list is not the point.
            message += (f" {len(errors)} could not be moved: "
                        + "; ".join(errors[:3]))
            if len(errors) > 3:
                message += f"; and {len(errors) - 3} more"

        if self.clean_thread:
            self.clean_thread.quit()
            self.clean_thread.wait()
            self.clean_thread = None
            self.clean_worker = None
        self.cleanup_rescan_btn.setEnabled(True)
        # Rescan first so the categories reflect what is actually left on
        # disk, then write the outcome -- start_cleanup_scan clears the
        # previous run's result line, so the order matters.
        self.start_cleanup_scan()
        self.clean_result.setText(message)
        self.clean_result.setVisible(True)

    def _refresh_usage(self):
        try:
            total, used, free = shutil.disk_usage(self.home)
        except OSError as e:
            self.drive_title.setText("Drive")
            self.usage_bar.setValue(0)
            self.usage_label.setText(f"Usage unavailable: {e}")
            return
        drive = os.path.splitdrive(self.home)[0] or os.path.sep
        percent = int(round(100 * used / total)) if total else 0
        self.drive_title.setText(f"Drive {drive}")
        self.usage_bar.setValue(percent)
        # Colour only where it carries meaning: neutral until the drive is
        # actually filling up.
        self.usage_bar.setObjectName(
            "DataBarCritical" if percent >= 90 else
            "DataBarWarn" if percent >= 75 else "DataBar")
        self.usage_bar.style().unpolish(self.usage_bar)
        self.usage_bar.style().polish(self.usage_bar)
        self.usage_label.setText(
            f"{percent}% used  -  {human_size(used)} of {human_size(total)}  -  "
            f"{human_size(free)} free"
        )

    # ------------------------------
    # Scanning
    # ------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        if not self._scanned:
            self._scanned = True
            self.start_scan()

    def start_scan(self):
        """Kick off the home-directory walk on a worker thread."""
        if self.thread is not None:
            return  # already scanning; Rescan is disabled meanwhile
        self._clear_rows()
        self._refresh_usage()
        self.empty_state.setVisible(False)
        self.skipped_notice.setVisible(False)
        self.scan_status.setText("Scanning your home directory...")
        self.scan_box.setVisible(True)
        self.rescan_btn.setEnabled(False)

        self.thread = QtCore.QThread(self)
        self.worker = ScanWorker(self.home)
        self.worker.moveToThread(self.thread)
        self.worker.scanning.connect(self.on_scanning)
        self.worker.finished.connect(self.on_scan_finished)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

    def on_scanning(self, name: str):
        self.scan_status.setText(f"Measuring {name}...")

    def on_scan_finished(self, results: List[Tuple[str, int, int]]):
        self.scan_box.setVisible(False)
        self.rescan_btn.setEnabled(True)
        self._render_rows(results)
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
            self.worker = None

    def shutdown(self):
        """Stop every in-flight worker so quitting mid-scan doesn't destroy a
        running QThread. Wired to QApplication.aboutToQuit.

        Every worker is stopped, not just Overview's: the Cleanup scan, the
        Duplicates scan and the two Recycle Bin moves each own a QThread of
        their own, and any one left running at quit is the same crash.
        """
        for worker, thread in (
            (self.worker, self.thread),
            (self.cleanup_worker, self.cleanup_thread),
            (self.clean_worker, self.clean_thread),
            (self.dup_worker, self.dup_thread),
            (self.dup_clean_worker, self.dup_clean_thread),
            (self.backup_worker, self.backup_thread),
        ):
            if worker:
                worker.abort()
            if thread:
                thread.quit()
                thread.wait()
        self.thread = self.worker = None
        self.cleanup_thread = self.cleanup_worker = None
        self.clean_thread = self.clean_worker = None
        self.dup_thread = self.dup_worker = None
        self.dup_clean_thread = self.dup_clean_worker = None
        self.backup_thread = self.backup_worker = None

    # ------------------------------
    # Rendering
    # ------------------------------
    def _clear_rows(self):
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _render_rows(self, results: List[Tuple[str, int, int]]):
        self._clear_rows()
        if not results:
            self.empty_state.setVisible(True)
            return

        largest = results[0][1]
        for name, size_bytes, skipped in results:
            self.rows_layout.addWidget(BreakdownRow(name, size_bytes, largest, skipped))

        partial = sum(1 for _, _, skipped in results if skipped)
        if partial:
            self.skipped_notice.setText(
                f"{partial} folder(s) contain items this app cannot read "
                "(typically Windows AppData); those items are skipped, so "
                "those totals read low. Hover a row for its count."
            )
            self.skipped_notice.setVisible(True)
