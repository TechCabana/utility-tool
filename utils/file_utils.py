# utils/file_utils.py
import os, re, shutil, datetime
from typing import Tuple, List, Optional
from send2trash import send2trash

def apply_regex(stem: str, find: str, replace: str) -> str:
    if not find:
        return stem
    try:
        return re.sub(find, replace or "", stem)
    except re.error:
        return stem

def format_date_token(pattern: str, date_obj: datetime.datetime) -> str:
    def repl(m):
        fmt = m.group(1) or "%Y%m%d"
        return date_obj.strftime(fmt)
    return re.sub(r"\{date:([^}]+)\}", repl, pattern)

def build_new_name(path: str, pattern: str, prefix: str, suffix: str, idx: int, start: int, pad: int, date_source: str, regex_find: str, regex_replace: str, case: str) -> Tuple[str, str]:
    folder, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    date_val = datetime.datetime.now() if date_source == "now" else datetime.datetime.fromtimestamp(os.path.getmtime(path))
    name_pattern = pattern
    name_pattern = format_date_token(name_pattern, date_val)
    num = str(start + idx).zfill(pad)
    name_pattern = name_pattern.replace("{name}", stem).replace("{ext}", ext.lstrip(".")).replace("{num}", num)
    name_pattern = apply_regex(name_pattern, regex_find, regex_replace)
    new_stem = f"{prefix}{name_pattern}{suffix}"
    if case == "lower": new_stem = new_stem.lower()
    if case == "upper": new_stem = new_stem.upper()
    if case == "title": new_stem = new_stem.title()
    new_name = f"{new_stem}{ext}"
    return new_name, os.path.join(folder, new_name)

def apply_renames(src_paths: List[str], dest_paths: List[str],
                  conflict_policy: str = "overwrite") -> List[Tuple[str, str]]:
    """Rename each src to its paired dest, honoring conflict_policy when a
    dest is already occupied by a different file. Returns one (status, path)
    per pair, in the order the pairs were given -- the same shape rename_file
    returns.

    The pairs are EXECUTED in order_renames() order, not in the order given,
    so a batch that collides with itself (a.txt -> b.txt, b.txt -> c.txt)
    completes as a unit whichever way round it was listed. Only a target that
    is still occupied once the batch's own sources have moved is a conflict,
    and that one goes through conflict_policy exactly as before.

    The default stays "overwrite" so existing callers keep the behaviour they
    had, now as a policy stated out loud rather than a silent one."""
    pairs = list(zip(src_paths, dest_paths))
    results: List[Optional[Tuple[str, str]]] = [None] * len(pairs)
    parked: dict = {}
    try:
        for i, src, target in order_renames(pairs):
            status, path = rename_file(src, target, conflict_policy)
            if target != pairs[i][1]:
                # A cycle member parked under a temp name: not its real step,
                # so it produces no result yet. Remember where it came from
                # in case the real step never lands.
                parked[i] = (path, src)
                continue
            results[i] = (status, path)
            if status == "done":
                parked.pop(i, None)
    finally:
        # Runs even if a later step raised: a park already on disk must
        # still be put back rather than left under a temp name that reads
        # as data loss to anyone who finds it later (see below).
        _restore_parked(parked)
    return results


def _restore_parked(parked: dict) -> None:
    for tmp, original in parked.values():
        # The real step was skipped by the policy, so this file never reached
        # its target -- put it back under the name it had rather than leaving
        # it parked under a temp one. `original` can by now be occupied by a
        # DIFFERENT pair of this same batch that already landed there (any
        # pair whose target is this file's pre-batch name lands there once
        # this file is parked out of the way -- order_renames' docstring on
        # cycles), so restoring must never blindly overwrite it: os.replace
        # would silently destroy a file this same call already reported
        # "done" for. "keep_both" guarantees the parked file always survives,
        # under its own name or next to it, and never erases someone else's.
        rename_file(tmp, original, "keep_both")


# ------------------------------
# Move / Copy / Delete (File Manager batch operations)
# ------------------------------
# Conflict policy values used below: "keep_both", "skip", "overwrite".
# "ask" is resolved to one of those three by the caller (tabs/file_tab.py,
# in the UI thread, before the batch worker runs) -- these functions never
# prompt, keeping this module Qt-free per CLAUDE.md §3.

def resolve_conflict_path(dest_path: str, conflict_policy: str) -> Optional[str]:
    """Given a destination path that already exists, return the path to
    actually write to, or None if the file should be skipped.

    - "overwrite" (or any unrecognized value): return dest_path unchanged.
    - "skip": return None.
    - "keep_both": return the first "name (1).ext", "name (2).ext", ... that
      does not already exist.
    """
    if conflict_policy == "skip":
        return None
    if conflict_policy == "keep_both":
        folder, filename = os.path.split(dest_path)
        stem, ext = os.path.splitext(filename)
        n = 1
        candidate = dest_path
        while os.path.exists(candidate):
            candidate = os.path.join(folder, f"{stem} ({n}){ext}")
            n += 1
        return candidate
    return dest_path  # "overwrite"


def same_path(a: str, b: str) -> bool:
    """True when a and b name the same existing file.

    os.path.samefile asks the filesystem for the file's identity, so a
    case-only rename on a case-insensitive filesystem reads as the same file
    rather than as a conflict with itself. Falls back to comparing absolute
    paths where the OS cannot answer (a path that is already gone)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def rename_file(src: str, dest_path: str, conflict_policy: str = "overwrite") -> Tuple[str, str]:
    """Rename src to dest_path, honoring conflict_policy if a different file
    already occupies dest_path. Returns (status, path) where status is
    "done" or "skipped". Raises on unexpected filesystem errors -- the
    QThread worker that calls this catches and reports those as errors.

    A dest_path that IS src (a no-op rename, or a case-only one on a
    case-insensitive filesystem) is not a conflict and never goes through
    the policy."""
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    if os.path.exists(dest_path) and not same_path(src, dest_path):
        resolved = resolve_conflict_path(dest_path, conflict_policy)
        if resolved is None:
            return "skipped", dest_path
        dest_path = resolved
    try:
        os.replace(src, dest_path)
    except OSError:
        # os.replace cannot cross drives; shutil.move can. This runs only
        # after the policy has chosen dest_path, so the fallback can never
        # overwrite something the policy protected.
        shutil.move(src, dest_path)
    return "done", dest_path


# ------------------------------
# Rename batch ordering
# ------------------------------
# A rename batch can collide with itself. A "shift" -- a.txt -> b.txt,
# b.txt -> c.txt -- is a valid renumber taken as a whole, but run in list
# order the first pair lands on a b.txt that has not moved yet. That is a
# real collision at that instant, so the policy fires: "Skip" drops the
# rename outright, "Keep both" invents a name nobody asked for, and the
# result depends on nothing more than which file was added first. Ordering
# the batch so each pair runs once its target is free is what makes it a
# batch rather than a sequence of unrelated renames (card 7gRxAGFc).

def _key(path: str) -> str:
    """Comparable identity for a path that need not exist yet.

    same_path() asks the filesystem, which cannot answer for a name nothing
    occupies -- and ordering is decided over targets, most of which do not
    exist when it is decided. normcase draws the same line the filesystem
    does: a.TXT and a.txt are one name on Windows and two on Linux."""
    return os.path.normcase(os.path.abspath(path))


def _temp_name(target: str) -> str:
    """A free path beside `target` to park a cycle member under."""
    folder, filename = os.path.split(target)
    n = 0
    while True:
        candidate = os.path.join(folder or ".", f".{filename}.renaming{n}")
        if not os.path.exists(candidate):
            return candidate
        n += 1


def order_renames(pairs: List[Tuple[str, str]]) -> List[Tuple[int, str, str]]:
    """Put a rename batch in an order that resolves its own collisions.

    Takes the batch's (src, target) pairs and returns (index, src, target)
    steps in the order they must run. `index` is the pair's position in
    `pairs` -- the batch position build_new_name numbered the file by, and
    the one every caller reports progress against -- so a caller executes
    the steps in the order given while still reporting the original index.

    A pair runs as soon as its target is free: nothing is there, it is the
    file itself (a no-op or case-only rename), or what is there is a file
    this batch has already moved away. A target still occupied by another
    pair's source waits for that pair. A target occupied by anything else is
    a genuine pre-existing conflict, so the pair runs and the caller's
    conflict policy decides -- ordering resolves the batch against itself
    and nothing more.

    When every remaining pair is waiting on another, the batch contains a
    cycle (a <-> b, or longer). One member of it is parked under a temp name
    beside its target, which frees its old name for the rest of the chain,
    and its real step runs at the end. Such a pair yields TWO steps with the
    same index: the parking one, whose target is NOT `pairs[index][1]`, and
    the real one, whose target is. That comparison is how a caller tells
    them apart.

    Pure apart from the os.path.exists() checks the ordering needs -- it
    renames nothing itself, and stays Qt-free per CLAUDE.md §3."""
    pending = {i: (s, t) for i, (s, t) in enumerate(pairs)}
    sources = {_key(s): i for i, (s, _) in enumerate(pairs)}
    steps: List[Tuple[int, str, str]] = []

    def is_free(i: int) -> bool:
        src, target = pending[i]
        if _key(target) == _key(src):
            return True
        if _key(target) not in sources:
            return True
        return not os.path.exists(target)

    while pending:
        ready = [i for i in sorted(pending) if is_free(i)]
        if ready:
            for i in ready:
                src, target = pending.pop(i)
                sources.pop(_key(src), None)
                steps.append((i, src, target))
            continue

        # Nothing can run, so every remaining target is another pending
        # source: there is a cycle. Walk the waiting chain until an index
        # repeats -- that one is IN the cycle rather than merely queued
        # behind it -- and park it, which frees its name for the pair that
        # was waiting on it.
        i = min(pending)
        seen: List[int] = []
        while i not in seen:
            seen.append(i)
            i = sources[_key(pending[i][1])]
        src, target = pending[i]
        tmp = _temp_name(target)
        steps.append((i, src, tmp))
        sources.pop(_key(src), None)
        pending[i] = (tmp, target)
        sources[_key(tmp)] = i

    return steps


def pending_conflicts(pairs: List[Tuple[str, str]]) -> List[bool]:
    """Which of these (src, target) pairs collide with a file the batch is
    NOT about to move.

    A target occupied by another pair's source is not a conflict:
    order_renames() runs that pair first, so the name is free by the time
    this one needs it. Only what is left once the batch's own sources are
    accounted for is a real conflict -- which is what the preview marks and
    what the "Ask" pre-scan counts, so neither prompts about a collision
    that resolves itself."""
    sources = {_key(s) for s, _ in pairs}
    return [os.path.exists(t) and not same_path(s, t) and _key(t) not in sources
            for s, t in pairs]


def move_file(src: str, dest_folder: str, conflict_policy: str = "overwrite") -> Tuple[str, str]:
    """Move src into dest_folder, honoring conflict_policy if a same-named
    file already exists there. Returns (status, path) where status is
    "done" or "skipped". Raises on unexpected filesystem errors -- the
    QThread worker that calls this catches and reports those as errors."""
    os.makedirs(dest_folder, exist_ok=True)
    dest_path = os.path.join(dest_folder, os.path.basename(src))
    if os.path.exists(dest_path) and os.path.abspath(dest_path) != os.path.abspath(src):
        resolved = resolve_conflict_path(dest_path, conflict_policy)
        if resolved is None:
            return "skipped", dest_path
        dest_path = resolved
    shutil.move(src, dest_path)
    return "done", dest_path


def copy_file(src: str, dest_folder: str, conflict_policy: str = "overwrite") -> Tuple[str, str]:
    """Copy src into dest_folder, honoring conflict_policy. Returns
    (status, path) where status is "done" or "skipped"."""
    os.makedirs(dest_folder, exist_ok=True)
    dest_path = os.path.join(dest_folder, os.path.basename(src))
    if os.path.exists(dest_path) and os.path.abspath(dest_path) != os.path.abspath(src):
        resolved = resolve_conflict_path(dest_path, conflict_policy)
        if resolved is None:
            return "skipped", dest_path
        dest_path = resolved
    if os.path.isdir(src):
        shutil.copytree(src, dest_path)
    else:
        shutil.copy2(src, dest_path)
    return "done", dest_path


def delete_file(path: str) -> Tuple[str, str]:
    """Send path to the OS Recycle Bin / Trash via send2trash -- never a
    permanent delete. Returns ("done", path)."""
    send2trash(path)
    return "done", path
