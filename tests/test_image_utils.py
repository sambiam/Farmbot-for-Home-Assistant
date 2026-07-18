"""Unit tests for custom_components/farmbot/image_utils.py (Pillow helpers)."""
import hashlib
import io

import pytest
from PIL import Image, ImageOps  # noqa: F401  (import surface asserted by test below)

from custom_components.farmbot import image_utils


def _make_jpeg_bytes(size=(1200, 800), color=(255, 0, 0), exif=None):
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, format="JPEG", exif=exif)
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


# --------------------------- PIL import surface ---------------------------

def test_pil_image_and_imageops_importable():
    """Part 1/8: `from PIL import Image, ImageOps` must work in this env."""
    from PIL import Image as _Image
    from PIL import ImageOps as _ImageOps

    assert hasattr(_Image, "open")
    assert hasattr(_ImageOps, "exif_transpose")


# --------------------------- resizing / aspect ratio ---------------------------

def test_process_image_resizes_preserving_aspect_ratio():
    raw = _make_jpeg_bytes(size=(1200, 800))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    assert result.width <= 640
    assert result.height <= 480
    # Original aspect ratio 1200:800 == 3:2; resized should keep that ratio.
    assert abs((result.width / result.height) - (1200 / 800)) < 0.02


@pytest.mark.parametrize(
    "box,expected",
    [
        ((640, 480), (640, 480)),
        ((960, 720), (960, 720)),
        ((1280, 960), (1280, 960)),
    ],
)
def test_process_image_native_frame_to_analysis_resolutions(box, expected):
    """Part 8: native 2592x1944 fits each requested analysis bounding box."""
    raw = _make_jpeg_bytes(size=(2592, 1944))
    result = image_utils.process_image(raw, max_width=box[0], max_height=box[1])
    assert (result.width, result.height) == expected
    assert result.source_width == 2592
    assert result.source_height == 1944
    assert result.oriented_width == 2592
    assert result.oriented_height == 1944
    assert result.resize_scale_x == pytest.approx(expected[0] / 2592)
    assert result.resize_scale_y == pytest.approx(expected[1] / 1944)


def test_process_image_non_4_3_fits_inside_box_without_stretching():
    """Part 3/8: a non-4:3 source is fitted inside the box, not cropped/stretched."""
    raw = _make_jpeg_bytes(size=(2000, 1000))  # 2:1
    result = image_utils.process_image(raw, max_width=960, max_height=720)
    # Width-bound: 960 wide, height scales to 480 (preserves 2:1), <= 720.
    assert result.width == 960
    assert result.height == 480
    assert result.resize_scale_x == pytest.approx(result.resize_scale_y)
    assert result.width / result.height == pytest.approx(2000 / 1000)


def test_process_image_never_upscales_beyond_original():
    raw = _make_jpeg_bytes(size=(100, 80))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    assert result.width == 100
    assert result.height == 80
    assert result.resize_scale_x == pytest.approx(1.0)
    assert result.resize_scale_y == pytest.approx(1.0)


def test_process_image_output_is_valid_jpeg():
    raw = _make_jpeg_bytes(size=(200, 100))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    with Image.open(io.BytesIO(result.jpeg_bytes)) as decoded:
        assert decoded.format == "JPEG"


# --------------------------- checksum contract ---------------------------

def test_process_image_sha256_is_over_returned_jpeg_not_original():
    """Part 2: sha256 must hash the returned JPEG; source_sha256 the original."""
    raw = _make_jpeg_bytes(size=(1200, 800))
    result = image_utils.process_image(raw, max_width=640, max_height=480)

    assert result.sha256 == hashlib.sha256(result.jpeg_bytes).hexdigest()
    assert result.source_sha256 == hashlib.sha256(raw).hexdigest()
    # The image was resized+re-encoded, so the two hashes must differ; the old
    # behaviour (sha256 over the original) would have made them equal here.
    assert result.jpeg_bytes != raw
    assert result.sha256 != result.source_sha256


def test_process_image_sha256_roundtrips_for_a_pass_through_image():
    """Even when no resize occurs, sha256 describes the *re-encoded* JPEG."""
    raw = _make_jpeg_bytes(size=(50, 50))
    result = image_utils.process_image(raw, max_width=640, max_height=480)
    # A decoder given image_base64/jpeg_bytes can confirm the hash.
    assert hashlib.sha256(result.jpeg_bytes).hexdigest() == result.sha256


# --------------------------- EXIF orientation ---------------------------

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
    # Source is decoded as 200x100; orientation=6 swaps to a 100x200 oriented
    # frame, and (no upscale) the output keeps those oriented dimensions.
    assert result.source_width == 200
    assert result.source_height == 100
    assert result.oriented_width == 100
    assert result.oriented_height == 200
    assert result.width == 100
    assert result.height == 200


# --------------------------- rejection paths ---------------------------

def test_process_image_rejects_corrupt_data():
    with pytest.raises(image_utils.ImageDecodeError):
        image_utils.process_image(b"not an image", max_width=640, max_height=480)


def test_process_image_rejects_empty_bytes():
    with pytest.raises(image_utils.ImageDecodeError):
        image_utils.process_image(b"", max_width=640, max_height=480)


def test_process_image_rejects_excessive_dimensions():
    """Part 8: decoded dimensions beyond the guard are rejected."""
    raw = _make_jpeg_bytes(size=(2000, 100))
    with pytest.raises(image_utils.ImageDecodeError):
        image_utils.process_image(
            raw, max_width=640, max_height=480, max_source_dimension=1000
        )


def test_process_image_rejects_excessive_pixel_count():
    """Part 8: decoded pixel count beyond the guard is rejected (bomb guard)."""
    raw = _make_jpeg_bytes(size=(1000, 1000))  # 1,000,000 px
    with pytest.raises(image_utils.ImageDecodeError):
        image_utils.process_image(
            raw, max_width=640, max_height=480, max_source_pixels=500_000
        )
