from PySide6 import QtCore, QtWidgets

from styles import tokens
from utils.presets import load_all, delete_preset, describe_preset
from widgets.common import (ConfirmDialog, ElidedLabel, EmptyState, PageHeader,
                            card, divider, icon_button, section_header)

THEME_CHOICES = (
    ("Match the system", "system"),
    ("Light", "light"),
    ("Dark", "dark"),
)


class SettingsTab(QtWidgets.QWidget):
    """Appearance, the shared preset library, and what this app is.

    This screen used to open with a card reading "Global settings coming
    soon...", which is a note to the developer shown to the user. A shipped
    screen either does something or is not in the navigation.
    """

    # Emitted with "system" / "light" / "dark"; MainWindow rebuilds the
    # stylesheet, since the theme is application-wide, not this page's.
    theme_changed = QtCore.Signal(str)

    def __init__(self):
        super().__init__()

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        body = QtWidgets.QWidget()
        body.setObjectName("CardBody")
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 6, 2)
        layout.setSpacing(tokens.SPACE_BLOCK)
        scroll.setWidget(body)

        layout.addWidget(PageHeader("Settings",
                                    "Appearance, saved presets, and what this app is."))

        layout.addWidget(self._appearance_card())
        layout.addWidget(self._presets_card())
        layout.addWidget(self._about_card())
        layout.addStretch(1)

        self.refresh_presets()

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
        for text, value in THEME_CHOICES:
            self.theme_combo.addItem(text, value)

        settings = QtCore.QSettings("TechCabana", "UtilityTool")
        current = str(settings.value("ui/theme", "system"))
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

    # -- presets ---------------------------------------------------------
    def _presets_card(self) -> QtWidgets.QFrame:
        panel, body = card("Presets")

        lede = QtWidgets.QLabel(
            "Saved settings from Image Tools and File Tools. Pick one from the "
            "Preset field on either screen to apply it.")
        lede.setObjectName("Hint")
        lede.setWordWrap(True)
        body.addWidget(lede)

        self._rows_layout = QtWidgets.QVBoxLayout()
        self._rows_layout.setSpacing(0)
        body.addLayout(self._rows_layout)

        self._empty_state = EmptyState(
            title="No presets yet",
            hint="Set up a batch in Image Tools or File Tools, then press Save preset.",
            icon="bookmark",
        )
        body.addWidget(self._empty_state)
        return panel

    def refresh_presets(self):
        """Rebuild the preset rows from disk (initial load, and after a delete)."""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        data = load_all()
        rows = []
        for kind, label in (("image", "Image"), ("file", "File")):
            for index, preset in enumerate(data.get(kind, [])):
                rows.append((kind, index, label, preset))

        self._empty_state.setVisible(not rows)

        for position, (kind, index, label, preset) in enumerate(rows):
            if position:
                self._rows_layout.addWidget(divider())
            self._rows_layout.addWidget(self._preset_row(kind, index, label, preset))

    def _preset_row(self, kind: str, index: int, label: str,
                    preset: dict) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        # Without this the base QWidget rule paints the page ground over the
        # card's surface, which is what produced the grey banding this list
        # used to show.
        row.setObjectName("CardBody")
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(12)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)

        name = QtWidgets.QLabel(preset.get("name", "(unnamed)"))
        name.setObjectName("H2")
        text.addWidget(name)

        summary = QtWidgets.QLabel(describe_preset(kind, preset))
        summary.setObjectName("Hint")
        summary.setWordWrap(True)
        text.addWidget(summary)
        layout.addLayout(text, 1)

        tag = QtWidgets.QLabel(label)
        tag.setObjectName("Tag")
        layout.addWidget(tag, 0, QtCore.Qt.AlignTop)

        # A row action, not an alarm: filled red is reserved for the confirm
        # button of the dialog this opens. A list of eleven filled red
        # buttons teaches people to stop reading red.
        remove = icon_button("trash", f"Delete the preset \"{preset.get('name', '')}\"",
                             f"Delete preset {preset.get('name', '')}")
        remove.clicked.connect(
            lambda _checked=False, k=kind, i=index, n=preset.get("name", ""): self._delete(k, i, n))
        layout.addWidget(remove, 0, QtCore.Qt.AlignTop)
        return row

    def _delete(self, kind: str, index: int, name: str):
        if not ConfirmDialog.ask(
            self,
            "Delete this preset?",
            f"\"{name}\" will be removed from the preset list. Files you have "
            f"already processed are not affected.",
            confirm_text="Delete preset",
        ):
            return
        delete_preset(kind, index)
        self.refresh_presets()

    # -- about -----------------------------------------------------------
    def _about_card(self) -> QtWidgets.QFrame:
        panel, body = card("About")

        # Imported here rather than at module scope: main.py imports this
        # module, so a top-level import back into main.py is a cycle.
        from main import __version__

        for label, value in (
            ("Version", f"v{__version__}"),
            ("Presets stored in", _presets_location()),
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

            # Elided, not plain: the presets path is one unbreakable word,
            # and at the app's minimum window it set a minimum width wider
            # than the card, pushing this screen's content off its own
            # viewport. The full path stays in the tooltip.
            data = ElidedLabel(value)
            data.setObjectName("DataMuted")
            line.addWidget(data, 1)
            body.addWidget(row)

        body.addWidget(section_header("Privacy"))
        privacy = QtWidgets.QLabel(
            "UtilityTool makes no network calls. Every image, file and folder it "
            "touches stays on this machine, and nothing is uploaded, logged or "
            "shared. It needs no account and works with the network switched off.")
        privacy.setObjectName("Hint")
        privacy.setWordWrap(True)
        body.addWidget(privacy)

        body.addWidget(section_header("Licences"))
        licences = QtWidgets.QLabel(
            "UtilityTool is MIT licensed. It bundles the Manrope and Geist Mono "
            "typefaces (SIL Open Font License 1.1) and Lucide icons (ISC) — the "
            "full licence texts ship in assets/fonts/ and assets/icons/.")
        licences.setObjectName("Hint")
        licences.setWordWrap(True)
        body.addWidget(licences)
        return panel


def _presets_location() -> str:
    from utils.presets import PRESETS_FILE

    return PRESETS_FILE
