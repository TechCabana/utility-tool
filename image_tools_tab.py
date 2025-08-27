import os, time
from typing import List
from PySide6 import QtWidgets, QtCore
from image_utils import open_image, resize_if_needed, estimate_output_size, save_with_format, STANDARD_SIZES_MM

class FileRow(QtWidgets.QWidget):
    """Row with filename + per-file progress bar."""
    def __init__(self, path: str):
        super().__init__()
        self.path = path
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(4,2,4,2)
        self.label = QtWidgets.QLabel(os.path.basename(path))
        self.bar = QtWidgets.QProgressBar()
        self.bar.setValue(0)
        lay.addWidget(self.label, 2)
        lay.addWidget(self.bar, 1)

class Worker(QtCore.QObject):
    progress = QtCore.Signal(int, int)         # (index, percent)
    file_done = QtCore.Signal(int, int)        # (index, bytes_out)
    all_done = QtCore.Signal(float)            # total seconds
    error = QtCore.Signal(int, str)            # (index, message)

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
        for i, path in enumerate(self.files):
            if self._abort: break
            try:
                img = open_image(path)
                img = resize_if_needed(img, self.size_key)
                # Build output name
                base, ext = os.path.splitext(os.path.basename(path))
                out_ext = (self.fmt if self.fmt!="ORIGINAL" else ext.replace(".", "")) or "jpg"
                out_name = f"{self.prefix}{base}{self.suffix}.{out_ext.lower()}"
                out_path = os.path.join(self.out_dir, out_name)

                # Estimate size (emit 10%)
                est = estimate_output_size(img, self.fmt, self.quality, self.keep_exif)
                self.progress.emit(i, 10)

                # Save actual file (emit 100%)
                save_with_format(img, out_path, self.fmt, self.quality, self.keep_exif)
                self.progress.emit(i, 100)
                self.file_done.emit(i, os.path.getsize(out_path))
            except Exception as e:
                self.error.emit(i, str(e))
        self.all_done.emit(time.time()-t0)

    def abort(self):
        self._abort = True

class ImageToolsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        v = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Image Tools — Convert • Resize • Compress")
        title.setStyleSheet("font-weight:700; font-size:18px;")
        v.addWidget(title)

        # file list (drag & drop)
        self.list_area = QtWidgets.QListWidget()
        self.list_area.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list_area.setAcceptDrops(True)
        self.list_area.dragEnterEvent = self._drag_enter
        self.list_area.dragMoveEvent = self._drag_enter
        self.list_area.dropEvent = self._drop
        v.addWidget(self.list_area, 1)

        # controls
        form = QtWidgets.QFormLayout()
        self.format_box = QtWidgets.QComboBox()
        self.format_box.addItems(["ORIGINAL","JPEG","PNG","WEBP","TIFF"])
        form.addRow("Output format:", self.format_box)

        self.size_box = QtWidgets.QComboBox()
        self.size_box.addItems(list(STANDARD_SIZES_MM.keys()))
        form.addRow("Target size:", self.size_box)

        self.quality = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.quality.setRange(10, 100)
        self.quality.setValue(85)
        form.addRow("Quality:", self.quality)

        self.prefix = QtWidgets.QLineEdit()
        self.suffix = QtWidgets.QLineEdit()
        form.addRow("Prefix:", self.prefix)
        form.addRow("Suffix:", self.suffix)

        self.keep_exif = QtWidgets.QCheckBox("Keep EXIF (when available)")
        self.keep_exif.setChecked(True)
        form.addRow("", self.keep_exif)

        v.addLayout(form)

        path_row = QtWidgets.QHBoxLayout()
        self.out_dir = QtWidgets.QLineEdit()
        choose = QtWidgets.QPushButton("Choose Output Folder")
        choose.clicked.connect(self._choose_out)
        path_row.addWidget(QtWidgets.QLabel("Output folder:"))
        path_row.addWidget(self.out_dir, 1)
        path_row.addWidget(choose)
        v.addLayout(path_row)

        # preview size for selected file
        self.preview_btn = QtWidgets.QPushButton("Preview Selected → Estimated Output Size")
        self.preview_btn.clicked.connect(self.preview_selected)
        self.preview_label = QtWidgets.QLabel("Estimated: —")
        v.addWidget(self.preview_btn)
        v.addWidget(self.preview_label)

        # action buttons
        hb = QtWidgets.QHBoxLayout()
        self.run_btn = QtWidgets.QPushButton("Process")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.clear_btn = QtWidgets.QPushButton("Clear List")
        hb.addWidget(self.run_btn); hb.addWidget(self.stop_btn); hb.addWidget(self.clear_btn)
        v.addLayout(hb)

        # per-file rows container
        self.rows: list[FileRow] = []

        # overall progress + ETA
        self.overall = QtWidgets.QProgressBar()
        self.status = QtWidgets.QLabel("Idle.")
        v.addWidget(self.overall)
        v.addWidget(self.status)

        # connections
        self.run_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.clear_btn.clicked.connect(self.list_area.clear)

        self.thread = None
        self.worker = None

    # drag&drop handlers
    def _drag_enter(self, e): 
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def _drop(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p) and p.lower().split(".")[-1] in ("jpg","jpeg","png","webp","tif","tiff"):
                self._add_file(p)

    def _add_file(self, path: str):
        item = QtWidgets.QListWidgetItem(os.path.basename(path))
        item.setData(QtCore.Qt.UserRole, path)
        self.list_area.addItem(item)

    def _choose_out(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if d:
            self.out_dir.setText(d)

    def preview_selected(self):
        item = self.list_area.currentItem()
        if not item:
            QtWidgets.QMessageBox.information(self, "Preview", "Select one image first.")
            return
        path = item.data(QtCore.Qt.UserRole)
        try:
            img = open_image(path)
            img = resize_if_needed(img, self.size_box.currentText())
            est = estimate_output_size(img, self.format_box.currentText(), self.quality.value(), self.keep_exif.isChecked())
            self.preview_label.setText(f"Estimated: {est/1024:.1f} KB")
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Preview failed", str(e))

    def start(self):
        files = [self.list_area.item(i).data(QtCore.Qt.UserRole) for i in range(self.list_area.count())]
        if not files:
            QtWidgets.QMessageBox.warning(self, "No files", "Add images first.")
            return
        out_dir = self.out_dir.text() or os.path.dirname(files[0])
        fmt = self.format_box.currentText()
        size_key = self.size_box.currentText()
        quality = self.quality.value()
        prefix = self.prefix.text()
        suffix = self.suffix.text()
        keep_exif = self.keep_exif.isChecked()

        # prepare per-file rows under the list (replace list with row widgets)
        self._inflate_rows(files)

        self.overall.setValue(0)
        self.status.setText("Starting…")

        self.thread = QtCore.QThread(self)
        self.worker = Worker(files, out_dir, fmt, size_key, quality, prefix, suffix, keep_exif)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.file_done.connect(self.on_file_done)
        self.worker.all_done.connect(self.on_all_done)
        self.worker.error.connect(self.on_error)
        self.thread.start()

        self._start_time = time.time()
        self._count = len(files)
        self._done = 0

    def stop(self):
        if self.worker:
            self.worker.abort()
        self.status.setText("Stopping…")

    def on_progress(self, index: int, percent: int):
        if 0 <= index < len(self.rows):
            self.rows[index].bar.setValue(percent)
        # overall as average of bars
        total = sum(r.bar.value() for r in self.rows)
        overall = int(total / max(1, len(self.rows)))
        self.overall.setValue(overall)
        elapsed = time.time() - self._start_time
        # rough ETA using completed count
        completed = sum(1 for r in self.rows if r.bar.value() >= 100)
        remaining = self._count - completed
        eta = (elapsed / max(1, completed)) * remaining if completed else 0
        self.status.setText(f"Overall {overall}% • ETA ~ {eta:.1f}s")

    def on_file_done(self, index: int, bytes_out: int):
        self._done += 1
        if 0 <= index < len(self.rows):
            self.rows[index].label.setText(f"{self.rows[index].label.text()}  —  {bytes_out/1024:.1f} KB")
            self.rows[index].bar.setValue(100)

    def on_error(self, index: int, msg: str):
        if 0 <= index < len(self.rows):
            self.rows[index].label.setText(self.rows[index].label.text() + f"  —  ERROR: {msg}")
            self.rows[index].bar.setStyleSheet("QProgressBar::chunk { background:#d9534f; }")

    def on_all_done(self, seconds: float):
        self.status.setText(f"Completed in {seconds:.2f}s")
        if self.thread:
            self.thread.quit(); self.thread.wait()
            self.thread = None; self.worker = None

    def _inflate_rows(self, files: list[str]):
        # replace the simple list display with embedded rows showing per-file progress
        self.rows.clear()
        self.list_area.clear()
        for f in files:
            row = FileRow(f)
            self.rows.append(row)
            item = QtWidgets.QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self.list_area.addItem(item)
            self.list_area.setItemWidget(item, row)
