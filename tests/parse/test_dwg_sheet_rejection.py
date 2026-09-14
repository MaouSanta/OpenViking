# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Sheet rejection reuses region selection while preserving every native object."""

import copy
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


def _assert_preserved(drawing, sheets, markdown_handles):
    preserved = build_json_artifacts(drawing, sheets)
    records = read_json_records(preserved["files"].values())
    objects = {row["object_index"]: row for row in records if row["kind"] == "object"}
    assert set(objects) == set(range(len(drawing["OBJECTS"])))
    assert next(row["value"] for row in records if row["kind"] == "header") == {
        "FILEHEADER": drawing["FILEHEADER"]
    }
    for index, original in enumerate(drawing["OBJECTS"]):
        record = objects[index]
        assert record["value"] == original
        assert "markdown_fields" not in record
        assert bool(record.get("markdown")) == (original["handle"][-1] in markdown_handles)
    return preserved, records


def test_three_sheets_and_nineteen_rejected_candidates_share_one_classifier_pass():
    drawing = _drawing()
    original = copy.deepcopy(drawing)
    calls = []
    renderer = Mock(return_value=b"fixed-image")

    def classify(_image, evidence):
        calls.append(evidence)
        if evidence["bbox"][0] >= 600:
            return {"is_sheet": False}
        return {
            "regions": [{"type": "document", "bbox": evidence["bbox"]}],
        }

    sheets = _build(drawing, classify, renderer)["sheets"]

    assert len(calls) == renderer.call_count == 22
    assert all("detail" not in call for call in calls)
    assert {sheet["source_locator"]["source_ref"]["handle"] for sheet in sheets} == {
        2134,
        4115,
        5128,
    }
    markdown = "\n".join(text for sheet in sheets for _, text in sheet["content_files"])
    for index in range(22):
        assert (f"第{index}处原始说明。" in markdown) is (index < 3)
    preserved, records = _assert_preserved(drawing, sheets, {30000, 30001, 30002})
    # Known frame-external content is document-scoped data, not a parse failure.
    assert preserved["parse_status"] == "complete"
    outside = read_json_records([preserved["files"]["drawing.json"]])
    assert {
        row["value"]["handle"][-1]
        for row in outside
        if row["kind"] == "object" and row["value"].get("entity") == "TEXT"
    } == set(range(30003, 30022))
    assert drawing == original


def test_rejecting_every_candidate_keeps_one_partial_synthetic_sheet_without_a_retry():
    drawing = _drawing()
    classifier = Mock(return_value={"is_sheet": False})
    renderer = Mock(return_value=b"fixed-image")

    result = _build(drawing, classifier, renderer)

    assert classifier.call_count == renderer.call_count == 22
    assert result["document"]["sheet_count"] == 0
    assert result["document"]["sheet_bundle_count"] == 1
    sheet = result["sheets"][0]
    assert sheet["kind"] == "synthetic"
    assert sheet["status"] == "partial"
    assert not sheet["markdown_uids"]
    assert sheet["content_paths"]
    assert not any("原始说明" in text for _, text in sheet["content_files"])
    _assert_preserved(drawing, [sheet], set())


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


def test_unknown_prose_does_not_reject_the_sheet():
    drawing = _drawing(1)
    classifier = Mock(return_value={"regions": [{"type": "unknown", "bbox": [0, 0, 100, 80]}]})
    sheets = _build(drawing, classifier, lambda _: b"image")["sheets"]
    assert len(sheets) == 1 and sheets[0]["kind"] == "model_frame"
    assert sheets[0]["status"] == "partial" and not sheets[0]["markdown_uids"]
    classifier.assert_called_once()
    _assert_preserved(drawing, sheets, set())
