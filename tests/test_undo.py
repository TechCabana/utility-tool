"""Tests for reversing the last Move, Copy or Rename batch.

The owner's decision was all-or-nothing: if any file cannot be put back, none
of them are. So the cases that matter most are the refusals, and the thing
each of them has to prove is not just that `ok` is False but that **nothing
on disk moved** - a refusal that has already shuffled half the batch is the
exact failure this design exists to prevent.
"""
import os

import pytest

from utils import undo


def make_batch(tmp_path, count=3):
    """A finished rename batch: `a.txt` -> `renamed_a.txt`, and so on."""
    pairs = []
    for index in range(count):
        original = tmp_path / f"file{index}.txt"
        final = tmp_path / f"renamed_file{index}.txt"
        final.write_text(f"contents {index}", encoding="utf-8")
        pairs.append((str(original), str(final)))
    return undo.record_batch("rename", pairs)


def test_a_clean_reverse_puts_every_file_back(tmp_path):
    record = make_batch(tmp_path)
    result = undo.reverse(record)
    assert result["ok"] is True
    assert result["reversed"] == 3
    for original, final in record.pairs:
        assert os.path.exists(original)
        assert not os.path.exists(final)


def test_a_copy_batch_removes_the_copies_and_keeps_the_originals(tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("keep me", encoding="utf-8")
    copy = tmp_path / "out" / "original.txt"
    copy.parent.mkdir()
    copy.write_text("keep me", encoding="utf-8")

    record = undo.record_batch("copy", [(str(source), str(copy))])
    result = undo.reverse(record)
    assert result["ok"] is True
    assert source.exists(), "undoing a copy must never touch the original"
    assert not copy.exists()


def test_a_reversal_is_refused_when_a_file_changed_since_the_batch(tmp_path):
    record = make_batch(tmp_path)
    changed = record.pairs[1][1]
    with open(changed, "w", encoding="utf-8") as handle:
        handle.write("someone edited this afterwards")

    result = undo.reverse(record)
    assert result["ok"] is False
    assert "has been changed" in result["message"]
    # The point of all-or-nothing: the two untouched files stayed put.
    for _, final in record.pairs:
        assert os.path.exists(final), "a refused reversal must move nothing"


def test_a_reversal_is_refused_when_a_file_has_gone(tmp_path):
    record = make_batch(tmp_path)
    os.remove(record.pairs[0][1])

    result = undo.reverse(record)
    assert result["ok"] is False
    assert "no longer there" in result["message"]
    for _, final in record.pairs[1:]:
        assert os.path.exists(final)


def test_a_reversal_is_refused_when_something_occupies_the_original_name(tmp_path):
    record = make_batch(tmp_path)
    # A file this app never touched now sits where the original came from.
    blocker = record.pairs[2][0]
    with open(blocker, "w", encoding="utf-8") as handle:
        handle.write("unrelated file")

    result = undo.reverse(record)
    assert result["ok"] is False
    assert "occupies" in result["message"]
    assert open(blocker, encoding="utf-8").read() == "unrelated file"
    for _, final in record.pairs:
        assert os.path.exists(final)


def test_a_second_reversal_is_refused_rather_than_repeated(tmp_path):
    record = make_batch(tmp_path)
    assert undo.reverse(record)["ok"] is True

    # The files are back where they started, so the final paths are gone and
    # reversing again would be meaningless. It must refuse, not raise, and
    # not go looking for something else to move.
    again = undo.reverse(record)
    assert again["ok"] is False
    assert again["reversed"] == 0
    for original, _ in record.pairs:
        assert os.path.exists(original), "the first reversal's result must stand"


def test_the_message_names_the_files_but_does_not_run_on(tmp_path):
    record = make_batch(tmp_path, count=6)
    for _, final in record.pairs:
        with open(final, "w", encoding="utf-8") as handle:
            handle.write("all changed")

    message = undo.reverse(record)["message"]
    # Three named, the rest counted: a refusal has to be readable in a status
    # line, not a wall listing two hundred files.
    assert "and 3 more" in message


def test_nothing_is_recorded_for_an_operation_that_cannot_be_undone(tmp_path):
    # Delete already goes to the Recycle Bin, where the OS provides the undo.
    assert undo.record_batch("delete", [("a", "b")]) is None
    assert undo.record_batch("rename", []) is None


def test_reversing_an_empty_or_missing_record_is_a_refusal_not_a_crash():
    assert undo.reverse(None)["ok"] is False
    assert undo.reverse(undo.BatchRecord("rename"))["ok"] is False


def test_a_fingerprint_of_a_missing_file_is_distinct(tmp_path):
    present = tmp_path / "here.txt"
    present.write_text("x", encoding="utf-8")
    assert undo.fingerprint(str(present)) != undo.fingerprint(str(tmp_path / "gone.txt"))


@pytest.mark.parametrize("operation", ["rename", "move"])
def test_a_move_or_rename_recreates_a_missing_parent_directory(tmp_path, operation):
    # Undoing a move back into a folder the user has since deleted should
    # recreate it rather than fail the whole batch.
    original = tmp_path / "gone-folder" / "file.txt"
    final = tmp_path / "elsewhere.txt"
    final.write_text("data", encoding="utf-8")

    record = undo.record_batch(operation, [(str(original), str(final))])
    result = undo.reverse(record)
    assert result["ok"] is True
    assert original.exists()
