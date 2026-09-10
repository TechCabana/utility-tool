# widgets/pattern.py
"""A naming-pattern field that explains itself.

The rename engine (`utils.file_utils.build_new_name`) understands `{name}`,
`{ext}`, `{num}` and `{date:...}`. None of that appeared anywhere in the UI:
the field shipped containing `{name}` and the user was left to guess that
other tokens existed, let alone what they were called. A feature nobody can
discover is a feature nobody has.

This pairs the line edit with two things:

  * chips that insert a token at the cursor, so the vocabulary is visible
    and typing it correctly is not a test;
  * a live example built from a real file in the batch, so the answer to
    "what will this actually produce" is on screen before anything is
    renamed, rather than after.
"""
from __future__ import annotations

import os

from PySide6 import QtCore, QtWidgets

TOKENS = (
    ("{name}", "The original file name, without its extension"),
    ("{num}", "A running number, padded to the width set below"),
    ("{date:%Y%m%d}", "A date, in any strftime format"),
    ("{ext}", "The original extension, without the dot"),
)

# Stands in for a real file when the batch is empty, so the example line is
# never blank -- an example that only appears once you have added files is an
# example you cannot use while deciding whether to add them.
SAMPLE_FILE = "holiday photo.jpg"


class PatternField(QtWidgets.QWidget):
    """A pattern line edit, its token chips, and a live example line."""

    changed = QtCore.Signal(str)

    def __init__(self, value: str = "{name}", parent=None):
        super().__init__(parent)
        self.setObjectName("CardBody")
        self._sample_path = ""
        self._describe = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.edit = QtWidgets.QLineEdit(value)
        self.edit.setObjectName("Mono")
        self.edit.setAccessibleName("Naming pattern")
        self.edit.textChanged.connect(self._on_changed)
        layout.addWidget(self.edit)

        chips = QtWidgets.QHBoxLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(6)
        for token, explanation in TOKENS:
            chip = QtWidgets.QPushButton(token)
            chip.setObjectName("Ghost")
            chip.setToolTip(explanation)
            chip.setCursor(QtCore.Qt.PointingHandCursor)
            chip.setAccessibleName(f"Insert {token}")
            chip.clicked.connect(lambda _checked=False, t=token: self._insert(t))
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

        self.example = QtWidgets.QLabel()
        self.example.setObjectName("Hint")
        self.example.setWordWrap(True)
        layout.addWidget(self.example)

    # -- public ----------------------------------------------------------
    def text(self) -> str:
        return self.edit.text()

    def setText(self, value: str):
        self.edit.setText(value)

    def set_example_source(self, path: str, describe):
        """Point the example line at a real file.

        `describe` is a callback taking (path, sample_name) and returning the
        finished example string -- the two tool tabs word it differently
        ("becomes" vs "is renamed to") and only they know the rest of the
        form, so the sentence is theirs to build.
        """
        self._sample_path = path
        self._describe = describe
        self.refresh()

    def refresh(self):
        path = self._sample_path or SAMPLE_FILE
        try:
            text = self._describe(path) if self._describe else ""
        except Exception as error:
            # A half-typed `{date:` is an ordinary state of a text field
            # being typed into, not an error worth a dialog.
            text = f"Pattern is not valid yet ({error})"
        self.example.setText(text)

    # -- internals -------------------------------------------------------
    def _insert(self, token: str):
        self.edit.insert(token)
        self.edit.setFocus()

    def _on_changed(self, value: str):
        self.refresh()
        self.changed.emit(value)


def sample_name(path: str) -> str:
    return os.path.basename(path) or SAMPLE_FILE
