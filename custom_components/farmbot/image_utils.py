"""Synchronous Pillow-based image decode/orientation/resize helpers.

This module is intentionally synchronous and CPU-bound: callers (the
``farmbot.get_vision_image`` service handler) must invoke ``process_image``
via ``hass.async_add_executor_job`` so decoding never blocks the Home
Assistant event loop. Only one image is processed per call -- multiple
images are never decoded concurrently.

Kept deliberately minimal: Pillow only, no OpenCV/NumPy/ML libraries. This
module does no FarmBot-specific computer vision; it only prepares a
downloaded image for transport back to the FarmBot Vision app.

Pillow itself is provided by Home Assistant Core (it is a Core dependency,
pinned in Core's ``package_constraints.txt``); the FarmBot integration does
*not* declare Pillow in its manifest, to avoid a version conflict with that
constraint. See ``docs``/README for the dependency rationale.

Dimension contract returned by :func:`process_image`:

- ``source_width`` / ``source_height``   -- immediately after decode, before
  EXIF orientation correction.
- ``oriented_width`` / ``oriented_height`` -- after EXIF orientation
  correction, before resizing. This is the coordinate system FarmBot camera
  calibration is expressed in.
- ``width`` / ``height``                  -- the JPEG actually returned to the
  companion app.
- ``resize_scale_x`` = ``width / oriented_width``
- ``resize_scale_y`` = ``height / oriented_height``

Checksum contract:

- ``sha256`` is computed over the exact JPEG bytes placed in ``jpeg_bytes``
  (and, upstream, in ``image_base64``), so the companion app can decode the
  transported image and confirm the hash.
- ``source_sha256`` is computed over the original downloaded bytes. It is
  supplementary and never replaces ``sha256``.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

from .const import MAX_SOURCE_IMAGE_DIMENSION, MAX_SOURCE_IMAGE_PIXELS

JPEG_QUALITY = 85


class ImageDecodeError(Exception):
    """Raised when image bytes cannot be safely decoded or resized."""


@dataclass(frozen=True)
class CaptureImageQuality:
    usable: bool
    reason: str
    clipped_neutral_fraction: float = 0.0
    contrast: float = 0.0
    laplacian_energy: float = 0.0


def inspect_capture_image(raw: bytes) -> CaptureImageQuality:
    """Conservatively reject undecodable, washed-out, or severely blurred captures."""
    if not raw:
        return CaptureImageQuality(False, "downloaded image was empty")
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            if oriented is None:
                return CaptureImageQuality(False, "image had no decodable pixels")
            try:
                rgb = oriented.convert("RGB")
                rgb.thumbnail((640, 480), Image.Resampling.LANCZOS)
                if rgb.width < 32 or rgb.height < 32:
                    return CaptureImageQuality(False, "image dimensions were too small")
                grey = rgb.convert("L")
                try:
                    luminance = grey.histogram()
                    total = max(1, sum(luminance))

                    def percentile(fraction: float) -> int:
                        threshold = total * fraction
                        cumulative = 0
                        for value, count in enumerate(luminance):
                            cumulative += count
                            if cumulative >= threshold:
                                return value
                        return 255

                    neutral_highlights = sum(
                        1
                        for red, green, blue in rgb.get_flattened_data()
                        if max(red, green, blue) >= 235
                        and max(red, green, blue) - min(red, green, blue) <= 85
                    )
                    clipped_fraction = neutral_highlights / total
                    median = percentile(0.50)
                    lower = percentile(0.10)
                    contrast = float(ImageStat.Stat(grey).stddev[0])
                    washed_out = clipped_fraction >= 0.30 and median >= 214 and lower >= 148

                    laplacian = grey.filter(
                        ImageFilter.Kernel(
                            (3, 3),
                            (0, 1, 0, 1, -4, 1, 0, 1, 0),
                            scale=1,
                            offset=128,
                        )
                    )
                    try:
                        cropped = laplacian.crop((2, 2, laplacian.width - 2, laplacian.height - 2))
                        histogram = cropped.histogram()
                        count = max(1, sum(histogram))
                        laplacian_energy = sum(
                            ((value - 128) ** 2) * frequency
                            for value, frequency in enumerate(histogram)
                        ) / count
                    finally:
                        laplacian.close()
                    blurry = laplacian_energy <= 5.0 and contrast >= 6.4
                finally:
                    grey.close()
                    rgb.close()
            finally:
                if oriented is not opened:
                    oriented.close()
    except (UnidentifiedImageError, OSError, ValueError) as err:
        return CaptureImageQuality(False, f"image could not be decoded: {err}")

    if washed_out:
        return CaptureImageQuality(
            False,
            f"image was washed out ({clipped_fraction:.0%} neutral highlights)",
            clipped_fraction,
            contrast,
            laplacian_energy,
        )
    if blurry:
        return CaptureImageQuality(
            False,
            f"image was blurry (detail score {laplacian_energy:.2f})",
            clipped_fraction,
            contrast,
            laplacian_energy,
        )
    return CaptureImageQuality(
        True,
        "usable",
        clipped_fraction,
        contrast,
        laplacian_energy,
    )


@dataclass
class ProcessedImage:
    """The result of preparing one downloaded image for the Vision app.

    ``sha256`` is the hash of ``jpeg_bytes`` (the returned image), *not* the
    original download -- that is ``source_sha256``.
    """

    jpeg_bytes: bytes
    source_width: int
    source_height: int
    oriented_width: int
    oriented_height: int
    width: int
    height: int
    resize_scale_x: float
    resize_scale_y: float
    sha256: str
    source_sha256: str


def process_image(
    raw: bytes,
    *,
    max_width: int,
    max_height: int,
    max_source_dimension: int = MAX_SOURCE_IMAGE_DIMENSION,
    max_source_pixels: int = MAX_SOURCE_IMAGE_PIXELS,
) -> ProcessedImage:
    """Decode, correct EXIF orientation, resize and re-encode as JPEG.

    The aspect ratio is preserved and the image is never upscaled; the
    result fits inside ``max_width`` x ``max_height`` (high-quality Lanczos
    downsampling). Decoded geometry is bounded by ``max_source_dimension``
    and ``max_source_pixels`` to defend against decompression bombs -- a
    small compressed file that would decode to an enormous bitmap.

    ``sha256`` is computed over the returned JPEG bytes; ``source_sha256``
    over ``raw``.
    """
    if not raw:
        raise ImageDecodeError("empty image bytes")

    source_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            source_width, source_height = opened.size
            # Reject implausible geometry *before* forcing a full decode, so a
            # decompression bomb never gets the chance to allocate its bitmap.
            if source_width <= 0 or source_height <= 0:
                raise ImageDecodeError("image reported non-positive dimensions")
            if source_width > max_source_dimension or source_height > max_source_dimension:
                raise ImageDecodeError(
                    f"image dimension exceeds limit "
                    f"({source_width}x{source_height} > {max_source_dimension})"
                )
            if source_width * source_height > max_source_pixels:
                raise ImageDecodeError(
                    f"image pixel count exceeds limit "
                    f"({source_width * source_height} > {max_source_pixels})"
                )

            opened.load()  # force full decode here so corrupt data raises in this try

            oriented = ImageOps.exif_transpose(opened)
            if oriented is None:
                raise ImageDecodeError("image had no data after orientation correction")
            try:
                oriented_width, oriented_height = oriented.size
                if oriented_width <= 0 or oriented_height <= 0:
                    raise ImageDecodeError("oriented image reported non-positive dimensions")

                if oriented.mode not in ("RGB", "L"):
                    converted = oriented.convert("RGB")
                    if converted is not oriented:
                        oriented.close()
                    oriented = converted

                # thumbnail() shrinks in place, preserves aspect ratio, and
                # never upscales; Lanczos gives high-quality downsampling.
                oriented.thumbnail((max_width, max_height), Image.LANCZOS)
                width, height = oriented.size

                buffer = io.BytesIO()
                try:
                    oriented.save(buffer, format="JPEG", quality=JPEG_QUALITY)
                    jpeg_bytes = buffer.getvalue()
                finally:
                    buffer.close()
            finally:
                oriented.close()

        sha256 = hashlib.sha256(jpeg_bytes).hexdigest()
        return ProcessedImage(
            jpeg_bytes=jpeg_bytes,
            source_width=source_width,
            source_height=source_height,
            oriented_width=oriented_width,
            oriented_height=oriented_height,
            width=width,
            height=height,
            resize_scale_x=width / oriented_width,
            resize_scale_y=height / oriented_height,
            sha256=sha256,
            source_sha256=source_sha256,
        )
    except ImageDecodeError:
        raise
    except UnidentifiedImageError as err:
        raise ImageDecodeError("unrecognized image format") from err
    except Exception as err:  # noqa: BLE001 - Pillow raises many distinct decode errors
        raise ImageDecodeError(f"failed to decode image: {err}") from err
