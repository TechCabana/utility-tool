# utils/verify.py
"""Checking a finished copy against its manifest: the "full" re-read and the
"quick" size-check-plus-sample.

Split out of utils/backup_utils.py (card 95) as a pure move; that module
re-exports everything here. Qt-free.
"""
import hashlib
import json
import math
import os
import random
import stat
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from utils.copying import CHUNK, BackupAborted
from utils.fs_utils import long_path


# Quick mode's sample: about this fraction of the files, at least one, at
# most QUICK_SAMPLE_CAP, plus the largest and the most recently modified
# file. The cap keeps a quick check of a huge tree bounded (500 files at the
# ~1 MB/s small-file re-read speed measured here is well under a minute); the
# size check still covers every file.
QUICK_SAMPLE_FRACTION = 0.05
QUICK_SAMPLE_CAP = 500


# How many offending files a failed verification names in its message.
VERIFY_NAMED = 5


def _verify_copy(partial: str, manifest: str,
                 on_progress: Optional[Callable[[int, int], None]],
                 should_abort: Optional[Callable[[], bool]],
                 total: int) -> Tuple[int, int, List[str]]:
    """Re-read every file the manifest lists from `partial`; compare.

    Streams the manifest a line at a time and returns `(files checked, bad
    count, the first VERIFY_NAMED bad paths)`, so memory stays flat however
    many files there are. A file is bad if it is missing, unreadable, a different size
    or a different SHA-256 from what was recorded while copying.

    The re-read is a fresh open of the copied file, not the hash computed
    during the copy, so it catches a fault anywhere on the write path. It
    does not prove the bytes are on the platter: the OS file cache can serve
    this read from memory. Bit rot on the stored disk is a later scheduled
    deep-verify's job, not this one's.
    """
    bad = 0
    named: List[str] = []
    done = 0
    if on_progress is not None:
        on_progress(0, total)
    for entry in _manifest_files(manifest, should_abort):
        if entry is None:
            bad += 1
            if len(named) < VERIFY_NAMED:
                named.append("(damaged manifest line)")
            continue
        if not _rehash_matches(partial, entry):
            bad += 1
            if len(named) < VERIFY_NAMED:
                named.append(entry.get("path", ""))
        done += 1
        if on_progress is not None:
            on_progress(done, total)
    return done, bad, named


def _manifest_files(manifest: str, should_abort: Optional[Callable[[], bool]]
                    ) -> Iterator[Optional[Dict]]:
    """Stream a manifest's file entries; None for each damaged line.

    Checks `should_abort` per line (raising BackupAborted) so both verify
    passes cancel promptly. Raises OSError if the manifest cannot be read.
    """
    with open(long_path(manifest), "r", encoding="utf-8") as handle:
        for line in handle:
            if should_abort is not None and should_abort():
                raise BackupAborted()
            try:
                entry = json.loads(line)
            except ValueError:
                entry = None
            if not isinstance(entry, dict):
                yield None
            elif entry.get("type") == "file":
                yield entry


def _copied_path(partial: str, entry: Dict) -> str:
    """Where a manifest entry's file sits inside the `.partial` folder."""
    return os.path.join(partial, *entry.get("path", "").split("/"))


def _rehash(path: str) -> Tuple[int, str]:
    """Re-read `path` from disk: `(bytes read, sha256 hex)`. Raises OSError.

    The one place verification reads a copied file's content, so tests can
    count exactly how many files a mode re-read.
    """
    digest = hashlib.sha256()
    size = 0
    with open(long_path(path), "rb") as copied:
        while True:
            chunk = copied.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _rehash_matches(partial: str, entry: Dict) -> bool:
    """True if the copied file re-reads to the size and hash recorded."""
    try:
        size, sha = _rehash(_copied_path(partial, entry))
    except OSError:
        return False
    return size == entry.get("size") and sha == entry.get("sha256")


def _quick_sample(count: int, largest: int, newest: int, seed: str) -> set:
    """Indexes (into the manifest's file entries) quick mode re-hashes.

    About QUICK_SAMPLE_FRACTION of `count`, at least 1 and at most
    QUICK_SAMPLE_CAP, always including `largest` and `newest` (which can make
    it one or two over a tiny target). Drawn with `random.Random(seed)` -- the
    version stamp -- so a given run's sample is reproducible.
    """
    if count <= 0:
        return set()
    want = min(count, QUICK_SAMPLE_CAP,
               max(1, math.ceil(count * QUICK_SAMPLE_FRACTION)))
    chosen = {largest, newest}
    extra = [i for i in random.Random(seed).sample(range(count), min(count, want + 2))
             if i not in chosen]
    chosen.update(extra[:max(0, want - len(chosen))])
    return chosen


def _quick_verify(partial: str, manifest: str,
                  on_progress: Optional[Callable[[int, int], None]],
                  should_abort: Optional[Callable[[], bool]],
                  seed: str) -> Tuple[int, int, List[str], int]:
    """Quick mode: every file's size, a sample's content.

    Pass 1 stats every file the manifest lists (no content read) and
    requires it to exist as a file of the recorded size, noting the largest
    and the most recently modified (by manifest mtime). Pass 2 re-reads only
    `_quick_sample`'s files and compares their SHA-256. If pass 1 already
    found a bad file the run fails anyway, so pass 2 is skipped.

    Returns `(files checked, bad count, the first VERIFY_NAMED bad paths,
    files re-hashed)`. Progress (`on_progress`) runs over the sample only;
    it reports `(0, 0)` -- an indeterminate bar -- while pass 1 stats.

    The honest limit: a file outside the sample whose bytes are wrong but
    whose size is right is NOT caught. That is the trade the owner chose for
    speed; "full" catches it.
    """
    bad = 0
    named: List[str] = []
    count = 0
    largest = newest = (None, -1)  # (key, index)
    if on_progress is not None:
        on_progress(0, 0)
    for entry in _manifest_files(manifest, should_abort):
        if entry is None:
            bad += 1
            if len(named) < VERIFY_NAMED:
                named.append("(damaged manifest line)")
            continue
        try:
            info = os.stat(long_path(_copied_path(partial, entry)))
            ok = stat.S_ISREG(info.st_mode) and info.st_size == entry.get("size")
        except OSError:
            ok = False
        if not ok:
            bad += 1
            if len(named) < VERIFY_NAMED:
                named.append(entry.get("path", ""))
        size, mtime = entry.get("size"), entry.get("mtime")
        if isinstance(size, int) and (largest[0] is None or size > largest[0]):
            largest = (size, count)
        if (isinstance(mtime, (int, float))
                and (newest[0] is None or mtime > newest[0])):
            newest = (mtime, count)
        count += 1
    if bad:
        return count, bad, named, 0

    sample = _quick_sample(count, max(largest[1], 0), max(newest[1], 0), seed)
    done = 0
    if on_progress is not None:
        on_progress(0, len(sample))
    for index, entry in enumerate(_manifest_files(manifest, should_abort)):
        if index not in sample:
            continue
        if not _rehash_matches(partial, entry):
            bad += 1
            if len(named) < VERIFY_NAMED:
                named.append(entry.get("path", ""))
        done += 1
        if on_progress is not None:
            on_progress(done, len(sample))
    return count, bad, named, done
