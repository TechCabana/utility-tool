# tabs/disk_tab.py
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Qt

from utils.disk_utils import (
    CleanupCategory, cleanup_scan, human_size, top_level_breakdown,
)
from utils.file_utils import delete_file
from widgets.common import ConfirmDialog, EmptyState


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
        v.setContentsMargins(0, 6, 0, 6)
        v.setSpacing(4)

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

    Overview and Cleanup have real content. Backup is a separate card; its
    sub-tab carries an EmptyState rather than a half-built screen. Duplicates
    is a further section inside Cleanup (DESIGN.md v4), not a fourth sub-tab,
    and is not built yet.
    """

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

        v = QtWidgets.QVBoxLayout(self)
        # Margins leave room for the card drop shadows to render un-clipped
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(10)

        header = QtWidgets.QLabel("Disk")
        header.setObjectName("H1")
        v.addWidget(header)

        # Sub-navigation is a plain QTabWidget. QTabBar is already fully
        # themed in styles/theme.qss (accent-filled selected tab) and unused
        # anywhere else in the app, so the three Disk areas get the app's own
        # chrome for free -- no new QSS, and no custom segmented control to
        # maintain alongside the sidebar's existing nav paradigm.
        self.sub = QtWidgets.QTabWidget()
        self.sub.addTab(self._build_overview(), "Overview")
        self.sub.addTab(self._build_cleanup(), "Cleanup")
        self.sub.currentChanged.connect(self.on_sub_changed)
        self.sub.addTab(
            EmptyState(
                title="Backup isn't built yet",
                hint="Scheduled source-to-target backup jobs and restore "
                     "land in a later release.",
            ),
            "Backup",
        )
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

        self.usage_bar = QtWidgets.QProgressBar()
        self.usage_bar.setRange(0, 100)
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
        bv.setContentsMargins(2, 8, 2, 2)
        bv.setSpacing(12)

        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        cv = QtWidgets.QVBoxLayout(card)
        cv.setSpacing(8)

        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Quick scan")
        title.setObjectName("H2")
        head.addWidget(title)
        head.addStretch(1)
        self.cleanup_rescan_btn = QtWidgets.QPushButton("Rescan")
        self.cleanup_rescan_btn.setObjectName("Secondary")
        self.cleanup_rescan_btn.clicked.connect(self.start_cleanup_scan)
        head.addWidget(self.cleanup_rescan_btn)
        cv.addLayout(head)

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
        self.cleanup_bar.setRange(0, 0)  # indeterminate: no file total upfront
        self.cleanup_bar.setTextVisible(False)
        sv.addWidget(self.cleanup_bar)
        self.cleanup_status = QtWidgets.QLabel("Scanning...")
        self.cleanup_status.setObjectName("Hint")
        sv.addWidget(self.cleanup_status)
        self.cleanup_scan_box.setVisible(False)
        cv.addWidget(self.cleanup_scan_box)

        # A header row over every repeated-row table (DESIGN.md v4 rule 3), so
        # the three columns are labelled rather than inferred.
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
            label.setObjectName("FormLabel")
            label.setAlignment(align)
            hh.addWidget(label, stretch)
        # A spacer matching the per-row expand button, so the columns line up.
        spacer = QtWidgets.QLabel("")
        spacer.setMinimumWidth(96)
        hh.addWidget(spacer)
        cv.addWidget(header)

        self.sections_host = QtWidgets.QWidget()
        self.sections_host.setObjectName("CardBody")
        self.sections_layout = QtWidgets.QVBoxLayout(self.sections_host)
        self.sections_layout.setContentsMargins(0, 0, 0, 0)
        self.sections_layout.setSpacing(2)
        cv.addWidget(self.sections_host)

        bv.addWidget(card)
        bv.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        # The drill-down lists real paths, some of them very deep. Denying the
        # horizontal scrollbar holds the categories to the window width and
        # makes the wrapped detail labels wrap instead of widening the page.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- footer -----------------------------------------------------
        footer = QtWidgets.QFrame()
        footer.setObjectName("Card")
        fv = QtWidgets.QVBoxLayout(footer)
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
        self.clean_bar.setVisible(False)
        fv.addWidget(self.clean_bar)

        self.clean_result = QtWidgets.QLabel()
        self.clean_result.setObjectName("Hint")
        self.clean_result.setWordWrap(True)
        self.clean_result.setVisible(False)
        fv.addWidget(self.clean_result)

        outer.addWidget(footer)
        return page

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
        for category in categories:
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
        for section in self.sections:
            for path, size in section.selected_items():
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
        self.usage_label.setText(
            f"{human_size(used)} used of {human_size(total)}  -  "
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

        All three workers are stopped, not just Overview's: the Cleanup scan
        and the Recycle Bin move each own a QThread of their own, and either
        one left running at quit is the same crash.
        """
        for worker, thread in (
            (self.worker, self.thread),
            (self.cleanup_worker, self.cleanup_thread),
            (self.clean_worker, self.clean_thread),
        ):
            if worker:
                worker.abort()
            if thread:
                thread.quit()
                thread.wait()
        self.thread = self.worker = None
        self.cleanup_thread = self.cleanup_worker = None
        self.clean_thread = self.clean_worker = None

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
