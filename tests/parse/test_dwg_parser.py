# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused checks for the deterministic DWG parser contract."""

import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.dwg import DWGParser, _normalize_libredwg_json
from openviking.parse.parsers.dwg_extract import build_drawing_text_chunks
from openviking.parse.registry import ParserRegistry


class _FakeVikingFS:
    def __init__(self):
        self.dirs = []
        self.files = {}
        self._counter = 0

    def create_temp_uri(self):
        self._counter += 1
        return f"viking://temp/dwg_{self._counter}"

    async def mkdir(self, uri, exist_ok=False):
        if uri not in self.dirs:
            self.dirs.append(uri)

    async def write_file(self, uri, content):
        self.files[uri] = content.encode("utf-8") if isinstance(content, str) else content

    async def write_file_bytes(self, uri, content):
        self.files[uri] = content

    async def delete_temp(self, uri):
        self.files = {key: value for key, value in self.files.items() if not key.startswith(uri)}
        self.dirs = [item for item in self.dirs if not item.startswith(uri)]


def _drawing_json():
    return {
        "SummaryInfo": {"TITLE": "建筑方案"},
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "图签"},
            {
                "object": "LAYOUT",
                "block_header": [0, 0, 10],
                "layout_name": "布局1",
            },
            {
                "entity": "TEXT",
                "handle": [0, 0, 100],
                "ownerhandle": [0, 0, 10],
                "ins_pt": [10, 20],
                "text_value": "项目名称：示例住宅",
            },
            {
                "entity": "TEXT",
                "handle": [0, 0, 101],
                "ownerhandle": [0, 0, 10],
                "ins_pt": [30, 40],
                "text_value": "总建筑面积：125.4㎡",
            },
            {
                "entity": "TEXT",
                "index": 102,
                "handle": [0, 0, 102],
                "ins_pt": [50, 60],
                "text_value": "KL1(1) 300x500",
            },
            {
                "entity": "TEXT",
                "index": 103,
                "ins_pt": [100, 10],
                "height": 2,
                "text_value": "2栋组串接线图",
            },
            {
                "entity": "TEXT",
                "index": 104,
                "ins_pt": [150, 10],
                "height": 2,
                "text_value": "JDDL-FLLS-20",
            },
            {"entity": "TEXT", "text_value": "125.4"},
            {"object": "PROXY_OBJECT"},
        ],
    }


def _png_bytes():
    output = BytesIO()
    Image.new("RGB", (1, 1), color="white").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_dwg_parser_writes_small_deterministic_artifacts(tmp_path: Path, monkeypatch):
    source = tmp_path / "building-plan.dwg"
    source.write_bytes(b"fake-dwg")
    fake_fs = _FakeVikingFS()
    parser = DWGParser()
    drawing = _drawing_json()

    async def fake_export(_source_path):
        return drawing, json.dumps(drawing, ensure_ascii=False).encode("utf-8"), []

    monkeypatch.setattr(parser, "_export_dwg_json", fake_export)
    with patch.object(BaseParser, "_get_viking_fs", return_value=fake_fs):
        result = await parser.parse(source)

    assert result.precomputed_semantics is False
    assert any(uri.endswith("/building-plan.dwg") for uri in fake_fs.files)
    assert not any(uri.endswith("/libredwg.json") for uri in fake_fs.files)
    assert not any(uri.endswith("/.overview.md") for uri in fake_fs.files)
    topology = json.loads(
        next(value for uri, value in fake_fs.files.items() if uri.endswith("/beam_topology.json"))
    )
    drawing_text_index = json.loads(
        next(
            value
            for uri, value in fake_fs.files.items()
            if uri.endswith("/drawing_text/index.json")
        )
    )
    drawing_text_chunk_bytes = [
        value
        for uri, value in fake_fs.files.items()
        if "/drawing_text/" in uri and not uri.endswith("/index.json")
    ]
    drawing_text_chunks = [json.loads(value) for value in drawing_text_chunk_bytes]
    assert topology["schema_version"] == "deterministic-dwg-topology-v2"
    assert topology["records"][0]["beam_id"] == "KL1"
    assert drawing_text_index["schema"] == "deterministic-dwg-text-index-v1"
    assert drawing_text_index["coverage"]["complete"] is True
    assert drawing_text_chunks[0]["schema"] == "deterministic-dwg-text-chunk-v1"
    assert b"\n" in drawing_text_chunk_bytes[0]
    assert "2栋组串接线图" in drawing_text_chunks[0]["search_text"]
    assert "JDDL-FLLS-20" in drawing_text_chunks[0]["search_text"]
    assert not any(uri.endswith("/drawing_text.json") for uri in fake_fs.files)
    assert not any(uri.endswith("/drawing_index.md") for uri in fake_fs.files)
    assert result.meta["source_sha256"]
    assert "No usable embedded DWG preview" in result.warnings
    assert any("proxy objects" in warning for warning in result.warnings)


def test_spatial_chunk_halo_keeps_adjacent_text_across_a_partition_boundary():
    def record(index, text, x):
        return {
            "i": index,
            "h": [0, 0, index],
            "t": "TEXT",
            "l": "0",
            "p": [x, 0.0],
            "r": 0.0,
            "z": 1.0,
            "raw": text,
            "txt": text,
        }

    drawing_text = {
        "source": "plan.dwg",
        "source_sha256": "abc",
        "units": {"p": "DWG drawing units"},
        "records": [
            record(1, "2栋组串接线图", 0.0),
            record(2, "JDDL-FLLS-20", 20.0),
            record(3, "远处文字", 1000.0),
        ],
    }

    index, chunks = build_drawing_text_chunks(
        drawing_text,
        max_core_records=1,
        max_core_text_chars=1,
        max_chunk_records=2,
    )

    assert index["coverage"] == {"source_records": 3, "core_records": 3, "complete": True}
    assert any(
        "2栋组串接线图" in chunk["search_text"] and "JDDL-FLLS-20" in chunk["search_text"]
        for chunk in chunks
    )


@pytest.mark.asyncio
async def test_dwg_parser_keeps_an_embedded_preview(tmp_path: Path, monkeypatch):
    source = tmp_path / "preview.dwg"
    source.write_bytes(b"fake-dwg")
    fake_fs = _FakeVikingFS()
    parser = DWGParser()
    drawing = _drawing_json()
    drawing["THUMBNAILIMAGE"] = {"chain": _png_bytes().hex()}

    async def fake_export(_source_path):
        return drawing, json.dumps(drawing, ensure_ascii=False).encode("utf-8"), []

    monkeypatch.setattr(parser, "_export_dwg_json", fake_export)
    with patch.object(BaseParser, "_get_viking_fs", return_value=fake_fs):
        await parser.parse(source)

    assert any(uri.endswith("/preview.png") for uri in fake_fs.files)


def test_registry_routes_dwg_to_the_dedicated_parser():
    parser = ParserRegistry().get_parser_for_file(Path("drawing.dwg"))
    assert isinstance(parser, DWGParser)


def test_libredwg_non_strict_json_is_normalized_for_grep():
    raw = (
        b'{"OBJECTS":[{"entity":"TEXT","text_value":"'
        + "项目名称".encode("utf-8")
        + b'"},{"object":"PROXY_OBJECT","proxy_data":"\xc6"}],"value":nan}'
    )

    drawing, normalized, warnings = _normalize_libredwg_json(raw, "sample.dwg")

    assert drawing["OBJECTS"][0]["text_value"] == "项目名称"
    assert json.loads(normalized.decode("utf-8"))["value"] is None
    assert "项目名称" in normalized.decode("utf-8")
    assert len(warnings) == 2
