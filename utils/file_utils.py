import os, re, datetime
from typing import Tuple

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

def apply_renames(src_paths: list, dest_paths: list) -> None:
    for s, d in zip(src_paths, dest_paths):
        os.makedirs(os.path.dirname(d), exist_ok=True)
        os.rename(s, d)
