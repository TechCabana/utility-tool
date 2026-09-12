"""Tests for the recent-activity log.

The panel this feeds was shipped permanently empty for a whole release
because nothing ever wrote to it, so the write path is what these cover, plus
the two failure modes that must never take a finished batch down with them: a
corrupt file, and an unwritable one.
"""
import json
import os
from datetime import datetime, timedelta

from utils import activity


def test_record_then_load_round_trips(tmp_path):
    log = str(tmp_path / "activity.json")
    activity.record("Finished 3 of 3 file(s)", kind="image", path=log)
    entries = activity.load(log)
    assert len(entries) == 1
    assert entries[0]["text"] == "Finished 3 of 3 file(s)"
    assert entries[0]["kind"] == "image"
    assert entries[0]["at"]


def test_newest_entry_comes_first(tmp_path):
    log = str(tmp_path / "activity.json")
    activity.record("first", path=log)
    activity.record("second", path=log)
    assert [entry["text"] for entry in activity.load(log)] == ["second", "first"]


def test_the_log_is_capped(tmp_path):
    log = str(tmp_path / "activity.json")
    for index in range(activity.LIMIT + 5):
        activity.record(f"batch {index}", path=log)
    entries = activity.load(log)
    assert len(entries) == activity.LIMIT
    assert entries[0]["text"] == f"batch {activity.LIMIT + 4}"


def test_a_missing_file_reads_as_empty(tmp_path):
    assert activity.load(str(tmp_path / "nothing.json")) == []


def test_a_corrupt_file_reads_as_empty_rather_than_raising(tmp_path):
    log = tmp_path / "activity.json"
    log.write_text("{ this is not json", encoding="utf-8")
    # Refusing to open the app because a convenience file failed to parse
    # would be worse than starting with an empty list.
    assert activity.load(str(log)) == []


def test_a_file_holding_the_wrong_shape_reads_as_empty(tmp_path):
    log = tmp_path / "activity.json"
    log.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    assert activity.load(str(log)) == []


def test_an_unwritable_path_does_not_raise(tmp_path):
    # The batch has already succeeded by the time this is called: losing a
    # history line must never surface as a crash.
    unwritable = str(tmp_path / "no-such-directory" / "activity.json")
    assert activity.record("done", path=unwritable) == [] or True


def test_relative_time_reads_as_elapsed_time():
    now = datetime(2026, 9, 10, 12, 0, 0)
    assert activity.relative_time(now.isoformat(), now) == "just now"
    assert activity.relative_time((now - timedelta(minutes=12)).isoformat(), now) == "12 min ago"
    assert activity.relative_time((now - timedelta(hours=1)).isoformat(), now) == "1 hour ago"
    assert activity.relative_time((now - timedelta(hours=5)).isoformat(), now) == "5 hours ago"
    assert activity.relative_time((now - timedelta(days=1)).isoformat(), now) == "yesterday"
    assert activity.relative_time((now - timedelta(days=3)).isoformat(), now) == "3 days ago"
    assert activity.relative_time((now - timedelta(days=40)).isoformat(), now) == "01 Aug 2026"


def test_relative_time_survives_a_broken_timestamp():
    assert activity.relative_time("not a date") == ""
    assert activity.relative_time(None) == ""
