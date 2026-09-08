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
        # Shape and colors both come from the '#Sidebar QPushButton' rules in
        # the QSS. No inline stylesheet here: a widget-level one outranks the
        # app-level sheet and would silently pin the radius against the theme.


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
        self.home_tab = HomeTab()
        self.stacked.addWidget(self.home_tab)  # 0
        self.stacked.addWidget(ImageTab())     # 1
        self.stacked.addWidget(FileTab())      # 2
        self.stacked.addWidget(SettingsTab())  # 3

        # Home's entry cards switch tabs through the same path the sidebar
        # buttons use, so both stay in sync.
        self.home_tab.switch_requested.connect(self.go_to_tab)

        root.addWidget(sidebar)
        root.addWidget(self.stacked, 1)

        # Default selection
        self.buttons[0].setChecked(True)
        self.stacked.setCurrentIndex(0)

        # Soft elevation on every panel/card, once the whole tree exists
        apply_card_shadows(central)

    def change_page(self):
        btn = self.sender()
        if not isinstance(btn, SidebarButton):
            return
        self.go_to_tab(btn.index)

    def go_to_tab(self, index):
        for b in self.buttons:
            b.setChecked(b.index == index)
        self.stacked.setCurrentIndex(index)


# Panels/cards named in the QSS that should read as elevated surfaces.
SHADOWED_FRAMES = ("Card", "Sidebar")


def apply_card_shadows(root):
    """Give every card/panel frame under `root` the Soft Rose card shadow.

    QSS has no `box-shadow`, so the theme's elevation cue has to be a
    QGraphicsDropShadowEffect set on each widget. Walking the tree once from
    here keeps that out of the tab modules: a new card only needs
    setObjectName("Card") to pick the shadow up, exactly as it already does
    to pick up the QSS surface styling.
    """
    for frame in root.findChildren(QtWidgets.QFrame):
        if frame.objectName() not in SHADOWED_FRAMES:
            continue
        # One effect instance per widget - a QGraphicsEffect cannot be shared.
        shadow = QtWidgets.QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 2)
        shadow.setColor(QtGui.QColor(24, 24, 27, 28))  # rgba(24,24,27,.11)
        frame.setGraphicsEffect(shadow)


def load_stylesheet(app, path="styles/theme.qss"):
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
