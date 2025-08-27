from PIL import Image, ImageOps
import io, os
from typing import Tuple, Optional

STANDARD_SIZES_MM = {
    "Original": None,
    "Passport – India (35×45 mm)": (35, 45),
    "Passport – Netherlands (35×45 mm)": (35, 45),
    "Photo 4×6 in (102×152 mm)": (102, 152),
    "Photo 5×7 in (127×178 mm)": (127, 178),
    "A4 (210×297 mm)": (210, 297),
    "A5 (148×210 mm)": (148, 210),
    "Instagram 1080×1080 px": "px:1080x1080"
}

def mm_to_px(mm_w: float, mm_h: float, dpi: int = 300) -> Tuple[int, int]:
    inch_w = mm_w / 25.4
    inch_h = mm_h / 25.4
    return int(round(inch_w * dpi)), int(round(inch_h * dpi))

def spec_to_pixels(key: str, dpi: int = 300) -> Optional[Tuple[int,int]]:
    spec = STANDARD_SIZES_MM.get(key)
    if spec is None:
        return None
    if isinstance(spec, str) and spec.startswith("px:"):
        parts = spec.replace("px:", "").split("x")
        return (int(parts[0]), int(parts[1]))
    w_mm, h_mm = spec
    return mm_to_px(w_mm, h_mm, dpi=dpi)

def open_image(path: str) -> Image.Image:
    img = Image.open(path)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

def estimate_compressed_size(path: str, fmt: str, quality: int, keep_exif: bool) -> Optional[int]:
    img = open_image(path)
    buf = io.BytesIO()
    fmt_uc = fmt.upper() if fmt else "JPEG"
    params = {}
    if fmt_uc in ("JPEG","JPG"):
        if img.mode in ("RGBA","P"):
            img = img.convert("RGB")
        params.update(dict(quality=int(quality), optimize=True))
    if fmt_uc == "WEBP":
        params.update(dict(quality=int(quality)))
    if keep_exif and "exif" in img.info:
        params["exif"] = img.info["exif"]
    img.save(buf, fmt_uc if fmt_uc != "ORIGINAL" else "JPEG", **params)
    return buf.tell()

def convert_and_save(path: str, out_path: str, fmt: str, size_px: Optional[Tuple[int,int]], quality: int, keep_exif: bool) -> int:
    img = open_image(path)
    if size_px:
        img.thumbnail(size_px, Image.LANCZOS)
    fmt_uc = fmt.upper() if fmt else None
    save_kwargs = {}
    if fmt_uc in ("JPEG","JPG"):
        if img.mode in ("RGBA","P"):
            img = img.convert("RGB")
        save_kwargs.update(dict(quality=int(quality), optimize=True))
    if fmt_uc == "WEBP":
        save_kwargs.update(dict(quality=int(quality)))
    if keep_exif and "exif" in img.info:
        save_kwargs["exif"] = img.info["exif"]
    fmt_to_use = None if (fmt_uc in (None, "", "ORIGINAL")) else fmt_uc
    img.save(out_path, fmt_to_use, **save_kwargs)
    return os.path.getsize(out_path)