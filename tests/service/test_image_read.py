# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from openviking.parse.output import LocalParseOutputStore
from openviking.parse.parsers.media.image import ImageParser
from openviking.utils.image_read import (
    IMAGE_SOURCE_FILENAME,
    image_source,
    read_image,
    render_image,
    validate_image_region,
)
from openviking_cli.exceptions import InvalidArgumentError, PermissionDeniedError
from openviking_cli.utils.config.parser_config import ImageConfig


def image_bytes(size=(5000, 3000), *, orientation=None):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((2450, 1450, 2549, 1549), fill="red")
    buf = io.BytesIO()
    if orientation:
        exif = image.getexif()
        exif[274] = orientation
        image.save(buf, "PNG", exif=exif)
    else:
        image.save(buf, "PNG")
    return buf.getvalue()


def decode(result):
    return Image.open(io.BytesIO(base64.b64decode(result["data"])))


@pytest.mark.parametrize(
    "point,bounds",
    [
        ((0.5, 0.5), [2450, 1450, 2550, 1550]),
        ((0, 0), [0, 0, 100, 100]),
        ((1, 1), [4900, 2900, 5000, 3000]),
        ((1, 0), [4900, 0, 5000, 100]),
        ((0, 1), [0, 2900, 100, 3000]),
    ],
)
def test_crop_uses_full_image_pixels(point, bounds):
    result = render_image(
        image_bytes(),
        uri="source.png",
        config=ImageConfig(),
        x=point[0],
        y=point[1],
        width=100,
        height=100,
    )
    assert result["bounds"] == bounds
    assert decode(result).size == (100, 100)
    if point == (0.5, 0.5):
        assert decode(result).getpixel((50, 50)) == (255, 0, 0)


def test_preview_and_default_crop():
    data = image_bytes()
    preview = render_image(data, uri="source.png", config=ImageConfig())
    assert preview["kind"] == "preview"
    assert decode(preview).size == (2048, 1229)
    assert "x、y" in preview["text"]
    crop = render_image(data, uri="source.png", config=ImageConfig(), x=0.5, y=0.5)
    assert crop["width"] == crop["height"] == 2048
    assert crop["bounds"] == [1476, 476, 3524, 2524]


def test_exif_orientation_and_no_upscale():
    result = render_image(
        image_bytes((50, 20), orientation=6), uri="source.png", config=ImageConfig(), x=0, y=0
    )
    assert (result["original_width"], result["original_height"]) == (20, 50)
    assert decode(result).size == (20, 50)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"x": 0.5},
        {"y": 0.5},
        {"x": float("nan"), "y": 0},
        {"x": 1.1, "y": 0},
        {"x": True, "y": 0},
        {"width": 100},
        {"x": 0, "y": 0, "width": 2049},
        {"x": 0, "y": 0, "height": 0},
        {"x": 0, "y": 0, "width": 2.5},
    ],
)
def test_invalid_region(kwargs):
    with pytest.raises(InvalidArgumentError):
        validate_image_region(**kwargs)


class MemoryFS:
    def __init__(self, files):
        self.files = files
        self.reads = []

    async def stat(self, uri, ctx=None):
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return {"size": len(self.files[uri]), "isDir": uri.endswith("/tiles")}

    async def read_file_bytes(self, uri, ctx=None):
        self.reads.append((uri, ctx))
        if uri not in self.files:
            raise FileNotFoundError(uri)
        return self.files[uri]


@pytest.mark.asyncio
async def test_original_and_preview_resolve_same_crop():
    root = "viking://resources/picture"
    original = image_bytes()
    meta = {"version": 1, "original": "source.png", "preview": "source_preview.jpg"}
    fs = MemoryFS(
        {
            f"{root}/source.png": original,
            f"{root}/source_preview.jpg": b"preview",
            f"{root}/{IMAGE_SOURCE_FILENAME}": json.dumps(meta).encode(),
        }
    )
    kwargs = {"ctx": "identity", "x": 0.5, "y": 0.5, "width": 100, "height": 100}
    a = await read_image(fs, f"{root}/source.png", **kwargs)
    b = await read_image(fs, f"{root}/source_preview.jpg", **kwargs)
    assert a == b
    assert all(ctx == "identity" for _, ctx in fs.reads)


@pytest.mark.asyncio
async def test_legacy_preview_requires_reimport_for_crop():
    fs = MemoryFS(
        {
            "viking://resources/pic_preview.jpg": image_bytes((50, 20)),
            "viking://resources/tiles": b"",
        }
    )
    with pytest.raises(InvalidArgumentError, match="Re-import"):
        await read_image(fs, "viking://resources/pic_preview.jpg", ctx=None, x=0, y=0)


@pytest.mark.asyncio
async def test_association_cannot_escape_parent_or_bypass_permissions():
    fs = MemoryFS(
        {
            "viking://resources/image-source.json": json.dumps(
                {"version": 1, "original": "../secret.png", "preview": "p.png"}
            ).encode()
        }
    )
    with pytest.raises(InvalidArgumentError, match="sibling"):
        await image_source(fs, "viking://resources/p.png")
    fs.stat = AsyncMock(side_effect=PermissionDeniedError("denied"))
    fs.reads.clear()
    with pytest.raises(PermissionDeniedError):
        await read_image(fs, "viking://resources/p.png", ctx=None)
    assert fs.reads == []


@pytest.mark.asyncio
async def test_parser_preserves_original_without_tiles(tmp_path):
    source = tmp_path / "drawing.png"
    data = image_bytes()
    source.write_bytes(data)
    result = await ImageParser().parse(
        source, parse_output_store=LocalParseOutputStore(tmp_path / "output")
    )
    root = Path(result.artifact_ref.root) / result.artifact_ref.resource_rel
    assert (root / "drawing.png").read_bytes() == data
    assert (root / "drawing_preview.jpg").is_file()
    assert not (root / "tiles").exists()
    meta = json.loads((root / IMAGE_SOURCE_FILENAME).read_text())
    assert meta["width"] == 5000
    assert meta["original"] == "drawing.png"


@pytest.mark.asyncio
async def test_summary_does_not_send_original_to_vlm(monkeypatch):
    from openviking.parse.parsers.media import utils

    fs = MemoryFS(
        {
            "viking://resources/image-source.json": json.dumps(
                {"version": 1, "original": "original.png", "preview": "preview.jpg"}
            ).encode()
        }
    )
    monkeypatch.setattr(utils, "get_viking_fs", lambda: fs)
    vlm = SimpleNamespace(get_vision_completion_async=AsyncMock())
    result = await utils.generate_image_summary(
        "viking://resources/original.png", "original.png", vlm=vlm
    )
    assert "Full-resolution" in result["summary"]
    vlm.get_vision_completion_async.assert_not_awaited()
    assert len(fs.reads) == 1


@pytest.mark.asyncio
async def test_initial_read_uses_saved_preview_without_loading_original():
    root = "viking://resources/picture"
    meta = {
        "version": 1,
        "original": "source.png",
        "preview": "source_preview.jpg",
        "width": 5000,
        "height": 3000,
    }
    fs = MemoryFS(
        {
            f"{root}/source.png": b"original bytes not needed for preview",
            f"{root}/source_preview.jpg": image_bytes((200, 120)),
            f"{root}/{IMAGE_SOURCE_FILENAME}": json.dumps(meta).encode(),
        }
    )
    result = await read_image(fs, f"{root}/source.png", ctx=None)
    assert result["kind"] == "preview"
    assert result["original_width"] == 5000
    assert f"{root}/source.png" not in [uri for uri, _ in fs.reads]


def test_pixel_limit_is_not_disabled(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(InvalidArgumentError, match="safely"):
        render_image(image_bytes((100, 100)), uri="image.png", config=ImageConfig())


@pytest.mark.asyncio
async def test_missing_original_does_not_upscale_preview():
    root = "viking://resources/picture"
    fs = MemoryFS(
        {
            f"{root}/preview.png": image_bytes((100, 100)),
            f"{root}/{IMAGE_SOURCE_FILENAME}": json.dumps(
                {
                    "version": 1,
                    "original": "missing.png",
                    "preview": "preview.png",
                }
            ).encode(),
        }
    )
    with pytest.raises(InvalidArgumentError, match="re-import"):
        await read_image(fs, f"{root}/preview.png", ctx=None, x=0, y=0)


@pytest.mark.asyncio
async def test_thin_panorama_ingestion(tmp_path):
    source = tmp_path / "panorama.png"
    source.write_bytes(image_bytes((10000, 1)))
    result = await ImageParser().parse(
        source, parse_output_store=LocalParseOutputStore(tmp_path / "output")
    )
    root = Path(result.artifact_ref.root) / result.artifact_ref.resource_rel
    with Image.open(root / "panorama_preview.jpg") as preview:
        assert preview.size == (2048, 1)


@pytest.mark.asyncio
async def test_embedding_uses_saved_preview_without_reading_original(monkeypatch):
    from openviking.utils import embedding_utils

    root = "viking://resources/picture"
    fs = MemoryFS(
        {
            f"{root}/{IMAGE_SOURCE_FILENAME}": json.dumps(
                {
                    "version": 1,
                    "original": "source.png",
                    "preview": "preview.png",
                }
            ).encode(),
            f"{root}/preview.png": image_bytes((100, 100)),
        }
    )
    monkeypatch.setattr(
        embedding_utils, "get_openviking_config", lambda: SimpleNamespace(image=ImageConfig())
    )
    output = await embedding_utils._build_image_data_uri(
        f"{root}/source.png", "source.png", fs, None
    )
    assert output.startswith("data:image/png;base64,")
    assert len(fs.reads) == 2
    assert f"{root}/source.png" not in [uri for uri, _ in fs.reads]


@pytest.mark.asyncio
async def test_preview_cannot_bypass_original_access_check():
    root = "viking://resources/picture"
    fs = MemoryFS(
        {
            f"{root}/preview.png": image_bytes((100, 100)),
            f"{root}/{IMAGE_SOURCE_FILENAME}": json.dumps(
                {
                    "version": 1,
                    "original": "private.png",
                    "preview": "preview.png",
                }
            ).encode(),
        }
    )
    original_stat = fs.stat

    async def stat(uri, ctx=None):
        if uri.endswith("/private.png"):
            raise PermissionDeniedError("private image")
        return await original_stat(uri, ctx=ctx)

    fs.stat = stat
    with pytest.raises(PermissionDeniedError):
        await read_image(fs, f"{root}/preview.png", ctx="viewer", x=0, y=0)
    assert [uri for uri, _ in fs.reads] == [f"{root}/{IMAGE_SOURCE_FILENAME}"]
