"""Unit tests for custom_components/farmbot/image_utils.py (Pillow helpers)."""
import io

import pytest
from PIL import Image

from custom_components.farmbot import image_utils


def _make_jpeg_bytes(size=(1200, 800), color=(255, 0, 0), exif=None):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


def test_process_image_resizes_preserving_aspect_ratio():
    raw = _make_jpeg_bytes(size=(1200, 800))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    assert result.width <= 640
    assert result.height <= 480
    # Original aspect ratio 1200:800 == 3:2; resized should keep that ratio.
    assert abs((result.width / result.height) - (1200 / 800)) < 0.02


def test_process_image_never_upscales_beyond_original():
    raw = _make_jpeg_bytes(size=(100, 80))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    assert result.width == 100
    assert result.height == 80


def test_process_image_output_is_valid_jpeg():
    raw = _make_jpeg_bytes(size=(200, 100))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    with Image.open(io.BytesIO(result.jpeg_bytes)) as decoded:
        assert decoded.format == "JPEG"


def test_process_image_sha256_is_over_returned_jpeg():
    # The app verifies the bytes it actually receives, so sha256 is over the
    # re-encoded JPEG; source_sha256 remains over the original download.
    import hashlib
    raw = _make_jpeg_bytes(size=(50, 50))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    assert result.sha256 == hashlib.sha256(result.jpeg_bytes).hexdigest()
    assert result.source_sha256 == hashlib.sha256(raw).hexdigest()


def test_process_image_emits_v2_dimension_metadata():
    raw = _make_jpeg_bytes(size=(1200, 800))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    assert (result.source_width, result.source_height) == (1200, 800)
    assert (result.oriented_width, result.oriented_height) == (1200, 800)
    assert result.resize_scale_x == pytest.approx(result.width / 1200)
    assert result.resize_scale_y == pytest.approx(result.height / 800)


def test_process_image_v2_metadata_after_exif_rotation():
    img = Image.new("RGB", (200, 100), (0, 255, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation tag -> becomes 100x200
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    result = image_utils.process_image(buf.getvalue(), max_width=1000, max_height=1000)
    assert (result.source_width, result.source_height) == (200, 100)
    assert (result.oriented_width, result.oriented_height) == (100, 200)


def test_process_image_corrects_exif_orientation():
    # EXIF orientation 6 = rotate 270deg (needs a 90deg CW rotation to display
    # upright), taken on a portrait-oriented sensor recording landscape data.
    img = Image.new("RGB", (200, 100), (0, 255, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation tag
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    raw = buf.getvalue()

    result = image_utils.process_image(raw, max_width=1000, max_height=1000)
    # After orientation correction, a 200x100 source tagged orientation=6
    # should present as 100x200 (width/height swapped).
    assert result.width == 100
    assert result.height == 200


def test_process_image_rejects_corrupt_data():
    with pytest.raises(image_utils.ImageDecodeError):
        image_utils.process_image(b"not an image", max_width=640, max_height=480)


def test_process_image_rejects_empty_bytes():
    with pytest.raises(image_utils.ImageDecodeError):
        image_utils.process_image(b"", max_width=640, max_height=480)
