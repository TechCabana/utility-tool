"""The single source of truth for every colour, size and duration in the UI.

Why this file exists
--------------------
QSS has no variables, so the previous `theme.qss` wrote every value out
literally and carried a comment telling the next person to change `#a8354e`
in all 31 places by hand. That is not a design system, it is a search and
replace waiting to go wrong, and it made a second theme impossible.

`styles/theme.qss.tmpl` is now a template whose `@@token@@` placeholders are
substituted from the dictionaries below (see `build_stylesheet`). One palette
drives both themes; adding a colour means adding it here and nowhere else.

Nothing in this module imports Qt, so the palette and its contrast rules can
be tested without a QApplication (tests/test_tokens.py).
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# Typography
# --------------------------------------------------------------------------
# Two families, both bundled as static .ttf in assets/fonts/ and registered
# before the sheet is applied. They are named here rather than in the
# template so a family swap is one edit.
#
# Manrope carries the UI voice: a slightly rounded geometric grotesk with
# real weight range (400-800), so hierarchy can come from weight rather than
# from size alone. Geist Mono carries data -- sizes, counts, percentages,
# paths, timestamps -- which a proportional face renders with drifting
# columns.
#
# Qt does not apply a variable font's weight axis (measured on 6.7.2: every
# requested weight of Manrope[wght].ttf rendered at an identical advance
# width), so the five Manrope weights ship as static instances cut from the
# google/fonts OFL master. Do not replace them with the variable file.
FONT_UI = "Manrope"
FONT_MONO = "Geist Mono"

FALLBACK = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
FALLBACK_MONO = '"Cascadia Mono", Consolas, "SF Mono", Menlo, monospace'

# Role -> (px, weight, letter-spacing px). One row per role; a component that
# wants a size not in this table is a component that has not decided what it
# is. `caps` roles are the only uppercase text in the app.
TYPE_SCALE = {
    "display":  (26, 800, -0.5),   # page title, one per screen
    "title":    (16, 700, -0.2),   # card / section title
    "group":    (12, 700, 0.8),    # uppercase group heading over a hairline
    "body":     (14, 400, 0.0),
    "label":    (13, 500, 0.0),    # field label - sentence case, no colon
    "caption":  (13, 400, 0.0),    # hint, metadata, secondary prose
    "button":   (14, 600, 0.0),
    "data":     (13, 500, 0.0),    # Geist Mono, tabular figures
    "nav":      (14, 600, -0.1),
}

# --------------------------------------------------------------------------
# Geometry and motion
# --------------------------------------------------------------------------
RADIUS_S = 6      # controls
RADIUS_M = 10     # panels, cards, drop zones
RADIUS_PILL = 999

SPACE_FIELD = 10  # between fields inside one group
SPACE_BLOCK = 16  # between top-level blocks on a page
SPACE_PAGE = 20   # page gutter

# Motion. QSS cannot animate, so these are consumed by QPropertyAnimation.
# Both are wrapped by `motion_enabled()` in widgets/motion.py, which honours
# the OS "reduce motion" setting.
DURATION_STATE = 140   # hover/press/selection feedback
DURATION_VIEW = 320    # switching pages

# --------------------------------------------------------------------------
# Palettes
# --------------------------------------------------------------------------
# Every key must exist in both palettes -- tests/test_tokens.py fails the
# build if one theme grows a token the other lacks, which is how a dark theme
# silently rots.
#
# Role discipline, enforced by review not by code:
#   accent*      interaction only  - focus, active nav, primary button, selection
#   status*      meaning only      - success / warning / error
#   data*        measurement only  - usage bars, progress fills
# A measurement bar never uses the accent: a 44%-full drive drawn in the same
# red as the primary button reads as an alarm.

LIGHT = {
    "name": "light",

    "bg":            "#fafafa",
    "bg_elev":       "#ffffff",
    "bg_elev_2":     "#f0f0f1",
    "bg_sunken":     "#f4f4f5",

    "border":        "rgba(24, 24, 27, 0.08)",
    "border_strong": "rgba(24, 24, 27, 0.16)",
    "border_hover":  "rgba(24, 24, 27, 0.28)",

    "text":          "#18181b",
    "text_muted":    "#52525b",
    "text_faint":    "#6b6b74",
    "text_disabled": "#a1a1aa",

    "accent":        "#a8354e",
    "accent_hover":  "#bd3c58",
    "accent_press":  "#8e2c42",
    "accent_text":   "#ffffff",
    "accent_soft":   "rgba(168, 53, 78, 0.08)",
    "accent_soft_2": "rgba(168, 53, 78, 0.14)",

    "success":       "#17724c",
    "warn":          "#8a5709",
    "error":         "#b3352d",
    # A destructive *button* fill is a different token from destructive
    # *text*: on a dark ground the red that reads well as a status word is
    # too light to carry white label text, and darkening the status word to
    # suit the button would make it hard to read as text. Two roles, two
    # tokens, both asserted in tests/test_tokens.py.
    "danger_fill":   "#b3352d",
    "danger_hover":  "#c44039",
    "danger_press":  "#992c25",
    "danger_text":   "#ffffff",

    "data_track":    "#e6e6e9",
    "data_fill":     "#6b6b74",
    "data_warn":     "#8a5709",
    "data_critical": "#b3352d",

    "scrollbar":     "rgba(24, 24, 27, 0.22)",
    "scrollbar_hover": "rgba(24, 24, 27, 0.38)",

    "shadow_alpha":  28,   # QGraphicsDropShadowEffect colour alpha, 0-255
    "shadow_rgb":    (24, 24, 27),
}

DARK = {
    "name": "dark",

    "bg":            "#131316",
    "bg_elev":       "#1b1b1f",
    "bg_elev_2":     "#26262c",
    "bg_sunken":     "#0e0e10",

    "border":        "rgba(244, 244, 245, 0.10)",
    "border_strong": "rgba(244, 244, 245, 0.18)",
    "border_hover":  "rgba(244, 244, 245, 0.32)",

    "text":          "#f4f4f5",
    "text_muted":    "#b4b4bd",
    "text_faint":    "#9a9aa4",
    "text_disabled": "#5c5c66",

    # The light accent is too dark to sit on a #131316 ground, so the hue is
    # kept and the lightness raised until white label text clears AA.
    "accent":        "#c9445f",
    "accent_hover":  "#d9556f",
    "accent_press":  "#ad3850",
    "accent_text":   "#ffffff",
    "accent_soft":   "rgba(201, 68, 95, 0.16)",
    "accent_soft_2": "rgba(201, 68, 95, 0.26)",

    "success":       "#4bbd8a",
    "warn":          "#d9a441",
    "error":         "#e8695f",
    "danger_fill":   "#a83028",
    "danger_hover":  "#c03a31",
    "danger_press":  "#8f2721",
    "danger_text":   "#ffffff",

    "data_track":    "#2e2e35",
    "data_fill":     "#8f8f9a",
    "data_warn":     "#d9a441",
    "data_critical": "#e8695f",

    "scrollbar":     "rgba(244, 244, 245, 0.20)",
    "scrollbar_hover": "rgba(244, 244, 245, 0.36)",

    "shadow_alpha":  90,
    "shadow_rgb":    (0, 0, 0),
}

PALETTES = {"light": LIGHT, "dark": DARK}


# --------------------------------------------------------------------------
# Contrast
# --------------------------------------------------------------------------
# The previous theme's own notes record a token pair that "read fine on a
# monitor calibrated bright but failed in normal use", brightened only after
# the owner complained. A ratio is cheaper than a complaint, so every
# text-on-surface pair in the app is asserted in tests/test_tokens.py against
# these functions rather than eyeballed.

def _srgb_to_linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    """WCAG 2.1 relative luminance of an opaque #rrggbb colour."""
    value = hex_colour.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"expected an opaque #rrggbb colour, got {hex_colour!r}")
    r, g, b = (int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * _srgb_to_linear(r)
            + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b))


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG 2.1 contrast ratio between two opaque colours, 1.0 to 21.0."""
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# Pairs every theme must clear. AA body text is 4.5:1; large/bold text
# (>=18.66px bold, which covers `display` and `title`) is 3.0:1. Disabled
# text is exempt under WCAG 1.4.3 and is deliberately absent from this list.
AA_BODY = 4.5
AA_LARGE = 3.0

TEXT_PAIRS = (
    ("text", "bg", AA_BODY),
    ("text", "bg_elev", AA_BODY),
    ("text", "bg_elev_2", AA_BODY),
    ("text_muted", "bg", AA_BODY),
    ("text_muted", "bg_elev", AA_BODY),
    ("text_faint", "bg", AA_BODY),
    ("text_faint", "bg_elev", AA_BODY),
    ("accent_text", "accent", AA_BODY),
    ("accent_text", "accent_press", AA_BODY),
    ("success", "bg_elev", AA_BODY),
    ("warn", "bg_elev", AA_BODY),
    ("error", "bg_elev", AA_BODY),
    ("danger_text", "danger_fill", AA_BODY),
    ("danger_text", "danger_press", AA_BODY),
    ("data_fill", "bg_elev", AA_LARGE),      # a bar, not text
    ("data_warn", "bg_elev", AA_LARGE),
    ("data_critical", "bg_elev", AA_LARGE),
)


def failing_pairs(palette: dict) -> list:
    """Return [(fg, bg, required, actual)] for every pair below its floor."""
    failures = []
    for fg, bg, floor in TEXT_PAIRS:
        ratio = contrast_ratio(palette[fg], palette[bg])
        if ratio < floor:
            failures.append((fg, bg, floor, round(ratio, 2)))
    return failures


# --------------------------------------------------------------------------
# Stylesheet build
# --------------------------------------------------------------------------
# `@@name@@` was chosen over str.format because QSS is made of braces --
# `str.format` on a stylesheet raises on the first rule it meets.
import re as _re

_PLACEHOLDER = _re.compile(r"@@([a-z0-9_]+)@@")


def substitutions(palette: dict) -> dict:
    """Every value the template may reference, flattened to strings."""
    values = {key: str(value) for key, value in palette.items()
              if key not in ("shadow_rgb",)}
    values["font_ui"] = FONT_UI
    values["font_mono"] = FONT_MONO
    values["fallback"] = FALLBACK
    values["fallback_mono"] = FALLBACK_MONO
    values["radius_s"] = f"{RADIUS_S}px"
    values["radius_m"] = f"{RADIUS_M}px"
    for role, (size, weight, tracking) in TYPE_SCALE.items():
        values[f"size_{role}"] = f"{size}px"
        values[f"weight_{role}"] = str(weight)
        values[f"track_{role}"] = f"{tracking}px"
    return values


def build_stylesheet(template: str, theme: str = "light", extra: dict | None = None) -> str:
    """Substitute @@token@@ in `template` for the named theme's values.

    An unknown token raises rather than silently leaving `@@typo@@` in the
    sheet, where Qt would drop the whole rule without a word -- the same
    silent-failure class as naming a font that is not installed.
    """
    palette = PALETTES[theme]
    values = substitutions(palette)
    # `extra` carries values that cannot be known without Qt -- currently the
    # recoloured icon files QSS points `image: url(...)` at, written per
    # theme by widgets/icons.py. Keeping them out of this module is what lets
    # the palette and its contrast rules be tested with no QApplication.
    values.update(extra or {})

    missing = []

    def replace(match):
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return values[key]

    sheet = _PLACEHOLDER.sub(replace, template)
    if missing:
        raise KeyError(f"unknown style token(s): {sorted(set(missing))}")
    return sheet
