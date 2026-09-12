# widgets/common.py
"""The shared UI vocabulary: page header, card, form field, divider, empty
state, and the confirm-before-destructive-action dialog.

These are Qt widgets, so they live here rather than in utils/, which stays
Qt-free by design (CLAUDE.md §8). Every screen builds from this file rather
than hand-rolling its own panel, heading or modal -- that is what keeps five
screens looking like one product.

Styling comes from styles/theme.qss.tmpl. There are no inline stylesheets
here beyond the drop shadow, which QSS cannot express.
"""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from styles import tokens

# The app's vertical spacing scale, in px, re-exported from the token module
# so call sites import one thing. Two values, deliberately: fields inside one
# group sit close together, and top-level blocks get noticeably more air. The
# third gap -- between two groups -- is #SectionHeader's own asymmetric
# margin, so a heading binds to the fields it introduces rather than floating
# between two groups.
SPACE_FIELD = tokens.SPACE_FIELD
SPACE_BLOCK = tokens.SPACE_BLOCK

# How wide a centred explanatory sentence is allowed to get. Long measures are
# hard to read centred, and pinning the width is also what makes a wrapped
# QLabel report its real height (see EmptyState).
HINT_WIDTH = 380


def page_title(text: str) -> QtWidgets.QLabel:
    label = QtWidgets.QLabel(text)
    label.setObjectName("H1")
    return label


class PageHeader(QtWidgets.QWidget):
    """A screen's title and its one-line explanation of what the screen does.

    The lede is not decoration. Every screen has to answer "what is this and
    what do I do here" without a manual, and a bare 26px title answers
    neither.
    """

    def __init__(self, title: str, lede: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("CardBody")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(page_title(title))
        if lede:
            label = QtWidgets.QLabel(lede)
            label.setObjectName("PageLede")
            label.setWordWrap(True)
            layout.addWidget(label)


def card(title: str = "", lede: str = ""):
    """A panel plus the layout to fill it. Returns (frame, body_layout).

    Sections inside a card are separated by `section_header()` and spacing,
    never by giving each group its own bordered box -- a card inside a card
    flattens the surface hierarchy the elevation already establishes.
    """
    frame = QtWidgets.QFrame()
    frame.setObjectName("Card")
    body = QtWidgets.QVBoxLayout(frame)
    body.setSpacing(SPACE_FIELD)
    if title:
        heading = QtWidgets.QLabel(title)
        heading.setObjectName("H2")
        body.addWidget(heading)
    if lede:
        label = QtWidgets.QLabel(lede)
        label.setObjectName("Hint")
        label.setWordWrap(True)
        body.addWidget(label)
    return frame, body


def section_header(text: str) -> QtWidgets.QLabel:
    """A heading for one group of fields inside a panel."""
    label = QtWidgets.QLabel(text)
    label.setObjectName("SectionHeader")
    return label


def section(title: str, *actions: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """A group heading with optional actions on its right, over a hairline.

    `section_header()` covers the common case of a heading with nothing
    beside it. This is the other case: a section that owns a control, like
    Cleanup's Rescan or the Duplicates mode picker. Both exist so a screen
    can be one surface divided by headings rather than a column of separate
    cards -- a card inside a card flattens the hierarchy the elevation
    already establishes.

    The label uses #SectionLabel rather than #SectionHeader: the composite
    draws its own divider and owns its own margins, so borrowing the other
    style would draw the rule twice.
    """
    host = QtWidgets.QWidget()
    host.setObjectName("CardBody")
    column = QtWidgets.QVBoxLayout(host)
    # Asymmetric, for the same reason #SectionHeader's margins are: more air
    # above a heading than below it, so it binds to what follows rather than
    # floating between two groups.
    column.setContentsMargins(0, 14, 0, 0)
    column.setSpacing(6)

    row = QtWidgets.QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    label = QtWidgets.QLabel(title)
    label.setObjectName("SectionLabel")
    row.addWidget(label)
    row.addStretch(1)
    for widget in actions:
        row.addWidget(widget)
    column.addLayout(row)
    column.addWidget(divider())
    return host


def table_header(text: str) -> QtWidgets.QLabel:
    """A column heading in a repeated-row table.

    Its own object name rather than the field-label style it used to borrow:
    a column heading and a field label are different roles, and giving one
    style three jobs is how a design system stops meaning anything.
    """
    label = QtWidgets.QLabel(text)
    label.setObjectName("TableHeader")
    return label


def divider() -> QtWidgets.QFrame:
    """A hairline between repeated rows in a list.

    A QFrame rather than a styled plain QWidget on purpose: QSS paints a
    background on a QFrame without further ceremony, where a bare QWidget
    needs WA_StyledBackground before it paints anything at all.
    """
    line = QtWidgets.QFrame()
    line.setObjectName("Divider")
    return line


def data_label(text: str = "", muted: bool = False) -> QtWidgets.QLabel:
    """A number, size, percentage, path or timestamp, in the mono face.

    Anything that lines up in a column, or that the eye compares between
    rows, belongs in this rather than in the proportional UI face.
    """
    label = QtWidgets.QLabel(text)
    label.setObjectName("DataMuted" if muted else "Data")
    return label


def form_layout() -> QtWidgets.QFormLayout:
    """A label/field form for one group, on the app's spacing scale.

    Left-aligned, not right-aligned: a right-aligned column of labels ending
    in colons is the visual signature of a 1990s dialog box, and it puts a
    ragged edge between the label and the thing it labels.
    """
    form = QtWidgets.QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setVerticalSpacing(SPACE_FIELD)
    form.setHorizontalSpacing(16)
    form.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
    form.setFormAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
    return form


def add_field(form: QtWidgets.QFormLayout, label: str, widget: QtWidgets.QWidget,
              hint: str = "", tooltip: str = "") -> QtWidgets.QWidget:
    """Add one labelled row to `form`, with an optional explanation under it.

    Passing a plain string to `QFormLayout.addRow` makes an unstyled label,
    which is how this app ended up with two competing label conventions. This
    is the only way a field should be added.

    `hint` is where a control explains itself: a setting whose name does not
    tell you what it does ("Compression effort") needs a sentence, not a
    manual.
    """
    caption = QtWidgets.QLabel(label)
    caption.setObjectName("FieldLabel")
    caption.setBuddy(widget)
    if tooltip:
        caption.setToolTip(tooltip)
        widget.setToolTip(tooltip)
    if not widget.accessibleName():
        widget.setAccessibleName(label)

    if hint:
        host = QtWidgets.QWidget()
        host.setObjectName("CardBody")
        column = QtWidgets.QVBoxLayout(host)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(widget)
        note = QtWidgets.QLabel(hint)
        note.setObjectName("Hint")
        note.setWordWrap(True)
        column.addWidget(note)
        form.addRow(caption, host)
        return host

    form.addRow(caption, widget)
    return widget


def field_pair(first, label: str, second) -> QtWidgets.QWidget:
    """Two short fields sharing one form row, the second carrying its label.

    Several of this app's fields are a two-digit spin box or a four-item
    enum. One per row turns a form into a tall column of mostly empty space,
    which is most of why these screens read as cramped -- the rows were close
    together *and* there were twice as many as the content needed.
    """
    host = QtWidgets.QWidget()
    host.setObjectName("CardBody")
    layout = QtWidgets.QHBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addWidget(first, 1)
    caption = QtWidgets.QLabel(label)
    caption.setObjectName("FieldLabel")
    caption.setBuddy(second)
    layout.addWidget(caption)
    layout.addWidget(second, 1)
    return host


def icon_button(icon_name: str, tooltip: str, accessible_name: str = "") -> QtWidgets.QPushButton:
    """A square, borderless button carrying one glyph and no text."""
    from widgets import icons

    button = QtWidgets.QPushButton()
    button.setObjectName("IconButton")
    icons.set_icon(button, icon_name, "text_muted", 16)
    button.setToolTip(tooltip)
    # An icon-only control is unlabelled to a screen reader unless it is told
    # what it is, which is why this argument is not optional in practice.
    button.setAccessibleName(accessible_name or tooltip)
    button.setCursor(QtCore.Qt.PointingHandCursor)
    return button


class ElidedLabel(QtWidgets.QLabel):
    """A label that shrinks: text too long for its space is elided to fit.

    A word-wrapped QLabel cannot shrink below its longest unbreakable word,
    and a filesystem path is one unbreakable word. In a full-width label that
    never shows, because the label is wider than any path. In a table column
    or a narrow card it does: one deep path sets the minimum width of its own
    column, which sets the minimum width of the row, the card and the page,
    and pushes everything to its right off the edge of a window whose
    horizontal scrollbar is deliberately switched off.

    Eliding in the middle keeps both the drive and the leaf visible, which is
    what identifies a path; the whole thing stays one hover away in the
    tooltip. The text is elided rather than the painting overridden, so the
    label keeps its ordinary QSS styling.
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setMinimumWidth(0)
        # Ignored: the width comes from the layout, never from how long this
        # particular path happens to be.
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                           QtWidgets.QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        width = max(0, self.width() - 2)
        super().setText(self.fontMetrics().elidedText(
            self._full, QtCore.Qt.ElideMiddle, width) if width else self._full)


class ConfirmDialog(QtWidgets.QDialog):
    """A shared confirm dialog for destructive actions.

    Use the static `ask()` helper in most call sites:

        if ConfirmDialog.ask(self, "Delete these files?",
                             "This sends 3 files to the Recycle Bin.",
                             confirm_text="Delete"):
            ...perform the destructive action...

    Cancelling (or closing the dialog) returns False and the caller must not
    perform the action. The confirm button is the only filled-red surface in
    the app: by the time it is on screen the question has already been asked
    in words, which is what makes the colour mean something.
    """

    def __init__(self, parent, title: str, message: str,
                 confirm_text: str = "Delete", cancel_text: str = "Cancel"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QtWidgets.QFrame()
        panel.setObjectName("Card")
        outer.addWidget(panel)

        # Same recipe as apply_card_shadows() in main.py -- this dialog is
        # built outside the tree that function walks at startup, so it
        # applies its own instance rather than depending on that walk.
        from main import active_palette

        palette = active_palette()
        red, green, blue = palette["shadow_rgb"]
        shadow = QtWidgets.QGraphicsDropShadowEffect(panel)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        shadow.setColor(QtGui.QColor(red, green, blue, palette["shadow_alpha"]))
        panel.setGraphicsEffect(shadow)

        body = QtWidgets.QVBoxLayout(panel)
        body.setSpacing(10)

        heading = QtWidgets.QLabel(title)
        heading.setObjectName("H2")
        heading.setWordWrap(True)
        body.addWidget(heading)

        message_label = QtWidgets.QLabel(message)
        message_label.setObjectName("Hint")
        message_label.setWordWrap(True)
        body.addWidget(message_label)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)

        cancel = QtWidgets.QPushButton(cancel_text)
        cancel.setObjectName("Secondary")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        confirm = QtWidgets.QPushButton(confirm_text)
        confirm.setObjectName("Danger")
        confirm.setDefault(True)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(confirm)

        body.addLayout(buttons)

        # Cancel holds focus, not the destructive button: Enter on an
        # unexamined dialog should not delete anything.
        cancel.setFocus()

    @staticmethod
    def ask(parent, title: str, message: str,
            confirm_text: str = "Delete", cancel_text: str = "Cancel") -> bool:
        """Show the dialog modally; return True only if the user confirmed."""
        dialog = ConfirmDialog(parent, title, message, confirm_text, cancel_text)
        return dialog.exec() == QtWidgets.QDialog.Accepted


class EmptyState(QtWidgets.QWidget):
    """A real empty-state placeholder for a batch/list screen.

    Swap it in for the list/table when there is no data yet instead of
    leaving a blank area that assumes data always exists. Two common uses:

    - As an overlay: construct with `parent=<the list widget>`, resize it to
      match on the list's resizeEvent, and toggle `setVisible()` on
      item-count changes -- the list stays the live drop target underneath.
    - As a swapped layout member: add/remove it from a QVBoxLayout in place
      of the list/table widget.

    `icon` is the name of a bundled SVG in assets/icons/, not an emoji: an
    emoji is a different typeface's artwork at a different weight, and it
    renders differently on every OS.
    """

    def __init__(self, title: str, hint: str = "", icon: str = "",
                 cta_text: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("EmptyState")

        layout = QtWidgets.QVBoxLayout(self)
        layout.setAlignment(QtCore.Qt.AlignCenter)
        layout.setSpacing(8)

        if icon:
            from widgets import icons as icon_set

            glyph = QtWidgets.QLabel()
            glyph.setAlignment(QtCore.Qt.AlignCenter)
            icon_set.set_pixmap(glyph, icon, "text_faint", 28)
            layout.addWidget(glyph)

        title_label = QtWidgets.QLabel(title)
        title_label.setObjectName("H2")
        title_label.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(title_label)

        if hint:
            hint_label = QtWidgets.QLabel(hint)
            hint_label.setObjectName("Hint")
            hint_label.setAlignment(QtCore.Qt.AlignCenter)
            hint_label.setWordWrap(True)
            # A wrapped QLabel inside a centre-aligned box reports the height
            # of ONE line unless its width is pinned, so the layout gives it a
            # single line and the second one is clipped. Capping the width is
            # what makes heightForWidth resolve -- and a centred sentence
            # should not run the full width of a card anyway.
            hint_label.setMaximumWidth(HINT_WIDTH)
            hint_label.setMinimumHeight(hint_label.heightForWidth(HINT_WIDTH))
            layout.addWidget(hint_label, 0, QtCore.Qt.AlignHCenter)

        # Exposed so a caller can connect a real action (e.g. open a file
        # browser). None when no cta_text is given.
        self.cta = None
        if cta_text:
            self.cta = QtWidgets.QPushButton(cta_text)
            self.cta.setObjectName("Secondary")
            row = QtWidgets.QHBoxLayout()
            row.addStretch(1)
            row.addWidget(self.cta)
            row.addStretch(1)
            layout.addLayout(row)
