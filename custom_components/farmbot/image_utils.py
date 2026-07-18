"""Synchronous Pillow-based image decode/orientation/resize helpers.

This module is intentionally synchronous and CPU-bound: callers (the
``farmbot.get_vision_image`` service handler) must invoke ``process_image``
via ``hass.async_add_executor_job`` so decoding never blocks the Home
Assistant event loop. Only one image is processed per call -- multiple
images are never decoded concurrently.

Kept deliberately minimal: Pillow only, no OpenCV/NumPy/ML libraries. This
module does no FarmBot-specific computer vision; it only prepares a
downloaded image for transport back to the FarmBot Vision app.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

JPEG_QUALITY = 85


class ImageDecodeError(Exception):
    """Raised when image bytes cannot be safely decoded or resized."""


@dataclass
class ProcessedImage:
    jpeg_bytes: bytes
    width: int
    height: int
    sha256: str
    # Contract v2 provenance: the app validates resize scales and dimensions
    # against these so it can calibrate the exact processed pixels.
    source_width: int
    source_height: int
    oriented_width: int
    oriented_height: int
    resize_scale_x: float
    resize_scale_y: float
    source_sha256: str


def process_image(raw: bytes, *, max_width: int, max_height: int) -> ProcessedImage:
    """Decode, correct EXIF orientation, resize and re-encode as JPEG.

    The aspect ratio is preserved; the result never exceeds
    ``max_width`` x ``max_height``. ``sha256`` is computed over the re-encoded
    JPEG the app actually receives (so the app can verify the returned bytes);
    ``source_sha256`` is over the original download. ``source_*`` are the raw
    dimensions before EXIF orientation, ``oriented_*`` after it, and the resize
    scales are ``width / oriented_width`` and ``height / oriented_height``.
    """
    source_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()  # force full decode here so corrupt data raises in this try
            source_width, source_height = opened.width, opened.height
            image = ImageOps.exif_transpose(opened)
            if image is None:
                raise ImageDecodeError("image had no data after orientation correction")
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            oriented_width, oriented_height = image.width, image.height
            image.thumbnail((max_width, max_height), Image.LANCZOS)

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
            jpeg_bytes = buffer.getvalue()
            return ProcessedImage(
                jpeg_bytes=jpeg_bytes,
                width=image.width,
                height=image.height,
                sha256=hashlib.sha256(jpeg_bytes).hexdigest(),
                source_width=source_width,
                source_height=source_height,
                oriented_width=oriented_width,
                oriented_height=oriented_height,
                resize_scale_x=image.width / oriented_width,
                resize_scale_y=image.height / oriented_height,
                source_sha256=source_sha256,
            )
    except ImageDecodeError:
        raise
    except UnidentifiedImageError as err:
        raise ImageDecodeError("unrecognized image format") from err
    except Exception as err:  # noqa: BLE001 - Pillow raises many distinct decode errors
        raise ImageDecodeError(f"failed to decode image: {err}") from err
