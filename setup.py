from setuptools import setup
import os

APP = ['main.py']  # entry point of your app

# Bundle resources: presets + stylesheet
DATA_FILES = [
    ('presets', ['presets/default.json']),   # include your default presets
    ('styles', ['styles/dark.qss']),         # include your dark theme stylesheet
]

OPTIONS = {
    'argv_emulation': True,
    'packages': ['PyQt5', 'PIL'],  # add other deps here if needed
     #'iconfile': 'icon.icns',       # optional app icon
    'resources': ['presets', 'styles'],  # ensures folders are copied
    'plist': {
        'CFBundleName': 'UtilityTool',
        'CFBundleDisplayName': 'UtilityTool',
        'CFBundleIdentifier': 'com.yourname.utilitytool',
        'CFBundleVersion': '0.1',
        'CFBundleShortVersionString': '0.1',
    }
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
