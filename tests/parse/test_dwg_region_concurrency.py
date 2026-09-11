# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Bounded region selection preserves candidate order and isolates model failures."""

import copy
import json
import threading

from openviking.parse.parsers.dwg_json import build_json_artifacts, read_json_records
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts


def _drawing():
    objects = [{"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"}]
    for index in range(8):
        x = index * 200
        objects.extend(
            [
                {
                    "entity": "LWPOLYLINE",
                    "handle": [0, 0, 10 + index],
                    "entmode": 2,
                    "flag": 1,
                    "points": [[x, 0], [x + 100, 0], [x + 100, 80], [x, 80]],
                },
                {
                    "entity": "TEXT",
                    "handle": [0, 0, 30 + index],
                    "entmode": 2,
                    "ins_pt": [x + 10, 60],
                    "height": 2,
                    "text_value": f"第{index}处原始说明。",
                    "custom": {"integer": 1, "float": 1.0, "boolean": True},
                },
            ]
        )
    return {"FILEHEADER": {"version": "AC1032"}, "OBJECTS": objects}


def _fixed_result(evidence):
    index = int(evidence["bbox"][0] / 200)
    if index == 1:
        return {"regions": []}
    if index == 2:
        return {"is_sheet": False}
    if index == 5:
        raise TimeoutError("fixed provider failure with private details")
    if index == 6:
        return {"regions": [{"type": "invalid", "bbox": evidence["bbox"]}]}
    kind = {3: "unknown", 4: "table"}.get(index, "document")
    return {"regions": [{"type": kind, "bbox": evidence["bbox"]}]}


def _build(drawing, classifier, workers):
    return build_sheet_artifacts(
        drawing,
        source_name="concurrency-fixture.dwg",
        source_sha256="a" * 64,
        source_size_bytes=100,
        region_classifier=classifier,
        sheet_renderer=lambda box: json.dumps(box).encode(),
        max_concurrent_regions=workers,
    )


def test_out_of_order_selection_preserves_markdown_json_and_failure_isolation():
    drawing = _drawing()
    original = copy.deepcopy(drawing)
    serial = _build(drawing, lambda _image, evidence: _fixed_result(evidence), 1)
    condition = threading.Condition()
    expected_completion = [3, 2, 1, 0, 7, 6, 5, 4]
    completed = []
    active = 0
    peak = 0

    def parallel(image, evidence):
        nonlocal active, peak
        assert json.loads(image) == evidence["bbox"]
        index = int(evidence["bbox"][0] / 200)
        with condition:
            active += 1
            peak = max(peak, active)
            assert condition.wait_for(
                lambda: expected_completion[len(completed)] == index, timeout=5
            )
            completed.append(index)
            active -= 1
            condition.notify_all()
        return _fixed_result(evidence)

    concurrent = _build(drawing, parallel, 4)
    assert completed == expected_completion
    assert peak == 4
    assert concurrent == serial
    serial_json = build_json_artifacts(drawing, serial["sheets"])
    concurrent_json = build_json_artifacts(drawing, concurrent["sheets"])
    assert concurrent_json == serial_json
    assert drawing == original

    sheets = concurrent["sheets"]
    handles = [sheet["source_locator"]["source_ref"]["handle"] for sheet in sheets]
    assert handles == [10, 11, 13, 14, 15, 16, 17]
    by_handle = dict(zip(handles, sheets, strict=True))
    assert by_handle[10]["status"] == by_handle[17]["status"] == "complete"
    assert by_handle[11]["regions"] == [] and by_handle[11]["status"] == "complete"
    for handle in (13, 15, 16):
        assert by_handle[handle]["status"] == "partial"
        assert not by_handle[handle]["markdown_uids"]
    assert by_handle[15]["region_diagnostic"] == {"status": "failed", "error": "TimeoutError"}
    assert by_handle[16]["region_diagnostic"] == {"status": "failed", "error": "ValueError"}
    restored = read_json_records(concurrent_json["files"].values())
    records = {row["object_index"]: row for row in restored if row["kind"] == "object"}
    assert set(records) == set(range(len(drawing["OBJECTS"])))
    for index, record in records.items():
        expected = copy.deepcopy(drawing["OBJECTS"][index])
        for field in record.get("markdown_fields", []):
            del expected[field]
        assert record["value"] == expected


def test_concurrent_all_rejected_and_no_vlm_keep_existing_fallbacks():
    drawing = _drawing()
    for classify in (lambda *_: {"is_sheet": False}, None):
        serial = _build(drawing, classify, 1)
        concurrent = _build(drawing, classify, 4)
        assert concurrent == serial
        assert build_json_artifacts(drawing, concurrent["sheets"]) == build_json_artifacts(
            drawing, serial["sheets"]
        )
    rejected = _build(drawing, lambda *_: {"is_sheet": False}, 4)
    assert len(rejected["sheets"]) == 1
    assert rejected["sheets"][0]["kind"] == "synthetic"
    assert rejected["sheets"][0]["status"] == "partial"
