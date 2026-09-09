# tabs/image_tab.py
import os, time
from typing import List, Optional
from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Qt
from utils.image_utils import (
    SUPPORTED_SUFFIXES, spec_to_pixels, estimate_compressed_size, convert_and_save,
)
from utils.file_utils import build_new_name
from utils.presets import add_image_preset, get_image_presets, load_all
from widgets.common import (
    ConfirmDialog, EmptyState, SPACE_BLOCK, SPACE_FIELD, field_pair, form_layout,
    section_header,
)

class ImageRow(QtWidgets.QWidget):
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4,2,4,2)
        self.label = QtWidgets.QLabel(os.path.basename(path))
        self.bar = QtWidgets.QProgressBar()
        self.bar.setValue(0)
        layout.addWidget(self.label, 3)
        layout.addWidget(self.bar, 2)

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
                # small step: estimate
                try:
                    est = estimate_compressed_size(p, self.fmt, self.quality, self.keep_exif, self.compression)
                except Exception:
                    est = None
                self.progress.emit(i, 5)
                # Same pattern-based naming engine File Tools uses, so an
                # image preset's naming fields (pattern/prefix/suffix/num/
                # regex/case/date) behave identically -- only the extension
                # is swapped for the chosen output format below.
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
                # do save (this is the heavy op)
                bytes_out = convert_and_save(p, out_path, self.fmt, size_px, self.quality, self.keep_exif, self.compression)
                self.progress.emit(i, 100)
                self.done.emit(i, bytes_out)
            except Exception as e:
                self.error.emit(i, str(e))
        self.finished.emit(time.time() - t0)

    def abort(self):
        self._abort = True

class ImageTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.presets = load_all()
        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(SPACE_BLOCK)

        # Header
        header = QtWidgets.QLabel("Image Tools")
        header.setObjectName("H1")
        v.addWidget(header)

        # file list (also the drop target). Bounded, and not the stretch
        # target -- the specification form below is what gets worked on, so
        # the spare vertical space belongs to it.
        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.listw.setAcceptDrops(True)
        self.listw.setMinimumHeight(120)
        self.listw.setMaximumHeight(160)
        self.listw.dragEnterEvent = self._drag_enter
        self.listw.dropEvent = self._drop
        v.addWidget(self.listw)

        # Empty-state overlay: shown only while no files are added, hidden
        # once files land via drag/drop. Transparent to mouse events so
        # drag/drop still reaches the list widget underneath it.
        self.empty_state = EmptyState(
            title="No images added yet",
            hint="Drag and drop image files here to begin",
            parent=self.listw,
        )
        self.empty_state.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.listw.resizeEvent = lambda e: (
            self.empty_state.resize(self.listw.size()),
            QtWidgets.QListWidget.resizeEvent(self.listw, e),
        )
        self._update_empty_state()

        # ----- Specification stage -------------------------------------
        # Fourteen fields in one flat form read as a settings dump, and on a
        # short window Qt squeezed the rows until their values clipped. They
        # are split into labelled groups by section_header() inside one card,
        # in a scroll area -- grouped by spacing and a rule, never by giving
        # each group its own box inside the card.
        spec_body = QtWidgets.QWidget()
        spec_body.setObjectName("CardBody")
        spec_body_v = QtWidgets.QVBoxLayout(spec_body)
        # Margins so the card's drop shadow has room inside the viewport.
        spec_body_v.setContentsMargins(2, 2, 2, 8)

        spec_card = QtWidgets.QFrame()
        spec_card.setObjectName("Card")
        spec = QtWidgets.QVBoxLayout(spec_card)
        spec.setSpacing(SPACE_FIELD)
        spec_body_v.addWidget(spec_card)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(spec_body)
        v.addWidget(scroll, 1)

        # --- Output: what the images become ----------------------------
        spec.addWidget(section_header("Output"))
        output = form_layout()
        spec.addLayout(output)

        # Preset: applies format/size/quality/compression/naming fields in
        # one shot, the same way File Tools' preset dropdown applies its
        # pattern-based rename presets.
        self.preset_combo = QtWidgets.QComboBox()
        output.addRow("Preset:", self.preset_combo)

        self.fmt = QtWidgets.QComboBox(); self.fmt.addItems(["ORIGINAL","JPEG","PNG","WEBP","TIFF"])
        output.addRow("Format:", self.fmt)

        self.size = QtWidgets.QComboBox()
        self.size.addItems(["Original", "1920px long edge", "1024px long edge", "Instagram 1080×1080 px",
                            "Passport – India (35×45 mm)", "Passport – Netherlands (35×45 mm)",
                            "Photo 4×6 in (102×152 mm)", "Photo 5×7 in (127×178 mm)", "A4 (210×297 mm)", "A5 (148×210 mm)"])
        output.addRow("Target size:", self.size)

        # A bare slider showed no value at all, so the one setting most worth
        # knowing was the one the screen would not tell you.
        self.quality = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.quality.setRange(10, 100); self.quality.setValue(85)
        self.quality_value = QtWidgets.QLabel(str(self.quality.value()))
        self.quality_value.setMinimumWidth(28)
        self.quality_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.quality.valueChanged.connect(
            lambda value: self.quality_value.setText(str(value)))
        quality_row = QtWidgets.QWidget()
        quality_row.setObjectName("CardBody")
        qh = QtWidgets.QHBoxLayout(quality_row)
        qh.setContentsMargins(0, 0, 0, 0); qh.setSpacing(10)
        qh.addWidget(self.quality, 1); qh.addWidget(self.quality_value)
        output.addRow("Quality:", quality_row)

        self.compression = QtWidgets.QSpinBox(); self.compression.setRange(1, 6); self.compression.setValue(4)
        # The range moved from the label into the box ("4 / 6"), so the label
        # column is not sized by a parenthetical. setSuffix is display only --
        # value() still returns the plain int the worker expects.
        self.compression.setSuffix(" / 6")
        output.addRow("Compression effort:", self.compression)

        self.keep_exif = QtWidgets.QCheckBox("Keep EXIF"); self.keep_exif.setChecked(True)
        # An empty label so the checkbox starts on the field column with
        # everything else, rather than spanning back to the card's edge.
        output.addRow("", self.keep_exif)

        # --- File naming: same fields/shape as File Tools ---------------
        # (utils/file_utils.build_new_name drives both.)
        spec.addWidget(section_header("File naming"))
        naming = form_layout()
        spec.addLayout(naming)

        self.pattern = QtWidgets.QLineEdit("{name}")
        naming.addRow("Pattern:", self.pattern)

        self.prefix = QtWidgets.QLineEdit(); self.suffix = QtWidgets.QLineEdit()
        naming.addRow("Prefix:", field_pair(self.prefix, "Suffix:", self.suffix))

        # Named num_start (not `start`) -- ImageTab already has a `start()`
        # method (the batch-start handler) that an instance attribute of the
        # same name would shadow.
        self.num_start = QtWidgets.QSpinBox(); self.num_start.setRange(0, 1_000_000); self.num_start.setValue(1)
        self.pad = QtWidgets.QSpinBox(); self.pad.setRange(1, 10); self.pad.setValue(3)
        naming.addRow("Number start:", field_pair(self.num_start, "Pad:", self.pad))

        self.date_source = QtWidgets.QComboBox(); self.date_source.addItems(["now", "file_modified"])
        naming.addRow("Date source:", self.date_source)

        # --- Find & replace --------------------------------------------
        spec.addWidget(section_header("Find & replace"))
        replace = form_layout()
        spec.addLayout(replace)

        self.regex_find = QtWidgets.QLineEdit()
        self.regex_replace = QtWidgets.QLineEdit()
        replace.addRow("Regex find:", self.regex_find)
        replace.addRow("Regex replace:", self.regex_replace)

        self.case = QtWidgets.QComboBox(); self.case.addItems(["none", "lower", "upper", "title"])
        replace.addRow("Case:", self.case)

        # --- Destination ------------------------------------------------
        spec.addWidget(section_header("Destination"))
        destination = form_layout()
        spec.addLayout(destination)

        out_row = QtWidgets.QWidget()
        out_row.setObjectName("CardBody")
        out_h = QtWidgets.QHBoxLayout(out_row)
        out_h.setContentsMargins(0, 0, 0, 0); out_h.setSpacing(8)
        self.out_edit = QtWidgets.QLineEdit()
        self.out_edit.setPlaceholderText("Leave blank to write beside the originals")
        choose = QtWidgets.QPushButton("Choose...")
        choose.clicked.connect(self._choose_out)
        out_h.addWidget(self.out_edit, 1); out_h.addWidget(choose)
        destination.addRow("Output folder:", out_row)

        # Surplus height goes to the bottom of the card. Without this the
        # layout hands it to whichever children can grow, which on a tall
        # window meant the section headings stretching and their underlines
        # drifting away from the text they belong to.
        spec.addStretch(1)

        self._reload_presets()
        self.preset_combo.currentIndexChanged.connect(self._apply_selected_preset)

        # ----- Processing (footer) --------------------------------------
        # Outside the scroll area, so the run controls and the progress of a
        # live batch stay on screen while the form above is scrolled.
        footer = QtWidgets.QFrame()
        footer.setObjectName("Card")
        fv = QtWidgets.QVBoxLayout(footer)
        fv.setSpacing(SPACE_FIELD)

        # One row: what runs the batch on the left, what inspects it on the
        # right. Two rows of buttons made the footer taller than the form it
        # acts on.
        act = QtWidgets.QHBoxLayout()
        act.setSpacing(8)
        self.start_btn = QtWidgets.QPushButton("Start")
        # The screen's one accent-filled surface is the action it exists to
        # perform (DESIGN.md, Materials & Components).
        self.start_btn.setObjectName("Primary")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.save_preset_btn = QtWidgets.QPushButton("Save Preset")
        act.addWidget(self.start_btn); act.addWidget(self.stop_btn)
        act.addWidget(self.clear_btn); act.addWidget(self.save_preset_btn)
        act.addStretch(1)

        self.preview_btn = QtWidgets.QPushButton("Estimate Selected")
        self.preview_btn.setObjectName("Secondary")
        self.preview_btn.clicked.connect(self.preview_selected)
        self.preview_label = QtWidgets.QLabel("Estimated: —")
        act.addWidget(self.preview_btn)
        act.addWidget(self.preview_label)
        fv.addLayout(act)

        self.overall = QtWidgets.QProgressBar()
        self.status = QtWidgets.QLabel("Idle")
        self.status.setObjectName("Hint")
        fv.addWidget(self.overall); fv.addWidget(self.status)
        v.addWidget(footer)

        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.clear_btn.clicked.connect(self.clear_files)
        self.save_preset_btn.clicked.connect(self.save_current_preset)

        self.thread = None
        self.worker = None
        self.rows: List[ImageRow] = []
        self._start_time = 0.0
        self._count = 0

    # drag/drop
    def _drag_enter(self, ev):
        if ev.mimeData().hasUrls(): ev.acceptProposedAction()
    def _drop(self, ev):
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p) and p.lower().endswith(SUPPORTED_SUFFIXES):
                self._add_file(p)

    def _add_file(self, path: str):
        it = QtWidgets.QListWidgetItem(os.path.basename(path))
        it.setData(QtCore.Qt.UserRole, path)
        self.listw.addItem(it)
        self._update_empty_state()

    def _update_empty_state(self):
        self.empty_state.setVisible(self.listw.count() == 0)

    def clear_files(self):
        """Clear button: discards the current file selection.

        Nothing gets deleted from disk, but this does throw away the
        user's batch setup (added files, per-file rows) with no way to
        undo it, so it goes through the shared confirm dialog -- Move/
        Copy/Delete in File Manager aren't built yet, so this is the
        first genuinely destructive-from-the-user's-POV action in the app.
        """
        count = self.listw.count()
        if not count:
            return
        if not ConfirmDialog.ask(
            self,
            "Clear file list?",
            f"This removes all {count} file(s) from the list. "
            "Files on disk are not affected.",
            confirm_text="Clear",
        ):
            return
        self.listw.clear()
        self.rows.clear()
        self._update_empty_state()

    def _choose_out(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose output folder")
        if d:
            self.out_edit.setText(d)

    def preview_selected(self):
        item = self.listw.currentItem()
        if not item:
            QtWidgets.QMessageBox.information(self, "Estimate", "Select one image to estimate.")
            return
        path = item.data(QtCore.Qt.UserRole)
        try:
            est = estimate_compressed_size(path, self.fmt.currentText(), self.quality.value(), self.keep_exif.isChecked())
            self.preview_label.setText(f"Estimated: {est/1024:.1f} KB" if est else "Estimated: —")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Estimate failed", str(e))

    def _inflate_rows(self, files: List[str]):
        self.rows.clear()
        self.listw.clear()
        for p in files:
            row = ImageRow(p)
            self.rows.append(row)
            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            item.setData(QtCore.Qt.UserRole, p)
            self.listw.addItem(item)
            self.listw.setItemWidget(item, row)
        self._update_empty_state()

    def start(self):
        files = [self.listw.item(i).data(QtCore.Qt.UserRole) for i in range(self.listw.count())]
        if not files:
            QtWidgets.QMessageBox.information(self, "No files", "Add images first.")
            return
        out_dir = self.out_edit.text() or os.path.dirname(files[0])
        fmt = self.fmt.currentText()
        size_key = self.size.currentText()
        quality = self.quality.value()
        compression = self.compression.value()
        prefix = self.prefix.text()
        suffix = self.suffix.text()
        keep_exif = self.keep_exif.isChecked()
        pattern = self.pattern.text()
        start = self.num_start.value()
        pad = self.pad.value()
        regex_find = self.regex_find.text()
        regex_replace = self.regex_replace.text()
        case = self.case.currentText()
        date_source = self.date_source.currentText()

        self._inflate_rows(files)
        self.overall.setValue(0)
        self.status.setText("Starting…")

        self.thread = QtCore.QThread(self)
        self.worker = ImageWorker(files, out_dir, fmt, size_key, quality, compression, keep_exif,
                                   pattern, prefix, suffix, start, pad, regex_find, regex_replace, case, date_source)
        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)
        self.thread.started.connect(self.worker.run)
        self.thread.start()

        self._start_time = time.time()
        self._count = len(files)

    def stop(self):
        if self.worker:
            self.worker.abort()
        self.status.setText("Stopping…")

    def on_progress(self, idx: int, percent: int):
        if 0 <= idx < len(self.rows):
            self.rows[idx].bar.setValue(percent)
        total = sum(r.bar.value() for r in self.rows)
        overall = int(total / max(1, len(self.rows)))
        self.overall.setValue(overall)
        completed = sum(1 for r in self.rows if r.bar.value() >= 100)
        elapsed = time.time() - getattr(self, "_start_time", time.time())
        eta = (elapsed / max(1, completed) * (len(self.rows)-completed)) if completed else 0
        self.status.setText(f"{overall}% • ETA ~ {eta:.1f}s")

    def on_done(self, idx: int, bytes_out: int):
        if 0 <= idx < len(self.rows):
            self.rows[idx].label.setText(f"{self.rows[idx].label.text()}  —  {bytes_out/1024:.1f} KB")
            self.rows[idx].bar.setValue(100)

    def on_error(self, idx: int, msg: str):
        if 0 <= idx < len(self.rows):
            self.rows[idx].label.setText(self.rows[idx].label.text() + f" — ERROR: {msg}")
            self.rows[idx].bar.setStyleSheet("QProgressBar::chunk { background: #c23b32; }")

    def on_finished(self, seconds: float):
        self.status.setText(f"Completed in {seconds:.1f}s")
        if self.thread:
            self.thread.quit(); self.thread.wait()
            self.thread = None; self.worker = None

    def save_current_preset(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Save Image Preset", "Preset name:")
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
        QtWidgets.QMessageBox.information(self, "Saved", f"Image preset '{name}' saved.")

    # ------------------------------
    # Presets: load + apply
    # ------------------------------
    def _reload_presets(self):
        """Repopulate the preset dropdown from disk (initial load, and after
        Save Preset adds a new one)."""
        self._image_presets = get_image_presets()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— Select preset —")
        for p in self._image_presets:
            self.preset_combo.addItem(p.get("name", "(unnamed)"))
        self.preset_combo.blockSignals(False)

    def _apply_selected_preset(self, index: int):
        """Apply a preset's format/size/quality/compression/naming fields to
        the form -- mirrors File Tools' _apply_selected_preset."""
        if index <= 0 or index - 1 >= len(self._image_presets):
            return
        p = self._image_presets[index - 1]
        self.fmt.setCurrentText(p.get("format", "ORIGINAL"))
        self.size.setCurrentText(p.get("size_key", "Original"))
        self.quality.setValue(int(p.get("quality", 85)))
        self.compression.setValue(int(p.get("compression", 4)))
        self.keep_exif.setChecked(bool(p.get("keep_exif", True)))
        self.pattern.setText(p.get("pattern", "{name}"))
        self.prefix.setText(p.get("prefix", ""))
        self.suffix.setText(p.get("suffix", ""))
        self.num_start.setValue(int(p.get("start", 1)))
        self.pad.setValue(int(p.get("pad", 3)))
        self.regex_find.setText(p.get("regex_find", ""))
        self.regex_replace.setText(p.get("regex_replace", ""))
        self.case.setCurrentText(p.get("case", "none"))
        self.date_source.setCurrentText(p.get("date_source", "now"))
