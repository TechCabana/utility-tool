import io, math, os
from typing import Tuple, Optional
from PIL import Image, ImageOps, ExifTags

# Map EXIF orientation tag
EXIF_ORIENTATION_TAG = None
for k, v in ExifTags.TAGS.items():
    if v == "Orientation":
        EXIF_ORIENTATION_TAG = k
        break

# mm to pixels at given DPI (default 300 for print)
def mm_to_px(w_mm: float, h_mm: float, dpi: int = 300) -> Tuple[int, int]:
    return int(round(w_mm/25.4*dpi)), int(round(h_mm/25.4*dpi))

# Catalog of standard sizes (mm)
STANDARD_SIZES_MM = {
    "Original": None,
    # Paper
    "A0 (841x1189 mm)": (841, 1189),
    "A1 (594x841 mm)": (594, 841),
    "A2 (420x594 mm)": (420, 594),
    "A3 (297x420 mm)": (297, 420),
    "A4 (210x297 mm)": (210, 297),
    "A5 (148x210 mm)": (148, 210),
    "A6 (105x148 mm)": (105, 148),
    # Photo
    "4x6 in (102x152 mm)": (102, 152),
    "5x7 in (127x178 mm)": (127, 178),
    "8x10 in (203x254 mm)": (203, 254),
    "Square 1:1 (100x100 mm)": (100, 100),
    # Passport / ID
    "Passport (India 35x45 mm)": (35, 45),
    "Passport (Netherlands 35x45 mm)": (35, 45),
    "Passport (US 51x51 mm)": (51, 51),
}

def open_image(path: str) -> Image.Image:
    img = Image.open(path)
    # auto-orient by EXIF
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img

def resize_if_needed(img: Image.Image, size_key: str, dpi: int = 300) -> Image.Image:
    spec = STANDARD_SIZES_MM.get(size_key)
    if not spec:
        return img.copy()
    w_px, h_px = mm_to_px(*spec, dpi=dpi)
    return img.copy().resize((w_px, h_px), Image.LANCZOS)

def save_with_format(img: Image.Image, out_path: str, fmt: str, quality: int, keep_exif: bool):
    params = {}
    fmt_upper = fmt.upper()
    if fmt_upper == "ORIGINAL":
        fmt_upper = None  # let Pillow infer from extension
    if fmt_upper in ("JPEG", "JPG"):
        params.update(dict(quality=quality, optimize=True, progressive=True, subsampling="keep"))
    elif fmt_upper == "PNG":
        params.update(dict(optimize=True))
    elif fmt_upper == "WEBP":
        params.update(dict(quality=quality, method=6))
    elif fmt_upper == "TIFF":
        params.update(dict(compression="tiff_lzw"))

    # EXIF handling
    if keep_exif and "exif" in img.info:
        params["exif"] = img.info["exif"]

    img.save(out_path, fmt_upper if fmt_upper else None, **params)

def estimate_output_size(img: Image.Image, fmt: str, quality: int, keep_exif: bool) -> int:
    buf = io.BytesIO()
    save_with_format(img, buf, fmt, quality, keep_exif)
    return buf.tell()  # bytes
