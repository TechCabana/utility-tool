from PySide6 import QtWidgets, QtCore


class HomeTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        layout = QtWidgets.QVBoxLayout(self)
        # Margins leave room for the card drop shadows to render un-clipped
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        welcome = QtWidgets.QLabel("Welcome to the Utility Tool - Your Swiss Army Knife for Files & Images")
        welcome.setStyleSheet("font-size: 20px; font-weight: 600; color: #18181b;")
        welcome.setAlignment(QtCore.Qt.AlignCenter)

        description = QtWidgets.QLabel(
            "Use the tabs above to manage files, process images, and save your presets.\n"
            "Presets can be applied to both File Tools and Image Tools."
        )
        description.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(welcome)
        layout.addWidget(description)
        layout.addStretch()

        self.setLayout(layout)
        
        # Header
        header = QtWidgets.QLabel("Preset Manager")
        header.setObjectName("H1")
        layout.addWidget(header)

        # Row: two cards side-by-side (save + list)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(12)
        layout.addLayout(row)

        # Save preset card
        save_card = QtWidgets.QFrame()
        save_card.setObjectName("Card")
        save_l = QtWidgets.QVBoxLayout(save_card)
        save_l.setSpacing(8)

        sub = QtWidgets.QLabel("Save New Preset")
        sub.setObjectName("H2")
        save_l.addWidget(sub)

        self.kind = QtWidgets.QComboBox()
        self.kind.addItems(["Image Tools", "File Tools"])
        self.name_edit = QtWidgets.QLineEdit()
        self.name_edit.setPlaceholderText("Preset name…")

        save_l.addWidget(QtWidgets.QLabel("Preset Type"))
        save_l.addWidget(self.kind)
        save_l.addWidget(QtWidgets.QLabel("Name"))
        save_l.addWidget(self.name_edit)

        btns = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save Preset")
        save_btn.setObjectName("Primary")
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.setObjectName("Secondary")
        btns.addWidget(save_btn)
        btns.addWidget(clear_btn)
        save_l.addLayout(btns)

        row.addWidget(save_card, 1)

        # Saved presets card
        list_card = QtWidgets.QFrame()
        list_card.setObjectName("Card")
        list_l = QtWidgets.QVBoxLayout(list_card)
        list_l.setSpacing(8)

        sub2 = QtWidgets.QLabel("Saved Presets")
        sub2.setObjectName("H2")
        list_l.addWidget(sub2)

        self.listw = QtWidgets.QListWidget()
        self.listw.addItems(["Example Image Preset", "Example File Preset"])
        list_l.addWidget(self.listw)

        act = QtWidgets.QHBoxLayout()
        apply_btn = QtWidgets.QPushButton("Apply")
        apply_btn.setObjectName("Primary")
        delete_btn = QtWidgets.QPushButton("Delete")
        delete_btn.setObjectName("Secondary")
        act.addWidget(apply_btn)
        act.addWidget(delete_btn)
        list_l.addLayout(act)

        row.addWidget(list_card, 1)

        # Glue
        layout.addStretch(1)

        # Wire placeholder actions
        clear_btn.clicked.connect(lambda: self.name_edit.clear())
        save_btn.clicked.connect(self._save_placeholder)
        delete_btn.clicked.connect(self._delete_placeholder)
        apply_btn.clicked.connect(self._apply_placeholder)

    # Placeholder slots — replace with real preset logic later
    def _save_placeholder(self):
        name = self.name_edit.text().strip()
        if not name:
            QtWidgets.QMessageBox.information(self, "Preset", "Enter a preset name.")
            return
        self.listw.addItem(f"{name}")
        self.name_edit.clear()
        QtWidgets.QMessageBox.information(self, "Preset", "Preset saved (placeholder).")

    def _delete_placeholder(self):
        row = self.listw.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.information(self, "Preset", "Select a preset to delete.")
            return
        self.listw.takeItem(row)

    def _apply_placeholder(self):
        item = self.listw.currentItem()
        if not item:
            QtWidgets.QMessageBox.information(self, "Preset", "Select a preset to apply.")
            return
        QtWidgets.QMessageBox.information(self, "Preset", f"Applied '{item.text()}' (placeholder).")
