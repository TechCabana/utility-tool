import json, os, sys
from dataclasses import dataclass, asdict
from typing import List, Dict, Any

APP_NAME = "UtilityTool"

def app_data_dir() -> str:
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)
    elif os.name == "nt":
        base = os.path.join(os.getenv("APPDATA") or os.path.expanduser("~\\AppData\\Roaming"), APP_NAME)
    else:
        base = os.path.join(os.path.expanduser("~/.config"), APP_NAME)
    os.makedirs(base, exist_ok=True)
    return base

PRESETS_PATH = os.path.join(app_data_dir(), "presets.json")

@dataclass
class ImagePreset:
    name: str
    format: str = "ORIGINAL"  # ORIGINAL/JPEG/PNG/WEBP/TIFF
    quality: int = 85
    target_size_key: str = "Original"
    prefix: str = ""
    suffix: str = ""
    keep_exif: bool = True

@dataclass
class FileRenamePreset:
    name: str
    prefix: str = ""
    suffix: str = ""
    numbering_start: int = 1
    numbering_pad: int = 3
    pattern: str = "{name}"  # supports {name}, {ext}, {date:%Y-%m-%d}, {num}
    regex_find: str = ""
    regex_replace: str = ""

def load_presets() -> Dict[str, List[Dict[str, Any]]]:
    if not os.path.exists(PRESETS_PATH):
        defaults = {
            "image": [asdict(ImagePreset(name="Default Image Compression", format="JPEG", quality=85))],
            "file": [asdict(FileRenamePreset(name="Date Prefix", pattern="{date:%Y-%m-%d}_{name}"))]
        }
        save_presets(defaults)
        return defaults
    try:
        with open(PRESETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"image": [], "file": []}

def save_presets(data: Dict[str, List[Dict[str, Any]]]) -> None:
    with open(PRESETS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
