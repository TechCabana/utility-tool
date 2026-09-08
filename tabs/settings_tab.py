from PySide6 import QtWidgets

from utils.presets import load_all, delete_preset
from widgets.common import ConfirmDialog, EmptyState


class SettingsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        layout = QtWidgets.QVBoxLayout(self)
        # Margins leave room for the card drop shadow to render un-clipped
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        header = QtWidgets.QLabel("Settings")
        header.setObjectName("H1")
        layout.addWidget(header)

        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        v = QtWidgets.QVBoxLayout(card)
        v.addWidget(QtWidgets.QLabel("Global settings coming soon…"))

        layout.addWidget(card)

        # Presets panel: lists every saved image + file preset with Delete.
        presets_card = QtWidgets.QFrame()
        presets_card.setObjectName("Card")
        pv = QtWidgets.QVBoxLayout(presets_card)
        pv.setSpacing(10)

        presets_heading = QtWidgets.QLabel("Presets")
        presets_heading.setObjectName("H2")
        pv.addWidget(presets_heading)

        self._rows_layout = QtWidgets.QVBoxLayout()
        self._rows_layout.setSpacing(4)
        pv.addLayout(self._rows_layout)

        self._empty_state = EmptyState(
            title="No presets yet",
            hint="Save a preset from Image Tools or File Tools to see it here.",
        )
        pv.addWidget(self._empty_state)

        layout.addWidget(presets_card)
        layout.addStretch(1)

        self.refresh_presets()

    def refresh_presets(self):
        """Rebuild the preset row list from disk (initial load, and after
        every delete)."""
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        data = load_all()
        rows = []
        for kind, label in (("image", "Image"), ("file", "File")):
            for idx, preset in enumerate(data.get(kind, [])):
                rows.append((kind, idx, label, preset.get("name", "(unnamed)")))

        self._empty_state.setVisible(len(rows) == 0)

        for kind, idx, label, name in rows:
            row = QtWidgets.QWidget()
            h = QtWidgets.QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)

            name_lbl = QtWidgets.QLabel(name)
            kind_lbl = QtWidgets.QLabel(label)
            kind_lbl.setObjectName("Hint")
            del_btn = QtWidgets.QPushButton("Delete")
            del_btn.setObjectName("Danger")
            del_btn.clicked.connect(
                lambda _checked=False, k=kind, i=idx, n=name: self._delete(k, i, n)
            )

            h.addWidget(name_lbl, 1)
            h.addWidget(kind_lbl)
            h.addWidget(del_btn)
            self._rows_layout.addWidget(row)

    def _delete(self, kind: str, index: int, name: str):
        if not ConfirmDialog.ask(
            self,
            "Delete preset?",
            f"This permanently removes the preset '{name}'.",
            confirm_text="Delete",
        ):
            return
        delete_preset(kind, index)
        self.refresh_presets()
