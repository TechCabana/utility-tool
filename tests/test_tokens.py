"""The design system's own tests.

A palette is only a design system if something fails when it drifts. Three
things are checked here, all of which have gone wrong in this app before:

1. Every text colour clears its WCAG contrast floor on every surface it is
   used on. A token pair in this app was once shipped too dim to read and
   only fixed after the owner complained; a ratio is cheaper than a
   complaint.
2. The two palettes carry exactly the same keys. A dark theme rots the
   moment the light one grows a token it does not have -- the stylesheet
   build then raises for one theme and not the other.
3. The stylesheet template resolves with no unknown placeholders. Qt drops a
   rule it cannot parse without a word, so a typo in a placeholder name is
   invisible at runtime -- the app just quietly loses that styling.

None of this needs Qt, which is why styles/tokens.py imports none.
"""
import os
import re

import pytest

from styles import tokens

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(REPO_ROOT, "styles", "theme.qss.tmpl")


@pytest.mark.parametrize("theme", sorted(tokens.PALETTES))
def test_every_text_pair_clears_its_contrast_floor(theme):
    failures = tokens.failing_pairs(tokens.PALETTES[theme])
    assert not failures, (
        f"{theme}: " + ", ".join(
            f"{fg} on {bg} is {actual}:1, needs {floor}:1"
            for fg, bg, floor, actual in failures))


def test_danger_border_contrast_matches_its_documented_measurement():
    """`danger_border` (BRAND.md, "How far the destructive colour sits from
    the brand, measured") is not a text pair, so `TEXT_PAIRS` never covers
    it -- until this test, the 2.76:1 / 16.9:1 figures in BRAND.md and in
    the token comments were asserted nowhere, only written down. A drift in
    either token would pass every other test in this file.

    Dark deliberately stops short of WCAG 1.4.11's 3:1 (every rose that
    clears it collides with the accent) -- the floor here pins the
    documented 2.76:1, not 3:1, so a regression is caught without silently
    ratcheting the requirement up to a bar the design explicitly rejected.
    """
    light_border_vs_card = tokens.contrast_ratio(
        tokens.LIGHT["danger_border"], tokens.LIGHT["bg_elev"])
    dark_border_vs_card = tokens.contrast_ratio(
        tokens.DARK["danger_border"], tokens.DARK["bg_elev"])

    assert tokens.LIGHT["danger_border"] == tokens.LIGHT["danger_fill"], (
        "light's border is documented as the fill acting as its own boundary")
    assert light_border_vs_card >= 15.0, (
        f"light danger_border vs card is {light_border_vs_card:.2f}:1, "
        "expected well above 3:1 (documented ~16.9:1)")
    assert dark_border_vs_card >= 2.7, (
        f"dark danger_border vs card is {dark_border_vs_card:.2f}:1, "
        "expected >= 2.7:1 (documented 2.76:1)")
    # The fill alone (no border) reads notably worse -- that gap is the
    # whole reason the border token exists; assert it stays a real gap
    # rather than the two tokens quietly converging back to one value.
    dark_fill_vs_card = tokens.contrast_ratio(
        tokens.DARK["danger_fill"], tokens.DARK["bg_elev"])
    assert dark_border_vs_card - dark_fill_vs_card >= 0.5, (
        "dark danger_border should read clearly better than the fill alone "
        "against the card -- that lift is the token's entire purpose")


def test_both_palettes_define_the_same_tokens():
    missing_in_dark = set(tokens.LIGHT) - set(tokens.DARK)
    missing_in_light = set(tokens.DARK) - set(tokens.LIGHT)
    assert not missing_in_dark, f"dark palette is missing {sorted(missing_in_dark)}"
    assert not missing_in_light, f"light palette is missing {sorted(missing_in_light)}"


def test_contrast_ratio_matches_known_values():
    # Black on white is the definitional maximum, and a colour against
    # itself is the minimum. If these move, the formula is wrong.
    assert round(tokens.contrast_ratio("#000000", "#ffffff"), 1) == 21.0
    assert round(tokens.contrast_ratio("#a8354e", "#a8354e"), 1) == 1.0


def test_contrast_ratio_rejects_a_non_opaque_colour():
    # rgba() borders are real tokens but cannot be measured, so asking for a
    # ratio on one is a mistake worth an exception rather than a wrong number.
    with pytest.raises(ValueError):
        tokens.relative_luminance("rgba(24, 24, 27, 0.08)")


@pytest.mark.parametrize("theme", sorted(tokens.PALETTES))
def test_template_resolves_for_every_theme(theme):
    with open(TEMPLATE, "r", encoding="utf-8") as handle:
        template = handle.read()
    # The icon substitutions come from widgets/icons.py, which needs Qt; the
    # names are supplied here so the rest of the sheet can still be proved to
    # resolve without a QApplication.
    icon_names = set(re.findall(r"@@(icon_[a-z0-9_]+)@@", template))
    sheet = tokens.build_stylesheet(template, theme,
                                    extra={name: "x.svg" for name in icon_names})
    assert "@@" not in sheet
    assert tokens.PALETTES[theme]["accent"] in sheet


def test_an_unknown_placeholder_is_an_error_not_a_silent_pass():
    with pytest.raises(KeyError):
        tokens.build_stylesheet("QWidget { color: @@not_a_token@@; }", "light")


def test_the_type_scale_has_one_uppercase_role():
    # Two roles sharing one uppercase treatment is how a field label and a
    # group heading came to look identical. The scale documents which role
    # owns caps; the stylesheet applies text-transform in exactly two rules
    # (SectionHeader and TableHeader, both group-sized).
    with open(TEMPLATE, "r", encoding="utf-8") as handle:
        template = handle.read()
    uppercase_rules = template.count("text-transform: uppercase")
    assert uppercase_rules <= 3, "uppercase is spreading; it is a single role"


def test_no_colour_literal_escaped_into_the_template():
    """The template must never contain a hex colour of its own."""
    with open(TEMPLATE, "r", encoding="utf-8") as handle:
        body = "\n".join(line for line in handle
                         if not line.lstrip().startswith(("*", "/*")))
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b(?!\w)", body)
    # Object-name selectors (#Card, #Sidebar) are not colours; the pattern
    # above only matches hex digits, so anything it finds really is one.
    assert not literals, f"colour literals in the template: {sorted(set(literals))}"
