import sys
from pathlib import Path

from PySide6 import QtWidgets, QtCore, QtGui

from tabs.home_tab import HomeTab
from tabs.image_tab import ImageTab
from tabs.file_tab import FileTab
from tabs.disk_tab import DiskTab
from tabs.settings_tab import SettingsTab

# Assets and styles are read relative to this file, not the working directory,
# so the app behaves the same whether it is started from the repo root or from
# anywhere else.
BASE_DIR = Path(__file__).resolve().parent


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
        self.setWindowTitle("UtilityTool")
        set_window_icon(self)

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
        title = QtWidgets.QLabel("UtilityTool")
        title.setObjectName("AppTitle")
        sbl.addWidget(title)

        # Nav buttons
        self.buttons = []
        for idx, name in enumerate(["Home", "Image Tools", "File Tools", "Disk", "Settings"]):
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
        self.stacked.addWidget(DiskTab())      # 3
        self.stacked.addWidget(SettingsTab())  # 4

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


def set_window_icon(window):
    """Put the Soft Rose app mark on the window (title bar and taskbar).

    Checked rather than passed straight to QIcon: a missing file makes a null
    icon with no error at all, which is how the previous reference to
    assets/icon.png sat dead in this file long enough to need its own card.
    """
    path = BASE_DIR / "assets" / "icon.png"
    if not path.is_file():
        print(f"[WARN] Could not load window icon '{path}': file not found")
        return
    window.setWindowIcon(QtGui.QIcon(str(path)))


def load_fonts(directory=None):
    """Register the bundled UI typeface so the QSS can name it.

    The app makes no network calls, so a webfont is not an option: the Geist
    .ttf files ship in assets/fonts/ and are registered here, before the
    stylesheet is applied. Without this the 'Geist' in theme.qss would fall
    back to a system face with no warning -- see DESIGN.md, Typography.
    """
    directory = Path(directory or BASE_DIR / "assets" / "fonts")
    files = sorted(directory.glob("*.ttf"))
    if not files:
        # Silence here would mean the app quietly renders in a system font.
        print(f"[WARN] No bundled fonts found in '{directory}'; the UI will use a fallback face")
        return
    for path in files:
        if QtGui.QFontDatabase.addApplicationFont(str(path)) == -1:
            print(f"[WARN] Could not load font '{path}'")


def load_stylesheet(app, path=None):
    path = path or BASE_DIR / "styles" / "theme.qss"
    try:
        with open(path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())
    except Exception as e:
        print(f"[WARN] Could not load stylesheet '{path}': {e}")


def run_backup_job_cli(job_id):
    """Run one backup job with no GUI at all, and return a process exit code.

    This is what the Windows scheduled task registered by the Backup screen
    actually invokes (`main.py --run-backup-job <id>`). It must not construct
    a QApplication or open a window: a scheduled backup runs while nobody is
    at the machine, and a window appearing at 8pm every night is not a
    feature. Everything it needs is in utils/backup_utils.py, which is
    Qt-free by design, so nothing here touches the widget layer.

    Exit codes: 0 backed up, 1 the backup failed, 2 no such job.
    """
    from utils import backup_utils

    job = backup_utils.get_job(job_id)
    if job is None:
        print(f"[ERROR] No backup job with id '{job_id}'")
        return 2
    result = backup_utils.run_job(job)
    backup_utils.record_run(job_id, result)
    print(("[OK] " if result.get("ok") else "[ERROR] ") + result.get("message", ""))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    # The headless path is resolved before any Qt object exists, so a
    # scheduled run costs no QApplication and shows no window.
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-backup-job":
        sys.exit(run_backup_job_cli(sys.argv[2]))
    if "--run-backup-job" in sys.argv:
        print("usage: main.py --run-backup-job <job id>")
        sys.exit(2)

    app = QtWidgets.QApplication(sys.argv)
    load_fonts()
    load_stylesheet(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
