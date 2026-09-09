from PySide6 import QtWidgets, QtCore


class EntryCard(QtWidgets.QFrame):
    """A clickable dashboard card that requests a tab switch.

    Uses the existing "#Card" objectName so it picks up the Soft Rose panel
    styling and drop shadow for free (see apply_card_shadows() in main.py) --
    no new QSS needed beyond the #Card:focus rule in styles/theme.qss.

    DESIGN.md's Operate mode requires "every component is a real, working
    control ... the world is a skin over standard Qt affordances, not a
    replacement for them" -- a QFrame with only a mouse handler fails that:
    it is invisible to Tab and unusable from the keyboard. StrongFocus plus
    a Return/Space keyPressEvent makes it behave like the real button it
    visually reads as.
    """
    clicked = QtCore.Signal()

    def __init__(self, title: str, description: str, badge: str = ""):
        super().__init__()
        self.setObjectName("Card")
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(description)

        v = QtWidgets.QVBoxLayout(self)
        v.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        name = QtWidgets.QLabel(title)
        name.setObjectName("H2")
        head.addWidget(name)
        head.addStretch(1)
        if badge:
            tag = QtWidgets.QLabel(badge)
            tag.setObjectName("FormLabel")
            head.addWidget(tag)
        v.addLayout(head)

        desc = QtWidgets.QLabel(description)
        desc.setObjectName("Hint")
        desc.setWordWrap(True)
        v.addWidget(desc)

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        # Activate the same as a click on Return/Enter/Space -- the standard
        # QAbstractButton keyboard-activation keys -- so a Tab-focused card
        # is actually usable, not just visually a button.
        if event.key() in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class HomeTab(QtWidgets.QWidget):
    # Emitted with the target QStackedWidget index; MainWindow wires this to
    # the same tab-switch path the sidebar buttons use (see main.py).
    switch_requested = QtCore.Signal(int)

    def __init__(self):
        super().__init__()

        layout = QtWidgets.QVBoxLayout(self)
        # Margins leave room for the card drop shadows to render un-clipped
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(16)

        header = QtWidgets.QLabel("UtilityTool")
        header.setObjectName("H1")
        layout.addWidget(header)

        sub = QtWidgets.QLabel("Pick a tool to get started.")
        sub.setObjectName("Hint")
        layout.addWidget(sub)

        # Entry cards: Image Tools / File Manager / Disk
        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(12)
        layout.addLayout(cards)

        image_card = EntryCard("Image Tools", "Batch compress, resize and convert images.")
        image_card.clicked.connect(lambda: self.switch_requested.emit(1))
        cards.addWidget(image_card, 1)

        file_card = EntryCard("File Manager", "Batch rename, move, copy or delete files.")
        file_card.clicked.connect(lambda: self.switch_requested.emit(2))
        cards.addWidget(file_card, 1)

        # Disk (Overview / Cleanup / Backup, see DESIGN.md). Only Overview is
        # built so far; the card routes to the real tab, which carries its own
        # placeholders for the two sub-areas that aren't.
        disk_card = EntryCard("Disk", "Usage overview, cleanup and backup.")
        disk_card.clicked.connect(lambda: self.switch_requested.emit(3))
        cards.addWidget(disk_card, 1)

        # Recent Activity
        activity_header = QtWidgets.QLabel("Recent Activity")
        activity_header.setObjectName("H2")
        layout.addWidget(activity_header)

        self._activity_card = QtWidgets.QFrame()
        self._activity_card.setObjectName("Card")
        self._activity_layout = QtWidgets.QVBoxLayout(self._activity_card)
        self._activity_layout.setSpacing(6)
        layout.addWidget(self._activity_card)

        self._activity_entries = []
        self._render_activity()

        layout.addStretch(1)

    def add_activity(self, text):
        """Record a batch-operation summary at the top of Recent Activity.

        ponytail: in-memory only, cleared on restart -- no batch in the app
        calls this yet (nothing logs history today), so a persistence layer
        would be speculative. Add one (e.g. a small JSON file next to
        presets.json) if a real activity log is ever wanted.
        """
        stamp = QtCore.QDateTime.currentDateTime().toString("yyyy-MM-dd hh:mm")
        self._activity_entries.insert(0, f"{stamp}  -  {text}")
        self._render_activity()

    def _render_activity(self):
        while self._activity_layout.count():
            item = self._activity_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not self._activity_entries:
            empty = QtWidgets.QLabel("No recent activity yet - batches you run will show up here.")
            empty.setObjectName("Hint")
            empty.setWordWrap(True)
            self._activity_layout.addWidget(empty)
            return

        for entry in self._activity_entries[:5]:
            row = QtWidgets.QLabel(entry)
            self._activity_layout.addWidget(row)
