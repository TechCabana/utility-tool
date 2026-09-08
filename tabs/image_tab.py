# tabs/image_tab.py
import os, time
from typing import List, Optional
from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Qt
from utils.image_utils import spec_to_pixels, estimate_compressed_size, convert_and_save
from utils.presets import add_image_preset, load_all

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

    def __init__(self, files: List[str], out_dir: str, fmt: str, size_key: str, quality: int, prefix: str, suffix: str, keep_exif: bool):
        super().__init__()
        self.files = files
        self.out_dir = out_dir
        self.fmt = fmt
        self.size_key = size_key
        self.quality = quality
        self.prefix = prefix
        self.suffix = suffix
        self.keep_exif = keep_exif
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
                    est = estimate_compressed_size(p, self.fmt, self.quality, self.keep_exif)
                except Exception:
                    est = None
                self.progress.emit(i, 5)
                base, ext = os.path.splitext(os.path.basename(p))
                out_ext = (self.fmt if self.fmt != "ORIGINAL" else ext.replace(".", "")) or "jpg"
                out_name = f"{self.prefix}{base}{self.suffix}.{out_ext.lower()}"
                out_path = os.path.join(self.out_dir, out_name)
                size_px = spec_to_pixels(self.size_key)
                # do save (this is the heavy op)
                bytes_out = convert_and_save(p, out_path, self.fmt, size_px, self.quality, self.keep_exif)
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
        v.setSpacing(10)

        # Header
        header = QtWidgets.QLabel("Image Tools")
        header.setObjectName("H1")
        v.addWidget(header)

        # Instruction label (centered in the available space)
        self.drop_label = QtWidgets.QLabel("Drag and drop your files to begin")
        self.drop_label.setAlignment(QtCore.Qt.AlignCenter)
        self.drop_label.setStyleSheet("color: #71717a; font-size: 14px; padding: 40px;")
        v.addWidget(self.drop_label)
    
        # file list
        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.listw.setAcceptDrops(True)
        self.listw.dragEnterEvent = self._drag_enter
        self.listw.dropEvent = self._drop
        v.addWidget(self.listw, 1)

        # controls
        form = QtWidgets.QFormLayout()
        self.fmt = QtWidgets.QComboBox(); self.fmt.addItems(["ORIGINAL","JPEG","PNG","WEBP","TIFF"])
        self.size = QtWidgets.QComboBox(); self.size.addItems(list(spec_to_pixels.__self__ if hasattr(spec_to_pixels, "__self__") else [] ) or list(load_all().get("image", [])))
        # issue: above is placeholder — override with STANDARD keys below
        # But we can't import STANDARD_SIZES_MM here without circular import; instead, provide fixed list:
        self.size.clear()
        self.size.addItems(["Original", "Passport – India (35×45 mm)", "Passport – Netherlands (35×45 mm)",
                            "Photo 4×6 in (102×152 mm)", "Photo 5×7 in (127×178 mm)", "A4 (210×297 mm)", "A5 (148×210 mm)"])
        self.quality = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.quality.setRange(10, 100); self.quality.setValue(85)
        self.prefix = QtWidgets.QLineEdit(); self.suffix = QtWidgets.QLineEdit()
        self.keep_exif = QtWidgets.QCheckBox("Keep EXIF"); self.keep_exif.setChecked(True)

        form.addRow("Format:", self.fmt)
        form.addRow("Target size:", self.size)
        form.addRow("Quality:", self.quality)
        form.addRow("Prefix:", self.prefix)
        form.addRow("Suffix:", self.suffix)
        form.addRow("", self.keep_exif)
        v.addLayout(form)

        # output folder
        out_h = QtWidgets.QHBoxLayout()
        self.out_edit = QtWidgets.QLineEdit()
        choose = QtWidgets.QPushButton("Choose Output Folder")
        choose.clicked.connect(self._choose_out)
        out_h.addWidget(self.out_edit, 1); out_h.addWidget(choose)
        v.addLayout(out_h)

        # preview & estimate
        est_h = QtWidgets.QHBoxLayout()
        self.preview_btn = QtWidgets.QPushButton("Estimate Selected")
        self.preview_btn.clicked.connect(self.preview_selected)
        self.preview_label = QtWidgets.QLabel("Estimated: —")
        est_h.addWidget(self.preview_btn); est_h.addWidget(self.preview_label)
        v.addLayout(est_h)

        # action buttons
        act = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.clear_btn = QtWidgets.QPushButton("Clear")
        self.save_preset_btn = QtWidgets.QPushButton("Save Preset")
        act.addWidget(self.start_btn); act.addWidget(self.stop_btn); act.addWidget(self.clear_btn); act.addWidget(self.save_preset_btn)
        v.addLayout(act)

        self.overall = QtWidgets.QProgressBar()
        self.status = QtWidgets.QLabel("Idle")
        v.addWidget(self.overall); v.addWidget(self.status)

        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.clear_btn.clicked.connect(self.listw.clear)
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
            if os.path.isfile(p) and p.lower().split(".")[-1] in ("jpg","jpeg","png","webp","tif","tiff"):
                self._add_file(p)

    def _add_file(self, path: str):
        it = QtWidgets.QListWidgetItem(os.path.basename(path))
        it.setData(QtCore.Qt.UserRole, path)
        self.listw.addItem(it)

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

    def start(self):
        files = [self.listw.item(i).data(QtCore.Qt.UserRole) for i in range(self.listw.count())]
        if not files:
            QtWidgets.QMessageBox.information(self, "No files", "Add images first.")
            return
        out_dir = self.out_edit.text() or os.path.dirname(files[0])
        fmt = self.fmt.currentText()
        size_key = self.size.currentText()
        quality = self.quality.value()
        prefix = self.prefix.text()
        suffix = self.suffix.text()
        keep_exif = self.keep_exif.isChecked()

        self._inflate_rows(files)
        self.overall.setValue(0)
        self.status.setText("Starting…")

        self.thread = QtCore.QThread(self)
        self.worker = ImageWorker(files, out_dir, fmt, size_key, quality, prefix, suffix, keep_exif)
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
            "prefix": self.prefix.text(),
            "suffix": self.suffix.text(),
            "keep_exif": self.keep_exif.isChecked()
        }
        add_image_preset(preset)
        QtWidgets.QMessageBox.information(self, "Saved", f"Image preset '{name}' saved.")
