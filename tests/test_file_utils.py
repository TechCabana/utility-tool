# tests/test_file_utils.py
# Covers utils/file_utils.py: the pattern-based naming engine, the batch
# ordering that lets a rename batch collide with itself and still complete,
# Move/Copy/Delete, and resolve_conflict_path's conflict policies. Only the
# policy resolution logic is tested here -- "ask" is a UI-thread concern
# resolved by tabs/file_tab.py before these functions are ever called, so it
# has no testable surface in utils/. Uses real temp files/dirs (tmp_path), no
# filesystem mocking.
import datetime
import os

import pytest

from utils.file_utils import (
    apply_regex,
    format_date_token,
    build_new_name,
    apply_renames,
    order_renames,
    pending_conflicts,
    rename_file,
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
# rename_file -- a rename onto a pre-existing file obeys the conflict policy
# ---------------------------------------------------------------------------

def test_rename_file_no_conflict(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello")
    dest = tmp_path / "renamed.txt"
    status, path = rename_file(str(src), str(dest))
    assert status == "done"
    assert path == str(dest)
    assert not src.exists()
    assert dest.read_text() == "hello"


def test_rename_file_conflict_skip(tmp_path):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new")
    dest.write_text("old")
    status, path = rename_file(str(src), str(dest), conflict_policy="skip")
    assert status == "skipped"
    assert path == str(dest)
    assert src.read_text() == "new"  # never renamed
    assert dest.read_text() == "old"  # and the existing file survives


def test_rename_file_conflict_keep_both(tmp_path):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new")
    dest.write_text("old")
    status, path = rename_file(str(src), str(dest), conflict_policy="keep_both")
    assert status == "done"
    assert path == str(tmp_path / "dest (1).txt")
    assert (tmp_path / "dest (1).txt").read_text() == "new"
    assert dest.read_text() == "old"  # original untouched
    assert not src.exists()


def test_rename_file_conflict_overwrite(tmp_path):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new")
    dest.write_text("old")
    status, path = rename_file(str(src), str(dest), conflict_policy="overwrite")
    assert status == "done"
    assert path == str(dest)
    assert not src.exists()
    assert dest.read_text() == "new"


def test_rename_file_same_path_is_not_a_conflict(tmp_path):
    # A pattern that produces the file's current name is a no-op rename, not
    # a collision with itself -- under "skip" it must still report "done" and
    # leave the file where it is, rather than skipping every unchanged name.
    src = tmp_path / "src.txt"
    src.write_text("content")
    status, path = rename_file(str(src), str(src), conflict_policy="skip")
    assert status == "done"
    assert path == str(src)
    assert src.read_text() == "content"


def test_rename_file_creates_missing_destination_directory(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("content")
    dest = tmp_path / "nested" / "dest.txt"
    status, path = rename_file(str(src), str(dest))
    assert status == "done"
    assert dest.read_text() == "content"


def test_apply_renames_passes_the_policy_through(tmp_path):
    # apply_renames is a thin loop over rename_file, so the policy it is
    # given has to reach each pair -- and the per-file statuses come back.
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")
    dest_a, dest_b = tmp_path / "x.txt", tmp_path / "y.txt"
    dest_a.write_text("existing")  # only a's target is occupied
    results = apply_renames([str(a), str(b)], [str(dest_a), str(dest_b)],
                            conflict_policy="skip")
    assert results == [("skipped", str(dest_a)), ("done", str(dest_b))]
    assert dest_a.read_text() == "existing"
    assert a.read_text() == "A"  # left alone by the skip
    assert dest_b.read_text() == "B"


def test_apply_renames_chain_completes_in_either_order_under_skip(tmp_path):
    # Was test_apply_renames_chain_is_order_dependent_under_skip, which pinned
    # the opposite result: a "shift" renumber -- a.txt -> b.txt,
    # b.txt -> c.txt -- used to run in list order, so the first pair landed on
    # a b.txt that had not moved yet. That was a real collision at that
    # instant, "skip" dropped the rename, and whether the batch worked at all
    # came down to which file the user happened to add first.
    #
    # order_renames (card 7gRxAGFc) runs the batch as a unit: b.txt -> c.txt
    # goes first, freeing b.txt, and both pairs land whichever way round they
    # were listed. The results still come back in the order the pairs were
    # given, not in the order they ran.
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A-content")
    b.write_text("B-content")
    c = tmp_path / "c.txt"

    results = apply_renames([str(a), str(b)], [str(b), str(c)], conflict_policy="skip")
    assert results == [("done", str(b)), ("done", str(c))]
    assert not a.exists()
    assert b.read_text() == "A-content"
    assert c.read_text() == "B-content"

    # The same chain listed the other way round: the same outcome.
    a2, b2 = tmp_path / "a2.txt", tmp_path / "b2.txt"
    a2.write_text("A2"); b2.write_text("B2")
    c2 = tmp_path / "c2.txt"
    results2 = apply_renames([str(b2), str(a2)], [str(c2), str(b2)], conflict_policy="skip")
    assert results2 == [("done", str(c2)), ("done", str(b2))]
    assert not a2.exists()
    assert b2.read_text() == "A2"
    assert c2.read_text() == "B2"


def test_apply_renames_swaps_two_files_under_skip(tmp_path):
    # A swap is a cycle: neither pair can go first, so one file is parked
    # under a temp name and lands at the end. The temp is an implementation
    # detail -- nothing but the two real names is left behind.
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")

    results = apply_renames([str(a), str(b)], [str(b), str(a)], conflict_policy="skip")
    assert results == [("done", str(b)), ("done", str(a))]
    assert a.read_text() == "B"
    assert b.read_text() == "A"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.txt", "b.txt"]


def test_apply_renames_rotates_a_three_file_cycle_under_skip(tmp_path):
    x, y, z = tmp_path / "x.txt", tmp_path / "y.txt", tmp_path / "z.txt"
    x.write_text("X")
    y.write_text("Y")
    z.write_text("Z")

    results = apply_renames([str(x), str(y), str(z)], [str(y), str(z), str(x)],
                            conflict_policy="skip")
    assert results == [("done", str(y)), ("done", str(z)), ("done", str(x))]
    assert y.read_text() == "X"
    assert z.read_text() == "Y"
    assert x.read_text() == "Z"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.txt", "y.txt", "z.txt"]


def test_apply_renames_never_clobbers_a_landed_file_when_restoring_a_park(tmp_path):
    # Found by adversarial review of card 7gRxAGFc, not by the card's own
    # tests. x -> a and b -> a share a target: b is the OTHER half of a
    # genuine 2-cycle with a (a -> b, b -> a), so a is parked under a temp
    # name while x lands there first. Once a's real step (the temp landing
    # on b) is skipped -- b.txt is occupied and "skip" is the policy -- the
    # cleanup used to restore the park with a bare os.replace(tmp, original),
    # which silently overwrote x's just-landed file: two different files
    # reported "done" while only one of them actually survived on disk.
    # "keep_both" for the restore step closes that: the parked file always
    # lands somewhere, under its own name or next to it, never on top of
    # another file this same call already reported successful.
    x, a, b = tmp_path / "x.txt", tmp_path / "a.txt", tmp_path / "b.txt"
    x.write_text("X-CONTENT")
    a.write_text("A-CONTENT")
    b.write_text("B-CONTENT")

    results = apply_renames([str(x), str(a), str(b)], [str(a), str(b), str(a)],
                            conflict_policy="skip")

    assert results[0] == ("done", str(a))
    # x's content must actually be at a.txt -- not silently replaced by the
    # parked file's restore.
    assert a.read_text() == "X-CONTENT"
    # b.txt was never touched: its own landing step was skipped too.
    assert b.read_text() == "B-CONTENT"
    # a.txt's ORIGINAL content is not gone -- it was parked and then could
    # not go back under its own name, so it must survive under another one.
    survivors = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert "A-CONTENT" in survivors.values(), (
        f"a.txt's original content was lost during the restore: {survivors}")


def test_apply_renames_chain_lands_but_a_pre_existing_target_still_skips(tmp_path):
    # Ordering resolves the batch against itself and nothing more. A target
    # occupied by a file this batch is NOT moving is still a conflict, and
    # still goes through the policy -- the silent overwrite card hq0Px4yy
    # fixed must not come back through the ordering.
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    d = tmp_path / "d.txt"
    occupied = tmp_path / "occupied.txt"
    a.write_text("A")
    b.write_text("B")
    d.write_text("D")
    occupied.write_text("NOT OURS")
    c = tmp_path / "c.txt"

    results = apply_renames([str(a), str(b), str(d)],
                            [str(b), str(c), str(occupied)],
                            conflict_policy="skip")
    assert results == [("done", str(b)), ("done", str(c)), ("skipped", str(occupied))]
    assert b.read_text() == "A"        # the chain still completes
    assert c.read_text() == "B"
    assert d.read_text() == "D"        # left where it was by the skip
    assert occupied.read_text() == "NOT OURS"


def test_apply_renames_without_a_chain_runs_in_list_order(tmp_path):
    # The ordinary case -- no pair's target is another pair's source -- must
    # be untouched by the ordering: same order, same results.
    first, second = tmp_path / "one.txt", tmp_path / "two.txt"
    first.write_text("1")
    second.write_text("2")
    pairs = [(str(first), str(tmp_path / "first.txt")),
             (str(second), str(tmp_path / "second.txt"))]

    assert order_renames(pairs) == [(0, pairs[0][0], pairs[0][1]),
                                    (1, pairs[1][0], pairs[1][1])]
    results = apply_renames([s for s, _ in pairs], [t for _, t in pairs],
                            conflict_policy="skip")
    assert results == [("done", pairs[0][1]), ("done", pairs[1][1])]
    assert (tmp_path / "first.txt").read_text() == "1"
    assert (tmp_path / "second.txt").read_text() == "2"


def test_num_numbering_follows_batch_position_not_execution_order(tmp_path):
    # build_new_name numbers a file by its position in the batch, and the
    # batch is only reordered afterwards, for execution. If the reordering
    # ever fed back into the numbering, {num} would silently renumber the
    # files whenever a batch happened to chain.
    #
    # Here it does chain: x.txt is listed first and wants "1.txt", which the
    # second file currently occupies, so execution runs them the other way
    # round -- and the numbers must not follow.
    existing_one = tmp_path / "1.txt"
    x = tmp_path / "x.txt"
    existing_one.write_text("ONE")
    x.write_text("X")

    paths = [str(x), str(existing_one)]
    targets = [build_new_name(p, pattern="{num}", prefix="", suffix="", idx=idx,
                              start=1, pad=1, date_source="now", regex_find="",
                              regex_replace="", case="none")[1]
               for idx, p in enumerate(paths)]
    assert targets == [str(tmp_path / "1.txt"), str(tmp_path / "2.txt")]
    # x.txt is listed first but has to run second: its target is still held.
    assert [i for i, _, _ in order_renames(list(zip(paths, targets)))] == [1, 0]

    results = apply_renames(paths, targets, conflict_policy="skip")
    assert results == [("done", targets[0]), ("done", targets[1])]
    assert (tmp_path / "1.txt").read_text() == "X"    # the first file listed
    assert (tmp_path / "2.txt").read_text() == "ONE"  # the second


# ---------------------------------------------------------------------------
# order_renames -- the execution order that makes a batch a unit
# ---------------------------------------------------------------------------

def test_order_renames_runs_a_chain_back_to_front(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")
    c = tmp_path / "c.txt"

    # Listed a -> b, b -> c; b -> c has to run first to free b.txt. The index
    # travels with each step so the caller can still report the row the file
    # was added on.
    assert order_renames([(str(a), str(b)), (str(b), str(c))]) == [
        (1, str(b), str(c)),
        (0, str(a), str(b)),
    ]


def test_order_renames_breaks_a_cycle_with_one_temp_step(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")

    steps = order_renames([(str(a), str(b)), (str(b), str(a))])
    # Three steps for two files: one of them is parked out of the way first
    # and lands on its real target last. A parked step is recognisable by its
    # target not being the pair's target, which is how a caller knows not to
    # report it.
    assert len(steps) == 3
    assert [i for i, _, _ in steps] == [0, 1, 0]
    assert steps[0][2] not in (str(a), str(b))       # parked under a temp name
    assert steps[1] == (1, str(b), str(a))
    assert steps[2] == (0, steps[0][2], str(b))      # the temp lands at the end
    assert not os.path.exists(steps[0][2])           # nothing was renamed yet


def test_order_renames_leaves_a_pre_existing_target_for_the_policy(tmp_path):
    # An occupied target that is NOT another pair's source is a real conflict
    # and must not be reordered around: the pair runs where it was, and
    # rename_file's policy decides what happens to it.
    src = tmp_path / "src.txt"
    occupied = tmp_path / "occupied.txt"
    src.write_text("S")
    occupied.write_text("O")

    assert order_renames([(str(src), str(occupied))]) == [(0, str(src), str(occupied))]


def test_order_renames_treats_a_no_op_rename_as_ready(tmp_path):
    # A pattern that produces the file's own name: the target exists and is
    # this batch's own source, which must not be read as waiting on itself.
    src = tmp_path / "src.txt"
    src.write_text("S")
    assert order_renames([(str(src), str(src))]) == [(0, str(src), str(src))]


def test_order_renames_of_an_empty_batch_is_empty():
    assert order_renames([]) == []


# ---------------------------------------------------------------------------
# pending_conflicts -- what the preview marks and the "Ask" pre-scan counts
# ---------------------------------------------------------------------------

def test_pending_conflicts_ignores_a_target_the_batch_is_vacating(tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("A")
    b.write_text("B")
    c = tmp_path / "c.txt"
    # b.txt is occupied, but by a file this batch is about to rename away.
    assert pending_conflicts([(str(a), str(b)), (str(b), str(c))]) == [False, False]


def test_pending_conflicts_reports_a_file_the_batch_does_not_touch(tmp_path):
    src = tmp_path / "src.txt"
    occupied = tmp_path / "occupied.txt"
    src.write_text("S")
    occupied.write_text("O")
    assert pending_conflicts([(str(src), str(occupied))]) == [True]


def test_pending_conflicts_does_not_count_a_file_against_itself(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("S")
    assert pending_conflicts([(str(src), str(src))]) == [False]


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
