# widgets/icons.py
"""The app's one icon vocabulary: bundled Lucide SVGs, recoloured on load.

Why bundled files and not a font or a CDN
-----------------------------------------
The app makes no network calls (CLAUDE.md §4), so an icon font or a remote
sprite is not an option. Every glyph is a 24x24, 1.5px-stroke SVG in
`assets/icons/`, licensed ISC (`assets/icons/LICENSE-lucide.txt`), which is
one consistent family across every screen rather than a per-screen mix.

Why recolouring happens here
----------------------------
A Lucide SVG paints with `stroke="currentColor"`, which means nothing to Qt:
loaded as-is it renders black on both themes. `icon()` substitutes a real
colour into the markup before handing it to Qt, so the same file serves the
light theme, the dark theme, a muted row action and an accent-filled button.

Two consumers, two shapes:
  * `icon(name, colour)` -> QIcon, for widgets (buttons, nav, empty states)
  * `qss_icon_path(name, colour)` -> str, for the handful of places QSS needs
    a file it can point `image: url(...)` at (combo/spin arrows, the checked
    checkbox mark). Those are written into a per-theme cache directory, since
    QSS cannot take an in-memory pixmap.
"""
from __future__ import annotations

import hashlib
import os
import re

from PySide6 import QtCore, QtGui

from utils.presets import app_dir

ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "icons")
CACHE_DIR = os.path.join(app_dir(), "icon-cache")

# Lucide's default 2px stroke is heavy next to Manrope at 13-14px; 1.6px is
# the weight the design system specifies for a 20px icon.
STROKE_WIDTH = "1.6"

_STROKE = re.compile(r'stroke="currentColor"')
_WIDTH = re.compile(r'stroke-width="[^"]*"')


def _svg_source(name: str, colour: str) -> bytes:
    path = os.path.join(ICON_DIR, f"{name}.svg")
    if not os.path.isfile(path):
        # Loud rather than silent: a missing glyph renders as an empty box,
        # which reads as a layout bug rather than as a missing file.
        raise FileNotFoundError(f"no icon named {name!r} in {ICON_DIR}")
    with open(path, "r", encoding="utf-8") as handle:
        markup = handle.read()
    markup = _STROKE.sub(f'stroke="{colour}"', markup)
    markup = _WIDTH.sub(f'stroke-width="{STROKE_WIDTH}"', markup)
    return markup.encode("utf-8")


def pixmap(name: str, colour: str, size: int = 20) -> QtGui.QPixmap:
    """Render one icon at one size, device-pixel-ratio aware."""
    from PySide6 import QtSvg  # imported lazily: only icon users need QtSvg

    ratio = QtGui.QGuiApplication.primaryScreen().devicePixelRatio() if QtGui.QGuiApplication.instance() else 1.0
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(_svg_source(name, colour)))
    image = QtGui.QPixmap(int(size * ratio), int(size * ratio))
    image.setDevicePixelRatio(ratio)
    image.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(image)
    renderer.render(painter)
    painter.end()
    return image


def icon(name: str, colour: str, size: int = 20) -> QtGui.QIcon:
    """A QIcon for `name`, stroked in `colour`."""
    return QtGui.QIcon(pixmap(name, colour, size))


def qss_icon_path(name: str, colour: str) -> str:
    """Write a recoloured copy to the cache and return a QSS-safe path.

    QSS cannot reference an in-memory pixmap, so the few glyphs the sheet
    draws itself have to exist as files. The name is hashed on the colour so
    a theme switch cannot serve the other theme's copy, and the path is
    returned with forward slashes because a Windows backslash inside a QSS
    `url()` is read as an escape.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    digest = hashlib.sha1(f"{name}:{colour}:{STROKE_WIDTH}".encode("utf-8")).hexdigest()[:10]
    path = os.path.join(CACHE_DIR, f"{name}-{digest}.svg")
    if not os.path.isfile(path):
        with open(path, "wb") as handle:
            handle.write(_svg_source(name, colour))
    return path.replace("\\", "/")


def stylesheet_icons(palette: dict) -> dict:
    """The `@@icon_*@@` substitutions `styles/theme.qss.tmpl` expects."""
    return {
        "icon_chevron_down": qss_icon_path("chevron-down", palette["text_muted"]),
        "icon_chevron_down_disabled": qss_icon_path("chevron-down", palette["text_disabled"]),
        "icon_chevron_up": qss_icon_path("chevron-up", palette["text_muted"]),
        "icon_check": qss_icon_path("check", palette["accent_text"]),
    }
