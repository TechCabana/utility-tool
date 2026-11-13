from PySide6 import QtCore, QtWidgets

from styles import tokens
from utils.fs_utils import APP_NAME, app_dir
from widgets.common import ElidedLabel, PageHeader, card, section_header

THEME_CHOICES = (
    ("Match the system", "system"),
    ("Light", "light"),
    ("Dark", "dark"),
)


class SettingsTab(QtWidgets.QWidget):
    """Appearance, and what this app is.

    This screen used to open with a card reading "Global settings coming
    soon...", which is a note to the developer shown to the user. A shipped
    screen either does something or is not in the navigation.
    """

    # Emitted with "system" / "light" / "dark"; MainWindow rebuilds the
    # stylesheet, since the theme is application-wide, not this page's.
    theme_changed = QtCore.Signal(str)

    def __init__(self, settings: QtCore.QSettings):
        super().__init__()
        # Passed in rather than built here: a second settings object for the
        # same org and app would hardcode main.py's names a second time and
        # defeat any harness that redirects where this screen reads from.
        self._settings = settings

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        scroll.setAccessibleName("Settings content")
        outer.addWidget(scroll)

        body = QtWidgets.QWidget()
        body.setObjectName("CardBody")
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 6, 2)
        layout.setSpacing(tokens.SPACE_BLOCK)
        scroll.setWidget(body)

        layout.addWidget(PageHeader("Settings", "Appearance, and what this app is."))

        layout.addWidget(self._appearance_card())
        layout.addWidget(self._about_card())
        layout.addStretch(1)

    # -- appearance ------------------------------------------------------
    def _appearance_card(self) -> QtWidgets.QFrame:
        panel, body = card("Appearance")

        row = QtWidgets.QWidget()
        row.setObjectName("CardBody")
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)

        label = QtWidgets.QLabel("Theme")
        label.setObjectName("FieldLabel")
        row_layout.addWidget(label)

        self.theme_combo = QtWidgets.QComboBox()
        self.theme_combo.setAccessibleName("Theme")
        for text, value in THEME_CHOICES:
            self.theme_combo.addItem(text, value)

        current = str(self._settings.value("ui/theme", "system"))
        index = self.theme_combo.findData(current)
        self.theme_combo.setCurrentIndex(index if index >= 0 else 0)
        self.theme_combo.currentIndexChanged.connect(
            lambda _: self.theme_changed.emit(self.theme_combo.currentData()))
        row_layout.addWidget(self.theme_combo, 1)
        row_layout.addStretch(1)

        body.addWidget(row)

        hint = QtWidgets.QLabel(
            "\"Match the system\" follows the light or dark setting Windows or macOS is using.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        body.addWidget(hint)
        return panel

    # -- about -----------------------------------------------------------
    def _about_card(self) -> QtWidgets.QFrame:
        panel, body = card("About")

        # Imported here rather than at module scope: main.py imports this
        # module, so a top-level import back into main.py is a cycle.
        from main import __version__

        for label, value in (
            ("Version", f"v{__version__}"),
            ("Data stored in", app_dir()),
        ):
            row = QtWidgets.QWidget()
            row.setObjectName("CardBody")
            line = QtWidgets.QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(12)

            caption = QtWidgets.QLabel(label)
            caption.setObjectName("FieldLabel")
            caption.setMinimumWidth(120)
            line.addWidget(caption)

            # Elided, not plain: the data path is one unbreakable word,
            # and at the app's minimum window it set a minimum width wider
            # than the card, pushing this screen's content off its own
            # viewport. The full path stays in the tooltip.
            data = ElidedLabel(value)
            data.setObjectName("DataMuted")
            line.addWidget(data, 1)
            body.addWidget(row)

        body.addWidget(section_header("Privacy"))
        privacy = QtWidgets.QLabel(
            f"{APP_NAME} makes no network calls. Every file and folder it "
            "touches stays on this machine, and nothing is uploaded, logged or "
            "shared. It needs no account and works with the network switched off.")
        privacy.setObjectName("Hint")
        privacy.setWordWrap(True)
        body.addWidget(privacy)

        body.addWidget(section_header("Licences"))
        licences = QtWidgets.QLabel(
            f"{APP_NAME} is MIT licensed. It bundles the Manrope and Geist Mono "
            "typefaces (SIL Open Font License 1.1) and Lucide icons (ISC) — the "
            "full licence texts ship in assets/fonts/ and assets/icons/.")
        licences.setObjectName("Hint")
        licences.setWordWrap(True)
        body.addWidget(licences)
        return panel
