import sys
import os
import re
import json
from PyQt5 import QtCore, QtWidgets

# macOS-specific fix for missing Qt 'cocoa' plugin
if sys.platform == "darwin":
    plugin_path = os.path.join(os.path.dirname(QtCore.__file__), 'Qt', 'plugins')
    os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = plugin_path

PRESET_FILE = "presets.json"


# ----------------- Home Tab -----------------
class HomeTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout()

        title = QtWidgets.QLabel("Global Settings & Presets")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: bold;")

        # Preset list
        self.preset_list = QtWidgets.QListWidget()
        self.load_presets()

        # Buttons
        btn_add = QtWidgets.QPushButton("Add Preset")
        btn_remove = QtWidgets.QPushButton("Remove Preset")
        btn_load = QtWidgets.QPushButton("Load Preset")

        btn_add.clicked.connect(self.add_preset)
        btn_remove.clicked.connect(self.remove_preset)
        btn_load.clicked.connect(self.load_selected_preset)

        layout.addWidget(title)
        layout.addWidget(self.preset_list)
        layout.addWidget(btn_add)
        layout.addWidget(btn_remove)
        layout.addWidget(btn_load)
        self.setLayout(layout)

    def load_presets(self):
        """Load presets from JSON file into the list widget"""
        self.preset_list.clear()
        if os.path.exists(PRESET_FILE):
            try:
                with open(PRESET_FILE, "r") as f:
                    presets = json.load(f)
                    for preset in presets:
                        self.preset_list.addItem(preset)
            except json.JSONDecodeError:
                QtWidgets.QMessageBox.warning(self, "Error", "Presets file is corrupted.")
        else:
            # Create default presets on first run
            presets = ["Default Image Compression", "File Rename - Date Prefix"]
            with open(PRESET_FILE, "w") as f:
                json.dump(presets, f, indent=2)
            self.preset_list.addItems(presets)

    def save_presets(self):
        """Save the list widget presets into JSON file"""
        presets = [self.preset_list.item(i).text() for i in range(self.preset_list.count())]
        with open(PRESET_FILE, "w") as f:
            json.dump(presets, f, indent=2)

    def add_preset(self):
        """Add a new preset to the list"""
        text, ok = QtWidgets.QInputDialog.getText(self, "Add Preset", "Preset Name:")
        if ok and text:
            self.preset_list.addItem(text)
            self.save_presets()

    def remove_preset(self):
        """Remove selected preset"""
        selected_item = self.preset_list.currentItem()
        if selected_item:
            self.preset_list.takeItem(self.preset_list.row(selected_item))
            self.save_presets()

    def load_selected_preset(self):
        """Load selected preset (placeholder for applying settings)"""
        selected_item = self.preset_list.currentItem()
        if selected_item:
            QtWidgets.QMessageBox.information(self, "Load Preset", f"Loaded preset: {selected_item.text()}")


# ----------------- File Tools Tab -----------------
class FileToolsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout()

        title = QtWidgets.QLabel("File Tools - Batch Rename & Management")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: bold;")

        # File selection
        self.file_list = QtWidgets.QListWidget()
        btn_add_files = QtWidgets.QPushButton("Add Files")
        btn_add_files.clicked.connect(self.add_files)

        # Rename pattern
        rename_layout = QtWidgets.QHBoxLayout()
        self.prefix_input = QtWidgets.QLineEdit()
        self.prefix_input.setPlaceholderText("Prefix")
        self.suffix_input = QtWidgets.QLineEdit()
        self.suffix_input.setPlaceholderText("Suffix")
        self.regex_input = QtWidgets.QLineEdit()
        self.regex_input.setPlaceholderText("Regex Pattern (optional)")
        rename_layout.addWidget(self.prefix_input)
        rename_layout.addWidget(self.suffix_input)
        rename_layout.addWidget(self.regex_input)

        # Rename button
        btn_rename = QtWidgets.QPushButton("Apply Rename")
        btn_rename.clicked.connect(self.apply_rename)

        # Progress bar
        self.progress = QtWidgets.QProgressBar()

        layout.addWidget(title)
        layout.addWidget(self.file_list)
        layout.addWidget(btn_add_files)
        layout.addLayout(rename_layout)
        layout.addWidget(btn_rename)
        layout.addWidget(self.progress)
        self.setLayout(layout)

    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Files")
        if files:
            self.file_list.addItems(files)

    def apply_rename(self):
        count = self.file_list.count()
        if count == 0:
            return

        for i in range(count):
            filepath = self.file_list.item(i).text()
            dirname, basename = os.path.split(filepath)
            name, ext = os.path.splitext(basename)

            # Apply prefix/suffix
            new_name = f"{self.prefix_input.text()}{name}{self.suffix_input.text()}{ext}"

            # Apply regex substitution if provided
            if self.regex_input.text():
                try:
                    new_name = re.sub(self.regex_input.text(), "", new_name)
                except re.error:
                    QtWidgets.QMessageBox.warning(self, "Regex Error", "Invalid regex pattern")

            new_path = os.path.join(dirname, new_name)
            os.rename(filepath, new_path)

            self.progress.setValue(int(((i + 1) / count) * 100))


# ----------------- Image Tools Tab -----------------
class ImageToolsTab(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout()

        title = QtWidgets.QLabel("Image Tools - Resize, Compress, Convert")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet("font-size: 18px; font-weight: bold;")

        # Image selection
        self.image_list = QtWidgets.QListWidget()
        btn_add_images = QtWidgets.QPushButton("Add Images")
        btn_add_images.clicked.connect(self.add_images)

        # Resize options
        resize_layout = QtWidgets.QHBoxLayout()
        self.resize_dropdown = QtWidgets.QComboBox()
        self.resize_dropdown.addItems([
            "Original Size",
            "A4 (210x297 mm)",
            "A5 (148x210 mm)",
            "Passport (India 35x45 mm)",
            "Passport (Netherlands 35x45 mm)"
        ])
        resize_layout.addWidget(QtWidgets.QLabel("Resize To:"))
        resize_layout.addWidget(self.resize_dropdown)

        # Compression slider
        compression_layout = QtWidgets.QHBoxLayout()
        self.compression_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.compression_slider.setRange(10, 100)
        self.compression_slider.setValue(80)
        compression_layout.addWidget(QtWidgets.QLabel("Compression Quality:"))
        compression_layout.addWidget(self.compression_slider)

        # Convert format
        self.format_dropdown = QtWidgets.QComboBox()
        self.format_dropdown.addItems(["JPEG", "PNG", "WEBP", "TIFF"])
        convert_layout = QtWidgets.QHBoxLayout()
        convert_layout.addWidget(QtWidgets.QLabel("Convert Format:"))
        convert_layout.addWidget(self.format_dropdown)

        # Process button
        btn_process = QtWidgets.QPushButton("Apply Changes")
        btn_process.clicked.connect(self.process_images)

        # Progress bar
        self.progress = QtWidgets.QProgressBar()

        layout.addWidget(title)
        layout.addWidget(self.image_list)
        layout.addWidget(btn_add_images)
        layout.addLayout(resize_layout)
        layout.addLayout(compression_layout)
        layout.addLayout(convert_layout)
        layout.addWidget(btn_process)
        layout.addWidget(self.progress)
        self.setLayout(layout)

    def add_images(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Select Images", "", "Images (*.png *.jpg *.jpeg *.webp *.tiff)")
        if files:
            self.image_list.addItems(files)

    def process_images(self):
        # For now, this just simulates work with the progress bar
        count = self.image_list.count()
        for i in range(count):
            QtCore.QThread.msleep(200)  # simulate processing delay
            self.progress.setValue(int(((i + 1) / count) * 100))


# ----------------- Main Window -----------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Utility Tool")
        self.resize(1000, 700)

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(HomeTab(), "Home")
        tabs.addTab(FileToolsTab(), "File Tools")
        tabs.addTab(ImageToolsTab(), "Image Tools")

        self.setCentralWidget(tabs)


# ----------------- Run Application -----------------
def load_stylesheet():
    try:
        base_path = getattr(sys, '_MEIPASS', os.path.abspath("."))
        style_path = os.path.join(base_path, "styles", "dark.qss")
        with open(style_path, "r") as f:
            return f.read()
    except Exception as e:
        print("Could not load stylesheet:", e)
        return ""

if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(load_stylesheet())
    from main_window import MainWindow  # your main window
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
