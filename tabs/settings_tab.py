from PySide6 import QtWidgets


class SettingsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
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
