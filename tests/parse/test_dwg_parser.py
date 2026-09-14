# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused checks for the deterministic DWG parser contract."""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.dwg import DWGParser, _normalize_libredwg_json
from openviking.parse.parsers.dwg_sheet import (
    _document_column_anchors,
    _document_flow,
    _select_frames,
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
async def test_dwg_parser_preserves_json_and_never_exports_sheet_dwg(tmp_path: Path, monkeypatch):
    source = tmp_path / "building-plan.dwg"
    source.write_bytes(b"fake-dwg")
    fake_fs = _FakeVikingFS()
    parser = DWGParser()
    drawing = _drawing_json()

    async def fake_export(_source_path):
        return drawing, json.dumps(drawing, ensure_ascii=False).encode("utf-8"), []

    def fake_sheet_export(_source_path, sheets):
        raise AssertionError("The parser must not invoke the old Sheet DWG exporter")

    monkeypatch.setattr(parser, "_export_dwg_json", fake_export)
    with patch.object(BaseParser, "_get_viking_fs", return_value=fake_fs):
        result = await parser.parse(source, sheet_dwg_exporter=fake_sheet_export)

    assert result.precomputed_semantics is False
    assert result.parser_version == "10.1"
    assert any(uri.endswith("/building-plan.dwg") for uri in fake_fs.files)
    assert not any(uri.endswith("/libredwg.json") for uri in fake_fs.files)
    assert not any(uri.endswith("/.overview.md") for uri in fake_fs.files)
    assert not any(
        uri.endswith(("/document.json", "/beam_topology.json", "/structure.json"))
        for uri in fake_fs.files
    )
    assert any(uri.endswith("/context.md") for uri in fake_fs.files)
    assert not any(uri.endswith("/sheet.dwg") for uri in fake_fs.files)
    assert any(uri.endswith(".json") for uri in fake_fs.files)
    assert not any("drawing_text" in uri or "preview" in uri for uri in fake_fs.files)
    assert not {"document_manifest", "beam_topology"} & result.meta.keys()
    assert result.meta["document_id"].startswith("dwg-")
    assert result.meta["sheet_bundles"][0]["content_paths"]
    assert "structure_path" not in result.meta["sheet_bundles"][0]
    assert "dwg_path" not in result.meta["sheet_bundles"][0]
    assert isinstance(result.meta["sheet_bundles"][0]["json_artifacts"], list)
    assert result.meta["json_artifacts"]
    assert any(Path(uri).name == "metadata.json" for uri in fake_fs.files)
    assert not any(
        segment in uri.split("/")
        for uri in fake_fs.files
        for segment in ("catalog", "shared", "unassigned", "views", "chunks")
    )
    assert result.meta["parse_status"] == "partial"
    assert result.meta["sheet_count"] == 0
    assert result.meta["sheet_bundle_count"] == 1
    assert result.meta["detection_statistics"]["unframed_bundle_count"] == 1
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
        await parser.parse(source, sheet_dwg_exporter=lambda *_args: ({}, []))

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
            "ins_pt": [1000, 49000],
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
        region_classifier=lambda *_: {
            "regions": [{"type": "document", "bbox": [0, 0, 84100, 59400]}]
        },
        sheet_renderer=lambda _: b"fixture-image",
    )

    assert result["document"]["sheet_count"] == 1
    sheet = result["sheets"][0]
    assert sheet["kind"] == "model_frame"
    assert sheet["source_locator"]["kind"] == "closed_polyline"
    assert sheet["region_diagnostic"]["status"] == "used"
    assert sheet["title"] == "结构平面图"
    assert all(re.fullmatch(r"context(?:_\d{3})?\.md", name) for name, _ in sheet["content_files"])
    assert "## 图纸元数据" in "\n".join(content for _, content in sheet["content_files"])
    assert all(len(content) <= 6000 for _, content in sheet["content_files"])
    assert sheet["status"] == "complete"
    markdown = "\n".join(content for _, content in sheet["content_files"])
    assert "| 名称 | 数量 |" in markdown
    assert "| 梁 | 2 |" in markdown
    assert len(sheet["markdown_uids"]) == 80
    assert objects[4]["rows"] == [["名称", "数量"], ["梁", "2"]]


def test_document_sheet_restores_column_order_and_inline_table_independent_of_entity_order():
    columns = [10.0, 260.0, 510.0, 760.0]
    headings = [
        (columns[0], 900, "1.工程概况"),
        (columns[0], 650, "2.设计总则"),
        (columns[0], 400, "3.设计与控制等级"),
        (columns[1], 900, "4.主要设计依据"),
        (columns[1], 650, "5.地基基础"),
        (columns[1], 400, "6.主要结构材料"),
        (columns[2], 900, "7.1结构混凝土环境类别及耐久性要求"),
        (columns[2], 700, "7.2钢筋混凝土保护层厚度"),
        (columns[2], 400, "7.3纵向受拉钢筋最小锚固及搭接长度"),
        (columns[3], 900, "8.砌体与混凝土墙连接要求"),
        (columns[3], 700, "9.施工要求"),
        (columns[3], 500, "10.防雷接地做法详电施图"),
        (columns[3], 300, "11.其它"),
    ]

    def text(index, x, y, value):
        return {
            "uid": f"u{index}",
            "text": value,
            "point": (x, y),
            "height": 8.0,
            "record": {"index": index, "handle": [0, 0, index]},
            "instance_path": [],
        }

    records = [text(index, x, y, value) for index, (x, y, value) in enumerate(headings, 1)]
    records.extend(
        text(100 + index, columns[index % 4] + 20, 150 - index, "这是一条完整的结构设计说明文字")
        for index in range(72)
    )
    tables = [
        {
            "title": "几何表格",
            "rows": [
                ["环境类别", "板、墙、壳", "梁、柱、杆"],
                ["一", "15", "20"],
                ["二a", "20", "25"],
                ["二b", "25", "35"],
            ],
            "header_rows": 1,
            "spans": [],
            "bbox": [510, 520, 720, 650],
            "source_refs": [{"object_index": 999, "handle": 999}],
            "_text_uids": [],
        }
    ]
    bbox = (0.0, 0.0, 1000.0, 1000.0)
    anchors = _document_column_anchors(records, bbox, set())

    assert anchors == columns
    blocks, _ = _document_flow(records, tables, set(), anchors)
    reversed_blocks, _ = _document_flow(list(reversed(records)), tables, set(), anchors)
    assert blocks == reversed_blocks
    content = "\n".join(blocks)
    ordered = [
        "1.工程概况",
        "2.设计总则",
        "3.设计与控制等级",
        "4.主要设计依据",
        "5.地基基础",
        "6.主要结构材料",
        "7.1结构混凝土环境类别及耐久性要求",
        "7.2钢筋混凝土保护层厚度",
        "| 环境类别 | 板、墙、壳 | 梁、柱、杆 |",
        "7.3纵向受拉钢筋最小锚固及搭接长度",
        "8.砌体与混凝土墙连接要求",
        "9.施工要求",
        "10.防雷接地做法详电施图",
        "11.其它",
    ]
    assert [content.index(value) for value in ordered] == sorted(
        content.index(value) for value in ordered
    )
    assert "## 2.设计总则" not in content


def test_document_flow_orders_a_table_from_its_caption_not_its_grid_top():
    records = [
        {
            "uid": "3.1",
            "text": "3.1 本工程主体结构设计使用年限为50年。",
            "point": (10, 110),
            "height": 5,
            "record": {"index": 1, "handle": [0, 0, 1]},
            "instance_path": [],
        },
        {
            "uid": "3.3",
            "text": "3.3 钢筋混凝土结构的抗震等级：",
            "point": (10, 50),
            "height": 5,
            "record": {"index": 3, "handle": [0, 0, 3]},
            "instance_path": [],
        },
    ]
    tables = [
        {
            "title": "3.2 结构设计等级：",
            "rows": [["安全等级", "二级"], ["抗震类别", "丙类"]],
            "header_rows": 1,
            "spans": [],
            "bbox": [10, 60, 80, 85],
            "anchor": (10, 90),
            "source_refs": [{"object_index": 2, "handle": 2}],
            "_text_uids": [],
        }
    ]

    blocks, _ = _document_flow(records, tables, set(), [10])

    assert blocks[0].startswith("3.1 本工程主体结构")
    assert blocks[1].startswith("3.2 结构设计等级：")
    assert blocks[2] == "3.3 钢筋混凝土结构的抗震等级："


def test_document_flow_inlines_native_checkbox_state_at_the_label_position():
    records = [
        {
            "uid": f"option-{index}",
            "text": label,
            "point": (10, 100 - index * 10),
            "height": 5,
            "record": {"index": index, "handle": [0, 0, index]},
            "instance_path": [],
        }
        for index, label in enumerate(("基坑支护工程", "滑模工程"), 1)
    ]
    markers = [
        {"_label_uid": "option-1", "state": "selected", "label": "基坑支护工程"},
        {"_label_uid": "option-2", "state": "unselected", "label": "滑模工程"},
    ]

    blocks, _ = _document_flow(records, [], set(), [10], markers)

    assert blocks == ["[x] 基坑支护工程", "[ ] 滑模工程"]


def test_document_flow_inlines_checkbox_state_inside_a_table_cell():
    records = [
        {
            "uid": "table-option",
            "text": "☑ 基坑支护工程",
            "point": (20, 70),
            "height": 5,
            "record": {"index": 1, "handle": [0, 0, 1]},
            "instance_path": [],
        }
    ]
    tables = [
        {
            "title": "几何表格",
            "rows": [["选项"], ["☑ 基坑支护工程"]],
            "header_rows": 1,
            "spans": [],
            "bbox": [10, 50, 90, 90],
            "anchor": (10, 90),
            "source_refs": [{"object_index": 2, "handle": 2}],
            "cell_source_refs": {"1:0": [{"object_index": 1, "handle": 1}]},
            "_text_uids": ["table-option"],
        }
    ]
    markers = [{"_label_uid": "table-option", "state": "selected", "label": "基坑支护工程"}]

    blocks, _ = _document_flow(records, tables, set(), [10], markers)

    assert "| [x] 基坑支护工程 |" in blocks[0]
    assert "☑" not in blocks[0]


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

    sheet = result["sheets"][0]
    assert sheet["kind"] == "synthetic"
    assert result["document"]["sheet_count"] == 0
    assert result["document"]["geometry_diagnostics"][0]["viewport_handles"] == [22]
    assert sheet["region_diagnostic"]["status"] == "unsupported_sheet"
    assert sheet["status"] == "partial"
    assert not sheet["markdown_uids"]


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


def test_repeated_electrical_block_bounds_do_not_displace_the_real_outer_frame():
    # Source 89fdddd5: one real frame and two repeated narrow electrical symbols.
    outer = {
        "source_kind": "closed_polyline",
        "source_ref": {"object_index": 5768, "handle": 43419},
        "block_name": "华天图框标准",
        "bbox": [53747748.438134015, 8671345.958182577, 53895588.438134015, 8775889.958182577],
    }
    symbols = [
        {
            "source_kind": "block_bbox",
            "source_ref": {"object_index": ordinal, "handle": handle},
            "block_name": "$element$00000532",
            "bbox": bbox,
        }
        for ordinal, handle, bbox in (
            (
                36751,
                167517,
                [53783515.663021035, 8727078.959970865, 53783875.13932135, 8728876.341472466],
            ),
            (
                36889,
                167749,
                [53824173.25170295, 8727078.959970865, 53824532.72800326, 8728876.341472466],
            ),
        )
    ]

    x0, y0, x1, y1 = outer["bbox"]
    outer["boundary"] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
    texts = [{"uid": "title", "text": "图号：E-01", "point": [x0 + 1000, y0 + 1000], "height": 250}]
    selected = _select_frames({"frames": [outer, *symbols], "texts": texts})
    assert [f["source_ref"]["handle"] for f in selected] == [43419]


def test_repeated_component_palette_does_not_create_sheets_outside_real_frame_blocks():
    # 11-PL: large A034-CN blocks are sheets; repeated *U symbols above them are not.
    frames = [
        {
            "source_kind": "block_bbox",
            "source_ref": {"handle": handle},
            "block_name": "A034-CN",
            "bbox": bbox,
        }
        for handle, bbox in (
            (
                125467,
                [-416908.7970944941, -14441.47104265018, -104796.2970944941, 111708.52895734982],
            ),
            (
                125472,
                [383091.2029055059, -14441.47104265018, 695203.7029055059, 111708.52895734982],
            ),
        )
    ]
    palette = [
        {
            "source_kind": "block_bbox",
            "source_ref": {"handle": handle},
            "block_name": "*U",
            "bbox": bbox,
        }
        for handle, bbox in (
            (
                43464,
                [-23480.431645522578, 203070.07682279585, -22280.431645522578, 203670.07682279585],
            ),
            (
                43504,
                [-32488.431645522593, 199070.07682279585, -31288.431645522593, 199670.07682279585],
            ),
        )
    ]

    texts = []
    for index, frame in enumerate(frames):
        x0, y0, x1, y1 = frame["bbox"]
        frame["source_kind"] = "closed_polyline"
        frame["boundary"] = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        texts.append(
            {
                "uid": str(index),
                "text": f"图号：E-{index}",
                "point": [x0 + 1000, y0 + 1000],
                "height": 250,
            }
        )
    selected = _select_frames({"frames": [*frames, *palette], "texts": texts})
    assert [f["source_ref"]["handle"] for f in selected] == [125467, 125472]


def test_repeated_top_level_frames_win_over_one_cross_sheet_container():
    objects = [
        {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
        {"object": "BLOCK_HEADER", "handle": [0, 0, 100], "name": "专业总容器"},
        {
            "entity": "LWPOLYLINE",
            "handle": [0, 0, 101],
            "ownerhandle": [0, 0, 100],
            "flag": 512,
            "points": [[-100, -2100], [2900, -2100], [2900, 100], [-100, 100]],
        },
        {
            "entity": "INSERT",
            "index": 40,
            "handle": [0, 0, 40],
            "entmode": 2,
            "block_header": [0, 0, 100],
            "ins_pt": [0, 0],
            "scale": [1, 1, 1],
        },
    ]
    for index, bottom in enumerate((0, -1000, -2000), 1):
        handle = 20 + index
        objects.extend(
            [
                {
                    "entity": "LWPOLYLINE",
                    "index": handle,
                    "handle": [0, 0, handle],
                    "entmode": 2,
                    "flag": 512,
                    "points": [
                        [0, bottom],
                        [200, bottom],
                        [200, bottom + 60],
                        [0, bottom + 60],
                    ],
                },
                {
                    "entity": "TEXT",
                    "index": 50 + index,
                    "handle": [0, 0, 50 + index],
                    "entmode": 2,
                    "ins_pt": [10, bottom + 30],
                    "height": 5,
                    "text_value": f"图纸名称：第{index}张图",
                },
            ]
        )
    objects.append(
        {
            "entity": "TEXT",
            "index": 99,
            "handle": [0, 0, 99],
            "entmode": 2,
            "ins_pt": [2500, 5000],
            "height": 5,
            "text_value": "模板残留文字",
        }
    )

    result = build_sheet_artifacts(
        {"OBJECTS": objects},
        source_name="stacked.dwg",
        source_sha256="1" * 64,
        source_size_bytes=1,
    )

    frames = [sheet for sheet in result["sheets"] if sheet["kind"] == "model_frame"]
    assert len(frames) == 3
    assert {sheet["source_locator"]["source_ref"]["handle"] for sheet in frames} == {21, 22, 23}
    assert result["document"]["sheet_count"] == 3


def test_oversized_table_header_does_not_create_one_file_per_character():
    body = f"| {'说明' * 4000} |\n| --- |\n| 普通行 |"

    parts = _split_body("表格", body, "# Sheet", max_tokens=2048, max_chars=6000)

    assert 1 < len(parts) < 10
    assert all(len(part) <= 6000 for part in parts)


def test_graphical_sheet_keeps_labels_native_without_a_figure_catalog():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
            {
                "entity": "LWPOLYLINE",
                "handle": [0, 0, 20],
                "entmode": 2,
                "flag": 512,
                "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
            {
                "entity": "TEXT",
                "handle": [0, 0, 21],
                "entmode": 2,
                "ins_pt": [10, 80],
                "height": 5,
                "text_value": "女儿墙节点图内标注",
            },
        ]
    }
    result = build_sheet_artifacts(
        drawing,
        source_name="figures.dwg",
        source_sha256="f" * 64,
        source_size_bytes=1,
        region_classifier=lambda *_: {"regions": []},
        sheet_renderer=lambda _: b"fixture-image",
    )
    sheet = result["sheets"][0]
    markdown = "\n".join(content for _, content in sheet["content_files"])
    assert "图形目录" not in markdown and "女儿墙节点图内标注" not in markdown
    assert not sheet["markdown_uids"] and sheet["status"] == "complete"
    assert sheet["title"] == "Sheet 001"
    assert sheet["_texts"][0]["record"] is drawing["OBJECTS"][2]


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


def test_libredwg_control_character_fallback_is_scoped():
    raw = b'{"OBJECTS":[{"entity":"TEXT","text_value":"A\x01B"}]}'

    drawing, normalized, warnings = _normalize_libredwg_json(raw, "sample.dwg")

    assert drawing["OBJECTS"][0]["text_value"] == "A\x01B"
    assert b"A\\u0001B" in normalized
    assert json.loads(normalized)["OBJECTS"][0]["text_value"] == "A\x01B"
    assert any("non-strict" in warning for warning in warnings)

    malformed = b'{"OBJECTS":[{"text_value":"A\x01B"}]'
    with pytest.raises(RuntimeError, match=r"LibreDWG produced invalid JSON for broken\.dwg"):
        _normalize_libredwg_json(malformed, "broken.dwg")
