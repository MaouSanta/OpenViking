# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Sheet rejection reuses region selection while preserving every native object."""

import copy
import json
from unittest.mock import Mock

import pytest

from openviking.parse.parsers.dwg_json import build_json_artifacts, read_json_records
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts


def _drawing(count=22):
    handles = [2134, 4115, 5128, *range(10000, 10019)]
    objects = [{"object": "BLOCK_HEADER", "handle": [0, 0, 2], "name": "*Model_Space"}]
    for index, handle in enumerate(handles[:count]):
        x = index * 200
        objects.extend(
            [
                {
                    "entity": "LWPOLYLINE",
                    "handle": [0, 0, handle],
                    "entmode": 2,
                    "flag": 512,
                    "points": [[x, 0], [x + 100, 0], [x + 100, 80], [x, 80]],
                },
                {
                    "entity": "TEXT",
                    "handle": [0, 0, 30000 + index],
                    "entmode": 2,
                    "ins_pt": [x + 10, 60],
                    "height": 2,
                    "text_value": f"第{index}处原始说明。",
                    "custom": {"integer": 1, "float": 1.0, "boolean": True},
                },
            ]
        )
    return {"FILEHEADER": {"version": "AC1032"}, "OBJECTS": objects}


def _build(drawing, classifier, renderer):
    return build_sheet_artifacts(
        drawing,
        source_name="candidate-fixture.dwg",
        source_sha256="a" * 64,
        source_size_bytes=100,
        region_classifier=classifier,
        sheet_renderer=renderer,
    )


def _assert_preserved(drawing, sheets, moved_handles):
    preserved = build_json_artifacts(drawing, sheets)
    records = read_json_records(preserved["files"].values())
    objects = {row["object_index"]: row for row in records if row["kind"] == "object"}
    assert set(objects) == set(range(len(drawing["OBJECTS"])))
    assert next(row["value"] for row in records if row["kind"] == "header") == {
        "FILEHEADER": drawing["FILEHEADER"]
    }
    for index, original in enumerate(drawing["OBJECTS"]):
        record = objects[index]
        moved = original["handle"][-1] in moved_handles
        expected = (
            {key: value for key, value in original.items() if key != "text_value"}
            if moved
            else original
        )
        assert json.dumps(record["value"], sort_keys=True) == json.dumps(expected, sort_keys=True)
        assert record.get("markdown_fields", []) == (["text_value"] if moved else [])
    return preserved, records


@pytest.mark.parametrize("remainder", ["rejected", "unknown", "empty"])
def test_only_explicit_false_rejects_candidates_in_one_classifier_pass(remainder):
    drawing = _drawing()
    original = copy.deepcopy(drawing)
    calls = []
    renderer = Mock(return_value=b"fixed-image")

    def classify(_image, evidence):
        calls.append(evidence)
        if evidence["bbox"][0] >= 600:
            if remainder == "rejected":
                return {"is_sheet": False}
            return {
                "regions": (
                    [{"type": "unknown", "bbox": evidence["bbox"]}]
                    if remainder == "unknown"
                    else []
                )
            }
        return {
            "regions": [{"type": "document", "bbox": evidence["bbox"]}],
        }

    sheets = _build(drawing, classify, renderer)["sheets"]

    assert len(calls) == renderer.call_count == 22
    assert not any(call["detail"] for call in calls)
    handles = {sheet["source_locator"]["source_ref"]["handle"] for sheet in sheets}
    if remainder == "rejected":
        assert handles == {2134, 4115, 5128}
        assert all(sheet["status"] == "complete" for sheet in sheets)
    else:
        assert len(sheets) == len(handles) == 22
        assert all(sheet["kind"] == "model_frame" for sheet in sheets)
        assert sum(sheet["status"] == "partial" for sheet in sheets) == (
            19 if remainder == "unknown" else 0
        )
    markdown = "\n".join(text for sheet in sheets for _, text in sheet["content_files"])
    for index in range(22):
        assert (f"第{index}处原始说明。" in markdown) is (index < 3)
    preserved, records = _assert_preserved(drawing, sheets, {30000, 30001, 30002})
    root_records = read_json_records(
        content for path, content in preserved["files"].items() if path.startswith("chunks/")
    )
    sheet_records = read_json_records(
        content for path, content in preserved["files"].items() if path.startswith("sheets/")
    )
    root_objects = {row["object_index"] for row in root_records if row["kind"] == "object"}
    sheet_objects = {row["object_index"] for row in sheet_records if row["kind"] == "object"}
    if remainder == "rejected":
        assert root_objects == set(range(7, len(drawing["OBJECTS"])))
        assert sheet_objects == set(range(1, 7))
    else:
        assert preserved["parse_status"] == ("partial" if remainder == "unknown" else "complete")
        assert not root_objects
        assert sheet_objects == set(range(1, len(drawing["OBJECTS"])))
    assert all("owner" not in row for row in records if row["kind"] == "instance")
    assert drawing == original


@pytest.mark.parametrize("classification", ["rejected", "unknown", "empty"])
def test_unknown_or_empty_keeps_real_sheets_but_explicit_rejection_uses_fallback(classification):
    drawing = _drawing()
    classifier = Mock(
        side_effect=lambda _image, evidence: (
            {"is_sheet": False}
            if classification == "rejected"
            else {
                "regions": (
                    [{"type": "unknown", "bbox": evidence["bbox"]}]
                    if classification == "unknown"
                    else []
                )
            }
        )
    )
    renderer = Mock(return_value=b"fixed-image")

    result = _build(drawing, classifier, renderer)

    assert classifier.call_count == renderer.call_count == 22
    sheets = result["sheets"]
    expected_count = 1 if classification == "rejected" else 22
    assert result["document"]["sheet_count"] == len(sheets) == expected_count
    for sheet in sheets:
        assert sheet["kind"] == ("synthetic" if classification == "rejected" else "model_frame")
        assert sheet["status"] == ("complete" if classification == "empty" else "partial")
        assert not sheet["markdown_uids"]
        assert sheet["content_paths"]
        assert not any("原始说明" in text for _, text in sheet["content_files"])
    preserved, _ = _assert_preserved(drawing, sheets, set())
    assert preserved["parse_status"] == ("complete" if classification == "empty" else "partial")
    root_records = read_json_records(
        content for path, content in preserved["files"].items() if path.startswith("chunks/")
    )
    assert {row["object_index"] for row in root_records if row["kind"] == "object"} == (
        set(range(1, len(drawing["OBJECTS"]))) if classification == "rejected" else set()
    )


def test_known_and_unknown_regions_keep_a_partial_sheet():
    drawing = _drawing(1)
    classifier = Mock(
        return_value={
            "regions": [
                {"type": "document", "bbox": [0, 0, 40, 80]},
                {"type": "unknown", "bbox": [50, 0, 100, 80]},
            ]
        }
    )

    sheet = _build(drawing, classifier, lambda _: b"image")["sheets"][0]

    assert classifier.call_count == 1
    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "partial"
    assert {region["type"] for region in sheet["regions"]} == {"document", "unknown"}
    assert sheet["markdown_uids"]
    _assert_preserved(drawing, [sheet], {30000})


@pytest.mark.parametrize("unavailable", [False, True])
def test_failed_or_unavailable_classifier_keeps_the_candidate_partial(unavailable):
    classifier = None if unavailable else Mock(side_effect=TimeoutError("test"))

    sheet = _build(_drawing(1), classifier, lambda _: b"image")["sheets"][0]

    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "partial"
    assert sheet["region_diagnostic"]["status"] == ("unavailable" if unavailable else "failed")
    assert not sheet["markdown_uids"]


@pytest.mark.parametrize("flag", [None, "false", 0])
def test_missing_or_nonboolean_sheet_flag_keeps_existing_region_behavior(flag):
    def classify(_image, evidence):
        response = {"regions": [{"type": "document", "bbox": evidence["bbox"]}]}
        if flag is not None:
            response["is_sheet"] = flag
        return response

    sheet = _build(_drawing(1), classify, lambda _: b"image")["sheets"][0]

    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "complete"
    assert sheet["markdown_uids"]


def test_detail_false_flag_does_not_reject_a_whole_sheet_or_add_a_retry():
    calls = []

    def classify(_image, evidence):
        calls.append(evidence)
        return {
            "is_sheet": not evidence["detail"],
            "regions": [
                {
                    "type": "document",
                    "bbox": evidence["bbox"],
                    "needs_detail": not evidence["detail"],
                }
            ],
        }

    sheet = _build(_drawing(1), classify, lambda _: b"image")["sheets"][0]

    assert [call["detail"] for call in calls] == [False, True]
    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "complete"
    assert sheet["markdown_uids"]
