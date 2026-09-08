# tabs/file_tab.py
import os, time
from typing import List
from PySide6 import QtWidgets, QtCore
from PySide6.QtCore import Qt
from utils.file_utils import build_new_name, apply_renames, move_file, copy_file, delete_file
from utils.presets import add_file_preset, get_file_presets, load_all
from widgets.common import ConfirmDialog

# Conflict-policy combo text -> the internal codes utils/file_utils.py
# understands. "Ask" is not in this map -- it is resolved to one of the
# other three, once, for the whole batch, in FileTab.apply() before the
# worker is built (see that method's docstring for why).
CONFLICT_POLICY_CODES = {"Keep both": "keep_both", "Skip": "skip", "Overwrite": "overwrite"}


class FileWorker(QtCore.QObject):
    progress = QtCore.Signal(int, int)  # idx, percent
    done = QtCore.Signal(int, str)      # idx, final_path
    skipped = QtCore.Signal(int, str)   # idx, dest_path (conflict policy: skip)
    error = QtCore.Signal(int, str)
    finished = QtCore.Signal(float)

    def __init__(self, operation: str, src_paths: List[str], dest_base: str = "",
                 conflict_policy: str = "overwrite",
                 pattern: str = "{name}", prefix: str = "", suffix: str = "",
                 start: int = 1, pad: int = 3, regex_find: str = "", regex_replace: str = "",
                 case: str = "none", date_source: str = "now"):
        super().__init__()
        self.operation = operation  # "rename" | "move" | "copy" | "delete"
        self.src_paths = src_paths
        self.dest_base = dest_base
        self.conflict_policy = conflict_policy
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
                if self.operation == "rename":
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

                elif self.operation == "move":
                    self.progress.emit(i, 20)
                    status, final_path = move_file(p, self.dest_base, self.conflict_policy)
                    self.progress.emit(i, 100)
                    (self.skipped if status == "skipped" else self.done).emit(i, final_path)

                elif self.operation == "copy":
                    self.progress.emit(i, 20)
                    status, final_path = copy_file(p, self.dest_base, self.conflict_policy)
                    self.progress.emit(i, 100)
                    (self.skipped if status == "skipped" else self.done).emit(i, final_path)

                elif self.operation == "delete":
                    self.progress.emit(i, 20)
                    status, final_path = delete_file(p)
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

        # Operation picker -- Specification stage, same Add/Specification/
        # Processing flow as Image Tools (DESIGN.md). Swaps which of the
        # field groups below are shown; Rename/Move/Copy/Delete all still
        # run through the one FileWorker + progress/status UI beneath.
        op_h = QtWidgets.QHBoxLayout()
        op_h.addWidget(QtWidgets.QLabel("Operation:"))
        self.operation_combo = QtWidgets.QComboBox()
        self.operation_combo.addItems(["Rename", "Move", "Copy", "Delete"])
        self.operation_combo.currentIndexChanged.connect(self._on_operation_changed)
        op_h.addWidget(self.operation_combo, 1)
        v.addLayout(op_h)

        # Destination chooser -- required for Move/Copy, optional for
        # Rename (blank = rename in place), hidden for Delete.
        self.dest_container = QtWidgets.QWidget()
        dest_h = QtWidgets.QHBoxLayout(self.dest_container)
        dest_h.setContentsMargins(0, 0, 0, 0)
        self.dest_edit = QtWidgets.QLineEdit()
        dest_btn = QtWidgets.QPushButton("Choose Destination Folder")
        dest_btn.clicked.connect(self.choose_dest)
        dest_h.addWidget(self.dest_edit)
        dest_h.addWidget(dest_btn)
        v.addWidget(self.dest_container)

        # Move/Copy-only: conflict policy.
        self.conflict_container = QtWidgets.QWidget()
        conflict_form = QtWidgets.QFormLayout(self.conflict_container)
        conflict_form.setContentsMargins(0, 0, 0, 0)
        self.conflict_combo = QtWidgets.QComboBox()
        self.conflict_combo.addItems(["Ask", "Keep both", "Skip", "Overwrite"])
        conflict_form.addRow("If a file already exists:", self.conflict_combo)
        v.addWidget(self.conflict_container)

        # Delete-only: warning (no extra fields -- confirmation happens on Apply).
        self.delete_warning = QtWidgets.QLabel(
            "Selected files are sent to the Recycle Bin, not permanently deleted. "
            "You will be asked to confirm before this runs."
        )
        self.delete_warning.setObjectName("Hint")
        self.delete_warning.setWordWrap(True)
        v.addWidget(self.delete_warning)

        # Rename-only fields, grouped so the whole block shows/hides as one
        # unit when the operation picker changes.
        self.rename_fields = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(self.rename_fields)
        form.setContentsMargins(0, 0, 0, 0)

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
        v.addWidget(self.rename_fields)

        # Preview
        v.addWidget(QtWidgets.QLabel("Preview:"))
        self.preview = QtWidgets.QTextEdit(); self.preview.setReadOnly(True)
        v.addWidget(self.preview, 1)

        # Run buttons (Processing stage)
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
        self.dest_edit.textChanged.connect(self.update_preview)

        self._reload_presets()
        self.preset_combo.currentIndexChanged.connect(self._apply_selected_preset)

        # Threading
        self.thread = None
        self.worker = None
        self._start_time = 0.0
        self._current_op = "rename"  # set again in apply(); used by on_done/on_skipped

        self._on_operation_changed()  # apply initial (Rename) field visibility

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
    # Operation picker
    # ------------------------------
    def _on_operation_changed(self):
        """Swap the Specification-stage fields for the selected operation.
        Rename/Move/Copy/Delete all keep sharing the one Add + Processing
        UI below (list, preview, progress) -- only these field groups and
        the Apply button's label/style change."""
        op = self.operation_combo.currentText()
        self.rename_fields.setVisible(op == "Rename")
        self.conflict_container.setVisible(op in ("Move", "Copy"))
        self.delete_warning.setVisible(op == "Delete")
        self.dest_container.setVisible(op != "Delete")

        self.apply_btn.setText({
            "Rename": "Apply Rename",
            "Move": "Apply Move",
            "Copy": "Apply Copy",
            "Delete": "Delete Files",
        }[op])
        # The one #Danger button hook (widgets/common.py's ConfirmDialog
        # confirm button reuses it too) so Delete reads distinctly from the
        # app's rose interaction accent; every other operation stays plain.
        self.apply_btn.setObjectName("Danger" if op == "Delete" else "")
        self.apply_btn.style().unpolish(self.apply_btn)
        self.apply_btn.style().polish(self.apply_btn)

        self.save_preset_btn.setVisible(op == "Rename")
        self.update_preview()

    # ------------------------------
    # Preview
    # ------------------------------
    def _compute_preview(self) -> List[str]:
        paths = [self.listw.item(i).text() for i in range(self.listw.count())]
        op = self.operation_combo.currentText()

        if op == "Rename":
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

        if op in ("Move", "Copy"):
            dest = self.dest_edit.text()
            lines = []
            for p in paths:
                base = os.path.basename(p)
                if not dest:
                    lines.append(f"{base}  →  (choose a destination folder)")
                    continue
                dest_path = os.path.join(dest, base)
                conflict = " — already exists, see conflict policy" if os.path.exists(dest_path) else ""
                lines.append(f"{base}  →  {dest_path}{conflict}")
            return lines

        # Delete
        return [f"{os.path.basename(p)}  →  Recycle Bin" for p in paths]

    def update_preview(self):
        lines = self._compute_preview()
        self.preview.setPlainText("\n".join(lines))

    # ------------------------------
    # Apply (Rename / Move / Copy / Delete)
    # ------------------------------
    def apply(self):
        paths = [self.listw.item(i).text() for i in range(self.listw.count())]
        if not paths:
            QtWidgets.QMessageBox.information(self, "No files", "Add files first.")
            return

        op = self.operation_combo.currentText()
        dest_base = self.dest_edit.text()

        if op in ("Move", "Copy") and not dest_base:
            QtWidgets.QMessageBox.information(self, "No destination", "Choose a destination folder first.")
            return

        conflict_policy = "overwrite"
        if op in ("Move", "Copy"):
            choice = self.conflict_combo.currentText()
            if choice == "Ask":
                # Prompting per-conflicting-file would mean popping a modal
                # from inside the QThread worker mid-batch, which is not
                # thread-safe. Instead: pre-scan for conflicts here on the
                # UI thread and, if any exist, ask once for how to handle
                # every conflict in this batch -- then the worker runs with
                # a single resolved policy like any other choice. Judgement
                # call, noted in the PR/report.
                conflicts = [
                    p for p in paths
                    if os.path.exists(os.path.join(dest_base, os.path.basename(p)))
                    and os.path.abspath(os.path.join(dest_base, os.path.basename(p))) != os.path.abspath(p)
                ]
                if conflicts:
                    resolved, ok = QtWidgets.QInputDialog.getItem(
                        self, "Files already exist",
                        f"{len(conflicts)} of {len(paths)} file(s) already exist in the destination. "
                        "Choose how to handle every conflict in this batch:",
                        ["Keep both", "Skip", "Overwrite"], 0, False
                    )
                    if not ok:
                        return
                    conflict_policy = CONFLICT_POLICY_CODES[resolved]
                # else: nothing conflicts, "overwrite" is a no-op default
            else:
                conflict_policy = CONFLICT_POLICY_CODES[choice]

        if op == "Delete":
            if not ConfirmDialog.ask(
                self, "Delete files?",
                f"This sends {len(paths)} file(s) to the Recycle Bin. "
                "You can restore them from there -- this is not permanent.",
                confirm_text="Delete",
            ):
                return

        self._current_op = op.lower()
        self.thread = QtCore.QThread(self)
        if op == "Rename":
            self.worker = FileWorker(
                "rename", paths, dest_base,
                pattern=self.pattern.text(), prefix=self.prefix.text(), suffix=self.suffix.text(),
                start=self.start.value(), pad=self.pad.value(),
                regex_find=self.regex_find.text(), regex_replace=self.regex_replace.text(),
                case=self.case.currentText(), date_source=self.date_source.currentText(),
            )
        elif op == "Delete":
            self.worker = FileWorker("delete", paths)
        else:  # Move / Copy
            self.worker = FileWorker(op.lower(), paths, dest_base, conflict_policy=conflict_policy)

        self.worker.moveToThread(self.thread)
        self.worker.progress.connect(self.on_progress)
        self.worker.done.connect(self.on_done)
        self.worker.skipped.connect(self.on_skipped)
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
        if not (0 <= idx < self.listw.count()):
            return
        orig = self.listw.item(idx).text()
        if self._current_op in ("rename", "move"):
            self.listw.item(idx).setText(final_path)
        elif self._current_op == "copy":
            self.listw.item(idx).setText(f"{orig}  →  copied to {final_path}")
        elif self._current_op == "delete":
            self.listw.item(idx).setText(f"{orig}  —  sent to Recycle Bin")

    def on_skipped(self, idx: int, dest_path: str):
        if not (0 <= idx < self.listw.count()):
            return
        orig = self.listw.item(idx).text()
        self.listw.item(idx).setText(f"{orig}  —  skipped (already exists at destination)")

    def on_error(self, idx: int, msg: str):
        QtWidgets.QMessageBox.warning(self, "Operation Error", f"Error at item {idx}: {msg}")

    def on_finished(self, seconds: float):
        QtWidgets.QMessageBox.information(self, "Done", f"Finished in {seconds:.1f}s")
        if self.thread:
            self.thread.quit()
            self.thread.wait()
            self.thread = None
            self.worker = None
        # Only Rename leaves item text as a real path afterward (Move too,
        # but its preview would then just show "no change" either way) --
        # Copy/Delete overwrite item text with a status sentence in on_done,
        # so recomputing a path-based preview from that text would be
        # nonsense. Leave the last preview showing for those.
        if self._current_op == "rename":
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
