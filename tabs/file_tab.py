# tabs/file_tab.py
import os, time
from typing import List
from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Qt
from utils.file_utils import build_new_name, apply_renames
from utils.presets import add_file_preset, get_file_presets, load_all


class FileWorker(QtCore.QObject):
    progress = QtCore.Signal(int, int)  # idx, percent
    done = QtCore.Signal(int, str)      # idx, final_path
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(float)

    def __init__(self, src_paths: List[str], dest_base: str, pattern: str, prefix: str, suffix: str,
                 start: int, pad: int, regex_find: str, regex_replace: str, case: str, date_source: str):
        super().__init__()
        self.src_paths = src_paths
        self.dest_base = dest_base
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
        for i, p in enumerate(self.src_paths):
            if self._abort:
                break
            try:
                new_name, new_path = build_new_name(
                    p, self.pattern, self.prefix, self.suffix,
                    i, self.start, self.pad, self.date_source,
                    self.regex_find, self.regex_replace, self.case
                )
                if self.dest_base:
                    os.makedirs(self.dest_base, exist_ok=True)
                    final_path = os.path.join(self.dest_base, os.path.basename(new_name))
                else:
                    final_path = os.path.join(os.path.dirname(p), new_name)

                self.progress.emit(i, 20)
                try:
                    if os.path.exists(final_path):
                        os.replace(p, final_path)
                    else:
                        os.rename(p, final_path)
                except Exception:
                    import shutil
                    shutil.move(p, final_path)

                self.progress.emit(i, 100)
                self.done.emit(i, final_path)

            except Exception as e:
                self.error.emit(i, str(e))

        self.finished.emit(time.time() - t0)

    def abort(self):
        self._abort = True


class FileTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.presets = load_all()
        self.setAcceptDrops(True)  # entire tab accepts drops

        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(10)

        # Header
        header = QtWidgets.QLabel("File Tools")
        header.setObjectName("H1")
        v.addWidget(header)

        # File list (drop zone)
        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        # Drop-zone styling comes from the QSS QListWidget rules, same as the
        # Image Tools list - no inline sheet, which would outrank the theme.
        v.addWidget(self.listw, 1)

        # Overlay placeholder
        self.placeholder = QtWidgets.QLabel("Drag and drop your files to begin", self.listw)
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet("color: #71717a; font-size: 14px; background: transparent;")
        self.placeholder.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.placeholder.resize(self.listw.size())
        self.listw.resizeEvent = lambda e: (
            self.placeholder.resize(self.listw.size()),
            QtWidgets.QListWidget.resizeEvent(self.listw, e)
        )

        # Buttons
        hb = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add Files")
        add.clicked.connect(self.add_files)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.clear_files)
        hb.addWidget(add)
        hb.addWidget(clear)
        v.addLayout(hb)

        # Destination chooser
        dest_h = QtWidgets.QHBoxLayout()
        self.dest_edit = QtWidgets.QLineEdit()
        dest_btn = QtWidgets.QPushButton("Choose Destination Folder")
        dest_btn.clicked.connect(self.choose_dest)
        dest_h.addWidget(self.dest_edit)
        dest_h.addWidget(dest_btn)
        v.addLayout(dest_h)

        # Form
        form = QtWidgets.QFormLayout()

        # Preset: applies pattern/prefix/suffix/number/regex/case/date fields
        # in one shot.
        self.preset_combo = QtWidgets.QComboBox()
        form.addRow("Preset:", self.preset_combo)

        self.prefix = QtWidgets.QLineEdit()
        self.suffix = QtWidgets.QLineEdit()
        self.pattern = QtWidgets.QLineEdit("{name}")
        self.start = QtWidgets.QSpinBox(); self.start.setRange(0, 1_000_000); self.start.setValue(1)
        self.pad = QtWidgets.QSpinBox(); self.pad.setRange(1, 10); self.pad.setValue(3)
        self.regex_find = QtWidgets.QLineEdit()
        self.regex_replace = QtWidgets.QLineEdit()
        self.case = QtWidgets.QComboBox(); self.case.addItems(["none", "lower", "upper", "title"])
        self.date_source = QtWidgets.QComboBox(); self.date_source.addItems(["now", "file_modified"])
        form.addRow("Prefix:", self.prefix)
        form.addRow("Suffix:", self.suffix)
        form.addRow("Pattern:", self.pattern)
        form.addRow("Number start:", self.start)
        form.addRow("Number pad:", self.pad)
        form.addRow("Regex find:", self.regex_find)
        form.addRow("Regex replace:", self.regex_replace)
        form.addRow("Case:", self.case)
        form.addRow("Date source:", self.date_source)
        v.addLayout(form)

        # Preview
        v.addWidget(QtWidgets.QLabel("Preview:"))
        self.preview = QtWidgets.QTextEdit(); self.preview.setReadOnly(True)
        v.addWidget(self.preview, 1)

        # Run buttons
        run_h = QtWidgets.QHBoxLayout()
        self.preview_btn = QtWidgets.QPushButton("Generate Preview")
        self.apply_btn = QtWidgets.QPushButton("Apply Rename")
        self.save_preset_btn = QtWidgets.QPushButton("Save Preset")
        self.progress = QtWidgets.QProgressBar()
        run_h.addWidget(self.preview_btn)
        run_h.addWidget(self.apply_btn)
        run_h.addWidget(self.save_preset_btn)
        run_h.addWidget(self.progress)
        v.addLayout(run_h)

        # Signals
        self.preview_btn.clicked.connect(self.update_preview)
        self.apply_btn.clicked.connect(self.apply)
        self.save_preset_btn.clicked.connect(self.save_current_preset)
        for w in (self.prefix, self.suffix, self.pattern, self.regex_find, self.regex_replace):
            if hasattr(w, "textChanged"):
                w.textChanged.connect(self.update_preview)
        self.start.valueChanged.connect(self.update_preview)
        self.pad.valueChanged.connect(self.update_preview)
        self.case.currentIndexChanged.connect(self.update_preview)
        self.date_source.currentIndexChanged.connect(self.update_preview)

        self._reload_presets()
        self.preset_combo.currentIndexChanged.connect(self._apply_selected_preset)

        # Threading
        self.thread = None
        self.worker = None
        self._start_time = 0.0

    # ------------------------------
    # File management
    # ------------------------------
    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Files")
        if files:
            self.listw.addItems(files)
            self.placeholder.setVisible(self.listw.count() == 0)
            self.update_preview()

    def clear_files(self):
        self.listw.clear()
        self.placeholder.setVisible(True)
        self.update_preview()

    def choose_dest(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Destination Folder")
        if d:
            self.dest_edit.setText(d)

    # ------------------------------
    # Preview
    # ------------------------------
    def _compute_preview(self) -> List[str]:
        paths = [self.listw.item(i).text() for i in range(self.listw.count())]
        lines = []
        for idx, p in enumerate(paths):
            new_name, new_path = build_new_name(
                p,
                self.pattern.text(),
                self.prefix.text(),
                self.suffix.text(),
                idx,
                self.start.value(),
                self.pad.value(),
                self.date_source.currentText(),
                self.regex_find.text(),
                self.regex_replace.text(),
                self.case.currentText()
            )
            if self.dest_edit.text():
                new_path = os.path.join(self.dest_edit.text(), os.path.basename(new_name))
            lines.append(f"{os.path.basename(p)}  →  {os.path.basename(new_path)}")
        return lines

    def update_preview(self):
        lines = self._compute_preview()
        self.preview.setPlainText("\n".join(lines))

    # ------------------------------
    # Apply rename
    # ------------------------------
    def apply(self):
        paths = [self.listw.item(i).text() for i in range(self.listw.count())]
        if not paths:
            QtWidgets.QMessageBox.information(self, "No files", "Add files first.")
            return

        dest_base = self.dest_edit.text()
        self.thread = QtCore.QThread(self)
        self.worker = FileWorker(
            paths,
            dest_base,
            self.pattern.text(),
            self.prefix.text(),
            self.suffix.text(),
            self.start.value(),
            self.pad.value(),
            self.regex_find.text(),
            self.regex_replace.text(),
            self.case.currentText(),
            self.date_source.currentText()
        )
        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.on_finished)
        self.thread.started.connect(self.worker.run)
        self.thread.start()
        self._start_time = time.time()

    def on_progress(self, idx: int, percent: int):
        total = self.listw.count()
        overall = int(((idx + percent/100.0) / max(1, total)) * 100)
        self.progress.setValue(overall)

    def on_done(self, idx: int, final_path: str):
        if 0 <= idx < self.listw.count():
            self.listw.item(idx).setText(final_path)
        count = self.listw.count()
        done = sum(1 for i in range(count) if os.path.exists(self.listw.item(i).text()))
        self.progress.setValue(int((done / max(1, count)) * 100))

    def on_error(self, idx: int, msg: str):
        QtWidgets.QMessageBox.warning(self, "Rename Error", f"Error at item {idx}: {msg}")

    def on_finished(self, seconds: float):
        QtWidgets.QMessageBox.information(self, "Done", f"Finished in {seconds:.1f}s")
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
            self.worker = None
        self.update_preview()

    # ------------------------------
    # Presets
    # ------------------------------
    def save_current_preset(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Save File Preset", "Preset name:")
        if not ok or not name.strip():
            return
        preset = {
            "name": name.strip(),
            "pattern": self.pattern.text(),
            "prefix": self.prefix.text(),
            "suffix": self.suffix.text(),
            "start": self.start.value(),
            "pad": self.pad.value(),
            "regex_find": self.regex_find.text(),
            "regex_replace": self.regex_replace.text(),
            "case": self.case.currentText(),
            "date_source": self.date_source.currentText()
        }
        add_file_preset(preset)
        self._reload_presets()
        QtWidgets.QMessageBox.information(self, "Saved", f"File preset '{name}' saved.")

    def _reload_presets(self):
        """Repopulate the preset dropdown from disk (initial load, and after
        Save Preset adds a new one)."""
        self._file_presets = get_file_presets()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— Select preset —")
        for p in self._file_presets:
            self.preset_combo.addItem(p.get("name", "(unnamed)"))
        self.preset_combo.blockSignals(False)

    def _apply_selected_preset(self, index: int):
        """Apply a preset's pattern-naming fields to the form."""
        if index <= 0 or index - 1 >= len(self._file_presets):
            return
        p = self._file_presets[index - 1]
        self.pattern.setText(p.get("pattern", "{name}"))
        self.prefix.setText(p.get("prefix", ""))
        self.suffix.setText(p.get("suffix", ""))
        self.start.setValue(int(p.get("start", 1)))
        self.pad.setValue(int(p.get("pad", 3)))
        self.regex_find.setText(p.get("regex_find", ""))
        self.regex_replace.setText(p.get("regex_replace", ""))
        self.case.setCurrentText(p.get("case", "none"))
        self.date_source.setCurrentText(p.get("date_source", "now"))
        self.update_preview()
