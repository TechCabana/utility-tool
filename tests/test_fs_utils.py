# tests/test_fs_utils.py
# Covers utils/fs_utils.py: the app-data directory, the symlink/junction
# predicate the backup walk relies on, and byte formatting. The human_size and
# junction cases are carried over from the removed tests/test_disk_utils.py,
# where these helpers used to live.
import os
import subprocess
import sys

import pytest

from utils import fs_utils
from utils.fs_utils import _is_link_like, human_size


def _entries(root):
    """Map each name in `root` to its DirEntry, the type _is_link_like takes."""
    with os.scandir(root) as entries:
        return {entry.name: entry for entry in entries}


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
# app_dir
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.name != "nt" or sys.platform == "darwin",
                    reason="the APPDATA branch is the Windows one")
def test_app_dir_lives_under_appdata_and_is_created(tmp_path, monkeypatch):
    # APPDATA is the override every sandboxed run of this app relies on, so
    # pin that it is honoured and that the directory is made on demand.
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = fs_utils.app_dir()
    assert path == os.path.join(str(tmp_path), fs_utils.APP_NAME)
    assert os.path.isdir(path)


# ---------------------------------------------------------------------------
# _is_link_like
# ---------------------------------------------------------------------------

def test_plain_directory_and_file_are_not_links(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "f.txt").write_text("x")
    entries = _entries(tmp_path)
    assert not _is_link_like(entries["real"])
    assert not _is_link_like(entries["f.txt"])


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-only")
def test_windows_junction_is_link_like(tmp_path):
    # DirEntry.is_symlink() is False for a junction, which is the whole reason
    # this predicate exists: the backup walk would otherwise follow it.
    (tmp_path / "real").mkdir()
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tmp_path / "link"), str(tmp_path / "real")],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction in this environment")

    entries = _entries(tmp_path)
    assert not entries["link"].is_symlink()
    assert _is_link_like(entries["link"])
    assert not _is_link_like(entries["real"])
