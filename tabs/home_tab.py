from PySide6 import QtCore, QtGui, QtWidgets

from styles import tokens
from utils import activity, firstrun
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


class WelcomePanel(QtWidgets.QFrame):
    """Shown once, on a profile that has never run this app.

    The owner chose this over both a guided tour and showing nothing: it
    answers "what is this, and is it safe" without standing between the user
    and the work. It occupies the space the empty activity list would take,
    so on a first run - the only time it appears - it costs no layout at all.

    Three facts, in the order someone actually wants them, and then it is
    gone for good.
    """
    dismissed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        heading = QtWidgets.QLabel("Welcome to UtilityTool")
        heading.setObjectName("H2")
        layout.addWidget(heading)

        for line in (
            "Pick a tool above. Every job runs as a batch: add your files, say "
            "what should happen to them, then run it.",
            "Nothing leaves this machine. The app makes no network calls, needs "
            "no account, and works with the network switched off.",
            "Deletes go to the Recycle Bin, and a rename, move or copy can be "
            "undone from the result line straight afterwards.",
        ):
            label = QtWidgets.QLabel(line)
            label.setObjectName("Hint")
            label.setWordWrap(True)
            layout.addWidget(label)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.dismiss_btn = QtWidgets.QPushButton("Got it")
        self.dismiss_btn.setObjectName("Primary")
        self.dismiss_btn.setToolTip("Dismiss this panel  (Esc)")
        self.dismiss_btn.setAccessibleName("Dismiss the welcome panel")
        self.dismiss_btn.clicked.connect(self.dismissed)
        row.addWidget(self.dismiss_btn)
        layout.addLayout(row)


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

        # Above the activity list, which on a first run is empty anyway.
        self.welcome = WelcomePanel()
        self.welcome.dismissed.connect(self.dismiss_welcome)
        self.welcome.setVisible(not firstrun.seen())
        layout.addWidget(self.welcome)

        # A QShortcut (default WindowShortcut context) fires from anywhere
        # in the window, not only when a HomeTab descendant has focus. A
        # keyPressEvent override does not: on the very launch this panel
        # exists for, the widget that actually holds focus is the sidebar's
        # own Home button, a sibling of HomeTab, not a descendant of it, so
        # Escape never reached HomeTab.keyPressEvent to bubble from.
        self._dismiss_shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self)
        self._dismiss_shortcut.activated.connect(self.dismiss_welcome)

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

    def dismiss_welcome(self):
        """Hide it and remember, so it never appears again on this profile."""
        if not self.welcome.isVisible():
            return
        firstrun.mark_seen()
        self.welcome.setVisible(False)

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
