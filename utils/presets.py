import os, sys, json
from typing import Dict, Any, List

APP_NAME = "UtilityTool"

def app_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    elif os.name == "nt":
        base = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~\\AppData\\Roaming"), APP_NAME)
    else:
        base = os.path.join(os.path.expanduser("~/.config"), APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base

PRESETS_FILE = os.path.join(app_dir(), "presets.json")

DEFAULT = {
    "image": [
        {
            "name": "Default Image (JPEG 85)",
            "format": "JPEG",
            "quality": 85,
            "size_key": "Original",
            "prefix": "",
            "suffix": "",
            "keep_exif": True
        }
    ],
    "file": [
        {
            "name": "Default File (date prefix)",
            "pattern": "{date:%Y%m%d}_{name}",
            "prefix": "",
            "suffix": "",
            "start": 1,
            "pad": 3,
            "regex_find": "",
            "regex_replace": "",
            "case": "none",
            "date_source": "now"
        }
    ]
}

def load_all() -> Dict[str, Any]:
    if not os.path.exists(PRESETS_FILE):
        save_all(DEFAULT)
        return DEFAULT.copy()
    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT.copy()

def save_all(data: Dict[str, Any]) -> None:
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# helpers
def get_image_presets() -> List[Dict[str, Any]]:
    return load_all().get("image", [])

def get_file_presets() -> List[Dict[str, Any]]:
    return load_all().get("file", [])

def add_image_preset(preset: Dict[str, Any]) -> None:
    d = load_all()
    d.setdefault("image", []).append(preset)
    save_all(d)

def add_file_preset(preset: Dict[str, Any]) -> None:
    d = load_all()
    d.setdefault("file", []).append(preset)
    save_all(d)

def delete_preset(kind: str, index: int) -> None:
    d = load_all()
    if kind in d and 0 <= index < len(d[kind]):
        d[kind].pop(index)
        save_all(d)

def update_preset(kind: str, index: int, new_preset: Dict[str, Any]) -> None:
    d = load_all()
    if kind in d and 0 <= index < len(d[kind]):
        d[kind][index] = new_preset
        save_all(d)