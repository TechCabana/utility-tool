# Utility Tool (PySide6)

A cross-platform desktop app for image and file utilities with global settings, presets, and progress tracking.  
Built using **PySide6** for a modern, dark-themed UI.

---

## 📦 Features
- **Home Tab**: Manage global app settings and presets.  
- **Image Tools**: Compress, resize (incl. passport photo sizes for India & Netherlands), and convert formats.  
- **File Tools**: Batch rename, adjust prefixes, choose custom save destinations, and track progress with completion times.  
- **Presets**: Save and load settings for images, files, and global app configs.  

---

## ⚙️ Requirements
- Python **3.9+** (recommended)  
- [VS Code](https://code.visualstudio.com/) or any IDE  
- Dependencies listed in `requirements.txt`

---

## 🚀 Setup Instructions

### 1. Clone or copy the project
```bash
cd ~/Documents/GitHubProjects
git clone <your-repo-url> UtilityTool
cd UtilityTool
```

### 2. Set up virtual environment

**For Mac:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**For Windows (Powershell):**
```bash
python -m venv venv
.\venv\Scripts\activate
```

### 3. Install Dependencies
**For Mac:**

```bash
pip install -r requirements.txt

```
### 4. ▶️ Run the App in VS Code

1. Open the UtilityTool folder in VS Code.
2. Select the Python interpreter for your venv (Cmd+Shift+P → Python: Select Interpreter).
3. Run:
```bash
python main.py
```

### 5. 📦 Build the App

**For Mac (Create App):**

```bash
pip install pyinstaller
pyinstaller --onefile --windowed main.py --name "UtilityTool"
```
Output app will be in dist/UtilityTool.app.

**For Windows (Create Exe):**
```bash
pip install pyinstaller
pyinstaller --onefile --windowed main.py --name "UtilityTool"
```
Output exe will be in dist/UtilityTool.exe.