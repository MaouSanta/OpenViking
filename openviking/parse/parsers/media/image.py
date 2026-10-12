# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Media parser interfaces for OpenViking - Future expansion.

This module defines parser interfaces for media types (image, audio, video).
These are placeholder implementations that raise NotImplementedError.
They serve as a design reference for future media parsing capabilities.

For current document parsing (PDF, Markdown, HTML, Text), see other parser modules.
"""

import io
import json
from pathlib import Path
from typing import List, Optional, Union

from PIL import Image, ImageOps

from openviking.parse.base import NodeType, ParseResult, ResourceNode
from openviking.parse.output import create_parse_artifact_writer
from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.media.constants import IMAGE_EXTENSIONS
from openviking.parse.parsers.media.large_image_processor import (
    create_low_res_preview,
    needs_large_image_processing,
    save_image_to_bytes,
)
from openviking.parse.parsers.media.naming import resolve_media_names
from openviking.parse.parsers.media.utils import _convert_svg_to_png
from openviking.storage.viking_fs import get_viking_fs
from openviking.utils.image_read import IMAGE_SOURCE_FILENAME
from openviking_cli.utils.config.parser_config import ImageConfig
from openviking_cli.utils.logger import get_logger
from openviking_cli.utils.uri import VikingURI

logger = get_logger(__name__)

# =============================================================================
# Configuration Classes
# =============================================================================


# =============================================================================
# Parser Classes
# =============================================================================


class ImageParser(BaseParser):
    """
    Image parser - Future implementation.

    Planned Features:
    1. Visual content understanding using VLM (Vision Language Model)
    2. OCR text extraction for images containing text
    3. Metadata extraction (dimensions, format, EXIF data)
    4. Generate semantic description and structured ResourceNode

    Example workflow:
        1. Load image file
        2. (Optional) Perform OCR to extract text
        3. (Optional) Use VLM to generate visual description
        4. Create ResourceNode with image metadata and descriptions
        5. Return ParseResult

    Supported formats: PNG, JPG, JPEG, GIF, BMP, WEBP, SVG, TIFF, ICO, DIB, ICNS, SGI, JP2
    """

    def __init__(self, config: Optional[ImageConfig] = None, **kwargs):
        """
        Initialize ImageParser.

        Args:
            config: Image parsing configuration
            **kwargs: Additional configuration parameters
        """
        self.config = config or ImageConfig()

    @property
    def supported_extensions(self) -> List[str]:
        """Return supported image file extensions."""
        return IMAGE_EXTENSIONS

    async def parse(self, source: Union[str, Path], instruction: str = "", **kwargs) -> ParseResult:
        """
        Parse image file:
        - For large images (>10MB or any dimension >4096px):
          - Preserve the original and an oriented preview
          - Read high-resolution regions on demand
        - For small images: just save as-is with metadata

        Args:
            source: Image file path
            **kwargs: Additional parsing parameters

        Returns:
            ParseResult with image content

        Raises:
            FileNotFoundError: If source file doesn't exist
            ValueError: If image processing fails
        """
        # Convert to Path object
        file_path = Path(source) if isinstance(source, str) else source
        if not file_path.exists():
            raise FileNotFoundError(f"Image file not found: {source}")

        # Load image (SVG is converted to PNG first since PIL doesn't support it)
        # For other formats not natively supported by VLM/embedding, convert to PNG
        # to ensure end-to-end compatibility with image understanding and vectorization
        try:
            file_bytes = file_path.read_bytes()
            if (
                file_path.suffix.lower() == ".svg"
                or file_bytes[:4] == b"<svg"
                or (file_bytes[:5] == b"<?xml" and b"<svg" in file_bytes[:100])
            ):
                png_bytes = _convert_svg_to_png(file_bytes)
                if png_bytes is None:
                    raise ValueError("SVG files require cairosvg. Install it: pip install cairosvg")
                img = Image.open(io.BytesIO(png_bytes))
                img.verify()
                img.close()
                img = Image.open(io.BytesIO(png_bytes))
                # Save as PNG in output
                needs_png_conversion = True
            else:
                # Check if extension is in VLM-natively supported list
                suffix_lower = file_path.suffix.lower()
                supported_by_vlm = suffix_lower in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".bmp",
                    ".webp",
                }
                if supported_by_vlm:
                    img = Image.open(file_path)
                    img.verify()  # Verify that it's a valid image
                    img.close()  # Close and reopen to reset after verify()
                    img = Image.open(file_path)
                    needs_png_conversion = False
                else:
                    # Convert unsupported formats to PNG for VLM compatibility
                    img = Image.open(file_path)
                    img.verify()
                    img.close()
                    img = Image.open(file_path)
                    needs_png_conversion = True
            width, height = img.size
            format_str = img.format or file_path.suffix[1:].upper() if file_path.suffix else "JPEG"
        except Exception as e:
            raise ValueError(f"Invalid image file: {file_path}. Error: {e}") from e

        # Resolve names
        display_stem, stem, original_filename = resolve_media_names(
            file_path, file_path.suffix, **kwargs
        )
        ext_no_dot = file_path.suffix[1:] if file_path.suffix else "jpg"
        if needs_png_conversion:
            # Convert to PNG and change extension for VLM/embedding compatibility
            ext_no_dot = "png"
            original_filename = f"{stem}.png"
            format_str = "png"

        root_dir_name = VikingURI.sanitize_segment(f"{stem}_{ext_no_dot}")
        output_store = kwargs.get("parse_output_store")
        writer = await create_parse_artifact_writer(
            output_store, viking_fs=get_viking_fs() if output_store is None else None
        )
        try:
            await writer.mkdir(root_dir_name)

            is_large = needs_large_image_processing(file_path, width, height, self.config)

            # Create root node with metadata
            root_node = ResourceNode(
                type=NodeType.ROOT,
                title=display_stem,
                level=0,
                detail_file=None,
                content_path=None,
                children=[],
                meta={
                    "width": width,
                    "height": height,
                    "format": format_str.lower(),
                    "content_type": "image",
                    "source_title": display_stem,
                    "semantic_name": display_stem,
                    "original_filename": original_filename,
                    "is_large_image": is_large,
                },
            )

            if needs_png_conversion:
                image_bytes = save_image_to_bytes(img, format="PNG")
            else:
                image_bytes = file_bytes
            await writer.write_bytes(f"{root_dir_name}/{original_filename}", image_bytes)

            if is_large:
                preview_filename = f"{stem}_preview.jpg"
                with ImageOps.exif_transpose(img) as oriented:
                    width, height = oriented.size
                    preview_bytes = create_low_res_preview(oriented, self.config)
                await writer.write_bytes(f"{root_dir_name}/{preview_filename}", preview_bytes)
                # A visible sidecar survives directory import, sync and export.
                # Only sibling names are stored, so moving the resource is safe.
                await writer.write_bytes(
                    f"{root_dir_name}/{IMAGE_SOURCE_FILENAME}",
                    json.dumps(
                        {
                            "version": 1,
                            "original": original_filename,
                            "preview": preview_filename,
                            "width": width,
                            "height": height,
                            "coordinate_space": "exif_transposed",
                        }
                    ).encode(),
                )
                root_node.meta.update(
                    {
                        "preview_filename": preview_filename,
                        "width": width,
                        "height": height,
                        "original_width": width,
                        "original_height": height,
                    }
                )
            img.close()

            artifact_ref = await writer.finalize(resource_rel=root_dir_name)
        except BaseException:
            img.close()
            await writer.cleanup()
            raise

        # Phase 3: Build directory structure (handled by TreeBuilder)
        return ParseResult(
            root=root_node,
            source_path=str(file_path),
            temp_dir_path=artifact_ref.root,
            artifact_ref=artifact_ref,
            source_format="image",
            parser_name="ImageParser",
            meta={
                "content_type": "image",
                "format": format_str.lower(),
                "is_large_image": is_large,
            },
        )

        # Write to files in temp directory
        # await viking_fs.write_file(f"{root_dir_uri}/.abstract.md", abstract)
        # await viking_fs.write_file(f"{root_dir_uri}/.overview.md", overview)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, instruction: str = "", **kwargs
    ) -> ParseResult:
        """
        Parse image from content string - Not yet implemented.

        Args:
            content: Image content (base64 or binary string)
            source_path: Optional source path for metadata
            **kwargs: Additional parsing parameters

        Returns:
            ParseResult with image content

        Raises:
            NotImplementedError: This feature is not yet implemented
        """
        raise NotImplementedError("Image parsing not yet implemented")
