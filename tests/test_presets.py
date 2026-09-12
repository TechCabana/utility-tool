# tests/test_presets.py
# Bonus coverage for utils/presets.py (same Qt-free logic layer, extended
# significantly in the last batch). Monkeypatches PRESETS_FILE to a tmp_path
# location so tests never touch the real user's app-data presets.json.
import json

import pytest

from utils import presets


@pytest.fixture(autouse=True)
def isolated_presets_file(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "PRESETS_FILE", str(tmp_path / "presets.json"))
    yield


def test_default_has_expected_preset_kinds_and_counts():
    assert len(presets.DEFAULT["image"]) == 6
    assert len(presets.DEFAULT["file"]) == 5


def test_load_all_writes_default_on_first_run(tmp_path):
    assert not (tmp_path / "presets.json").exists()
    data = presets.load_all()
    assert (tmp_path / "presets.json").exists()
    assert data == presets.DEFAULT


def test_save_and_load_round_trip():
    custom = {"image": [], "file": []}
    presets.save_all(custom)
    assert presets.load_all() == custom


def test_load_all_falls_back_to_default_on_corrupt_json():
    with open(presets.PRESETS_FILE, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert presets.load_all() == presets.DEFAULT


def test_add_image_preset_appends():
    presets.save_all({"image": [], "file": []})
    presets.add_image_preset({"name": "Custom"})
    assert presets.get_image_presets() == [{"name": "Custom"}]


def test_add_file_preset_appends():
    presets.save_all({"image": [], "file": []})
    presets.add_file_preset({"name": "Custom File"})
    assert presets.get_file_presets() == [{"name": "Custom File"}]


def test_delete_preset_removes_correct_index():
    presets.save_all({"image": [{"name": "a"}, {"name": "b"}, {"name": "c"}], "file": []})
    presets.delete_preset("image", 1)
    assert [p["name"] for p in presets.get_image_presets()] == ["a", "c"]


def test_delete_preset_out_of_range_is_noop():
    presets.save_all({"image": [{"name": "a"}], "file": []})
    presets.delete_preset("image", 5)
    assert presets.get_image_presets() == [{"name": "a"}]


def test_update_preset_replaces_value():
    presets.save_all({"image": [{"name": "old"}], "file": []})
    presets.update_preset("image", 0, {"name": "new"})
    assert presets.get_image_presets() == [{"name": "new"}]


def test_image_naming_defaults_present_on_every_default_image_preset():
    for p in presets.DEFAULT["image"]:
        assert p["pattern"] == "{name}" or "pattern" in p
        for key in ("prefix", "suffix", "start", "pad", "case", "date_source"):
            assert key in p


# ---------------------------------------------------------------------------
# describe_preset: the one-line summary shown on each row in Settings
# ---------------------------------------------------------------------------
# The preset list showed names only, so "Web Upload" and "Email Attachment"
# were indistinguishable without applying one. These pin the wording so a
# summary can never silently become empty or misleading.

def test_describe_image_preset_names_format_size_and_quality():
    preset = presets.DEFAULT["image"][0]  # Web Upload
    summary = presets.describe_preset("image", preset)
    assert "JPEG" in summary
    assert "1920px long edge" in summary
    assert "quality 85" in summary


def test_describe_image_preset_hides_quality_for_a_lossless_format():
    preset = presets._image_preset("Archive", "PNG", 100, "Original")
    summary = presets.describe_preset("image", preset)
    assert "PNG" in summary
    assert "quality" not in summary


def test_describe_image_preset_says_when_the_format_is_untouched():
    preset = presets._image_preset("As is", "ORIGINAL", 85, "Original")
    assert presets.describe_preset("image", preset).startswith("Keep format")


def test_describe_file_preset_reports_numbering():
    preset = presets.DEFAULT["file"][1]  # Sequential Numbering
    summary = presets.describe_preset("file", preset)
    assert "{name}_{num}" in summary
    assert "numbered from 1" in summary
    assert "3 digits" in summary


def test_describe_file_preset_reports_a_regex_deletion():
    preset = presets.DEFAULT["file"][2]  # Strip "copy N" Suffix
    summary = presets.describe_preset("file", preset)
    assert "removed" in summary


def test_describe_preset_never_returns_an_empty_row():
    # A row with no summary reads as a rendering bug; a preset that really
    # changes nothing should say so.
    assert presets.describe_preset("file", {"name": "Untouched"}) == \
        "Keeps every file as it is"
