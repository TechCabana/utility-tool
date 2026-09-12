# tabs/image_tab.py
import os
import time
from typing import List

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt

from utils import activity
from utils.file_utils import build_new_name
from utils.image_utils import (
    SUPPORTED_SUFFIXES, convert_and_save, estimate_compressed_size, spec_to_pixels,
)
from utils.presets import add_image_preset, get_image_presets
from widgets import icons
from widgets.common import (
    ConfirmDialog, EmptyState, PageHeader, SPACE_FIELD, add_field,
    card, data_label, field_pair, form_layout, section_header,
)
from widgets.pattern import PatternField


# The file list's two heights: a placeholder does not need the room a real
# list does, and at the app's minimum window the difference is most of the
# specification form's viewport.
LIST_HEIGHT_EMPTY = 116
LIST_HEIGHT_FULL = 150


def human_size(num_bytes: float) -> str:
    """Bytes as the smallest unit that keeps the number readable."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024 or unit == "GB":
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


class ImageRow(QtWidgets.QWidget):
    """One file in the batch: name, per-file progress, and its outcome."""

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.failed = False
        self.setObjectName("CardBody")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(10)

        self.label = QtWidgets.QLabel(os.path.basename(path))
        self.bar = QtWidgets.QProgressBar()
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.result = data_label("", muted=True)

        layout.addWidget(self.label, 3)
        layout.addWidget(self.bar, 2)
        layout.addWidget(self.result, 1)

    def mark_error(self, message: str):
        self.failed = True
        # The colour lives in the stylesheet, not in an inline sheet here: a
        # widget-level stylesheet outranks the app sheet and pins a literal
        # colour that the dark theme then cannot change.
        self.bar.setObjectName("DataBarCritical")
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)
        self.result.setObjectName("StatusError")
        self.result.setText("Failed")
        self.result.setToolTip(message)


class ImageWorker(QtCore.QObject):
    progress = QtCore.Signal(int, int)     # idx, percent
    done = QtCore.Signal(int, int)         # idx, bytes_out
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(float)

    def __init__(self, files: List[str], out_dir: str, fmt: str, size_key: str, quality: int,
                 compression: int, keep_exif: bool, pattern: str, prefix: str, suffix: str,
                 start: int, pad: int, regex_find: str, regex_replace: str, case: str, date_source: str):
        super().__init__()
        self.files = files
        self.out_dir = out_dir
        self.fmt = fmt
        self.size_key = size_key
        self.quality = quality
        self.compression = compression
        self.keep_exif = keep_exif
        self.pattern = pattern
        self.prefix = prefix
        self.suffix = suffix
        self.start = start
        self.pad = pad
        self.regex_find = regex_find
        self.regex_replace = regex_replace
        self.case = case
        self.date_source = date_source
        self._abort = False

    @QtCore.Slot()
    def run(self):
        t0 = time.time()
        for i, p in enumerate(self.files):
            if self._abort:
                break
            try:
                self.progress.emit(i, 5)
                # Same pattern-based naming engine File Tools uses, so an
                # image preset's naming fields behave identically -- only the
                # extension is swapped for the chosen output format below.
                base_name, _ = build_new_name(
                    p, self.pattern, self.prefix, self.suffix, i, self.start, self.pad,
                    self.date_source, self.regex_find, self.regex_replace, self.case
                )
                stem, _old_ext = os.path.splitext(base_name)
                _, src_ext = os.path.splitext(os.path.basename(p))
                out_ext = (self.fmt if self.fmt != "ORIGINAL" else src_ext.replace(".", "")) or "jpg"
                out_name = f"{stem}.{out_ext.lower()}"
                out_path = os.path.join(self.out_dir, out_name)
                size_px = spec_to_pixels(self.size_key)
                bytes_out = convert_and_save(p, out_path, self.fmt, size_px, self.quality,
                                             self.keep_exif, self.compression)
                self.progress.emit(i, 100)
                self.done.emit(i, bytes_out)
            except Exception as e:
                self.error.emit(i, str(e))
        self.finished.emit(time.time() - t0)

    def abort(self):
        self._abort = True


class ImageTab(QtWidgets.QWidget):
    """Batch compress / resize / convert.

    Layout follows the shape of the job: add the files, say what they should
    become, run it. The specification form is the part being worked on, so it
    is the part that gets the spare height -- previously the empty drop zone
    and the action footer between them took more of the window than the
    fourteen fields the user was actually filling in.
    """

    def __init__(self):
        super().__init__()
        self._out_dir_used = ""

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(SPACE_FIELD)

        root.addWidget(PageHeader(
            "Image Tools",
            "Compress, resize or convert a batch of images. Originals are untouched."))

        root.addWidget(self._build_files_card())
        root.addWidget(self._build_spec_area(), 1)
        root.addWidget(self._build_action_bar())

        self.thread = None
        self.worker = None
        self.rows: List[ImageRow] = []
        self._start_time = 0.0
        self._count = 0
        self._failures = 0

        self._reload_presets()
        self.preset_combo.currentIndexChanged.connect(self._apply_selected_preset)
        self._refresh_estimate()
        self._set_running(False)

        # The Add button's tooltip promises this, so it has to exist.
        from PySide6 import QtGui

        QtGui.QShortcut(QtGui.QKeySequence.Open, self).activated.connect(self._choose_files)

    # -- files -----------------------------------------------------------
    def _build_files_card(self) -> QtWidgets.QFrame:
        panel, body = card()

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(8)

        self.files_heading = QtWidgets.QLabel("Images")
        self.files_heading.setObjectName("H2")
        head.addWidget(self.files_heading)

        self.files_count = data_label("", muted=True)
        head.addWidget(self.files_count)
        head.addStretch(1)

        add_files = QtWidgets.QPushButton("  Add images")
        icons.set_icon(add_files, "plus", "text", 15)
        add_files.setToolTip("Add image files to the batch  (Ctrl+O)")
        add_files.clicked.connect(self._choose_files)
        head.addWidget(add_files)

        add_folder = QtWidgets.QPushButton("  Add folder")
        add_folder.setObjectName("Secondary")
        icons.set_icon(add_folder, "folder-open", "text_muted", 15)
        add_folder.setToolTip("Add every supported image in a folder")
        add_folder.clicked.connect(self._choose_folder)
        head.addWidget(add_folder)

        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.clear_btn.setObjectName("Ghost")
        self.clear_btn.setToolTip("Remove every file from the list. Nothing on disk is deleted.")
        self.clear_btn.clicked.connect(self.clear_files)
        head.addWidget(self.clear_btn)
        body.addLayout(head)

        # Bounded, and not the stretch target: the specification form below
        # is what gets worked on, so the spare vertical space belongs to it.
        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.listw.setAcceptDrops(True)
        # Sized by what it holds. An empty list is a placeholder and should
        # not reserve the height of a full one: at the app's 620px minimum
        # window that reservation left the fourteen-field form below it about
        # one field of viewport. `_resize_list()` grows it once files arrive.
        self.listw.setMinimumHeight(LIST_HEIGHT_EMPTY)
        self.listw.setMaximumHeight(LIST_HEIGHT_EMPTY)
        self.listw.dragEnterEvent = self._drag_enter
        self.listw.dropEvent = self._drop
        body.addWidget(self.listw)

        # Overlay rather than a swapped widget, so the list underneath stays
        # the live drop target while the empty state is showing.
        self.empty_state = EmptyState(
            title="No images yet",
            hint="Drag images here, or use Add images.",
            icon="file-image",
            parent=self.listw,
            compact=True,
        )
        self.empty_state.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.listw.resizeEvent = lambda e: (
            self.empty_state.resize(self.listw.size()),
            QtWidgets.QListWidget.resizeEvent(self.listw, e),
        )

        self._update_empty_state()
        return panel

    # -- specification ---------------------------------------------------
    def _build_spec_area(self) -> QtWidgets.QScrollArea:
        spec_body = QtWidgets.QWidget()
        spec_body.setObjectName("CardBody")
        outer = QtWidgets.QVBoxLayout(spec_body)
        # Margins so the card's drop shadow has room inside the viewport.
        outer.setContentsMargins(2, 2, 8, 8)

        panel, spec = card()
        outer.addWidget(panel)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(spec_body)

        # --- Output ------------------------------------------------------
        spec.addWidget(section_header("Output"))
        output = form_layout()
        spec.addLayout(output)

        self.preset_combo = QtWidgets.QComboBox()
        add_field(output, "Preset", self.preset_combo,
                  tooltip="Apply a saved combination of every setting on this screen")

        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItems(["ORIGINAL", "JPEG", "PNG", "WEBP", "TIFF"])
        self.fmt.currentIndexChanged.connect(self._refresh_estimate)
        add_field(output, "Format", self.fmt,
                  hint="ORIGINAL keeps each file in the format it already is.")

        self.size = QtWidgets.QComboBox()
        self.size.addItems(["Original", "1920px long edge", "1024px long edge",
                            "Instagram 1080×1080 px", "Passport – India (35×45 mm)",
                            "Passport – Netherlands (35×45 mm)", "Photo 4×6 in (102×152 mm)",
                            "Photo 5×7 in (127×178 mm)", "A4 (210×297 mm)", "A5 (148×210 mm)"])
        add_field(output, "Target size", self.size)

        self.quality = QtWidgets.QSlider(Qt.Horizontal)
        self.quality.setRange(10, 100)
        self.quality.setValue(85)
        self.quality_value = data_label("85")
        self.quality_value.setMinimumWidth(26)
        self.quality_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.quality.valueChanged.connect(self._on_quality_changed)

        quality_row = QtWidgets.QWidget()
        quality_row.setObjectName("CardBody")
        quality_layout = QtWidgets.QVBoxLayout(quality_row)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        quality_layout.setSpacing(2)

        slider_line = QtWidgets.QHBoxLayout()
        slider_line.setContentsMargins(0, 0, 0, 0)
        slider_line.setSpacing(10)
        slider_line.addWidget(self.quality, 1)
        slider_line.addWidget(self.quality_value)
        quality_layout.addLayout(slider_line)

        # Which end of a slider is "good" is not self-evident, and this one
        # trades two things people care about against each other.
        scale = QtWidgets.QHBoxLayout()
        scale.setContentsMargins(0, 0, 0, 0)
        smaller = QtWidgets.QLabel("Smaller file")
        smaller.setObjectName("Hint")
        better = QtWidgets.QLabel("Best quality")
        better.setObjectName("Hint")
        scale.addWidget(smaller)
        scale.addStretch(1)
        scale.addWidget(better)
        quality_layout.addLayout(scale)
        add_field(output, "Quality", quality_row)

        self.compression = QtWidgets.QSpinBox()
        self.compression.setRange(1, 6)
        self.compression.setValue(4)
        self.compression.setSuffix(" / 6")
        add_field(output, "Compression effort", self.compression,
                  hint="How hard to work at shrinking the file. Higher is smaller "
                       "but slower, and does not change how the image looks.")

        self.keep_exif = QtWidgets.QCheckBox("Keep EXIF data (camera, date, location)")
        self.keep_exif.setChecked(True)
        add_field(output, "", self.keep_exif)

        # --- File naming --------------------------------------------------
        spec.addWidget(section_header("File naming"))
        naming = form_layout()
        spec.addLayout(naming)

        self.pattern = PatternField("{name}")
        self.pattern.set_example_source("", self._describe_example)
        self.pattern.changed.connect(lambda _: None)
        add_field(naming, "Pattern", self.pattern)

        self.prefix = QtWidgets.QLineEdit()
        self.suffix = QtWidgets.QLineEdit()
        for widget in (self.prefix, self.suffix):
            widget.textChanged.connect(lambda _: self.pattern.refresh())
        add_field(naming, "Prefix", field_pair(self.prefix, "Suffix", self.suffix))

        # Named num_start (not `start`) -- ImageTab already has a `start()`
        # method that an instance attribute of the same name would shadow.
        self.num_start = QtWidgets.QSpinBox()
        self.num_start.setRange(0, 1_000_000)
        self.num_start.setValue(1)
        self.pad = QtWidgets.QSpinBox()
        self.pad.setRange(1, 10)
        self.pad.setValue(3)
        for widget in (self.num_start, self.pad):
            widget.valueChanged.connect(lambda _: self.pattern.refresh())
        add_field(naming, "Number from", field_pair(self.num_start, "Digits", self.pad),
                  hint="Used by the {num} token above.")

        self.date_source = QtWidgets.QComboBox()
        self.date_source.addItems(["now", "file_modified"])
        self.date_source.currentIndexChanged.connect(lambda _: self.pattern.refresh())
        add_field(naming, "Date comes from", self.date_source,
                  hint="Used by the {date:...} token: either today, or when the file was last changed.")

        # --- Find & replace ------------------------------------------------
        spec.addWidget(section_header("Find & replace"))
        replace = form_layout()
        spec.addLayout(replace)

        self.regex_find = QtWidgets.QLineEdit()
        self.regex_find.setObjectName("Mono")
        self.regex_find.setPlaceholderText(r"e.g.  \s+   or   IMG_")
        self.regex_replace = QtWidgets.QLineEdit()
        self.regex_replace.setObjectName("Mono")
        self.regex_replace.setPlaceholderText("Leave blank to delete what was found")
        for widget in (self.regex_find, self.regex_replace):
            widget.textChanged.connect(lambda _: self.pattern.refresh())
        add_field(replace, "Find", self.regex_find,
                  hint="A regular expression, matched against the name before the extension. "
                       "Leave blank to skip this step.")
        add_field(replace, "Replace with", self.regex_replace)

        self.case = QtWidgets.QComboBox()
        self.case.addItems(["none", "lower", "upper", "title"])
        self.case.currentIndexChanged.connect(lambda _: self.pattern.refresh())
        add_field(replace, "Letter case", self.case)

        # --- Destination ----------------------------------------------------
        spec.addWidget(section_header("Destination"))
        destination = form_layout()
        spec.addLayout(destination)

        out_row = QtWidgets.QWidget()
        out_row.setObjectName("CardBody")
        out_layout = QtWidgets.QHBoxLayout(out_row)
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.setSpacing(8)
        self.out_edit = QtWidgets.QLineEdit()
        self.out_edit.setPlaceholderText("Leave blank to write beside the originals")
        choose = QtWidgets.QPushButton("Choose…")
        choose.setObjectName("Secondary")
        choose.clicked.connect(self._choose_out)
        out_layout.addWidget(self.out_edit, 1)
        out_layout.addWidget(choose)
        add_field(destination, "Output folder", out_row)

        # Surplus height goes to the bottom of the card. Without this the
        # layout hands it to whichever children can grow, which on a tall
        # window meant section headings stretching and their underlines
        # drifting away from the text they belong to.
        spec.addStretch(1)
        return scroll

    # -- action bar ------------------------------------------------------
    def _build_action_bar(self) -> QtWidgets.QFrame:
        bar = QtWidgets.QFrame()
        bar.setObjectName("ActionBar")
        layout = QtWidgets.QVBoxLayout(bar)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)

        self.start_btn = QtWidgets.QPushButton("  Start")
        self.start_btn.setObjectName("Primary")
        icons.set_icon(self.start_btn, "play", "accent_text", 15)
        self.start_btn.setToolTip("Process every file in the list")
        self.start_btn.clicked.connect(self.start)
        row.addWidget(self.start_btn)

        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setObjectName("Secondary")
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.stop_btn)

        self.save_preset_btn = QtWidgets.QPushButton("  Save preset")
        self.save_preset_btn.setObjectName("Ghost")
        icons.set_icon(self.save_preset_btn, "bookmark", "text_muted", 15)
        self.save_preset_btn.setToolTip("Save every setting on this screen under a name")
        self.save_preset_btn.clicked.connect(self.save_current_preset)
        row.addWidget(self.save_preset_btn)

        row.addStretch(1)

        self.estimate_label = data_label("", muted=True)
        self.estimate_label.setToolTip(
            "Estimated from the selected image, or the first in the list, at the current settings")
        row.addWidget(self.estimate_label)
        layout.addLayout(row)

        self.overall = QtWidgets.QProgressBar()
        self.overall.setObjectName("WithText")
        self.overall.setFormat("%p%")
        self.overall.setValue(0)
        layout.addWidget(self.overall)

        self.status = QtWidgets.QLabel("Add images to get started.")
        self.status.setObjectName("Hint")
        layout.addWidget(self.status)

        self.retry_btn = QtWidgets.QPushButton("  Retry failed")
        self.retry_btn.setObjectName("Secondary")
        icons.set_icon(self.retry_btn, "rotate-ccw", "text_muted", 15)
        self.retry_btn.setToolTip(
            "Run the batch again with only the files that failed")
        self.retry_btn.clicked.connect(self.retry_failed)
        self.retry_btn.setVisible(False)
        layout.addWidget(self.retry_btn, 0, Qt.AlignLeft)

        self.open_output_btn = QtWidgets.QPushButton("  Open output folder")
        self.open_output_btn.setObjectName("Secondary")
        icons.set_icon(self.open_output_btn, "folder-open", "text_muted", 15)
        self.open_output_btn.clicked.connect(self._open_output)
        self.open_output_btn.setVisible(False)
        layout.addWidget(self.open_output_btn, 0, Qt.AlignLeft)
        return bar

    # -- state -----------------------------------------------------------
    def _set_running(self, running: bool):
        """One place decides what is pressable, so no button lies about state.

        Stop used to sit enabled while nothing was running, and Start stayed
        enabled with an empty list -- the screen said "press me" and then
        answered with a modal telling the user off for pressing it.
        """
        has_files = self.listw.count() > 0
        self.start_btn.setEnabled(has_files and not running)
        self.stop_btn.setEnabled(running)
        self.clear_btn.setEnabled(has_files and not running)
        self.save_preset_btn.setEnabled(not running)
        self.overall.setVisible(running or self.overall.value() > 0)

        if not has_files:
            self.start_btn.setToolTip("Add images to the list first")
        else:
            self.start_btn.setToolTip(f"Process {self.listw.count()} file(s)")

    def _resize_list(self):
        """Compact while empty, taller once there is something to show."""
        height = LIST_HEIGHT_EMPTY if self.listw.count() == 0 else LIST_HEIGHT_FULL
        self.listw.setMinimumHeight(height)
        self.listw.setMaximumHeight(height)

    def _update_empty_state(self):
        count = self.listw.count()
        self.empty_state.setVisible(count == 0)
        self._resize_list()
        self.files_count.setText("" if not count else f"{count} file{'s' if count != 1 else ''}")
        if hasattr(self, "start_btn"):
            self._set_running(self.thread is not None)
        if hasattr(self, "pattern"):
            self.pattern.set_example_source(self._first_path(), self._describe_example)
        self._refresh_estimate()

    def _first_path(self) -> str:
        item = self.listw.currentItem() or (self.listw.item(0) if self.listw.count() else None)
        return item.data(QtCore.Qt.UserRole) if item else ""

    # -- naming preview --------------------------------------------------
    def _describe_example(self, path: str) -> str:
        """The sentence under the pattern field: what the first file becomes."""
        source = path or "holiday photo.jpg"
        new_name, _ = build_new_name(
            source, self.pattern.text(), self.prefix.text(), self.suffix.text(), 0,
            self.num_start.value(), self.pad.value(), self.date_source.currentText(),
            self.regex_find.text(), self.regex_replace.text(), self.case.currentText())
        stem, _ = os.path.splitext(new_name)
        _, src_ext = os.path.splitext(os.path.basename(source))
        fmt = self.fmt.currentText()
        ext = (fmt.lower() if fmt != "ORIGINAL" else src_ext.lstrip(".")) or "jpg"
        return f"{os.path.basename(source)}  →  {stem}.{ext}"

    # -- estimate --------------------------------------------------------
    def _on_quality_changed(self, value: int):
        self.quality_value.setText(str(value))
        self._refresh_estimate()

    def _refresh_estimate(self):
        """Recompute the size estimate live rather than behind a button.

        This used to be an "Estimate Selected" button next to a label reading
        "Estimated: —", which is a question the app could answer on its own
        put to the user as a chore.
        """
        if not hasattr(self, "estimate_label"):
            return
        path = self._first_path()
        if not path or not os.path.isfile(path):
            self.estimate_label.setText("")
            return
        try:
            original = os.path.getsize(path)
            estimated = estimate_compressed_size(
                path, self.fmt.currentText(), self.quality.value(),
                self.keep_exif.isChecked(), self.compression.value())
        except Exception:
            # An estimate is a nicety; a file the estimator cannot read is
            # not a reason to interrupt the person setting up a batch.
            self.estimate_label.setText("")
            return
        if not estimated:
            self.estimate_label.setText("")
            return
        saved = 1 - (estimated / original) if original else 0
        direction = "smaller" if saved >= 0 else "larger"
        self.estimate_label.setText(
            f"{human_size(original)} → about {human_size(estimated)} "
            f"({abs(saved) * 100:.0f}% {direction})")

    # -- drag/drop and file adding ---------------------------------------
    def _drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _drop(self, event):
        added = 0
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path) and path.lower().endswith(SUPPORTED_SUFFIXES):
                self._add_file(path)
                added += 1
            elif os.path.isdir(path):
                added += self._add_folder(path)
        if not added:
            self.status.setText("Nothing added — those files are not image formats this tool reads.")

    def _add_file(self, path: str):
        item = QtWidgets.QListWidgetItem(os.path.basename(path))
        item.setData(QtCore.Qt.UserRole, path)
        item.setToolTip(path)
        self.listw.addItem(item)
        self._update_empty_state()

    def _add_folder(self, folder: str) -> int:
        added = 0
        for entry in sorted(os.listdir(folder)):
            path = os.path.join(folder, entry)
            if os.path.isfile(path) and path.lower().endswith(SUPPORTED_SUFFIXES):
                self._add_file(path)
                added += 1
        return added

    def _choose_files(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Add images", "",
            "Images (" + " ".join(f"*{s}" for s in SUPPORTED_SUFFIXES) + ")")
        for path in paths:
            self._add_file(path)
        if paths:
            self.status.setText(f"Added {len(paths)} file(s).")

    def _choose_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Add every image in a folder")
        if not folder:
            return
        added = self._add_folder(folder)
        self.status.setText(
            f"Added {added} image(s) from {os.path.basename(folder)}." if added
            else "That folder has no images this tool can read.")

    def clear_files(self):
        """Discard the current batch setup.

        Nothing is deleted from disk, but it throws away the list the user
        built with no undo, so it goes through the shared confirm dialog.
        """
        count = self.listw.count()
        if not count:
            return
        if not ConfirmDialog.ask(
            self,
            "Clear the image list?",
            f"This removes all {count} file(s) from the list. Files on disk are not affected.",
            confirm_text="Clear list",
        ):
            return
        self.listw.clear()
        self.rows.clear()
        self.overall.setValue(0)
        self.status.setText("Add images to get started.")
        self.open_output_btn.setVisible(False)
        self._update_empty_state()

    def _choose_out(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose output folder")
        if folder:
            self.out_edit.setText(folder)

    def _open_output(self):
        if not self._out_dir_used:
            return
        from PySide6 import QtGui

        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self._out_dir_used))

    # -- run -------------------------------------------------------------
    def _inflate_rows(self, files: List[str]):
        self.rows.clear()
        self.listw.clear()
        for path in files:
            row = ImageRow(path)
            self.rows.append(row)
            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setData(QtCore.Qt.UserRole, path)
            self.listw.addItem(item)
            self.listw.setItemWidget(item, row)
        self.empty_state.setVisible(False)

    def start(self):
        files = [self.listw.item(i).data(QtCore.Qt.UserRole) for i in range(self.listw.count())]
        if not files:
            # Unreachable through the UI (Start is disabled with an empty
            # list) and kept as a guard, not as the user-facing message.
            self.status.setText("Add images before starting.")
            return

        out_dir = self.out_edit.text() or os.path.dirname(files[0])
        self._out_dir_used = out_dir
        self._failures = 0
        self.open_output_btn.setVisible(False)
        self.retry_btn.setVisible(False)

        self._inflate_rows(files)
        self.overall.setValue(0)
        self.overall.setVisible(True)
        self.status.setText(f"Working on {len(files)} file(s)…")

        self.thread = QtCore.QThread(self)
        self.worker = ImageWorker(
            files, out_dir, self.fmt.currentText(), self.size.currentText(),
            self.quality.value(), self.compression.value(), self.keep_exif.isChecked(),
            self.pattern.text(), self.prefix.text(), self.suffix.text(),
            self.num_start.value(), self.pad.value(), self.regex_find.text(),
            self.regex_replace.text(), self.case.currentText(), self.date_source.currentText())
        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

        self._start_time = time.time()
        self._count = len(files)
        self._set_running(True)

    def retry_failed(self):
        """Rebuild the list from the files that failed, and run it again.

        A failure is usually about one file - a permission, a truncated
        image - and losing the other forty-nine files' worth of setup to get
        at it is the kind of thing that makes a tool feel hostile.
        """
        failed = [row.path for row in self.rows if row.failed]
        if not failed:
            return
        self.listw.clear()
        self.rows.clear()
        for path in failed:
            self._add_file(path)
        self.status.setText(f"Retrying {len(failed)} file(s) that failed.")
        self.start()

    def stop(self):
        if self.worker:
            self.worker.abort()
        self.status.setText("Stopping after the current file…")

    def on_progress(self, idx: int, percent: int):
        if 0 <= idx < len(self.rows):
            self.rows[idx].bar.setValue(percent)
        total = sum(row.bar.value() for row in self.rows)
        overall = int(total / max(1, len(self.rows)))
        self.overall.setValue(overall)
        completed = sum(1 for row in self.rows if row.bar.value() >= 100)
        elapsed = time.time() - (self._start_time or time.time())
        remaining = (elapsed / max(1, completed) * (len(self.rows) - completed)) if completed else 0
        self.status.setText(
            f"{completed} of {len(self.rows)} done"
            + (f" · about {remaining:.0f}s left" if remaining else ""))

    def on_done(self, idx: int, bytes_out: int):
        if 0 <= idx < len(self.rows):
            self.rows[idx].bar.setValue(100)
            self.rows[idx].result.setText(human_size(bytes_out))

    def on_error(self, idx: int, msg: str):
        self._failures += 1
        if 0 <= idx < len(self.rows):
            self.rows[idx].mark_error(msg)

    def on_finished(self, seconds: float):
        done = self._count - self._failures
        summary = f"Finished {done} of {self._count} file(s) in {seconds:.1f}s"
        if self._failures:
            summary += f" · {self._failures} failed — hover a row to see why"
            self.status.setObjectName("StatusWarn")
        else:
            self.status.setObjectName("Hint")
        self.status.setText(summary)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

        self.open_output_btn.setVisible(bool(self._out_dir_used))
        self.retry_btn.setVisible(bool(self._failures))
        activity.record(summary, kind="image")

        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
            self.worker = None
        self._set_running(False)

    # -- presets ---------------------------------------------------------
    def save_current_preset(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Save image preset", "Preset name")
        if not ok or not name.strip():
            return
        preset = {
            "name": name.strip(),
            "format": self.fmt.currentText(),
            "quality": self.quality.value(),
            "size_key": self.size.currentText(),
            "compression": self.compression.value(),
            "keep_exif": self.keep_exif.isChecked(),
            "pattern": self.pattern.text(),
            "prefix": self.prefix.text(),
            "suffix": self.suffix.text(),
            "start": self.num_start.value(),
            "pad": self.pad.value(),
            "regex_find": self.regex_find.text(),
            "regex_replace": self.regex_replace.text(),
            "case": self.case.currentText(),
            "date_source": self.date_source.currentText(),
        }
        add_image_preset(preset)
        self._reload_presets()
        # Inline, not a modal: a modal for a success message makes the user
        # dismiss a dialog to confirm that nothing went wrong.
        self.status.setText(f"Saved the preset “{name.strip()}”. It is in Settings › Presets.")

    def _reload_presets(self):
        self._image_presets = get_image_presets()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— Select preset —")
        for preset in self._image_presets:
            self.preset_combo.addItem(preset.get("name", "(unnamed)"))
        self.preset_combo.blockSignals(False)

    def _apply_selected_preset(self, index: int):
        if index <= 0 or index - 1 >= len(self._image_presets):
            return
        preset = self._image_presets[index - 1]
        self.fmt.setCurrentText(preset.get("format", "ORIGINAL"))
        self.size.setCurrentText(preset.get("size_key", "Original"))
        self.quality.setValue(int(preset.get("quality", 85)))
        self.compression.setValue(int(preset.get("compression", 4)))
        self.keep_exif.setChecked(bool(preset.get("keep_exif", True)))
        self.pattern.setText(preset.get("pattern", "{name}"))
        self.prefix.setText(preset.get("prefix", ""))
        self.suffix.setText(preset.get("suffix", ""))
        self.num_start.setValue(int(preset.get("start", 1)))
        self.pad.setValue(int(preset.get("pad", 3)))
        self.regex_find.setText(preset.get("regex_find", ""))
        self.regex_replace.setText(preset.get("regex_replace", ""))
        self.case.setCurrentText(preset.get("case", "none"))
        self.date_source.setCurrentText(preset.get("date_source", "now"))
        self.status.setText(f"Applied the preset “{preset.get('name', '')}”.")
