# tests/test_image_utils.py
# Covers utils/image_utils.py: size/dimension resolution, the compression-
# effort mapping, and format conversion. Uses real temp files (tmp_path) and
# real Pillow images -- no mocking the filesystem or PIL.
import pytest
from PIL import Image

from utils.image_utils import (
    mm_to_px,
    spec_to_pixels,
    _compression_kwargs,
    estimate_compressed_size,
    convert_and_save,
)


# ---------------------------------------------------------------------------
# mm_to_px / spec_to_pixels
# ---------------------------------------------------------------------------

def test_mm_to_px_default_dpi():
    # 35x45mm passport photo at 300 dpi
    w, h = mm_to_px(35, 45)
    assert (w, h) == (413, 531)


def test_mm_to_px_custom_dpi():
    w, h = mm_to_px(25.4, 25.4, dpi=100)
    assert (w, h) == (100, 100)


def test_spec_to_pixels_original_is_none():
    assert spec_to_pixels("Original") is None


def test_spec_to_pixels_px_spec():
    assert spec_to_pixels("Instagram 1080×1080 px") == (1080, 1080)


def test_spec_to_pixels_mm_spec_matches_mm_to_px():
    key = "Passport – India (35×45 mm)"
    assert spec_to_pixels(key, dpi=300) == mm_to_px(35, 45, dpi=300)


def test_spec_to_pixels_unknown_key_returns_none():
    assert spec_to_pixels("Not A Real Size") is None


# ---------------------------------------------------------------------------
# _compression_kwargs -- the effort-dial mapping added in the last batch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("effort,expected_compress_level", [
    (1, 2), (4, 6), (6, 9),
])
def test_compression_kwargs_png_maps_effort_to_compress_level(effort, expected_compress_level):
    assert _compression_kwargs("PNG", effort) == {"compress_level": expected_compress_level}


def test_compression_kwargs_webp_passes_effort_through_as_method():
    assert _compression_kwargs("WEBP", 3) == {"method": 3}


def test_compression_kwargs_jpeg_is_a_noop():
    assert _compression_kwargs("JPEG", 5) == {}


def test_compression_kwargs_tiff_is_a_noop():
    assert _compression_kwargs("TIFF", 5) == {}


def test_compression_kwargs_clamps_out_of_range_effort():
    # effort is clamped to [1, 6] before mapping
    assert _compression_kwargs("PNG", 99) == _compression_kwargs("PNG", 6)
    assert _compression_kwargs("PNG", -5) == _compression_kwargs("PNG", 1)


def test_compression_kwargs_defaults_when_falsy():
    # compression=None/0 falls back to 4 ("balanced")
    assert _compression_kwargs("PNG", None) == _compression_kwargs("PNG", 4)


# ---------------------------------------------------------------------------
# convert_and_save -- real files, real Pillow round trips
# ---------------------------------------------------------------------------

def _make_source_image(tmp_path, mode="RGB", size=(200, 100), fmt="JPEG", name="src.jpg"):
    path = tmp_path / name
    Image.new(mode, size, color=(120, 80, 40) if mode == "RGB" else 200).save(path, fmt)
    return str(path)


def test_convert_and_save_resizes_within_bounding_box(tmp_path):
    src = _make_source_image(tmp_path, size=(400, 200))
    out = str(tmp_path / "out.jpg")
    size = convert_and_save(src, out, "JPEG", (100, 100), quality=85, keep_exif=False)
    with Image.open(out) as im:
        # thumbnail() preserves aspect ratio and fits the longest edge
        assert im.size[0] <= 100 and im.size[1] <= 100
        assert im.size[0] == 100  # long edge (width, 2:1 ratio) hits the cap
    assert size == __import__("os").path.getsize(out)


def test_convert_and_save_no_size_keeps_original_dimensions(tmp_path):
    src = _make_source_image(tmp_path, size=(64, 48))
    out = str(tmp_path / "out.jpg")
    convert_and_save(src, out, "JPEG", None, quality=90, keep_exif=False)
    with Image.open(out) as im:
        assert im.size == (64, 48)


def test_convert_and_save_format_conversion_jpeg_to_png(tmp_path):
    src = _make_source_image(tmp_path, fmt="JPEG", name="src.jpg")
    out = str(tmp_path / "out.png")
    convert_and_save(src, out, "PNG", None, quality=100, keep_exif=False)
    with Image.open(out) as im:
        assert im.format == "PNG"


def test_convert_and_save_none_format_preserves_source_format(tmp_path):
    src = _make_source_image(tmp_path, fmt="PNG", name="src.png")
    out = str(tmp_path / "out.png")
    convert_and_save(src, out, None, None, quality=90, keep_exif=False)
    with Image.open(out) as im:
        assert im.format == "PNG"


def test_convert_and_save_rgba_converted_to_rgb_for_jpeg(tmp_path):
    # JPEG has no alpha channel -- RGBA source must not crash the save
    src = _make_source_image(tmp_path, mode="RGBA", fmt="PNG", name="src.png")
    out = str(tmp_path / "out.jpg")
    convert_and_save(src, out, "JPEG", None, quality=85, keep_exif=False)
    with Image.open(out) as im:
        assert im.mode == "RGB"


def test_convert_and_save_creates_missing_destination_directory(tmp_path):
    src = _make_source_image(tmp_path)
    out = str(tmp_path / "nested" / "deeper" / "out.jpg")
    convert_and_save(src, out, "JPEG", None, quality=80, keep_exif=False)
    assert __import__("os").path.exists(out)


def test_convert_and_save_unsupported_format_raises(tmp_path):
    src = _make_source_image(tmp_path)
    out = str(tmp_path / "out.bogus")
    with pytest.raises(Exception):
        convert_and_save(src, out, "NOT_A_REAL_FORMAT", None, quality=80, keep_exif=False)


def test_convert_and_save_zero_byte_source_raises(tmp_path):
    src = tmp_path / "empty.jpg"
    src.write_bytes(b"")
    out = str(tmp_path / "out.jpg")
    with pytest.raises(Exception):
        convert_and_save(str(src), out, "JPEG", None, quality=80, keep_exif=False)


def test_estimate_compressed_size_returns_positive_int(tmp_path):
    src = _make_source_image(tmp_path, size=(64, 64))
    size = estimate_compressed_size(src, "JPEG", quality=80, keep_exif=False)
    assert isinstance(size, int)
    assert size > 0


def test_estimate_compressed_size_higher_quality_is_not_smaller(tmp_path):
    # Real Pillow JPEG behavior: higher quality should not produce a smaller
    # buffer than lower quality for the same source image.
    src = _make_source_image(tmp_path, size=(128, 128))
    low = estimate_compressed_size(src, "JPEG", quality=10, keep_exif=False)
    high = estimate_compressed_size(src, "JPEG", quality=95, keep_exif=False)
    assert high >= low
