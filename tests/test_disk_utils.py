# tests/test_disk_utils.py
# Covers utils/disk_utils.py: byte formatting, the recursive folder walk, the
# sorted top-level breakdown and the Cleanup quick scan the Disk tab's QThread
# workers call. Uses real temp trees (tmp_path), no filesystem mocking -- the
# whole point of these walks is how they behave against a real directory.
import hashlib
import os
import subprocess
import sys
import time

import pytest
from PIL import Image

from utils.disk_utils import (
    IMAGE_HASH_BITS,
    IMAGE_HASH_MAX_DISTANCE,
    LARGE_FILE_BYTES,
    browser_cache_dirs,
    cleanup_roots,
    cleanup_scan,
    duplicate_roots,
    empty_folders,
    file_sha256,
    find_duplicate_files,
    find_duplicate_images,
    find_files,
    folder_size,
    hamming_distance,
    human_size,
    image_dhash,
    recycle_bin_stats,
    similarity_percent,
    top_level_breakdown,
)

DAY = 86400


def _write(path, size):
    """Create a file of exactly `size` bytes, making parents as needed.

    `truncate` rather than writing zeros: the walks under test only ever read
    `st_size`, and the large-and-old fixtures are 100 MB apiece -- writing
    them for real costs seconds per test for no extra coverage.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)


def _age(path, days):
    """Backdate a file's mtime by `days`, so age filters have something real
    to filter -- every file a test writes is otherwise seconds old."""
    old = time.time() - days * DAY
    os.utime(path, (old, old))


# ---------------------------------------------------------------------------
# human_size
# ---------------------------------------------------------------------------

def test_human_size_bytes_have_no_decimal():
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"


def test_human_size_binary_units():
    assert human_size(1024) == "1.0 KB"
    assert human_size(1536) == "1.5 KB"
    assert human_size(1024 ** 2) == "1.0 MB"
    assert human_size(1024 ** 3) == "1.0 GB"
    assert human_size(3 * 1024 ** 4) == "3.0 TB"


def test_human_size_clamps_negative_to_zero():
    assert human_size(-5) == "0 B"


# ---------------------------------------------------------------------------
# folder_size
# ---------------------------------------------------------------------------

def test_folder_size_sums_nested_files(tmp_path):
    _write(str(tmp_path / "a.bin"), 100)
    _write(str(tmp_path / "sub" / "b.bin"), 250)
    _write(str(tmp_path / "sub" / "deep" / "c.bin"), 400)

    total, skipped = folder_size(str(tmp_path))

    assert total == 750
    assert skipped == 0


def test_folder_size_empty_dir_is_zero(tmp_path):
    assert folder_size(str(tmp_path)) == (0, 0)


def test_folder_size_counts_unreadable_root_as_skipped(tmp_path):
    missing = str(tmp_path / "does-not-exist")

    total, skipped = folder_size(missing)

    # No crash, and the caller can tell the total is not trustworthy.
    assert total == 0
    assert skipped == 1


def test_folder_size_stops_when_aborted(tmp_path):
    _write(str(tmp_path / "a.bin"), 100)

    total, skipped = folder_size(str(tmp_path), should_abort=lambda: True)

    assert total == 0
    assert skipped == 0


# ---------------------------------------------------------------------------
# top_level_breakdown
# ---------------------------------------------------------------------------

def test_breakdown_is_sorted_largest_first(tmp_path):
    _write(str(tmp_path / "small" / "f.bin"), 10)
    _write(str(tmp_path / "big" / "f.bin"), 5000)
    _write(str(tmp_path / "medium" / "f.bin"), 300)

    results = top_level_breakdown(str(tmp_path))

    assert [name for name, _, _ in results] == ["big", "medium", "small"]
    assert [size for _, size, _ in results] == [5000, 300, 10]


def test_breakdown_ignores_loose_files_in_the_root(tmp_path):
    _write(str(tmp_path / "loose.bin"), 999)
    _write(str(tmp_path / "folder" / "f.bin"), 20)

    results = top_level_breakdown(str(tmp_path))

    assert results == [("folder", 20, 0)]


def test_breakdown_reports_each_folder_before_measuring_it(tmp_path):
    _write(str(tmp_path / "one" / "f.bin"), 1)
    _write(str(tmp_path / "two" / "f.bin"), 2)
    seen = []

    top_level_breakdown(str(tmp_path), on_folder=seen.append)

    # Callback order is directory order (sorted by name), not result order.
    assert seen == ["one", "two"]


def test_breakdown_of_unreadable_root_is_empty(tmp_path):
    assert top_level_breakdown(str(tmp_path / "nope")) == []


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_breakdown_skips_windows_junctions(tmp_path):
    # A real Windows home directory is full of legacy junctions ("My
    # Documents" -> "Documents"). DirEntry.is_symlink() is False for those,
    # so without the reparse-point check they get walked and double-counted.
    _write(str(tmp_path / "real" / "f.bin"), 500)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tmp_path / "link"), str(tmp_path / "real")],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")

    results = top_level_breakdown(str(tmp_path))

    assert results == [("real", 500, 0)]


def test_breakdown_stops_when_aborted(tmp_path):
    _write(str(tmp_path / "one" / "f.bin"), 1)
    _write(str(tmp_path / "two" / "f.bin"), 2)

    assert top_level_breakdown(str(tmp_path), should_abort=lambda: True) == []


# ---------------------------------------------------------------------------
# find_files -- the one finder behind system cache, logs, installers and
# large-and-old files, so its filters are tested rather than each caller's.
# ---------------------------------------------------------------------------

def test_find_files_returns_every_file_when_unfiltered(tmp_path):
    _write(str(tmp_path / "a.tmp"), 10)
    _write(str(tmp_path / "sub" / "b.tmp"), 20)

    items, skipped = find_files([str(tmp_path)])

    assert sorted(size for _, size in items) == [10, 20]
    assert skipped == 0


def test_find_files_filters_by_suffix_case_insensitively(tmp_path):
    _write(str(tmp_path / "keep.LOG"), 5)
    _write(str(tmp_path / "drop.txt"), 5)

    items, _ = find_files([str(tmp_path)], suffixes=(".log",))

    assert [os.path.basename(p) for p, _ in items] == ["keep.LOG"]


def test_find_files_filters_by_age(tmp_path):
    old = str(tmp_path / "old.exe")
    new = str(tmp_path / "new.exe")
    _write(old, 5)
    _write(new, 5)
    _age(old, 60)

    items, _ = find_files([str(tmp_path)], older_than_days=30)

    assert [os.path.basename(p) for p, _ in items] == ["old.exe"]


def test_find_files_filters_by_min_size(tmp_path):
    _write(str(tmp_path / "big.bin"), 500)
    _write(str(tmp_path / "small.bin"), 5)

    items, _ = find_files([str(tmp_path)], min_size=100)

    assert [os.path.basename(p) for p, _ in items] == ["big.bin"]


def test_find_files_max_depth_zero_stays_in_the_root(tmp_path):
    _write(str(tmp_path / "top.bin"), 1)
    _write(str(tmp_path / "sub" / "deep.bin"), 1)

    items, _ = find_files([str(tmp_path)], max_depth=0)

    assert [os.path.basename(p) for p, _ in items] == ["top.bin"]


def test_find_files_max_depth_one_visits_immediate_subfolders(tmp_path):
    _write(str(tmp_path / "sub" / "deep.bin"), 1)
    _write(str(tmp_path / "sub" / "deeper" / "deepest.bin"), 1)

    items, _ = find_files([str(tmp_path)], max_depth=1)

    assert [os.path.basename(p) for p, _ in items] == ["deep.bin"]


def test_find_files_does_not_descend_into_an_excluded_directory(tmp_path):
    _write(str(tmp_path / "kept.log"), 1)
    _write(str(tmp_path / "Temp" / "cached.log"), 1)

    items, _ = find_files([str(tmp_path)], exclude=[str(tmp_path / "Temp")])

    assert [os.path.basename(p) for p, _ in items] == ["kept.log"]


def test_cleanup_scan_does_not_report_a_temp_log_as_both_cache_and_log(
        fake_appdata, tmp_path):
    # %TEMP% lives inside %LOCALAPPDATA%, so without the exclusion the same
    # file lands in two categories: double-counted, and offered to the delete
    # worker twice.
    temp_log = str(tmp_path / "AppData" / "Local" / "Temp" / "run.log")
    _write(temp_log, 64)

    cats = {c.key: c for c in cleanup_scan(home=str(tmp_path))}

    assert [p for p, _ in cats["system_cache"].items] == [temp_log]
    assert cats["logs"].items == []


def test_find_files_counts_an_unreadable_root_as_skipped(tmp_path):
    items, skipped = find_files([str(tmp_path / "nope")])

    assert items == []
    assert skipped == 1


def test_find_files_ignores_a_missing_root_among_good_ones(tmp_path):
    _write(str(tmp_path / "real" / "f.bin"), 7)

    items, skipped = find_files([str(tmp_path / "real"), str(tmp_path / "gone")])

    assert [size for _, size in items] == [7]
    assert skipped == 1


def test_find_files_stops_when_aborted(tmp_path):
    _write(str(tmp_path / "f.bin"), 1)

    assert find_files([str(tmp_path)], should_abort=lambda: True) == ([], 0)


# ---------------------------------------------------------------------------
# empty_folders
# ---------------------------------------------------------------------------

def test_empty_folders_finds_a_nested_empty_directory(tmp_path):
    os.makedirs(str(tmp_path / "keep" / "hollow"))
    _write(str(tmp_path / "keep" / "f.bin"), 1)

    found, skipped = empty_folders([str(tmp_path)])

    assert [os.path.basename(p) for p, _ in found] == ["hollow"]
    assert [size for _, size in found] == [0]
    assert skipped == 0


def test_empty_folders_never_proposes_the_root_itself(tmp_path):
    # Documents being empty is not a reason to offer to bin Documents.
    assert empty_folders([str(tmp_path)]) == ([], 0)


def test_empty_folders_ignores_a_directory_holding_a_file(tmp_path):
    _write(str(tmp_path / "full" / "f.bin"), 1)

    found, _ = empty_folders([str(tmp_path)])

    assert found == []


def test_empty_folders_reports_the_leaf_not_the_parent(tmp_path):
    # A parent whose only child is an empty folder is not itself empty, so
    # one pass proposes the leaf alone -- the deliberately safe behaviour.
    os.makedirs(str(tmp_path / "outer" / "inner"))

    found, _ = empty_folders([str(tmp_path)])

    assert [os.path.basename(p) for p, _ in found] == ["inner"]


def test_empty_folders_stops_when_aborted(tmp_path):
    os.makedirs(str(tmp_path / "hollow"))

    assert empty_folders([str(tmp_path)], should_abort=lambda: True) == ([], 0)


# ---------------------------------------------------------------------------
# recycle_bin_stats / browser_cache_dirs / cleanup_roots
# ---------------------------------------------------------------------------

def test_recycle_bin_stats_returns_non_negative_numbers():
    # The real bin's contents are whatever this machine happens to hold, so
    # the contract is what is asserted: two non-negative ints, never a raise,
    # and (0, 0) rather than an error off Windows.
    size, count = recycle_bin_stats()

    assert isinstance(size, int) and isinstance(count, int)
    assert size >= 0 and count >= 0
    if sys.platform != "win32":
        assert (size, count) == (0, 0)


def test_browser_cache_dirs_lists_only_directories_that_exist(tmp_path):
    chrome = tmp_path / "Google" / "Chrome" / "User Data" / "Default" / "Cache"
    os.makedirs(str(chrome))
    profile = tmp_path / "Mozilla" / "Firefox" / "Profiles" / "abc.default" / "cache2"
    os.makedirs(str(profile))

    dirs = browser_cache_dirs(str(tmp_path))

    assert str(chrome) in dirs
    assert str(profile) in dirs
    # Edge is not installed in this fixture, so nothing of its is offered.
    assert not any("Edge" in d for d in dirs)


def test_browser_cache_dirs_is_empty_for_a_bare_directory(tmp_path):
    assert browser_cache_dirs(str(tmp_path)) == []


def test_cleanup_roots_only_returns_existing_directories(tmp_path):
    os.makedirs(str(tmp_path / "Downloads"))

    roots = cleanup_roots(str(tmp_path))

    assert roots["downloads"] == str(tmp_path / "Downloads")
    assert "documents" not in roots  # not created, so never scanned


# ---------------------------------------------------------------------------
# cleanup_scan
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_appdata(tmp_path, monkeypatch):
    """Point the AppData/Temp roots at the test's own tree.

    Without this, cleanup_scan reads %LOCALAPPDATA%/%APPDATA%/%TEMP% from the
    real machine even when `home` is a tmp_path: the run would take minutes,
    its numbers would differ per machine, and a test could not distinguish
    "found nothing" from "found the developer's browser cache".
    """
    local = tmp_path / "AppData" / "Local"
    for var, path in (("LOCALAPPDATA", local),
                      ("APPDATA", tmp_path / "AppData" / "Roaming"),
                      ("TEMP", local / "Temp"),
                      ("TMP", local / "Temp")):
        os.makedirs(str(path), exist_ok=True)
        monkeypatch.setenv(var, str(path))
    return tmp_path


def test_cleanup_scan_returns_all_eight_categories_in_comp_order(fake_appdata, tmp_path):
    cats = cleanup_scan(home=str(tmp_path))

    assert [c.key for c in cats] == [
        "system_cache", "logs", "leftovers", "browser_cache",
        "installers", "recycle_bin", "large_old", "empty_folders",
    ]


def test_cleanup_scan_marks_report_only_categories_as_not_removable(fake_appdata, tmp_path):
    cats = {c.key: c for c in cleanup_scan(home=str(tmp_path))}

    # Leftovers has no detection behind it and the bin's contents are already
    # binned -- neither may be handed to the Clean Selected flow.
    assert cats["leftovers"].removable is False
    assert cats["leftovers"].items == []
    assert cats["recycle_bin"].removable is False
    assert cats["recycle_bin"].items == []


def test_cleanup_scan_finds_a_real_old_installer(fake_appdata, tmp_path):
    installer = str(tmp_path / "Downloads" / "setup.exe")
    _write(installer, 2048)
    _age(installer, 90)
    _write(str(tmp_path / "Downloads" / "fresh.exe"), 2048)

    cats = {c.key: c for c in cleanup_scan(home=str(tmp_path))}

    assert cats["installers"].items == [(installer, 2048)]
    assert cats["installers"].size == 2048


def test_cleanup_scan_sizes_large_and_old_files_largest_first(fake_appdata, tmp_path):
    docs = tmp_path / "Documents"
    bigger = str(docs / "bigger.iso")
    smaller = str(docs / "smaller.iso")
    _write(bigger, LARGE_FILE_BYTES + 10)
    _write(smaller, LARGE_FILE_BYTES + 1)
    _age(bigger, 200)
    _age(smaller, 200)

    cats = {c.key: c for c in cleanup_scan(home=str(tmp_path))}

    assert [p for p, _ in cats["large_old"].items] == [bigger, smaller]
    assert cats["large_old"].size == 2 * LARGE_FILE_BYTES + 11


def test_cleanup_scan_reports_progress_per_category(fake_appdata, tmp_path):
    seen = []

    cleanup_scan(home=str(tmp_path), on_category=seen.append)

    assert seen[0] == "System cache"
    assert len(seen) == 8


def test_cleanup_scan_stops_after_the_first_category_when_aborted(fake_appdata, tmp_path):
    cats = cleanup_scan(home=str(tmp_path), should_abort=lambda: True)

    # An aborted scan returns what it finished, never a partial category or
    # an exception -- the tab renders the short list as-is.
    assert [c.key for c in cats] == ["system_cache"]


# ===========================================================================
# Duplicates -- exact (Files mode)
# ===========================================================================


def _write_bytes(path, data):
    """Write real content, not a sparse hole -- these tests hash the bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_file_sha256_matches_hashlib_over_the_same_bytes(tmp_path):
    path = str(tmp_path / "payload.bin")
    data = os.urandom(3 * 1024 * 1024 + 17)  # spans several read chunks
    _write_bytes(path, data)

    assert file_sha256(path) == hashlib.sha256(data).hexdigest()


def test_find_duplicate_files_groups_identical_copies(tmp_path):
    original = str(tmp_path / "invoice.pdf")
    copy = str(tmp_path / "sub" / "invoice (1).pdf")
    _write_bytes(original, b"same bytes")
    _write_bytes(copy, b"same bytes")
    _age(original, 30)  # the older file is the one to keep

    groups, skipped = find_duplicate_files([str(tmp_path)])

    assert skipped == 0
    assert len(groups) == 1
    assert groups[0].keep == (original, 10)
    assert groups[0].duplicates == [(copy, 10)]
    assert groups[0].similarity == 100
    assert groups[0].size == 10


def test_find_duplicate_files_keeps_the_oldest_of_three_copies(tmp_path):
    paths = []
    for name, age in (("a.txt", 1), ("b.txt", 400), ("c.txt", 90)):
        path = str(tmp_path / name)
        _write_bytes(path, b"identical")
        _age(path, age)
        paths.append(path)

    groups, _ = find_duplicate_files([str(tmp_path)])

    assert groups[0].keep[0] == paths[1]          # b.txt, backdated 400 days
    assert sorted(p for p, _ in groups[0].duplicates) == sorted(
        [paths[0], paths[2]])


def test_find_duplicate_files_ignores_same_size_different_content(tmp_path):
    _write_bytes(str(tmp_path / "one.bin"), b"aaaa")
    _write_bytes(str(tmp_path / "two.bin"), b"bbbb")

    groups, _ = find_duplicate_files([str(tmp_path)])

    # Equal size is only the cheap prefilter; the hash is what decides.
    assert groups == []


def test_find_duplicate_files_ignores_empty_files(tmp_path):
    _write_bytes(str(tmp_path / "empty_one"), b"")
    _write_bytes(str(tmp_path / "empty_two"), b"")

    groups, _ = find_duplicate_files([str(tmp_path)])

    # Every zero-byte file matches every other one and reclaims nothing.
    assert groups == []


def test_find_duplicate_files_sorts_groups_by_reclaimable_size(tmp_path):
    for name, data in (("small", b"xx"), ("big", b"y" * 500)):
        _write_bytes(str(tmp_path / (name + "_1")), data)
        _write_bytes(str(tmp_path / (name + "_2")), data)

    groups, _ = find_duplicate_files([str(tmp_path)])

    assert [g.size for g in groups] == [500, 2]


def test_find_duplicate_files_reports_hashing_progress(tmp_path):
    _write_bytes(str(tmp_path / "a"), b"dup")
    _write_bytes(str(tmp_path / "b"), b"dup")
    _write_bytes(str(tmp_path / "unique"), b"only me")
    seen = []

    find_duplicate_files([str(tmp_path)],
                         on_progress=lambda d, t: seen.append((d, t)))

    # The unique-sized file is never hashed, so it never reaches the total.
    assert seen == [(1, 2), (2, 2)]


def test_find_duplicate_files_stops_when_aborted(tmp_path):
    _write_bytes(str(tmp_path / "a"), b"dup")
    _write_bytes(str(tmp_path / "b"), b"dup")

    groups, _ = find_duplicate_files([str(tmp_path)], should_abort=lambda: True)

    assert groups == []


def test_duplicate_roots_are_the_large_and_old_file_roots(fake_appdata, tmp_path):
    (tmp_path / "Downloads").mkdir(exist_ok=True)
    (tmp_path / "Documents").mkdir(exist_ok=True)

    roots = duplicate_roots(home=str(tmp_path))

    # Desktop does not exist in this fixture, so it is simply absent.
    assert [os.path.basename(r) for r in roots] == ["Downloads", "Documents"]


# ===========================================================================
# Duplicates -- perceptual (Images mode)
# ===========================================================================


def _photo(path, size=(240, 180), seed=0):
    """A deterministic non-flat test image with real gradient structure.

    A solid colour is useless here: every flat image has the same difference
    hash, so a test built on one would pass whatever the hash did.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    width, height = size
    image = Image.new("RGB", size)
    image.putdata([
        ((x * 7 + seed * 53) % 256, (y * 5 + seed * 31) % 256, (x + y) % 256)
        for y in range(height) for x in range(width)
    ])
    image.save(path)
    return path


def test_image_dhash_is_stable_and_reports_dimensions(tmp_path):
    path = _photo(str(tmp_path / "a.png"), size=(120, 90))

    bits, width, height = image_dhash(path)

    assert (width, height) == (120, 90)
    assert image_dhash(path)[0] == bits          # deterministic
    assert 0 <= bits < 2 ** IMAGE_HASH_BITS


def test_image_dhash_returns_none_for_a_non_image(tmp_path):
    path = str(tmp_path / "notes.txt")
    _write_bytes(path, b"this is not a picture")

    assert image_dhash(path) is None


def test_image_dhash_survives_a_resave_and_a_resize(tmp_path):
    original = _photo(str(tmp_path / "original.png"), size=(240, 180))
    with Image.open(original) as im:
        im.resize((120, 90), Image.Resampling.LANCZOS).save(tmp_path / "small.png")

    a = image_dhash(original)[0]
    b = image_dhash(str(tmp_path / "small.png"))[0]

    assert hamming_distance(a, b) <= IMAGE_HASH_MAX_DISTANCE


def test_image_dhash_separates_different_pictures(tmp_path):
    a = image_dhash(_photo(str(tmp_path / "a.png"), seed=1))[0]
    b = image_dhash(_photo(str(tmp_path / "b.png"), seed=9))[0]

    assert hamming_distance(a, b) > IMAGE_HASH_MAX_DISTANCE


def test_similarity_percent_reads_as_a_badge():
    assert similarity_percent(0) == 100
    assert similarity_percent(IMAGE_HASH_BITS) == 0
    assert similarity_percent(IMAGE_HASH_MAX_DISTANCE) == 92


def test_find_duplicate_images_groups_a_resized_copy_and_keeps_the_bigger(tmp_path):
    big = _photo(str(tmp_path / "holiday.png"), size=(240, 180))
    small = str(tmp_path / "copies" / "holiday-small.png")
    os.makedirs(os.path.dirname(small), exist_ok=True)
    with Image.open(big) as im:
        im.resize((120, 90), Image.Resampling.LANCZOS).save(small)

    groups, _ = find_duplicate_images([str(tmp_path)])

    assert len(groups) == 1
    # Highest resolution is kept; the downscaled copy is what gets removed.
    assert groups[0].keep[0] == big
    assert [p for p, _ in groups[0].duplicates] == [small]
    assert groups[0].similarity >= 92


def test_find_duplicate_images_leaves_different_pictures_alone(tmp_path):
    _photo(str(tmp_path / "a.png"), seed=1)
    _photo(str(tmp_path / "b.png"), seed=9)

    assert find_duplicate_images([str(tmp_path)])[0] == []


def test_find_duplicate_images_ignores_non_image_files(tmp_path):
    _write_bytes(str(tmp_path / "one.txt"), b"same")
    _write_bytes(str(tmp_path / "two.txt"), b"same")

    groups, skipped = find_duplicate_images([str(tmp_path)])

    # Byte-identical, but not images -- that is Files mode's job, not this one.
    assert groups == []
    assert skipped == 0


def test_find_duplicate_images_reports_progress_and_aborts(tmp_path):
    _photo(str(tmp_path / "a.png"))
    _photo(str(tmp_path / "b.png"), seed=4)
    seen = []

    find_duplicate_images([str(tmp_path)],
                          on_progress=lambda d, t: seen.append((d, t)))

    assert seen == [(1, 2), (2, 2)]
    assert find_duplicate_images([str(tmp_path)], should_abort=lambda: True)[0] == []
