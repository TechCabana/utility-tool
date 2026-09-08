from PySide6 import QtWidgets


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
        layout.addStretch(1)
