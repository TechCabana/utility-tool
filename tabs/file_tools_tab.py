import os, re
from typing import List
from PySide6 import QtWidgets, QtCore
from utils.file_utils import build_new_name, apply_renames
from utils.presets import add_file_preset, load_all

class FileToolsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.presets = load_all()

        v = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("File Tools — Batch Rename")
        title.setStyleSheet("font-weight:700; font-size:16px;")
        v.addWidget(title)

        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        v.addWidget(self.listw, 1)

        hb = QtWidgets.QHBoxLayout()
        add = QtWidgets.QPushButton("Add Files")
        add.clicked.connect(self.add_files)
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.listw.clear)
        hb.addWidget(add); hb.addWidget(clear)
        v.addLayout(hb)

        dest_h = QtWidgets.QHBoxLayout()
        self.dest_edit = QtWidgets.QLineEdit()
        dest_btn = QtWidgets.QPushButton("Choose Destination Folder")
        dest_btn.clicked.connect(self.choose_dest)
        dest_h.addWidget(self.dest_edit); dest_h.addWidget(dest_btn)
        v.addLayout(dest_h)

        form = QtWidgets.QFormLayout()
        self.prefix = QtWidgets.QLineEdit()
        self.suffix = QtWidgets.QLineEdit()
        self.pattern = QtWidgets.QLineEdit("{name}")
        self.start = QtWidgets.QSpinBox(); self.start.setRange(0, 1_000_000); self.start.setValue(1)
        self.pad = QtWidgets.QSpinBox(); self.pad.setRange(1, 10); self.pad.setValue(3)
        self.regex_find = QtWidgets.QLineEdit()
        self.regex_replace = QtWidgets.QLineEdit()
        self.case = QtWidgets.QComboBox(); self.case.addItems(["none","lower","upper","title"])
        self.date_source = QtWidgets.QComboBox(); self.date_source.addItems(["now","file_modified"])
        form.addRow("Prefix (tokens allowed):", self.prefix)
        form.addRow("Suffix:", self.suffix)
        form.addRow("Pattern (use {name} {ext} {date:%Y%m%d} {num}):", self.pattern)
        form.addRow("Number start:", self.start)
        form.addRow("Number pad:", self.pad)
        form.addRow("Regex find:", self.regex_find)
        form.addRow("Regex replace:", self.regex_replace)
        form.addRow("Case:", self.case)
        form.addRow("Date source:", self.date_source)
        v.addLayout(form)

        v.addWidget(QtWidgets.QLabel("Preview:"))
        self.preview = QtWidgets.QTextEdit(); self.preview.setReadOnly(True)
        v.addWidget(self.preview, 1)

        run_h = QtWidgets.QHBoxLayout()
        self.preview_btn = QtWidgets.QPushButton("Generate Preview")
        self.apply_btn = QtWidgets.QPushButton("Apply Rename")
        self.save_preset_btn = QtWidgets.QPushButton("Save Preset")
        self.progress = QtWidgets.QProgressBar()
        run_h.addWidget(self.preview_btn); run_h.addWidget(self.apply_btn); run_h.addWidget(self.save_preset_btn); run_h.addWidget(self.progress)
        v.addLayout(run_h)

        # connections
        self.preview_btn.clicked.connect(self.update_preview)
        self.apply_btn.clicked.connect(self.apply)
        self.save_preset_btn.clicked.connect(self.save_current_preset)
        for w in (self.prefix, self.suffix, self.pattern, self.regex_find, self.regex_replace):
            if hasattr(w, "textChanged"):
                w.textChanged.connect(self.update_preview)
        self.start.valueChanged.connect(self.update_preview); self.pad.valueChanged.connect(self.update_preview)
        self.case.currentIndexChanged.connect(self.update_preview); self.date_source.currentIndexChanged.connect(self.update_preview)

    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Files")
        if files:
            self.listw.addItems(files)
            self.update_preview()

    def choose_dest(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Destination Folder")
        if d:
            self.dest_edit.setText(d)

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

    def apply(self):
        paths = [self.listw.item(i).text() for i in range(self.listw.count())]
        if not paths:
            QtWidgets.QMessageBox.information(self, "No files", "Add files first.")
            return
        lines = self._compute_preview()
        self.progress.setValue(0)
        total = len(paths)
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
                dest = self.dest_edit.text()
                os.makedirs(dest, exist_ok=True)
                final_path = os.path.join(dest, os.path.basename(new_name))
            else:
                final_path = os.path.join(os.path.dirname(p), new_name)
            try:
                os.makedirs(os.path.dirname(final_path), exist_ok=True)
                os.rename(p, final_path)
                self.listw.item(idx).setText(final_path)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Error", f"Failed to rename {p}: {e}")
                break
            self.progress.setValue(int(((idx+1)/total)*100))
        self.update_preview()
        QtWidgets.QMessageBox.information(self, "Done", "Rename operation completed.")

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
        QtWidgets.QMessageBox.information(self, "Saved", f"File preset '{name}' saved.")
