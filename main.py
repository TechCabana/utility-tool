import sys
from PySide6 import QtWidgets, QtGui
from tabs.home_tab import HomeTab
from tabs.image_tools_tab import ImageToolsTab
from tabs.file_tools_tab import FileToolsTab

def apply_dark(app):
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(30,30,30))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(20,20,20))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(45,45,45))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(235,235,235))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(235,235,235))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(45,45,45))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(235,235,235))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(0,120,215))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(255,255,255))
    app.setPalette(palette)

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    apply_dark(app)

    window = QtWidgets.QMainWindow()
    window.setWindowTitle("UtilityTool")
    window.resize(1100, 760)

    tabs = QtWidgets.QTabWidget()
    tabs.setStyleSheet("QTabBar::tab { color: white; }")
    tabs.addTab(HomeTab(), "Home")
    tabs.addTab(FileToolsTab(), "File Tools")
    tabs.addTab(ImageToolsTab(), "Image Tools")

    window.setCentralWidget(tabs)
    window.show()
    sys.exit(app.exec())
