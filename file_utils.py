import os, re, datetime
from typing import Tuple

def build_new_name(original_path: str,
                   pattern: str,
                   prefix: str,
                   suffix: str,
                   idx: int,
                   start: int,
                   pad: int) -> Tuple[str, str]:
    """Return (new_basename, new_fullpath) without touching disk."""
    dirname, basename = os.path.split(original_path)
    name, ext = os.path.splitext(basename)
    ext_no_dot = ext[1:] if ext.startswith(".") else ext

    # tokens
    num_val = start + idx
    def date_repl(m):
        fmt = m.group(1) or "%Y-%m-%d"
        return datetime.datetime.now().strftime(fmt)

    # {date:%Y%m%d}
    pat = re.sub(r"\{date:(.+?)\}", lambda m: date_repl(m), pattern)
    pat = pat.replace("{name}", name).replace("{ext}", ext_no_dot).replace("{num}", str(num_val).zfill(pad))

    new_name = f"{prefix}{pat}{suffix}{ext}"
    return new_name, os.path.join(dirname, new_name)
