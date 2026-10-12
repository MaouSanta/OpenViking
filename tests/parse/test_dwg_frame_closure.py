# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Only an existing native closing LINE can turn an open U into a frame."""

from copy import deepcopy

import pytest

from openviking.parse.parsers.dwg_json import build_json_artifacts, read_json_records
from openviking.parse.parsers.dwg_sheet import _drawing_graph, _walk_block, build_sheet_artifacts


def drawing_with_closing_line():
    return {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
            {
                "entity": "LWPOLYLINE",
                "index": 30,
                "handle": [0, 0, 30],
                "entmode": 2,
                "flag": 0,
                "points": [[0, 0], [100, 0], [100, 80], [0, 80]],
            },
            {
                "entity": "LINE",
                "index": 32,
                "handle": [0, 0, 32],
                "entmode": 2,
                "start": [0, 80, 0],
                "end": [0, 0, 0],
            },
            {
                "entity": "TEXT",
                "index": 31,
                "handle": [0, 0, 31],
                "entmode": 2,
                "ins_pt": [10, 10],
                "height": 2,
                "text_value": "图纸名称：结构平面图",
            },
        ]
    }


@pytest.mark.parametrize("line_first", [False, True])
def test_open_frame_uses_original_closing_line_and_preserves_native_fields(line_first):
    drawing = drawing_with_closing_line()
    if line_first:
        drawing["OBJECTS"][1:3] = reversed(drawing["OBJECTS"][1:3])
        line = drawing["OBJECTS"][1]
        line["start"], line["end"] = line["end"], line["start"]
    before = deepcopy(drawing)
    walked = _walk_block(10, _drawing_graph(drawing))
    frame = next(f for f in walked["frames"] if f["source_kind"] == "polyline_with_line")
    assert frame["bbox"] == (0, 0, 100, 80)
    assert frame["source_ref"] == {
        "handle": 30,
        "object_index": 30,
        "closing_line": {"handle": 32, "object_index": 32},
    }
    assert len(walked["lines"]) == 4  # Three real polyline edges plus the real LINE.
    assert all(line["record"] in drawing["OBJECTS"] for line in walked["lines"])
    sheets = build_sheet_artifacts(
        drawing,
        source_name="open.dwg",
        source_sha256="d" * 64,
        source_size_bytes=1,
        region_classifier=None,
    )["sheets"]
    assert len(sheets) == 1 and sheets[0]["kind"] == "model_frame"
    assert sheets[0]["source_locator"]["kind"] == "polyline_with_line"
    assert sheets[0]["source_locator"]["source_ref"] == frame["source_ref"]
    preserved = build_json_artifacts(drawing, sheets)
    restored = read_json_records(preserved["files"].values())
    objects = {r["object_index"]: r["value"] for r in restored if r.get("kind") == "object"}
    assert [objects[i] for i in range(len(before["OBJECTS"]))] == before["OBJECTS"]
    assert drawing == before


@pytest.mark.parametrize("invalid", ["missing", "gap", "curved", "other_instance"])
def test_open_polyline_needs_a_complete_closing_line_in_its_own_instance(invalid):
    drawing = drawing_with_closing_line()
    line = drawing["OBJECTS"][2]
    if invalid == "missing":
        drawing["OBJECTS"].remove(line)
    elif invalid == "gap":
        line["end"] = [0, 1e-4, 0]
    elif invalid == "curved":
        drawing["OBJECTS"][1]["bulges"] = [0.5, 0, 0, 0]
    else:
        line.pop("entmode")
        line["ownerhandle"] = [0, 0, 20]
        drawing["OBJECTS"].extend(
            [
                {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "other"},
                {
                    "entity": "INSERT",
                    "handle": [0, 0, 40],
                    "entmode": 2,
                    "block_header": [0, 0, 20],
                    "ins_pt": [0, 0],
                },
            ]
        )
    before = deepcopy(drawing)
    walked = _walk_block(10, _drawing_graph(drawing))
    assert not any(frame["source_kind"] == "polyline_with_line" for frame in walked["frames"])
    assert drawing == before
