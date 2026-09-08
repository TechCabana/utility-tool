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

def apply_renames(src_paths: List[str], dest_paths: List[str]) -> None:
    for s, d in zip(src_paths, dest_paths):
        os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
        # use replace to overwrite if needed
        if os.path.exists(d):
            os.replace(s, d)
        else:
            os.rename(s, d)


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
