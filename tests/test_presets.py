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
