# tests/test_file_utils.py
# Covers utils/file_utils.py: the pattern-based naming engine, Move/Copy/
# Delete, and resolve_conflict_path's conflict policies. Only the policy
# resolution logic is tested here -- "ask" is a UI-thread concern resolved
# by tabs/file_tab.py before these functions are ever called, so it has no
# testable surface in utils/. Uses real temp files/dirs (tmp_path), no
# filesystem mocking.
import datetime
import os

import pytest

from utils.file_utils import (
    apply_regex,
    format_date_token,
    build_new_name,
    apply_renames,
    resolve_conflict_path,
    move_file,
    copy_file,
    delete_file,
)


# ---------------------------------------------------------------------------
# apply_regex
# ---------------------------------------------------------------------------

def test_apply_regex_basic_substitution():
    assert apply_regex("Report (copy 2)", r" \(copy \d+\)", "") == "Report"


def test_apply_regex_empty_find_returns_stem_unchanged():
    assert apply_regex("myfile", "", "x") == "myfile"


def test_apply_regex_invalid_pattern_returns_stem_unchanged():
    # "[" is an unterminated character class -- re.error, must not raise
    assert apply_regex("myfile", "[", "x") == "myfile"


def test_apply_regex_replace_defaults_to_empty_string():
    assert apply_regex("a-b-c", "-", None) == "abc"


# ---------------------------------------------------------------------------
# format_date_token
# ---------------------------------------------------------------------------

def test_format_date_token_default_format():
    d = datetime.datetime(2026, 9, 8)
    assert format_date_token("{date}_report", d) == "{date}_report"  # no fmt spec -> untouched
    assert format_date_token("{date:%Y%m%d}_report", d) == "20260908_report"


def test_format_date_token_custom_strftime():
    d = datetime.datetime(2026, 1, 5)
    assert format_date_token("{date:%Y-%m}", d) == "2026-01"


def test_format_date_token_no_token_is_noop():
    d = datetime.datetime(2026, 1, 1)
    assert format_date_token("plain_name", d) == "plain_name"


# ---------------------------------------------------------------------------
# build_new_name -- the pattern-based naming engine
# ---------------------------------------------------------------------------

def test_build_new_name_basic_tokens(tmp_path):
    src = tmp_path / "photo.jpg"
    src.write_bytes(b"x")
    name, full_path = build_new_name(
        str(src), pattern="{name}_{num}", prefix="", suffix="", idx=0, start=1,
        pad=3, date_source="now", regex_find="", regex_replace="", case="none",
    )
    assert name == "photo_001.jpg"
    assert full_path == str(tmp_path / "photo_001.jpg")


def test_build_new_name_prefix_suffix_and_padding():
    src = "/some/dir/img.png"
    name, _ = build_new_name(
        src, pattern="{name}", prefix="IMG_", suffix="_final", idx=4, start=10,
        pad=4, date_source="now", regex_find="", regex_replace="", case="none",
    )
    assert name == "IMG_img_final.png"  # {num} not referenced in pattern -> not appended
    # confirm padding math directly through {num}
    name2, _ = build_new_name(
        src, pattern="{num}", prefix="", suffix="", idx=4, start=10,
        pad=4, date_source="now", regex_find="", regex_replace="", case="none",
    )
    assert name2 == "0014.png"


def test_build_new_name_date_token_uses_mtime_source(tmp_path):
    src = tmp_path / "f.txt"
    src.write_bytes(b"data")
    mtime = datetime.datetime(2020, 3, 4, 5, 6, 7).timestamp()
    os.utime(src, (mtime, mtime))
    name, _ = build_new_name(
        str(src), pattern="{date:%Y%m%d}_{name}", prefix="", suffix="", idx=0,
        start=1, pad=3, date_source="mtime", regex_find="", regex_replace="", case="none",
    )
    assert name == "20200304_f.txt"


def test_build_new_name_case_transforms():
    src = "/x/HELLO world.txt"
    for case, expected_stem in (("lower", "hello world"), ("upper", "HELLO WORLD"), ("title", "Hello World")):
        name, _ = build_new_name(
            src, pattern="{name}", prefix="", suffix="", idx=0, start=1, pad=3,
            date_source="now", regex_find="", regex_replace="", case=case,
        )
        assert name == f"{expected_stem}.txt"


def test_build_new_name_regex_applied_after_pattern_substitution():
    src = "/x/Report (copy 2).txt"
    name, _ = build_new_name(
        src, pattern="{name}", prefix="", suffix="", idx=0, start=1, pad=3,
        date_source="now", regex_find=r" \(copy \d+\)", regex_replace="", case="none",
    )
    assert name == "Report.txt"


def test_build_new_name_empty_pattern_with_prefix_suffix_only():
    src = "/x/file.txt"
    name, _ = build_new_name(
        src, pattern="", prefix="pre_", suffix="_post", idx=0, start=1, pad=3,
        date_source="now", regex_find="", regex_replace="", case="none",
    )
    assert name == "pre__post.txt"


# ---------------------------------------------------------------------------
# apply_renames
# ---------------------------------------------------------------------------

def test_apply_renames_moves_files_to_new_names(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")
    new_a = tmp_path / "a_renamed.txt"
    new_b = tmp_path / "b_renamed.txt"
    apply_renames([str(a), str(b)], [str(new_a), str(new_b)])
    assert not a.exists() and not b.exists()
    assert new_a.read_text() == "A"
    assert new_b.read_text() == "B"


def test_apply_renames_overwrites_existing_destination(tmp_path):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new")
    dest.write_text("old")
    apply_renames([str(src)], [str(dest)])
    assert not src.exists()
    assert dest.read_text() == "new"


def test_apply_renames_creates_missing_destination_directory(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("content")
    dest = tmp_path / "nested" / "dest.txt"
    apply_renames([str(src)], [str(dest)])
    assert dest.read_text() == "content"


# ---------------------------------------------------------------------------
# resolve_conflict_path -- the three utils-layer conflict policies
# ---------------------------------------------------------------------------

def test_resolve_conflict_path_skip_returns_none(tmp_path):
    dest = tmp_path / "f.txt"
    dest.write_text("x")
    assert resolve_conflict_path(str(dest), "skip") is None


def test_resolve_conflict_path_overwrite_returns_same_path(tmp_path):
    dest = tmp_path / "f.txt"
    dest.write_text("x")
    assert resolve_conflict_path(str(dest), "overwrite") == str(dest)


def test_resolve_conflict_path_unrecognized_policy_treated_as_overwrite(tmp_path):
    dest = tmp_path / "f.txt"
    dest.write_text("x")
    assert resolve_conflict_path(str(dest), "ask") == str(dest)


def test_resolve_conflict_path_keep_both_finds_first_free_suffix(tmp_path):
    dest = tmp_path / "f.txt"
    dest.write_text("x")
    (tmp_path / "f (1).txt").write_text("x")
    result = resolve_conflict_path(str(dest), "keep_both")
    assert result == str(tmp_path / "f (2).txt")


def test_resolve_conflict_path_keep_both_when_no_conflict_at_all():
    # dest_path itself is free -- the while loop's condition is false on the
    # first check, so the path returns unchanged (no " (1)" suffix appended
    # unless something actually occupies dest_path).
    result = resolve_conflict_path("/nonexistent/dir/f.txt", "keep_both")
    assert result == "/nonexistent/dir/f.txt"


# ---------------------------------------------------------------------------
# move_file / copy_file
# ---------------------------------------------------------------------------

def test_move_file_no_conflict(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello")
    dest_dir = tmp_path / "dest"
    status, path = move_file(str(src), str(dest_dir))
    assert status == "done"
    assert path == str(dest_dir / "src.txt")
    assert not src.exists()
    assert (dest_dir / "src.txt").read_text() == "hello"


def test_move_file_conflict_skip(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("new")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "src.txt").write_text("old")
    status, path = move_file(str(src), str(dest_dir), conflict_policy="skip")
    assert status == "skipped"
    assert src.exists()  # never moved
    assert (dest_dir / "src.txt").read_text() == "old"


def test_move_file_conflict_keep_both(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("new")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "src.txt").write_text("old")
    status, path = move_file(str(src), str(dest_dir), conflict_policy="keep_both")
    assert status == "done"
    assert path == str(dest_dir / "src (1).txt")
    assert (dest_dir / "src (1).txt").read_text() == "new"
    assert (dest_dir / "src.txt").read_text() == "old"  # original untouched


def test_move_file_conflict_overwrite(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("new")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "src.txt").write_text("old")
    status, path = move_file(str(src), str(dest_dir), conflict_policy="overwrite")
    assert status == "done"
    assert (dest_dir / "src.txt").read_text() == "new"


def test_copy_file_leaves_source_intact(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello")
    dest_dir = tmp_path / "dest"
    status, path = copy_file(str(src), str(dest_dir))
    assert status == "done"
    assert src.exists()  # copy, not move
    assert (dest_dir / "src.txt").read_text() == "hello"


def test_copy_file_directory_uses_copytree(tmp_path):
    src_dir = tmp_path / "srcdir"
    src_dir.mkdir()
    (src_dir / "inner.txt").write_text("nested")
    dest_parent = tmp_path / "dest"
    status, path = copy_file(str(src_dir), str(dest_parent))
    assert status == "done"
    assert (dest_parent / "srcdir" / "inner.txt").read_text() == "nested"


def test_copy_file_conflict_skip(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("new")
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    (dest_dir / "src.txt").write_text("old")
    status, path = copy_file(str(src), str(dest_dir), conflict_policy="skip")
    assert status == "skipped"
    assert (dest_dir / "src.txt").read_text() == "old"


# ---------------------------------------------------------------------------
# delete_file -- send2trash, real recycle bin
# ---------------------------------------------------------------------------

def test_delete_file_removes_source(tmp_path):
    target = tmp_path / "to_delete.txt"
    target.write_text("bye")
    status, path = delete_file(str(target))
    assert status == "done"
    assert path == str(target)
    assert not target.exists()
