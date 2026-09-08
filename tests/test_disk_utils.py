# tests/test_disk_utils.py
# Covers utils/disk_utils.py: byte formatting, the recursive folder walk and
# the sorted top-level breakdown the Disk tab's QThread worker calls. Uses
# real temp trees (tmp_path), no filesystem mocking -- the whole point of the
# walk is how it behaves against a real directory.
import os
import subprocess
import sys

import pytest

from utils.disk_utils import human_size, folder_size, top_level_breakdown


def _write(path, size):
    """Create a file of exactly `size` bytes, making parents as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)


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
