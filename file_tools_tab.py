from PySide6 import QtWidgets, QtCore
from typing import List
import os, re
from file_utils import build_new_name

class FileToolsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        v = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("File Tools — Batch Rename & Management")
        title.setStyleSheet("font-weight:700; font-size:18px;")
        v.addWidget(title)

        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        v.addWidget(self.listw, 1)

        hb = QtWidgets.QHBoxLayout()
        add_btn = QtWidgets.QPushButton("Add Files")
        clear_btn = QtWidgets.QPushButton("Clear")
        hb.addWidget(add_btn); hb.addWidget(clear_btn)
        v.addLayout(hb)

        form = QtWidgets.QFormLayout()
        self.prefix = QtWidgets.QLineEdit()
        self.suffix = QtWidgets.QLineEdit()
        self.pattern = QtWidgets.QLineEdit("{name}")
        self.start = QtWidgets.QSpinBox(); self.start.setRange(0, 1_000_000); self.start.setValue(1)
        self.pad = QtWidgets.QSpinBox(); self.pad.setRange(1, 10); self.pad.setValue(3)
        self.regex_find = QtWidgets.QLineEdit()
        self.regex_replace = QtWidgets.QLineEdit()
        form.addRow("Prefix:", self.prefix)
        form.addRow("Suffix:", self.suffix)
        form.addRow("Pattern (use {name} {ext} {date:%Y-%m-%d} {num}):", self.pattern)
        form.addRow("Numbering start:", self.start)
        form.addRow("Numbering pad:", self.pad)
        form.addRow("Regex find:", self.regex_find)
        form.addRow("Regex replace:", self.regex_replace)
        v.addLayout(form)

        self.preview = QtWidgets.QTextEdit()
        self.preview.setReadOnly(True)
        v.addWidget(QtWidgets.QLabel("Preview:"))
        v.addWidget(self.preview)

        run_row = QtWidgets.QHBoxLayout()
        self.rename_btn = QtWidgets.QPushButton("Apply Rename")
        self.progress = QtWidgets.QProgressBar()
        run_row.addWidget(self.rename_btn); run_row.addWidget(self.progress, 1)
        v.addLayout(run_row)

        # events
        add_btn.clicked.connect(self.add_files)
        clear_btn.clicked.connect(self.listw.clear)
        self.rename_btn.clicked.connect(self.apply)
        self.listw.itemSelectionChanged.connect(self.update_preview)
        for w in (self.prefix, self.suffix, self.pattern, self.start, self.pad, self.regex_find, self.regex_replace):
            if hasattr(w, "textChanged"):
                w.textChanged.connect(self.update_preview)
        self.start.valueChanged.connect(self.update_preview)
        self.pad.valueChanged.connect(self.update_preview)

    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Files")
        if files:
            self.listw.addItems(files)
            self.update_preview()

    def _compute_preview_lines(self) -> List[str]:
        items = [self.listw.item(i).text() for i in range(self.listw.count())]
        lines = []
        for idx, p in enumerate(items):
            new_name, new_path = build_new_name(
                p, self.pattern.text(), self.prefix.text(), self.suffix.text(),
                idx, self.start.value(), self.pad.value()
            )
            # regex
            if self.regex_find.text():
                try:
                    new_name = re.sub(self.regex_find.text(), self.regex_replace.text(), new_name)
                    new_path = os.path.join(os.path.dirname(p), new_name)
                except re.error:
                    new_name = f"[REGEX ERROR]"
            lines.append(f"{os.path.basename(p)}  →  {new_name}")
        return lines

    def update_preview(self):
        self.preview.setPlainText("\n".join(self._compute_preview_lines()))

    def apply(self):
        count = self.listw.count()
        if count == 0: 
            QtWidgets.QMessageBox.information(self, "No files", "Add files first.")
            return
        self.progress.setValue(0)
        for i in range(count):
            p = self.listw.item(i).text()
            new_name, new_path = build_new_name(
                p, self.pattern.text(), self.prefix.text(), self.suffix.text(),
                i, self.start.value(), self.pad.value()
            )
            if self.regex_find.text():
                try:
                    new_name = re.sub(self.regex_find.text(), self.regex_replace.text(), new_name)
                    new_path = os.path.join(os.path.dirname(p), new_name)
                except re.error:
                    QtWidgets.QMessageBox.warning(self, "Regex error", "Invalid regex pattern.")
                    return
            try:
                os.rename(p, new_path)
                self.listw.item(i).setText(new_path)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Rename failed", str(e))
                break
            self.progress.setValue(int((i+1)/count*100))
        self.update_preview()
        QtWidgets.QMessageBox.information(self, "Done", "Renaming complete.")
