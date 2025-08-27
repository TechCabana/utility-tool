from PySide6 import QtWidgets, QtCore
from utils.presets import load_all, add_image_preset, add_file_preset, delete_preset, save_all

class HomeTab(QtWidgets.QWidget):
    """
    Simplified home tab. Main responsibility: open Preset Manager dialog.
    Users save/load presets from here. No global default folder or theme options.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.data = load_all()

        layout = QtWidgets.QVBoxLayout(self)
        title = QtWidgets.QLabel("Home — Presets")
        title.setStyleSheet("font-weight:700; font-size:16px;")
        layout.addWidget(title)

        desc = QtWidgets.QLabel("Manage presets for Image Tools and File Tools.")
        layout.addWidget(desc)

        btn_manage = QtWidgets.QPushButton("Open Presets Manager")
        btn_manage.clicked.connect(self.open_manager)
        layout.addWidget(btn_manage)

        # Quick actions: save current settings from each tab will be triggered from tabs directly
        layout.addStretch()

    def open_manager(self):
        dlg = PresetsDialog(self)
        dlg.exec()


class PresetsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Presets Manager")
        self.resize(700, 420)
        self.data = load_all()

        v = QtWidgets.QVBoxLayout(self)
        tabs = QtWidgets.QTabWidget()
        v.addWidget(tabs)

        # Image presets tab
        img_tab = QtWidgets.QWidget()
        img_layout = QtWidgets.QVBoxLayout(img_tab)
        self.img_list = QtWidgets.QListWidget()
        self.img_list.addItems([p.get("name","Unnamed") for p in self.data.get("image", [])])
        img_layout.addWidget(self.img_list)
        hb_i = QtWidgets.QHBoxLayout()
        self.img_add = QtWidgets.QPushButton("Add (from current Image tab)")
        self.img_delete = QtWidgets.QPushButton("Delete")
        hb_i.addWidget(self.img_add); hb_i.addWidget(self.img_delete)
        img_layout.addLayout(hb_i)

        # File presets tab
        file_tab = QtWidgets.QWidget()
        file_layout = QtWidgets.QVBoxLayout(file_tab)
        self.file_list = QtWidgets.QListWidget()
        self.file_list.addItems([p.get("name","Unnamed") for p in self.data.get("file", [])])
        file_layout.addWidget(self.file_list)
        hb_f = QtWidgets.QHBoxLayout()
        self.file_add = QtWidgets.QPushButton("Add (from current File tab)")
        self.file_delete = QtWidgets.QPushButton("Delete")
        hb_f.addWidget(self.file_add); hb_f.addWidget(self.file_delete)
        file_layout.addLayout(hb_f)

        tabs.addTab(img_tab, "Image Presets")
        tabs.addTab(file_tab, "File Presets")

        # connections
        self.img_delete.clicked.connect(lambda: self.delete_selected("image"))
        self.file_delete.clicked.connect(lambda: self.delete_selected("file"))

        # Note: Add buttons are placeholders; actual 'save current tab state' will be invoked from the tabs themselves.
        self.img_add.clicked.connect(self.add_placeholder_image_preset)
        self.file_add.clicked.connect(self.add_placeholder_file_preset)

        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        v.addWidget(close)

    def delete_selected(self, kind: str):
        lw = self.img_list if kind=="image" else self.file_list
        idx = lw.currentRow()
        if idx < 0:
            QtWidgets.QMessageBox.information(self, "Select", "Please choose a preset to delete.")
            return
        delete_preset(kind, idx)
        data = load_all()
        lw.clear()
        if kind=="image":
            lw.addItems([p.get("name","Unnamed") for p in data.get("image", [])])
        else:
            lw.addItems([p.get("name","Unnamed") for p in data.get("file", [])])

    def add_placeholder_image_preset(self):
        # simple interactive name add — real save should be done from ImageToolsTab -> call presets.add_image_preset
        name, ok = QtWidgets.QInputDialog.getText(self, "Add Image Preset", "Preset name:")
        if not ok or not name.strip():
            return
        # placeholder minimal preset
        p = {"name": name.strip(), "format":"JPEG", "quality":85, "size_key":"Original", "prefix":"", "suffix":"", "keep_exif":True}
        add_image_preset(p)
        self.img_list.addItem(p["name"])

    def add_placeholder_file_preset(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Add File Preset", "Preset name:")
        if not ok or not name.strip():
            return
        p = {"name": name.strip(), "pattern":"{name}", "prefix":"","suffix":"","start":1,"pad":3,"regex_find":"","regex_replace":"","case":"none","date_source":"now"}
        add_file_preset(p)
        self.file_list.addItem(p["name"])
