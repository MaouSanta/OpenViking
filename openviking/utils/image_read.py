# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Shared, model-sized image reads with coordinates in the oriented full image."""

import asyncio
import base64
import io
import json
import math
from pathlib import PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

from openviking.pyagfs.exceptions import AGFSNotFoundError
from openviking.utils.media_limits import (
    MAX_INLINE_TOOL_RESULT_MEDIA_BYTES,
    MAX_MEDIA_FILE_BYTES,
    is_large_image_by_size,
)
from openviking_cli.exceptions import InvalidArgumentError, NotFoundError
from openviking_cli.utils.config.parser_config import ImageConfig

IMAGE_SOURCE_FILENAME = "image-source.json"
RASTER_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
        ".ico",
        ".dib",
        ".icns",
        ".sgi",
        ".jp2",
    }
)


def is_image_uri(uri: str) -> bool:
    return PurePosixPath(uri).suffix.lower() in RASTER_EXTENSIONS


def validate_image_region(x=None, y=None, width=None, height=None) -> None:
    if (x is None) != (y is None):
        raise InvalidArgumentError("Image coordinates x and y must be supplied together.")
    if x is None and (width is not None or height is not None):
        raise InvalidArgumentError("Image width/height require center coordinates x and y.")
    for name, value in (("x", x), ("y", y)):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise InvalidArgumentError(f"{name} must be a finite normalized coordinate in [0, 1].")
    for name, value in (("width", width), ("height", height)):
        if value is not None and (type(value) is not int or not 1 <= value <= 2048):
            raise InvalidArgumentError(f"{name} must be an integer between 1 and 2048 pixels.")


async def image_source(fs, uri: str, ctx=None) -> dict | None:
    """Read a relocatable source association; never trust paths from metadata."""
    parent, name = uri.rsplit("/", 1)
    try:
        raw = await fs.read_file_bytes(f"{parent}/{IMAGE_SOURCE_FILENAME}", ctx=ctx)
    except (NotFoundError, AGFSNotFoundError, FileNotFoundError):
        return None
    try:
        meta = json.loads(raw)
        if not isinstance(meta, dict) or meta.get("version") != 1:
            raise ValueError("unsupported image source metadata")
        for key in ("original", "preview"):
            value = meta[key]
            if (
                not isinstance(value, str)
                or value in {"", ".", ".."}
                or "/" in value
                or "\\" in value
            ):
                raise ValueError("image source paths must be sibling filenames")
        if name not in (meta["original"], meta["preview"]):
            return None
        return meta
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidArgumentError(f"Invalid {IMAGE_SOURCE_FILENAME}: {exc}") from exc


def _encode(image: Image.Image) -> tuple[bytes, str]:
    """Prefer lossless crops, falling back to bounded JPEG for noisy photographs."""
    image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = buf.getvalue()
    if len(data) <= MAX_INLINE_TOOL_RESULT_MEDIA_BYTES:
        return data, "image/png"
    for quality in (90, 80, 65, 45):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality)
        data = buf.getvalue()
        if len(data) <= MAX_INLINE_TOOL_RESULT_MEDIA_BYTES:
            return data, "image/jpeg"
    raise InvalidArgumentError(
        "Encoded image exceeds the inline media budget; request a smaller crop."
    )


def render_image(
    data: bytes,
    *,
    uri: str,
    config: ImageConfig,
    x=None,
    y=None,
    width=None,
    height=None,
    force_preview=False,
) -> dict:
    validate_image_region(x, y, width, height)
    try:
        with Image.open(io.BytesIO(data)) as source:
            # Same orientation for both preview and coordinates, including EXIF rotations.
            with ImageOps.exif_transpose(source) as image:
                original_width, original_height = image.size
                bounds = None
                large = (
                    force_preview
                    or is_large_image_by_size(
                        file_size_bytes=len(data),
                        width=original_width,
                        height=original_height,
                        config=config,
                    )
                    or len(data) > MAX_INLINE_TOOL_RESULT_MEDIA_BYTES
                )
                if x is not None:
                    w = min(width or 2048, original_width)
                    h = min(height or 2048, original_height)
                    left = max(0, min(round(x * (original_width - 1)) - w // 2, original_width - w))
                    top = max(
                        0, min(round(y * (original_height - 1)) - h // 2, original_height - h)
                    )
                    bounds = [left, top, left + w, top + h]
                    result_image = image.crop(bounds)
                    kind = "crop"
                else:
                    result_image = image.copy()
                    kind = "preview" if large else "image"
                    if large:
                        limit = min(config.preview_max_dimension, 2048)
                        result_image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
                try:
                    output, mime = _encode(result_image)
                    out_width, out_height = result_image.size
                finally:
                    result_image.close()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidArgumentError(f"Cannot decode image safely: {exc}") from exc
    instruction = (
        "图片过大，当前返回全图缩略图。请以左上角为原点，在全图上选择 0～1 的中心坐标 x、y，"
        "再次调用 read 获取高清局部。width、height 为原图像素，默认 2048，上限 2048。"
        if kind == "preview"
        else "Coordinates always refer to the oriented full image, not this crop."
    )
    text = (
        f"Source: {uri}\n{kind}: {out_width}x{out_height}; "
        f"original: {original_width}x{original_height}; bounds: {bounds}\n{instruction}"
    )
    return {
        "kind": kind,
        "uri": uri,
        "original_width": original_width,
        "original_height": original_height,
        "width": out_width,
        "height": out_height,
        "bounds": bounds,
        "text": text,
        "mime_type": mime,
        "data": base64.b64encode(output).decode("ascii"),
    }


async def read_image(
    fs, uri: str, *, ctx, config=None, x=None, y=None, width=None, height=None
) -> dict:
    """Read authorized source bytes and return a bounded preview or original-pixel crop."""
    validate_image_region(x, y, width, height)
    if not is_image_uri(uri):
        raise InvalidArgumentError("Image reads require a raster image URI.")
    # Check the requested resource before following its association.
    await fs.stat(uri, ctx=ctx)
    meta = await image_source(fs, uri, ctx)
    source_uri = f"{uri.rsplit('/', 1)[0]}/{meta['original']}" if meta else uri
    if meta is None and uri.endswith("_preview.jpg") and x is not None:
        try:
            await fs.stat(f"{uri.rsplit('/', 1)[0]}/tiles", ctx=ctx)
        except (NotFoundError, AGFSNotFoundError, FileNotFoundError):
            pass
        else:
            raise InvalidArgumentError(
                "This legacy preview has no original image. Re-import the original for HD crops."
            )
    try:
        stat = await fs.stat(source_uri, ctx=ctx)
        size = stat.get("size")
        if type(size) is not int or not 0 <= size <= MAX_MEDIA_FILE_BYTES:
            raise InvalidArgumentError(
                f"Image source size must be known and <= {MAX_MEDIA_FILE_BYTES} bytes."
            )
        read_uri = source_uri
        use_saved_preview = meta is not None and x is None
        if use_saved_preview:
            read_uri = f"{uri.rsplit('/', 1)[0]}/{meta['preview']}"
            preview_stat = await fs.stat(read_uri, ctx=ctx)
            size = preview_stat.get("size")
            if type(size) is not int or not 0 <= size <= MAX_MEDIA_FILE_BYTES:
                raise InvalidArgumentError("Preview size is unavailable or too large.")
        data = await fs.read_file_bytes(read_uri, ctx=ctx)
    except (NotFoundError, AGFSNotFoundError, FileNotFoundError) as exc:
        if meta:
            raise InvalidArgumentError(
                "Image source or preview is missing; re-import the original for HD reads."
            ) from exc
        raise
    if len(data) > size:
        raise InvalidArgumentError("Image changed after its size was checked; retry the read.")
    result = await asyncio.to_thread(
        render_image,
        data,
        uri=source_uri,
        config=config or ImageConfig(),
        force_preview=use_saved_preview,
        x=x,
        y=y,
        width=width,
        height=height,
    )
    if use_saved_preview:
        for key in ("width", "height"):
            if type(meta.get(key)) is not int or meta[key] <= 0:
                raise InvalidArgumentError(
                    "Image source dimensions are missing; re-import the image."
                )
        result.update(kind="preview", original_width=meta["width"], original_height=meta["height"])
        result["text"] = (
            f"Source: {source_uri}\npreview: {result['width']}x{result['height']}; "
            f"original: {meta['width']}x{meta['height']}\n"
            "图片过大，当前返回全图缩略图。请以左上角为原点，选择全图中 0～1 的中心坐标 x、y，"
            "再次调用 read 获取高清局部；width、height 为原图像素，默认及上限 2048。"
        )
    return result
