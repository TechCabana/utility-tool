import sys
from pathlib import Path

from PySide6 import QtWidgets, QtCore, QtGui

from styles import tokens
from tabs.home_tab import HomeTab
from tabs.image_tab import ImageTab
from tabs.file_tab import FileTab
from tabs.disk_tab import DiskTab
from tabs.settings_tab import SettingsTab
from widgets import icons
from widgets.motion import fade_in, motion_enabled

__version__ = "1.0.0"

# Assets and styles are read relative to this file, not the working directory,
# so the app behaves the same whether it is started from the repo root or from
# anywhere else.
BASE_DIR = Path(__file__).resolve().parent

ORG_NAME = "TechCabana"
APP_NAME = "UtilityTool"

# One row per sidebar entry: label, icon, and the stacked-widget index. The
# tuple is the single definition of navigation -- the buttons, the Ctrl+N
# shortcuts and Home's entry cards all read from it, so they cannot drift
# apart the way a hand-numbered second list does.
NAV = (
    ("Home", "house", 0),
    ("Image Tools", "image", 1),
    ("File Tools", "files", 2),
    ("Disk", "hard-drive", 3),
    ("Settings", "settings", 4),
)


class SidebarButton(QtWidgets.QPushButton):
    """A nav entry: icon plus label, checked when its page is showing."""

    def __init__(self, text: str, icon_name: str, index: int, parent=None):
        super().__init__(text, parent)
        self.index = index
        self.icon_name = icon_name
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumHeight(38)
        self.setIconSize(QtCore.QSize(18, 18))
        # Shape and colour both come from the '#Sidebar QPushButton' rules in
        # the sheet. No inline stylesheet here: a widget-level one outranks
        # the app-level sheet and would silently pin values against the theme.

    def apply_theme(self, palette: dict):
        """Re-stroke the icon for the active theme.

        An icon is a coloured asset, not a font glyph, so it does not follow
        a stylesheet colour change on its own -- without this the nav keeps
        the previous theme's icons after a switch.
        """
        colour = palette["accent_text"] if self.isChecked() else palette["text_muted"]
        self.setIcon(icons.icon(self.icon_name, colour, 18))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: QtCore.QSettings):
        super().__init__()
        self.settings = settings
        self.setWindowTitle(APP_NAME)
        set_window_icon(self)
        self.setMinimumSize(940, 620)

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(tokens.SPACE_BLOCK, tokens.SPACE_BLOCK,
                                tokens.SPACE_BLOCK, tokens.SPACE_BLOCK)
        root.setSpacing(tokens.SPACE_BLOCK)

        root.addWidget(self._build_sidebar())

        self.stacked = QtWidgets.QStackedWidget()
        self.home_tab = HomeTab()
        self.settings_tab = SettingsTab()
        for page in (self.home_tab, ImageTab(), FileTab(), DiskTab(), self.settings_tab):
            self.stacked.addWidget(page)
        root.addWidget(self.stacked, 1)

        # Home's entry cards switch pages through the same path the sidebar
        # buttons use, so the two can never disagree about what is selected.
        self.home_tab.switch_requested.connect(self.go_to_tab)
        self.settings_tab.theme_changed.connect(self._theme_chosen)

        for label, _icon, index in NAV:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(f"Ctrl+{index + 1}"), self)
            shortcut.activated.connect(lambda i=index: self.go_to_tab(i))

        restore_window_state(self, settings)
        start_index = int(settings.value("ui/last_tab", 0))
        self.go_to_tab(start_index if 0 <= start_index < len(NAV) else 0, animate=False)

        apply_card_shadows(central, active_palette())

    # -- construction ----------------------------------------------------
    def _build_sidebar(self) -> QtWidgets.QFrame:
        sidebar = QtWidgets.QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(208)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(0)

        title = QtWidgets.QLabel(APP_NAME)
        title.setObjectName("AppTitle")
        layout.addWidget(title)

        version = QtWidgets.QLabel(f"v{__version__}")
        version.setObjectName("AppVersion")
        layout.addWidget(version)

        # A button group so the five entries are announced as one selection
        # rather than five unrelated checkable buttons.
        self.nav_group = QtWidgets.QButtonGroup(self)
        self.nav_group.setExclusive(True)

        self.buttons = []
        for label, icon_name, index in NAV:
            button = SidebarButton(label, icon_name, index)
            button.setToolTip(f"{label}  (Ctrl+{index + 1})")
            button.clicked.connect(self.change_page)
            self.nav_group.addButton(button, index)
            self.buttons.append(button)
            layout.addWidget(button)

        layout.addStretch(1)

        footer = QtWidgets.QHBoxLayout()
        footer.setContentsMargins(2, 0, 2, 0)
        offline = QtWidgets.QLabel("Works offline")
        offline.setObjectName("SidebarFooter")
        offline.setToolTip("This app makes no network calls. Nothing you open leaves the machine.")
        footer.addWidget(offline)
        footer.addStretch(1)
        layout.addLayout(footer)

        return sidebar

    # -- navigation ------------------------------------------------------
    def change_page(self):
        button = self.sender()
        if isinstance(button, SidebarButton):
            self.go_to_tab(button.index)

    def go_to_tab(self, index: int, animate: bool = True):
        if index == self.stacked.currentIndex() and self.buttons[index].isChecked():
            return
        self.buttons[index].setChecked(True)
        self.stacked.setCurrentIndex(index)
        self.settings.setValue("ui/last_tab", index)
        self.refresh_icons()
        if animate:
            fade_in(self.stacked.currentWidget())

    def refresh_icons(self):
        palette = active_palette()
        for button in self.buttons:
            button.apply_theme(palette)

    # -- theme -----------------------------------------------------------
    def _theme_chosen(self, choice: str):
        self.settings.setValue("ui/theme", choice)
        apply_theme(QtWidgets.QApplication.instance(), choice)
        self.refresh_icons()
        # Sidebar icons re-stroke through refresh_icons() above; every other
        # icon-carrying widget (row actions, empty states, entry cards) was
        # registered through widgets.icons.set_icon()/set_pixmap() when it
        # was built, so this is what makes them track a *runtime* switch
        # instead of only the colour a fresh construction would pick up.
        icons.refresh_theme()
        apply_card_shadows(self.centralWidget(), active_palette())

    # -- lifecycle -------------------------------------------------------
    def closeEvent(self, event):
        # Window state is remembered because reopening a tool to the size and
        # screen you left it on is the difference between an app and a script.
        self.settings.setValue("ui/geometry", self.saveGeometry())
        super().closeEvent(event)


# Panels/cards named in the sheet that should read as elevated surfaces.
SHADOWED_FRAMES = ("Card", "Sidebar", "EntryCard", "ActionBar")

_ACTIVE_THEME = "light"


def active_palette() -> dict:
    return tokens.PALETTES[_ACTIVE_THEME]


def resolve_theme(choice: str, app: QtWidgets.QApplication) -> str:
    """Turn a stored preference into a concrete theme name.

    'system' follows the OS, which is what an app that is not a browser
    should do by default; 'light'/'dark' pin it.
    """
    if choice in ("light", "dark"):
        return choice
    scheme = app.styleHints().colorScheme()
    return "dark" if scheme == QtCore.Qt.ColorScheme.Dark else "light"


def apply_theme(app: QtWidgets.QApplication, choice: str = "system"):
    """Build the stylesheet for `choice` and apply it application-wide."""
    global _ACTIVE_THEME
    _ACTIVE_THEME = resolve_theme(choice, app)
    palette = tokens.PALETTES[_ACTIVE_THEME]
    template_path = BASE_DIR / "styles" / "theme.qss.tmpl"
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"[WARN] Could not read stylesheet template '{template_path}': {error}")
        return
    app.setStyleSheet(tokens.build_stylesheet(template, _ACTIVE_THEME,
                                              extra=icons.stylesheet_icons(palette)))


def apply_card_shadows(root, palette: dict):
    """Give every card/panel frame under `root` the theme's elevation.

    QSS has no `box-shadow`, so the elevation cue has to be a
    QGraphicsDropShadowEffect per widget. Walking the tree from here keeps
    that out of the tab modules: a new card only needs the right objectName
    to pick the shadow up, exactly as it already does for its surface colour.
    """
    red, green, blue = palette["shadow_rgb"]
    for frame in root.findChildren(QtWidgets.QFrame):
        if frame.objectName() not in SHADOWED_FRAMES:
            continue
        # One effect instance per widget - a QGraphicsEffect cannot be shared.
        shadow = QtWidgets.QGraphicsDropShadowEffect(frame)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 2)
        shadow.setColor(QtGui.QColor(red, green, blue, palette["shadow_alpha"]))
        frame.setGraphicsEffect(shadow)


def set_window_icon(window):
    """Put the app mark on the window (title bar and taskbar).

    Checked rather than passed straight to QIcon: a missing file makes a null
    icon with no error at all, which is how a dead reference to this path
    once sat in this file long enough to need its own card.
    """
    path = BASE_DIR / "assets" / "icon.png"
    if not path.is_file():
        print(f"[WARN] Could not load window icon '{path}': file not found")
        return
    window.setWindowIcon(QtGui.QIcon(str(path)))


def claim_taskbar_identity():
    """Make Windows show the app's own icon on the taskbar button.

    Without an explicit AppUserModelID, Windows groups the window under the
    interpreter and shows python.exe's icon there however many QIcons the
    app sets -- the taskbar reads the process identity, not the window.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{ORG_NAME}.{APP_NAME}.{__version__}")
    except Exception as error:  # pragma: no cover - cosmetic only
        print(f"[WARN] Could not set the taskbar identity: {error}")


def restore_window_state(window, settings: QtCore.QSettings):
    geometry = settings.value("ui/geometry")
    if isinstance(geometry, (QtCore.QByteArray, bytes)) and window.restoreGeometry(geometry):
        return
    window.resize(1180, 760)


def load_fonts(directory=None):
    """Register the bundled UI and data typefaces so the sheet can name them.

    The app makes no network calls, so a webfont is not an option: the .ttf
    files ship in assets/fonts/ and are registered here, before the
    stylesheet is applied. Without this the families named in the template
    fall back to a system face with no warning -- see DESIGN.md, Typography.
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


def main() -> int:
    claim_taskbar_identity()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)

    settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    load_fonts()
    apply_theme(app, str(settings.value("ui/theme", "system")))

    window = MainWindow(settings)
    window.show()
    if motion_enabled():
        fade_in(window.centralWidget())
    return app.exec()


if __name__ == "__main__":
    # The headless path is resolved before any Qt object exists, so a
    # scheduled run costs no QApplication and shows no window.
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-backup-job":
        sys.exit(run_backup_job_cli(sys.argv[2]))
    if "--run-backup-job" in sys.argv:
        print("usage: main.py --run-backup-job <job id>")
        sys.exit(2)
    sys.exit(main())
