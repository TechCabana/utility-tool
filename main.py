import sys
from PySide6 import QtWidgets
from home_tab import HomeTab
from image_tools_tab import ImageToolsTab
from file_tools_tab import FileToolsTab

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Utility Tool")
        self.resize(1100, 760)

        tabs = QtWidgets.QTabWidget()
        tabs.setDocumentMode(True)
        tabs.setTabPosition(QtWidgets.QTabWidget.North)

        self.home_tab = HomeTab()
        self.image_tab = ImageToolsTab()
        self.file_tab = FileToolsTab()

        tabs.addTab(self.home_tab, "Home")
        tabs.addTab(self.image_tab, "Image Tools")
        tabs.addTab(self.file_tab, "File Tools")

        self.setCentralWidget(tabs)

        # Dark theme
        self.setStyleSheet("""
            QMainWindow { background:#121212; }
            QWidget { color:#eaeaea; background:#121212; }
            QLabel { color:#eaeaea; }
            QLineEdit, QComboBox, QListWidget, QSpinBox, QDoubleSpinBox, QTextEdit {
                background:#1e1e1e; color:#eaeaea; border:1px solid #3a3a3a; border-radius:6px; padding:4px;
            }
            QPushButton { background:#2a2a2a; border:1px solid #444; padding:6px 10px; border-radius:6px; }
            QPushButton:hover { background:#333; }
            QProgressBar { background:#1b1b1b; border:1px solid #444; border-radius:6px; text-align:center; }
            QProgressBar::chunk { background:#4a90e2; border-radius:6px; }
            QTabBar::tab { background:#1a1a1a; padding:8px 14px; border:1px solid #333; border-bottom:none; }
            QTabBar::tab:selected { background:#2a2a2a; }
        """)

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())
