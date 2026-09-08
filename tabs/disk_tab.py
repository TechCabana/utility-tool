# tabs/disk_tab.py
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Qt

from utils.disk_utils import human_size, top_level_breakdown
from widgets.common import EmptyState


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

    Only Overview has real content. Cleanup (junk reclaim, with Duplicates as
    a section inside it) and Backup are separate cards; their sub-tabs carry
    an EmptyState rather than a half-built screen.
    """

    def __init__(self):
        super().__init__()
        self.home = str(Path.home())
        self.thread: Optional[QtCore.QThread] = None
        self.worker: Optional[ScanWorker] = None
        self._scanned = False

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
        self.sub.addTab(
            EmptyState(
                title="Cleanup isn't built yet",
                hint="Reclaiming system junk, caches, old installers and "
                     "duplicate files lands in a later release.",
            ),
            "Cleanup",
        )
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
        """Stop an in-flight scan so quitting mid-scan doesn't destroy a
        running QThread. Wired to QApplication.aboutToQuit."""
        if self.worker:
            self.worker.abort()
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
            self.worker = None

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
