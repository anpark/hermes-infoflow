from __future__ import annotations

import io

import pytest
from PIL import Image

from hermes_infoflow.media import INFOFLOW_IMAGE_MAX_BYTES, prepare_infoflow_image_bytes
from hermes_infoflow.utils import (
    _ImageLoadError,
    _downloaded_image_ext,
    _image_download_urls_from_raw_json,
)


def _png_bytes(width: int = 1, height: int = 1) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 20, 20)).save(out, format="PNG")
    return out.getvalue()


def _jpeg_bytes(width: int = 1, height: int = 1) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (20, 120, 200)).save(out, format="JPEG")
    return out.getvalue()


def test_prepare_infoflow_image_keeps_small_native_image() -> None:
    data = _png_bytes()

    prepared = prepare_infoflow_image_bytes(data)

    assert prepared.data == data
    assert prepared.mime_type == "image/png"
    assert prepared.final_size == len(data)
    assert prepared.compressed is False


def test_downloaded_image_ext_sniffs_octet_stream_jpeg() -> None:
    assert _downloaded_image_ext("application/octet-stream", _jpeg_bytes()) == ".jpg"


def test_image_download_urls_extract_private_pic_url() -> None:
    raw = (
        '{"FromUserId":"chengbo05","MsgType":"image",'
        '"PicUrl":"http://xp2.im.baidu.com/dev/getImg?fileid=abc",'
        '"MsgId":"1867772059026843836"}'
    )

    assert _image_download_urls_from_raw_json(raw) == [
        "http://xp2.im.baidu.com/dev/getImg?fileid=abc"
    ]


def test_prepare_infoflow_image_rejects_non_image_payload() -> None:
    with pytest.raises(_ImageLoadError, match="valid image"):
        prepare_infoflow_image_bytes(b"not an image")


def test_prepare_infoflow_image_compresses_large_png_under_limit() -> None:
    image = Image.effect_noise((1800, 1800), 100).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG")
    data = out.getvalue()
    assert len(data) > INFOFLOW_IMAGE_MAX_BYTES

    prepared = prepare_infoflow_image_bytes(data)

    assert prepared.compressed is True
    assert prepared.mime_type == "image/jpeg"
    assert prepared.final_size <= INFOFLOW_IMAGE_MAX_BYTES


def test_prepare_infoflow_image_rejects_large_gif() -> None:
    image = Image.effect_noise((1400, 1400), 100).convert("L")
    out = io.BytesIO()
    image.save(out, format="GIF")
    data = out.getvalue()
    assert len(data) > INFOFLOW_IMAGE_MAX_BYTES

    with pytest.raises(_ImageLoadError, match="image/gif payload exceeds"):
        prepare_infoflow_image_bytes(data)
