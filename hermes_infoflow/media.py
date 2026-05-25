"""Outbound image preparation for Infoflow native image messages."""

from __future__ import annotations

import io
from dataclasses import dataclass

from .utils import _ImageLoadError

INFOFLOW_IMAGE_MAX_BYTES = 1 * 1024 * 1024
IMAGE_LOAD_MAX_BYTES = 60 * 1024 * 1024
IMAGE_MAX_PIXELS = 40_000_000

_COMPRESS_MAX_SIDES = (2048, 1536, 1280, 1024, 800)
_COMPRESS_QUALITIES = (80, 70, 60, 50, 40)

_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
}
_INFOFLOW_NATIVE_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


@dataclass(frozen=True)
class PreparedInfoflowImage:
    """Image bytes ready to be base64 encoded for Infoflow."""

    data: bytes
    mime_type: str
    image_format: str
    original_size: int
    final_size: int
    compressed: bool


def prepare_infoflow_image_bytes(
    data: bytes,
    *,
    max_bytes: int = INFOFLOW_IMAGE_MAX_BYTES,
) -> PreparedInfoflowImage:
    """Validate and shrink image bytes for Infoflow's native image API."""
    if not data:
        raise _ImageLoadError("image payload is empty")

    image_format, mime_type = _inspect_image(data)
    if len(data) <= max_bytes and image_format in _INFOFLOW_NATIVE_FORMATS:
        return PreparedInfoflowImage(
            data=data,
            mime_type=mime_type,
            image_format=image_format,
            original_size=len(data),
            final_size=len(data),
            compressed=False,
        )

    if image_format == "GIF":
        raise _ImageLoadError(
            f"image/gif payload exceeds Infoflow {max_bytes} byte limit"
        )

    compressed = _compress_to_jpeg(data, max_bytes=max_bytes)
    compressed_format, compressed_mime = _inspect_image(compressed)
    return PreparedInfoflowImage(
        data=compressed,
        mime_type=compressed_mime,
        image_format=compressed_format,
        original_size=len(data),
        final_size=len(compressed),
        compressed=True,
    )


def _pillow_modules():
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise _ImageLoadError(
            "Pillow is required to send native Infoflow images"
        ) from exc

    Image.MAX_IMAGE_PIXELS = IMAGE_MAX_PIXELS
    return Image, ImageOps, UnidentifiedImageError


def _inspect_image(data: bytes) -> tuple[str, str]:
    Image, _, UnidentifiedImageError = _pillow_modules()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = (image.format or "").upper()
            if image_format not in _MIME_BY_FORMAT:
                raise _ImageLoadError("image format is not supported")
            width, height = image.size
            if width <= 0 or height <= 0:
                raise _ImageLoadError("image dimensions are invalid")
            if width * height > IMAGE_MAX_PIXELS:
                raise _ImageLoadError("image dimensions exceed safety limit")
            image.verify()
            return image_format, _MIME_BY_FORMAT[image_format]
    except UnidentifiedImageError as exc:
        raise _ImageLoadError("image payload is not a valid image") from exc
    except _ImageLoadError:
        raise
    except Exception as exc:
        raise _ImageLoadError("failed to inspect image payload") from exc


def _compress_to_jpeg(data: bytes, *, max_bytes: int) -> bytes:
    Image, ImageOps, UnidentifiedImageError = _pillow_modules()
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise _ImageLoadError("image dimensions are invalid")
            if width * height > IMAGE_MAX_PIXELS:
                raise _ImageLoadError("image dimensions exceed safety limit")
            image = ImageOps.exif_transpose(image)
            image = _to_rgb_with_white_background(image, Image)

            for side in _COMPRESS_MAX_SIDES:
                sized = image.copy()
                sized.thumbnail((side, side), Image.Resampling.LANCZOS)
                for quality in _COMPRESS_QUALITIES:
                    out = io.BytesIO()
                    sized.save(
                        out,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=False,
                        subsampling="4:2:0",
                    )
                    payload = out.getvalue()
                    if len(payload) <= max_bytes:
                        return payload
    except UnidentifiedImageError as exc:
        raise _ImageLoadError("image payload is not a valid image") from exc
    except _ImageLoadError:
        raise
    except Exception as exc:
        raise _ImageLoadError("failed to compress image payload") from exc

    raise _ImageLoadError(f"image cannot be compressed under {max_bytes} bytes")


def _to_rgb_with_white_background(image, Image):
    has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
    if not has_alpha:
        return image.convert("RGB")

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background
