# utils/presets.py
import os
import sys
import json
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

# Image presets carry the same pattern-based naming fields as file presets
# (pattern/prefix/suffix/start/pad/regex_find/regex_replace/case/date_source)
# plus the image-only fields (format/quality/size_key/compression/keep_exif).
# `pattern` defaults to "{name}" everywhere below, which preserves the
# original filename unless a preset explicitly overrides it (DESIGN.md).
_IMAGE_NAMING_DEFAULTS = {
    "pattern": "{name}",
    "prefix": "",
    "suffix": "",
    "start": 1,
    "pad": 3,
    "regex_find": "",
    "regex_replace": "",
    "case": "none",
    "date_source": "now",
}


def _image_preset(name: str, format: str, quality: int, size_key: str,
                   compression: int = 4, keep_exif: bool = True, **naming) -> Dict[str, Any]:
    """Build an image preset dict with the shared naming fields filled in.

    `compression` defaults to 4 ("balanced" effort, see DESIGN.md's starter
    preset table) unless a preset overrides it (e.g. Print 4x6 -> 6).
    """
    preset = {
        "name": name,
        "format": format,
        "quality": quality,
        "size_key": size_key,
        "compression": compression,
        "keep_exif": keep_exif,
    }
    preset.update(_IMAGE_NAMING_DEFAULTS)
    preset.update(naming)
    return preset


def _file_preset(name: str, **naming) -> Dict[str, Any]:
    preset = {"name": name}
    preset.update(_IMAGE_NAMING_DEFAULTS)
    preset.update(naming)
    return preset


# Starter presets (v1) -- DESIGN.md "Starter presets" table is the source of
# truth for these exact values. Shipped once, on first run only: load_all()
# below only ever writes DEFAULT when presets.json does not exist yet, so a
# user who deletes a starter preset never has it silently reappear.
DEFAULT = {
    "image": [
        _image_preset("Web Upload", "JPEG", 85, "1920px long edge"),
        _image_preset("Email Attachment", "JPEG", 70, "1024px long edge"),
        _image_preset("Social Media Square", "JPEG", 90, "Instagram 1080×1080 px"),
        _image_preset("Passport – India", "JPEG", 95, "Passport – India (35×45 mm)"),
        _image_preset("Print 4×6", "JPEG", 95, "Photo 4×6 in (102×152 mm)", compression=6),
        _image_preset("Archive (lossless)", "PNG", 100, "Original", keep_exif=True),
    ],
    "file": [
        _file_preset("Date Prefix", pattern="{date:%Y%m%d}_{name}"),
        _file_preset("Sequential Numbering", pattern="{name}_{num}", pad=3),
        # Strips a " (copy N)" suffix (e.g. "Report (copy 2)" -> "Report").
        _file_preset("Strip \"copy N\" Suffix", regex_find=r" \(copy \d+\)", regex_replace=""),
        # Web-safe slug: any run of non-alphanumeric characters -> a single
        # hyphen, then lowercased -- a single regex pass is all build_new_name
        # supports, so this is the one-shot slugify pattern.
        _file_preset("Lowercase + Hyphens", regex_find=r"[^A-Za-z0-9]+", regex_replace="-", case="lower"),
        _file_preset("Document Numbering", pattern="INV_{num}", pad=4, start=1001),
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


def describe_preset(kind: str, preset: Dict[str, Any]) -> str:
    """A one-line summary of what a preset actually does.

    The preset list used to show names only, so "Web Upload" and "Email
    Attachment" were indistinguishable without applying one and reading the
    form -- the user had to remember what they had saved. This puts the
    configuration on the row.

    Qt-free on purpose (CLAUDE.md §8): it is a pure string function over the
    stored dict, so tests/test_presets.py covers it without a widget.
    """
    parts: List[str] = []

    if kind == "image":
        fmt = str(preset.get("format", "ORIGINAL"))
        parts.append("Keep format" if fmt.upper() == "ORIGINAL" else fmt.upper())

        size = str(preset.get("size_key", "Original"))
        if size and size != "Original":
            parts.append(size)

        # Quality is meaningless for a lossless format, so it is only shown
        # where it changes the output.
        if fmt.upper() in ("JPEG", "WEBP", "ORIGINAL"):
            parts.append(f"quality {preset.get('quality', 85)}")

        if not preset.get("keep_exif", True):
            parts.append("EXIF stripped")

    pattern = str(preset.get("pattern", "{name}"))
    if pattern and pattern != "{name}":
        parts.append(f"name {pattern}")

    prefix, suffix = str(preset.get("prefix", "")), str(preset.get("suffix", ""))
    if prefix:
        parts.append(f"prefix {prefix!r}")
    if suffix:
        parts.append(f"suffix {suffix!r}")

    if preset.get("regex_find"):
        replacement = str(preset.get("regex_replace", ""))
        target = f"to {replacement!r}" if replacement else "removed"
        parts.append(f"{preset['regex_find']!r} {target}")

    case = str(preset.get("case", "none"))
    if case != "none":
        parts.append(f"{case}case")

    if "{num}" in pattern:
        parts.append(f"numbered from {preset.get('start', 1)}, {preset.get('pad', 3)} digits")

    return " · ".join(parts) if parts else "Keeps every file as it is"
