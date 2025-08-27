from PySide6 import QtWidgets, QtCore
from presets_manager import load_presets, save_presets

class HomeTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.data = load_presets()

        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Home • Global Settings & Presets")
        title.setStyleSheet("font-weight:700; font-size:18px;")
        layout.addWidget(title)

        splitter = QtWidgets.QSplitter()
        splitter.setOrientation(QtCore.Qt.Horizontal)

        # Image presets
        self.img_list = QtWidgets.QListWidget()
        self.img_list.addItems([p["name"] for p in self.data.get("image", [])])
        img_box = QtWidgets.QGroupBox("Image Presets")
        v1 = QtWidgets.QVBoxLayout(img_box)
        v1.addWidget(self.img_list)
        hb1 = QtWidgets.QHBoxLayout()
        b_add_i = QtWidgets.QPushButton("Add")
        b_del_i = QtWidgets.QPushButton("Delete")
        b_rename_i = QtWidgets.QPushButton("Rename")
        hb1.addWidget(b_add_i); hb1.addWidget(b_del_i); hb1.addWidget(b_rename_i)
        v1.addLayout(hb1)

        # File presets
        self.file_list = QtWidgets.QListWidget()
        self.file_list.addItems([p["name"] for p in self.data.get("file", [])])
        file_box = QtWidgets.QGroupBox("File Rename Presets")
        v2 = QtWidgets.QVBoxLayout(file_box)
        v2.addWidget(self.file_list)
        hb2 = QtWidgets.QHBoxLayout()
        b_add_f = QtWidgets.QPushButton("Add")
        b_del_f = QtWidgets.QPushButton("Delete")
        b_rename_f = QtWidgets.QPushButton("Rename")
        hb2.addWidget(b_add_f); hb2.addWidget(b_del_f); hb2.addWidget(b_rename_f)
        v2.addLayout(hb2)

        splitter.addWidget(img_box)
        splitter.addWidget(file_box)
        layout.addWidget(splitter)

        # Actions
        b_add_i.clicked.connect(lambda: self.add_preset(kind="image"))
        b_del_i.clicked.connect(lambda: self.delete_preset(kind="image"))
        b_rename_i.clicked.connect(lambda: self.rename_preset(kind="image"))
        b_add_f.clicked.connect(lambda: self.add_preset(kind="file"))
        b_del_f.clicked.connect(lambda: self.delete_preset(kind="file"))
        b_rename_f.clicked.connect(lambda: self.rename_preset(kind="file"))

    def add_preset(self, kind: str):
        name, ok = QtWidgets.QInputDialog.getText(self, "New Preset", "Preset name:")
        if not ok or not name.strip():
            return
        p = {"name": name.strip()}
        # minimal defaults; users can overwrite via tools tabs when saving back
        self.data.setdefault(kind, []).append(p)
        save_presets(self.data)
        (self.img_list if kind=="image" else self.file_list).addItem(name)

    def delete_preset(self, kind: str):
        lw = self.img_list if kind=="image" else self.file_list
        row = lw.currentRow()
        if row < 0: return
        lw.takeItem(row)
        del self.data[kind][row]
        save_presets(self.data)

    def rename_preset(self, kind: str):
        lw = self.img_list if kind=="image" else self.file_list
        row = lw.currentRow()
        if row < 0: return
        old = self.data[kind][row].get("name","")
        name, ok = QtWidgets.QInputDialog.getText(self, "Rename Preset", "New name:", text=old)
        if not ok or not name.strip():
            return
        self.data[kind][row]["name"] = name.strip()
        lw.item(row).setText(name.strip())
        save_presets(self.data)
