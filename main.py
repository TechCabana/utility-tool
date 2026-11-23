import sys
from datetime import datetime
from pathlib import Path

from PySide6 import QtWidgets, QtCore, QtGui

from styles import tokens
from tabs.backup_tab import BackupTab
from tabs.home_tab import HomeTab
from tabs.settings_tab import SettingsTab
from utils import migrate
from utils.fs_utils import APP_NAME, LEGACY_APP_NAME
from widgets import icons
from widgets.motion import fade_in, motion_enabled

__version__ = "1.0.0"

# Assets and styles are read relative to this file, not the working directory,
# so the app behaves the same whether it is started from the repo root or from
# anywhere else.
BASE_DIR = Path(__file__).resolve().parent

ORG_NAME = "TechCabana"

# One row per sidebar entry: label, icon, and the stacked-widget index. The
# tuple is the single definition of navigation -- the buttons, the Ctrl+N
# shortcuts and Home's entry cards all read from it, so they cannot drift
# apart the way a hand-numbered second list does.
NAV = (
    ("Home", "house", 0),
    ("Backup", "hard-drive", 1),
    ("Settings", "settings", 2),
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
        self.settings_tab = SettingsTab(self.settings)
        for page in (self.home_tab, BackupTab(), self.settings_tab):
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

        # A button group so the entries are announced as one selection
        # rather than unrelated checkable buttons.
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

    Exit codes: 0 backed up, 1 the backup failed, 2 no such job, 3 the jobs
    file is damaged (nothing was run or written), 4 skipped because another
    run of the same job is already in progress (nothing was run or written,
    and the job's last result is left as it was); 5 is `--precheck-job`'s
    "problems found" (see `precheck_job_cli`). If the run finished but its
    result could not be written back (the jobs-file lock timed out), the exit
    code is still the run's own, 0 or 1, and the message says so.
    """
    from utils import backup_utils, runlog

    def log_failure(cause, message, job=None):
        """Leave a line in the app-level run log for a headless failure; the
        log can never change the exit code (it swallows its own errors)."""
        now = datetime.now()
        runlog.append_run(job, {"ok": False, "cause": cause, "message": message},
                          "scheduled", now, now, app_level=True)

    # A task registered before the rename can fire before the renamed app has
    # ever been opened, so the jobs file it needs may still be in the old
    # app-data directory. Copy it across first. Only the data: re-registering
    # tasks is the GUI's job, and an unattended run must not touch the
    # scheduler.
    migrate.migrate_data()
    try:
        job = backup_utils.get_job(job_id)
    except backup_utils.JobsFileCorrupt as exc:
        hint = f" Open {APP_NAME} to restore it." if exc.has_backup else ""
        print(f"[ERROR] {exc}{hint}", file=sys.stderr)
        log_failure("jobs_file_damaged", f"{exc}{hint}")
        return 3
    if job is None:
        print(f"[ERROR] No backup job with id '{job_id}'")
        log_failure("unknown_job", f"No backup job with id '{job_id}'")
        return 2
    # Card 104: a job that was never pre-checked (made by an older version,
    # or scheduled before the check existed) gets one before its first
    # unattended run. It never blocks the run -- the backup's own failure
    # handling stays authoritative -- and it is recorded only once the run
    # was not refused as busy, which must leave the jobs file untouched. It
    # runs through `after_lock`, i.e. once the job's run lock is held, so a run
    # refused as busy never walks the source first.
    checked = {}

    def check_first_run():
        """Pre-check this job, now that the run lock is ours."""
        checked["report"] = _first_run_precheck(job)

    result = backup_utils.run_job(
        job, trigger="scheduled",
        after_lock=None if "last_precheck" in job else check_first_run)
    first_check = checked.get("report")
    if result.get("busy"):
        # Not a result of this job: recording it would overwrite the real last
        # run's outcome with "failed: already running".
        print(f"[SKIPPED] {result.get('message', '')}", file=sys.stderr)
        return 4
    if first_check is not None:
        _record_precheck(job_id, first_check)
    try:
        backup_utils.record_run(job_id, result)
    except backup_utils.JobsFileCorrupt as exc:
        print(f"[ERROR] The backup ran ({result.get('message', '')}) but its "
              f"result could not be recorded: {exc}", file=sys.stderr)
        log_failure("jobs_file_damaged", f"Result could not be recorded: {exc}", job)
        return 3
    except OSError as exc:
        # The jobs-file lock timed out or the file could not be written. The
        # run itself happened, so say how it went and exit on that; only the
        # job's stored last result is stale.
        print(("[OK] " if result.get("ok") else "[ERROR] ") + result.get("message", "")
              + f" This result could not be recorded: {exc}", file=sys.stderr)
        log_failure("jobs_file_unavailable", f"Result could not be recorded: {exc}", job)
        return 0 if result.get("ok") else 1
    print(("[OK] " if result.get("ok") else "[ERROR] ") + result.get("message", ""))
    if result.get("ok") and result.get("not_copied"):
        # Silence is never success, but the exit code stays the run's own.
        print(f"[WARN] {result['not_copied']} file(s) were not copied", file=sys.stderr)
    if result.get("ok") and result.get("pruning_held"):
        # The source lost a large share of its files: pruning was held. Same
        # contract as a skip -- loud on stderr, exit code unchanged.
        print("[WARN] " + backup_utils.pruning_held_sentence(
            result["files"], result["baseline_files"]), file=sys.stderr)
    return 0 if result.get("ok") else 1


def _safe_console():
    """Let a path the console code page cannot show print as an escape
    instead of crashing the headless run with UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass  # not a real text stream (a test capture): nothing to fix


def _record_precheck(job_id, report):
    """Store `report`'s summary as the job's `last_precheck`; a failure to
    store it is printed and never changes an exit code. Returns True if stored."""
    from utils import backup_utils, precheck
    try:
        backup_utils.record_precheck(job_id, precheck.summary(report))
        return True
    except (backup_utils.JobsFileCorrupt, OSError) as exc:
        print(f"[WARN] The pre-check result could not be recorded: {exc}", file=sys.stderr)
        return False


def _print_precheck(report, stream):
    """One line per problem: kind, path, what is wrong and the fix."""
    from utils import precheck
    for problem in report["problems"]:
        when = "" if problem["blocking"] else " (will be skipped, not a failure)"
        print(f"  {precheck.KIND_LABELS.get(problem['kind'], problem['kind'])}"
              f"{when}: {problem['path']}\n    {problem['detail']} "
              f"Fix: {problem['suggestion']}", file=stream)
    if report["problems_more"]:
        print(f"  ... and {report['problems_more']} more", file=stream)


def _first_run_precheck(job):
    """Pre-check a job before its first unattended run (card 104); return the
    report for the caller to record, or None if the check could not run.

    Prints any problems as warnings on stderr. Never raises and never stops
    the run that follows. The per-file open is skipped (`check_open=False`):
    this runs while the job's lock is held, so a slow walk (Defender scans
    each open, 72 s for 10,142 files on a first pass) would hold the lock and
    delay the first backup for no gain -- a file that cannot be opened is
    skipped, counted and named by the run itself, and nobody is watching an
    unattended run's warnings to act on them first. `--precheck-job` keeps it.
    """
    from utils import precheck
    _safe_console()
    try:
        report = precheck.precheck(job, check_open=False)
    except Exception as exc:  # the backup's own handling stays authoritative
        print(f"[WARN] The pre-check could not run: {exc}", file=sys.stderr)
        return None
    if report["problems"]:
        print(f"[WARN] The pre-check found {sum(report['counts'].values())} "
              "problem(s) this backup may run into:", file=sys.stderr)
        _print_precheck(report, sys.stderr)
    return report


def precheck_job_cli(job_id):
    """Pre-check one job with no GUI (`main.py --precheck-job <id>`), print
    the report and return a process exit code.

    Lists every file the job's target cannot hold (path too long, too large
    for the drive's filesystem, a name it cannot store, names differing only
    by case) and every file that will be skipped (locked or unreadable), with
    a fix for each. Read-only: nothing is written to the source or the target;
    the result is stored on the job as `last_precheck`.

    Exit codes, shared with `--run-backup-job` (1 and 4 are run-only):
    0 nothing to fix, 5 problems found (ANY problem, including files that
    would only be skipped -- the report's `ok` field is the narrower "nothing
    would fail the run"), 2 no such job, 3 the jobs file is damaged (nothing
    was checked or written). A result that could not be recorded keeps its
    0/5 and prints a warning.
    """
    from utils import backup_utils, precheck
    _safe_console()
    migrate.migrate_data()  # as --run-backup-job: the jobs file may be pre-rename
    try:
        job = backup_utils.get_job(job_id)
    except backup_utils.JobsFileCorrupt as exc:
        hint = f" Open {APP_NAME} to restore it." if exc.has_backup else ""
        print(f"[ERROR] {exc}{hint}", file=sys.stderr)
        return 3
    if job is None:
        print(f"[ERROR] No backup job with id '{job_id}'")
        return 2
    report = precheck.precheck(job)
    _record_precheck(job_id, report)
    total = sum(report["counts"].values())
    if not total:
        print(f"[OK] Checked {report['scanned']} file(s) in {report['took']:.1f} s: "
              "the backup drive can hold all of them.")
        return 0
    print(f"[PROBLEMS] Checked {report['scanned']} file(s): {total} problem(s), "
          f"{report['blocking']} of which would fail the backup.")
    _print_precheck(report, sys.stdout)
    return 5


def release_hold_cli(job_id):
    """Tell a job its pruning hold is expected (`main.py --release-hold <job id>`),
    print what happened and return a process exit code. No GUI.

    The scripted twin of the Backup screen's "This is expected: forget the
    larger backup" (card QnFM2GMZ). It never deletes anything: the earlier
    backup goes in a later run's normal clean-up.

    Exit codes: 0 released, or nothing to release (a no-op is not a failure),
    2 no such job, 3 the jobs file is damaged (nothing was written), 4 the
    job is running right now (nothing was written; try again afterwards), 1
    the acknowledgement could not be saved (the jobs-file lock timed out or
    the write failed).
    """
    from utils import backup_utils
    _safe_console()
    migrate.migrate_data()  # as --run-backup-job: the jobs file may be pre-rename
    try:
        result = backup_utils.release_hold(job_id, trigger="cli")
    except backup_utils.JobsFileCorrupt as exc:
        hint = f" Open {APP_NAME} to restore it." if exc.has_backup else ""
        print(f"[ERROR] {exc}{hint}", file=sys.stderr)
        return 3
    except OSError as exc:
        print(f"[ERROR] The hold could not be released: {exc}", file=sys.stderr)
        return 1
    if result.get("busy"):
        print(f"[SKIPPED] {result['message']}", file=sys.stderr)
        return 4
    if result.get("cause") == "unknown_job":
        print(f"[ERROR] No backup job with id '{job_id}'")
        return 2
    if not result["ok"]:
        print(f"[ERROR] {result['message']}", file=sys.stderr)
        return 1
    print("[OK] " + result["message"])
    return 0


# Usage for the headless entry points, printed for a malformed command line.
HEADLESS_USAGE = (
    "usage: main.py --run-backup-job <job id>   (exit 0 ok, 1 failed, 2 no such job,\n"
    "                                           3 jobs file damaged, 4 already running)\n"
    "       main.py --precheck-job <job id>     (exit 0 nothing to fix, 5 problems found,\n"
    "                                           2 no such job, 3 jobs file damaged)\n"
    "       main.py --release-hold <job id>     (exit 0 released or nothing to release,\n"
    "                                           1 could not save, 2 no such job,\n"
    "                                           3 jobs file damaged, 4 job is running)")


def main() -> int:
    claim_taskbar_identity()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(__version__)

    settings = QtCore.QSettings(ORG_NAME, APP_NAME)
    # Before any window is built: the settings carry the theme and last tab,
    # and the Backup screen reads the jobs file as it is constructed.
    migrate.migrate_settings(QtCore.QSettings(ORG_NAME, LEGACY_APP_NAME), settings)
    migrate.migrate_data()
    task_failures = report_task_migration(migrate.migrate_tasks())
    load_fonts()
    apply_theme(app, str(settings.value("ui/theme", "system")))

    window = MainWindow(settings)
    window.show()
    if motion_enabled():
        fade_in(window.centralWidget())
    if task_failures:
        # Queued so it opens over the window once the event loop is running.
        QtCore.QTimer.singleShot(0, lambda: QtWidgets.QMessageBox.warning(
            window, "Scheduled backups not moved",
            "Some scheduled backups could not be moved to their new "
            f"{APP_NAME} task. They keep running under their old name, and "
            f"{APP_NAME} will try again next time it starts.\n\n"
            + "\n".join(task_failures)))
    return app.exec()


def report_task_migration(results) -> list:
    """Print each scheduled-task migration result; return the failure lines.

    The console line is for a run from source; the failures also go to a
    dialog in main(), because a packaged build has no console to read.
    """
    failures = []
    for result in results:
        line = f"{result['name']}: {result['message']}"
        print(("[OK] " if result["ok"] else "[WARN] ") + line)
        if not result["ok"]:
            failures.append(line)
    return failures


if __name__ == "__main__":
    # The headless path is resolved before any Qt object exists, so a
    # scheduled run costs no QApplication and shows no window.
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-backup-job":
        sys.exit(run_backup_job_cli(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--precheck-job":
        sys.exit(precheck_job_cli(sys.argv[2]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--release-hold":
        sys.exit(release_hold_cli(sys.argv[2]))
    if any(flag in sys.argv for flag in ("--run-backup-job", "--precheck-job",
                                         "--release-hold")):
        print(HEADLESS_USAGE)
        sys.exit(2)
    sys.exit(main())
