# widgets/common.py
"""Shared, reusable Qt widgets: a confirm-before-destructive-action dialog
and an empty-state placeholder for batch/list screens.

These are Qt widgets (QDialog/QWidget subclasses), so they live here rather
than in utils/, which stays Qt-free by design (CLAUDE.md §8). Every screen
with a destructive action (File Manager Move/Copy/Delete, Cleanup, Duplicates,
Backup job removal, ...) or a batch/list that can be empty (no files added,
no backup jobs, no scan run yet) should import from here instead of rolling
its own modal or blank-area assumption.

Styling comes from styles/theme.qss: #Card for the panel, #Secondary for the
cancel button, #Danger for the confirm button (the --error token, so a
destructive confirm reads distinctly from the app's one rose interaction
accent), #H2/#Hint for text. No inline stylesheets here beyond the drop
shadow, which QSS cannot express (see apply_card_shadows() in main.py --
this dialog applies the same effect itself since it is created ad hoc,
outside the widget tree main.py walks at startup).
"""
from PySide6 import QtWidgets, QtCore, QtGui


class ConfirmDialog(QtWidgets.QDialog):
    """A shared confirm dialog for destructive actions.

    Use the static `ask()` helper in most call sites:

        if ConfirmDialog.ask(self, "Delete files?", "This removes 3 files
        from disk. This cannot be undone.", confirm_text="Delete"):
            ...perform the destructive action...

    Canceling (or closing the dialog) returns False and the caller must not
    perform the action.
    """

    def __init__(self, parent, title: str, message: str,
                 confirm_text: str = "Delete", cancel_text: str = "Cancel"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(380)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QtWidgets.QFrame()
        card.setObjectName("Card")
        outer.addWidget(card)

        # Same drop-shadow recipe as apply_card_shadows() in main.py -- this
        # dialog is built outside the tree that function walks at startup,
        # so it applies its own instance rather than depending on that walk.
        shadow = QtWidgets.QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(16)
        shadow.setOffset(0, 2)
        shadow.setColor(QtGui.QColor(24, 24, 27, 28))
        card.setGraphicsEffect(shadow)

        v = QtWidgets.QVBoxLayout(card)
        v.setSpacing(10)

        heading = QtWidgets.QLabel(title)
        heading.setObjectName("H2")
        heading.setWordWrap(True)
        v.addWidget(heading)

        body = QtWidgets.QLabel(message)
        body.setObjectName("Hint")
        body.setWordWrap(True)
        v.addWidget(body)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)

        cancel_btn = QtWidgets.QPushButton(cancel_text)
        cancel_btn.setObjectName("Secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        confirm_btn = QtWidgets.QPushButton(confirm_text)
        confirm_btn.setObjectName("Danger")
        confirm_btn.setDefault(True)
        confirm_btn.clicked.connect(self.accept)
        btn_row.addWidget(confirm_btn)

        v.addLayout(btn_row)

    @staticmethod
    def ask(parent, title: str, message: str,
            confirm_text: str = "Delete", cancel_text: str = "Cancel") -> bool:
        """Show the dialog modally; return True only if the user confirmed."""
        dlg = ConfirmDialog(parent, title, message, confirm_text, cancel_text)
        return dlg.exec() == QtWidgets.QDialog.Accepted


class EmptyState(QtWidgets.QWidget):
    """A real empty-state placeholder for a batch/list screen.

    Swap it in for the list/table when there is no data yet (no files
    added, no backup jobs, no scan run) instead of leaving a blank area
    that assumes data always exists. Two common uses:

    - As an overlay: construct with `parent=<the list widget>`, resize it
      to match on the list's resizeEvent, and toggle `setVisible()` on
      item-count changes (see tabs/image_tab.py) -- the list stays the
      live drop target underneath.
    - As a swapped layout member: add/remove it from a QVBoxLayout in
      place of the list/table widget.
    """

    def __init__(self, title: str, hint: str = "", icon: str = "",
                 cta_text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("EmptyState")

        v = QtWidgets.QVBoxLayout(self)
        v.setAlignment(QtCore.Qt.AlignCenter)
        v.setSpacing(6)

        if icon:
            icon_label = QtWidgets.QLabel(icon)
            icon_label.setAlignment(QtCore.Qt.AlignCenter)
            icon_label.setStyleSheet("font-size: 28px; background: transparent;")
            v.addWidget(icon_label)

        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("H2")
        title_label.setAlignment(QtCore.Qt.AlignCenter)
        v.addWidget(title_label)

        if hint:
            hint_label = QtWidgets.QLabel(hint)
            hint_label.setObjectName("Hint")
            hint_label.setAlignment(QtCore.Qt.AlignCenter)
            hint_label.setWordWrap(True)
            v.addWidget(hint_label)

        # Exposed so a caller can connect a real action (e.g. open a file
        # browse dialog). None when no cta_text is given -- most screens
        # (Backup Jobs, Cleanup, Duplicates) have no natural "go empty"
        # action of their own, per DESIGN.md.
        self.cta = None
        if cta_text:
            self.cta = QtWidgets.QPushButton(cta_text)
            self.cta.setObjectName("Secondary")
            btn_row = QtWidgets.QHBoxLayout()
            btn_row.addStretch(1)
            btn_row.addWidget(self.cta)
            btn_row.addStretch(1)
            v.addLayout(btn_row)
