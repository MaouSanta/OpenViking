# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused checks for the deterministic DWG parser contract."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.dwg import DWGParser, _normalize_libredwg_json
from openviking.parse.parsers.dwg_sheet import (
    _split_body,
    _table_markdown,
    build_sheet_artifacts,
)
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


@pytest.mark.asyncio
async def test_dwg_parser_writes_sheet_bundles_without_preview(tmp_path: Path, monkeypatch):
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
    document = json.loads(
        next(value for uri, value in fake_fs.files.items() if uri.endswith("/document.json"))
    )
    structure = json.loads(
        next(value for uri, value in fake_fs.files.items() if uri.endswith("/structure.json"))
    )
    assert topology["schema_version"] == "deterministic-dwg-topology-v2"
    assert topology["records"][0]["beam_id"] == "KL1"
    assert document["schema"] == "openviking.dwg-document/v1"
    assert document["sheet_count"] == 1
    assert structure["schema"] == "openviking.dwg-sheet-structure/v1"
    assert structure["coverage"]["complete"] is True
    assert structure["coverage"]["no_per_label_coordinates"] is True
    assert "point" not in json.dumps(structure)
    assert any(uri.endswith("/content.md") for uri in fake_fs.files)
    assert not any("drawing_text" in uri or "preview" in uri for uri in fake_fs.files)
    assert result.meta["document_manifest"] == "document.json"
    assert result.meta["sheet_bundles"][0]["content_paths"]
    assert result.meta["source_sha256"]
    assert any("proxy objects" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_dwg_parser_ignores_an_embedded_preview(tmp_path: Path, monkeypatch):
    source = tmp_path / "preview.dwg"
    source.write_bytes(b"fake-dwg")
    fake_fs = _FakeVikingFS()
    parser = DWGParser()
    drawing = _drawing_json()
    drawing["THUMBNAILIMAGE"] = {"chain": "89504e470d0a1a0a"}

    async def fake_export(_source_path):
        return drawing, json.dumps(drawing, ensure_ascii=False).encode("utf-8"), []

    monkeypatch.setattr(parser, "_export_dwg_json", fake_export)
    with patch.object(BaseParser, "_get_viking_fs", return_value=fake_fs):
        await parser.parse(source)

    assert not any(uri.endswith(("/preview.png", "/preview.webp")) for uri in fake_fs.files)


def test_sheet_parser_accepts_flag512_model_frames_skips_empty_layout_and_bounds_markdown():
    objects = [
        {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
        {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "*Paper_Space"},
        {
            "object": "LAYOUT",
            "handle": [0, 0, 21],
            "block_header": [0, 0, 20],
            "layout_name": "布局1",
        },
        {
            "entity": "LWPOLYLINE",
            "index": 30,
            "handle": [0, 0, 30],
            "entmode": 2,
            "flag": 512,
            "points": [[0, 0], [84100, 0], [84100, 59400], [0, 59400]],
        },
        {
            "entity": "TABLE",
            "index": 31,
            "handle": [0, 0, 31],
            "entmode": 2,
            "rows": [["名称", "数量"], ["梁", "2"]],
        },
    ]
    objects.extend(
        {
            "entity": "TEXT",
            "index": 100 + index,
            "handle": [0, 0, 100 + index],
            "entmode": 2,
            "ins_pt": [1000 + index, 50000 - index],
            "height": 250,
            "text_value": "图纸名称：结构平面图" if index == 0 else f"房间{index} " + "说明" * 80,
        }
        for index in range(80)
    )

    result = build_sheet_artifacts(
        {"OBJECTS": objects},
        source_name="structure.dwg",
        source_sha256="a" * 64,
        source_size_bytes=123,
    )

    assert result["document"]["sheet_count"] == 1
    sheet = result["sheets"][0]
    assert sheet["kind"] == "model_frame"
    assert sheet["source_locator"]["kind"] == "closed_polyline"
    assert all(len(content) <= 6000 for _, content in sheet["content_files"])
    assert sheet["structure"]["coverage"]["complete"] is True
    table = sheet["structure"]["tables"][0]
    assert table["cells"][0] == {
        "row": 0,
        "column": 0,
        "row_span": 1,
        "column_span": 1,
        "role": "column_header",
        "text": "名称",
        "source_refs": [{"object_index": 31, "handle": 31}],
    }


def test_open_four_point_polyline_is_not_invented_as_a_sheet_frame():
    result = build_sheet_artifacts(
        {
            "OBJECTS": [
                {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
                {
                    "entity": "LWPOLYLINE",
                    "index": 30,
                    "handle": [0, 0, 30],
                    "entmode": 2,
                    "flag": 0,
                    "points": [[0, 0], [84100, 0], [84100, 59400], [0, 59400]],
                },
                {
                    "entity": "TEXT",
                    "index": 31,
                    "handle": [0, 0, 31],
                    "entmode": 2,
                    "ins_pt": [1000, 1000],
                    "height": 250,
                    "text_value": "一层平面图",
                },
            ]
        },
        source_name="open.dwg",
        source_sha256="d" * 64,
        source_size_bytes=1,
    )

    assert not any(sheet["kind"] == "model_frame" for sheet in result["sheets"])


def test_layout_with_viewport_is_explicitly_marked_incomplete_until_projection():
    result = build_sheet_artifacts(
        {
            "OBJECTS": [
                {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "*Paper_Space"},
                {
                    "object": "LAYOUT",
                    "handle": [0, 0, 21],
                    "block_header": [0, 0, 20],
                    "layout_name": "平面图",
                    "viewports": [[0, 0, 22]],
                },
                {
                    "entity": "VIEWPORT",
                    "index": 22,
                    "handle": [0, 0, 22],
                    "entmode": 1,
                },
            ]
        },
        source_name="layout.dwg",
        source_sha256="e" * 64,
        source_size_bytes=1,
    )

    structure = result["sheets"][0]["structure"]
    assert structure["sheet"]["viewport_projection"] == "unresolved"
    assert structure["sheet"]["source_locator"]["viewport_handles"] == [22]
    assert structure["coverage"]["viewport_projection_complete"] is False
    assert structure["coverage"]["complete"] is False


def test_named_title_block_does_not_suppress_the_outer_sheet_frame():
    result = build_sheet_artifacts(
        {
            "OBJECTS": [
                {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
                {"object": "BLOCK_HEADER", "handle": [0, 0, 100], "name": "标准图签"},
                {
                    "entity": "LWPOLYLINE",
                    "index": 101,
                    "handle": [0, 0, 101],
                    "ownerhandle": [0, 0, 100],
                    "flag": 512,
                    "points": [[0, 0], [10000, 0], [10000, 10000], [0, 10000]],
                },
                {
                    "entity": "LWPOLYLINE",
                    "index": 30,
                    "handle": [0, 0, 30],
                    "entmode": 2,
                    "flag": 512,
                    "points": [[0, 0], [84100, 0], [84100, 59400], [0, 59400]],
                },
                {
                    "entity": "INSERT",
                    "index": 40,
                    "handle": [0, 0, 40],
                    "entmode": 2,
                    "block_header": [0, 0, 100],
                    "ins_pt": [70000, 0],
                    "scale": [1, 1, 1],
                },
                {
                    "entity": "TEXT",
                    "index": 50,
                    "handle": [0, 0, 50],
                    "entmode": 2,
                    "ins_pt": [1000, 50000],
                    "height": 250,
                    "text_value": "首层平面图",
                },
            ]
        },
        source_name="frame.dwg",
        source_sha256="f" * 64,
        source_size_bytes=1,
    )

    frames = [sheet for sheet in result["sheets"] if sheet["kind"] == "model_frame"]
    assert len(frames) == 1
    assert frames[0]["source_locator"]["source_ref"]["handle"] == 30


def test_oversized_table_header_does_not_create_one_file_per_character():
    body = f"| {'说明' * 4000} |\n| --- |\n| 普通行 |"

    parts = _split_body("表格", body, "# Sheet", max_tokens=2048, max_chars=6000)

    assert 1 < len(parts) < 10
    assert all(len(part) <= 6000 for part in parts)


def test_multilevel_table_headers_are_flattened_for_markdown():
    markdown = _table_markdown(
        [["截面尺寸", ""], ["宽", "高"], ["100", "200"]],
        header_rows=2,
        spans=[{"row": 0, "column": 0, "column_span": 2}],
    )

    assert "| 截面尺寸 / 宽 | 截面尺寸 / 高 |" in markdown


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
