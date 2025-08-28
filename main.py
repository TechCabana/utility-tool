import sys
from PySide6 import QtWidgets, QtCore, QtGui

from tabs.home_tab import HomeTab
from tabs.image_tab import ImageTab
from tabs.file_tab import FileTab
from tabs.settings_tab import SettingsTab


class SidebarButton(QtWidgets.QPushButton):
    def __init__(self, text: str, index: int, parent=None):
        super().__init__(text, parent)
        self.index = index
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumHeight(42)
        # Base shape; colors come from QSS
        self.setStyleSheet("border-radius: 10px; padding: 10px; text-align: left;")


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Utility Tool - Swiss Army Edition")
        self.setWindowIcon(QtGui.QIcon("assets/icon.png"))

        # Adjustable window size
        self.resize(1100, 700)
        self.setMinimumSize(900, 600)

        # Tab widget
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setTabPosition(QtWidgets.QTabWidget.North)
        self.tabs.setMovable(False)

        # Add tabs
        self.tabs.addTab(HomeTab(), "Home")
        self.tabs.addTab(FileTab(), "File Tools")
        self.tabs.addTab(ImageTab(), "Image Tools")

        self.setCentralWidget(self.tabs)

        # Central container
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # Sidebar
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(220)
        sbl = QtWidgets.QVBoxLayout(sidebar)
        sbl.setContentsMargins(12, 12, 12, 12)
        sbl.setSpacing(8)

        # App title (styled by QSS)
        title = QtWidgets.QLabel("Utility Tool")
        title.setObjectName("AppTitle")
        sbl.addWidget(title)

        # Nav buttons
        self.buttons = []
        for idx, name in enumerate(["Home", "Image Tools", "File Tools", "Settings"]):
            btn = SidebarButton(name, idx)
            btn.clicked.connect(self.change_page)
            self.buttons.append(btn)
            sbl.addWidget(btn)

        sbl.addStretch(1)

        # Stacked pages
        self.stacked = QtWidgets.QStackedWidget()
        self.stacked.addWidget(HomeTab())      # 0
        self.stacked.addWidget(ImageTab())     # 1
        self.stacked.addWidget(FileTab())      # 2
        self.stacked.addWidget(SettingsTab())  # 3

        root.addWidget(sidebar)
        root.addWidget(self.stacked, 1)

        # Default selection
        self.buttons[0].setChecked(True)
        self.stacked.setCurrentIndex(0)

    def change_page(self):
        btn = self.sender()
        if not isinstance(btn, SidebarButton):
            return
        for b in self.buttons:
            b.setChecked(False)
        btn.setChecked(True)
        self.stacked.setCurrentIndex(btn.index)


def load_stylesheet(app, path="styles/dark.qss"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except Exception as e:
        print(f"[WARN] Could not load stylesheet '{path}': {e}")


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    load_stylesheet(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
