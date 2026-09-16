from PySide6 import QtCore, QtWidgets

from styles import tokens
from utils import activity
from widgets import icons
from widgets.common import EmptyState, PageHeader, card, divider


class EntryCard(QtWidgets.QFrame):
    """A clickable dashboard card that requests a page switch.

    A card that behaves like a button has to *be* one: a QFrame with only a
    mouse handler is invisible to Tab and unusable from the keyboard.
    StrongFocus plus a Return/Space handler makes it behave like the control
    it visually reads as, and the hover state in the sheet is what tells a
    mouse user it is pressable at all.
    """
    clicked = QtCore.Signal()

    def __init__(self, title: str, description: str, icon_name: str):
        super().__init__()
        self.setObjectName("EntryCard")
        self.icon_name = icon_name
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        self.glyph = QtWidgets.QLabel()
        icons.set_pixmap(self.glyph, icon_name, "accent", 22)
        layout.addWidget(self.glyph)

        name = QtWidgets.QLabel(title)
        name.setObjectName("H2")
        layout.addWidget(name)

        detail = QtWidgets.QLabel(description)
        detail.setObjectName("Hint")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        layout.addStretch(1)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        # The standard QAbstractButton activation keys, so a Tab-focused card
        # is actually usable rather than merely focusable.
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class HomeTab(QtWidgets.QWidget):
    # Emitted with the target page index; MainWindow wires this to the same
    # path the sidebar buttons use.
    switch_requested = QtCore.Signal(int)

    ENTRIES = (
        ("Image Tools", "Compress, resize and convert a batch of images.", "image", 1),
        ("File Tools", "Rename, move, copy or delete a batch of files.", "files", 2),
        ("Disk", "See what is using space, clean up, and back folders up.", "hard-drive", 3),
    )

    def __init__(self):
        super().__init__()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(tokens.SPACE_BLOCK)

        layout.addWidget(PageHeader(
            "UtilityTool",
            "Batch tools for the files already on this machine. Nothing is uploaded."))

        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(tokens.SPACE_FIELD)
        layout.addLayout(cards)

        for title, description, icon_name, index in self.ENTRIES:
            entry = EntryCard(title, description, icon_name)
            entry.clicked.connect(lambda i=index: self.switch_requested.emit(i))
            cards.addWidget(entry, 1)

        self._activity_card, self._activity_body = card(
            "Recent activity", "The last batches this app ran, newest first.")
        layout.addWidget(self._activity_card, 1)

        self._rows = QtWidgets.QVBoxLayout()
        self._rows.setSpacing(0)
        self._activity_body.addLayout(self._rows)

        self._empty = EmptyState(
            title="Nothing run yet",
            hint="Batches you run from Image Tools, File Tools or Disk are listed here.",
            icon="clock",
        )
        self._activity_body.addWidget(self._empty)
        self._activity_body.addStretch(1)

        self.refresh()

    def showEvent(self, event):
        # Refreshed on show rather than pushed from the tool tabs: the log is
        # a file, so reading it when the screen appears keeps Home correct
        # even after a batch that ran before this widget existed.
        self.refresh()
        super().showEvent(event)

    def refresh(self):
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        entries = activity.load()[:6]
        self._empty.setVisible(not entries)

        for position, entry in enumerate(entries):
            if position:
                self._rows.addWidget(divider())
            self._rows.addWidget(self._row(entry))

    def _row(self, entry: dict) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        row.setObjectName("CardBody")
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 9, 0, 9)
        layout.setSpacing(10)

        glyph = QtWidgets.QLabel()
        icons.set_pixmap(glyph, entry.get("kind") or "clock", "text_faint", 16)
        layout.addWidget(glyph, 0, QtCore.Qt.AlignTop)

        text = QtWidgets.QLabel(entry.get("text", ""))
        text.setWordWrap(True)
        layout.addWidget(text, 1)

        when = QtWidgets.QLabel(activity.relative_time(entry.get("at", "")))
        when.setObjectName("DataMuted")
        layout.addWidget(when, 0, QtCore.Qt.AlignTop)
        return row
