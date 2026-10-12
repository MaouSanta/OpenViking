# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Fixed VLM outputs isolate the value and failure boundaries of region selection."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts


def _text(handle, text, x, y, **fields):
    return {
        "entity": "TEXT",
        "handle": [0, 0, handle],
        "index": handle,
        "entmode": 2,
        "ins_pt": [x, y],
        "height": 2,
        "text_value": text,
        **fields,
    }


def _drawing(*records):
    return {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
            {
                "entity": "LWPOLYLINE",
                "handle": [0, 0, 11],
                "index": 11,
                "entmode": 2,
                "flag": 1,
                "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
            *records,
        ]
    }


def _build(drawing, classifier, renderer=lambda bbox: b"test-image"):
    return build_sheet_artifacts(
        drawing,
        source_name="regions.dwg",
        source_sha256="a" * 64,
        source_size_bytes=100,
        region_classifier=classifier,
        sheet_renderer=renderer,
    )["sheets"][0]


def _markdown(sheet):
    return "\n".join(text for _, text in sheet["content_files"])


def test_region_ablation_moves_only_selected_prose_to_markdown():
    drawing = _drawing(_text(20, "安装前应核对尺寸。", 10, 80), _text(21, "设备接口 A42", 70, 80))
    original = copy.deepcopy(drawing)
    baseline = _build(drawing, None)
    selected = _build(
        drawing,
        lambda image, evidence: {
            "regions": [
                {"type": "document", "bbox": [0, 0, 40, 100]},
                {"type": "view", "bbox": [50, 0, 100, 100], "title": "设备详图"},
            ]
        },
    )
    assert baseline["status"] == "partial"
    assert not baseline["markdown_uids"]
    assert "安装前应核对尺寸" not in _markdown(baseline)
    assert selected["status"] == "complete"
    assert selected["markdown_uids"] == ["20@"]
    assert "安装前应核对尺寸" in _markdown(selected)
    assert "设备接口 A42" not in _markdown(selected)
    assert "设备详图" not in _markdown(selected)
    assert selected["regions"][1]["title"] == "设备详图"
    assert selected["_texts"][0]["record"] is drawing["OBJECTS"][2]
    assert drawing == original


def test_classifier_runs_on_sheet_with_no_text_or_prose_candidates():
    calls = []

    def classify(image, evidence):
        calls.append(evidence)
        return {"regions": [{"type": "view", "bbox": [0, 0, 100, 100]}]}

    sheet = _build(_drawing(), classify)
    assert len(calls) == 1
    assert calls[0]["bbox"] == [0, 0, 100, 100]
    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "complete"
    assert not sheet["markdown_uids"]


def test_empty_regions_keep_a_pure_drawing_sheet_and_native_labels():
    sheet = _build(_drawing(_text(20, "图内原生标注", 10, 80)), lambda *_: {"regions": []})
    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "complete"
    assert not sheet["markdown_uids"]
    assert len(sheet["_texts"]) == 1


def test_caption_group_metadata_is_kept_for_asset_navigation():
    sheet = _build(
        _drawing(_text(20, "组内子图A及表格原文", 10, 80), _text(21, "框外标签", 90, 10)),
        lambda *_: {
            "regions": [
                {
                    "type": "view",
                    "bbox": [0, 40, 80, 100],
                    "number": "图六",
                    "title": "梁侧面纵向构造筋和拉筋",
                    "description": "三种配筋示意及组内配筋表。",
                }
            ]
        },
    )
    region = sheet["regions"][0]
    assert region["number"] == "图六"
    assert region["description"] == "三种配筋示意及组内配筋表。"
    assert sheet["status"] == "complete"
    assert not sheet["markdown_uids"]
    assert "三种配筋示意" not in _markdown(sheet)


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"regions": [{"type": "invented", "bbox": [0, 0, 100, 100]}]},
        {"regions": [{"type": "view", "bbox": [-1, 0, 100, 100]}]},
        {"regions": [{"type": "view", "bbox": [0, 0, float("nan"), 100]}]},
        {"regions": [{"type": "view", "bbox": [0, 0, 0, 100]}]},
    ],
)
def test_invalid_regions_fail_closed(response):
    sheet = _build(_drawing(_text(20, "不得遗失这段文字。", 10, 80)), lambda *_: response)
    assert sheet["status"] == "partial"
    assert not sheet["regions"]
    assert not sheet["markdown_uids"]
    assert "不得遗失" not in _markdown(sheet)
    assert len(sheet["_texts"]) == 1


def test_timeout_and_empty_render_keep_text_unassigned():
    def timeout(*_):
        raise TimeoutError("test")

    for classifier, renderer in [(timeout, lambda _: b"image"), (lambda *_: {}, lambda _: b"")]:
        sheet = _build(_drawing(_text(20, "施工要求。", 10, 80)), classifier, renderer)
        assert sheet["status"] == "partial"
        assert not sheet["markdown_uids"]


def test_missing_renderer_keeps_text_unassigned_without_invoking_classifier():
    classifier = Mock(side_effect=AssertionError("no approximation may be sent to VLM"))
    sheet = _build(_drawing(_text(20, "施工要求。", 10, 80)), classifier, None)
    classifier.assert_not_called()
    assert sheet["status"] == "partial"
    assert not sheet["markdown_uids"]
    assert sheet["region_diagnostic"]["render_error"] == "sheet_renderer_not_supplied"


def test_requested_local_refinement_is_performed_only_once():
    rendered, calls = [], []

    def render(bbox):
        rendered.append(bbox)
        return b"image"

    def classify(image, evidence):
        calls.append(evidence)
        if evidence["detail"]:
            return {
                "regions": [
                    {"type": "view", "bbox": [0, 0, 45, 100], "needs_detail": True},
                    {"type": "view", "bbox": [55, 0, 100, 100]},
                ]
            }
        return {"regions": [{"type": "view", "bbox": [0, 0, 100, 100], "needs_detail": True}]}

    sheet = _build(_drawing(), classify, render)
    assert len(calls) == len(rendered) == 2
    assert [region["id"] for region in sheet["regions"]] == ["R001", "R002"]
    assert sheet["regions"][0]["type"] == "unknown"
    assert sheet["status"] == "partial"


def test_empty_detail_does_not_silently_drop_a_known_unresolved_region():
    calls = []

    def classify(_image, evidence):
        calls.append(evidence["detail"])
        return {
            "regions": []
            if evidence["detail"]
            else [
                {"type": "view", "bbox": [0, 0, 100, 100], "needs_detail": True},
            ]
        }

    sheet = _build(_drawing(_text(20, "保留在原生数据中", 10, 80)), classify)
    assert calls == [False, True]
    assert sheet["status"] == "partial"
    assert sheet["regions"][0]["type"] == "unknown"
    assert not sheet["markdown_uids"]


def test_failed_refinement_preserves_unknown_area():
    from openviking.parse.parsers.dwg_json import build_json_artifacts, read_json_records

    calls = []

    def classify(image, evidence):
        calls.append(evidence["detail"])
        if evidence["detail"]:
            return {"regions": [{"type": "view", "bbox": [0, 0, 101, 100]}]}
        return {"regions": [{"type": "view", "bbox": [0, 0, 100, 100], "needs_detail": True}]}

    drawing = _drawing(_text(20, "局部无法确认的文字", 10, 80))
    sheet = _build(drawing, classify)
    assert calls == [False, True]
    assert sheet["status"] == "partial"
    assert sheet["kind"] == "model_frame"
    assert sheet["regions"][0]["type"] == "unknown"
    preserved = build_json_artifacts(drawing, [sheet])
    records = read_json_records(preserved["files"].values())
    objects = sorted(
        (row for row in records if row["kind"] == "object"), key=lambda row: row["object_index"]
    )
    assert [row["value"] for row in objects] == drawing["OBJECTS"]
    assert any(row["kind"] == "instance" for row in records)


def test_overlapping_view_and_document_keep_label_in_json():
    sheet = _build(
        _drawing(_text(20, "交界处图内说明", 50, 80)),
        lambda *_: {
            "regions": [
                {"type": "document", "bbox": [0, 0, 60, 100]},
                {"type": "view", "bbox": [40, 0, 100, 100]},
            ]
        },
    )
    assert sheet["status"] == "partial"
    assert not sheet["markdown_uids"]
    assert "交界处图内说明" not in _markdown(sheet)


def test_native_standalone_table_uses_existing_markdown_formatter():
    table = {
        "entity": "TABLE",
        "handle": [0, 0, 20],
        "index": 20,
        "entmode": 2,
        "ins_pt": [10, 80],
        "rows": [["项目", "值"], ["压力", "20"]],
    }
    drawing = _drawing(table, _text(21, "图内标注", 70, 80))
    sheet = _build(
        drawing,
        lambda *_: {
            "regions": [
                {"type": "table", "bbox": [0, 0, 40, 100]},
                {"type": "view", "bbox": [50, 0, 100, 100]},
            ]
        },
    )
    assert "| 项目 | 值 |" in _markdown(sheet)
    assert "| 压力 | 20 |" in _markdown(sheet)
    assert "图内标注" not in _markdown(sheet)
    assert table["rows"] == [["项目", "值"], ["压力", "20"]]


def test_repeated_insert_text_instance_in_view_is_not_markdown():
    drawing = _drawing(
        {
            "object": "BLOCK_HEADER",
            "handle": [0, 0, 30],
            "name": "note",
            "base_pt": [0, 0],
            "entities": [[0, 0, 31]],
        },
        _text(31, "重复实例说明", 0, 0, ownerhandle=[0, 0, 30]),
        {
            "entity": "INSERT",
            "handle": [0, 0, 40],
            "entmode": 2,
            "block_header": [0, 0, 30],
            "ins_pt": [10, 80],
        },
        {
            "entity": "INSERT",
            "handle": [0, 0, 41],
            "entmode": 2,
            "block_header": [0, 0, 30],
            "ins_pt": [70, 80],
        },
    )
    sheet = _build(
        drawing,
        lambda *_: {
            "regions": [
                {"type": "document", "bbox": [0, 0, 40, 100]},
                {"type": "view", "bbox": [50, 0, 100, 100]},
            ]
        },
    )
    assert sheet["markdown_uids"] == ["31@40"]
    assert {text["uid"] for text in sheet["_texts"]} == {"31@40", "31@41"}
    assert sheet["_texts"][0]["record"] is sheet["_texts"][1]["record"]


def test_unframed_input_does_not_claim_sheet_partition_success():
    drawing = _drawing(_text(20, "没有图框的说明", 10, 80))
    del drawing["OBJECTS"][1]
    sheet = _build(drawing, lambda *_: pytest.fail("unsupported Sheet should not invoke VLM"))
    assert sheet["status"] == "partial"
    assert not sheet["markdown_uids"]


def test_distant_unframed_line_does_not_hide_existing_sheet():
    drawing = _drawing(
        _text(20, "图内标注", 10, 80),
        {
            "entity": "LINE",
            "handle": [0, 0, 30],
            "entmode": 2,
            "start": [1000, 1000],
            "end": [2000, 2000],
        },
    )
    sheet = _build(drawing, lambda *_: {"regions": [{"type": "view", "bbox": [0, 0, 100, 100]}]})
    assert sheet["kind"] == "model_frame"
    assert sheet["bbox"] == [0, 0, 100, 100]
    assert sheet["regions"][0]["type"] == "view"


def test_render_failure_message_is_retained_without_provider_error_contents():
    def unavailable(_):
        raise RuntimeError("ODA File Converter not found")

    sheet = _build(_drawing(), lambda *_: {}, unavailable)
    diagnostic = sheet["region_diagnostic"]
    assert diagnostic["render_error"] == "ODA File Converter not found"

    def provider_error(*_):
        raise RuntimeError("provider request with credential-like details")

    sheet = _build(_drawing(), provider_error)
    diagnostic = sheet["region_diagnostic"]
    assert "render_error" not in diagnostic
    assert "credential" not in repr(diagnostic)


def test_live_adapter_maps_normalized_image_box_to_nonsquare_cad_bounds(monkeypatch):
    from openviking.parse.parsers import dwg

    vlm = Mock()
    vlm.is_available.return_value = True
    vlm.get_vision_completion.return_value = json.dumps(
        {
            "regions": [
                {
                    "type": "view",
                    "title": "Plate",
                    "bbox": [250, 100, 750, 600],
                    "needs_detail": False,
                }
            ]
        }
    )
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    result = dwg._region_classifier()(
        b"synthetic-image", {"bbox": [-50, 10, 150, 70], "detail": False}
    )
    assert result == {
        "regions": [
            {
                "type": "view",
                "title": "Plate",
                "bbox": [0, 34, 100, 64],
                "needs_detail": False,
            }
        ]
    }
    kwargs = vlm.get_vision_completion.call_args.kwargs
    assert kwargs["images"] == [b"synthetic-image"]
    assert "top-left=(0,0)" in kwargs["prompt"]
    assert "y down" in kwargs["prompt"]


@pytest.mark.parametrize(
    "bbox",
    [
        [-1, 0, 1000, 1000],
        [0, 0, 1001, 1000],
        [0, 0, 1000, 1001],
        [0, 0, 0, 1000],
        [0, 0, 1000, float("nan")],
    ],
)
def test_live_adapter_rejects_invalid_normalized_bounds(monkeypatch, bbox):
    from openviking.parse.parsers import dwg

    vlm = Mock()
    vlm.is_available.return_value = True
    vlm.get_vision_completion.return_value = json.dumps(
        {"regions": [{"type": "view", "bbox": bbox}]}
    )
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    with pytest.raises(ValueError, match="Invalid normalized"):
        dwg._region_classifier()(b"synthetic-image", {"bbox": [-50, 10, 150, 70]})


@pytest.mark.parametrize("regions", [None, [{"bbox": "irrelevant invalid bounds"}]])
def test_live_adapter_preserves_explicit_whole_candidate_rejection(monkeypatch, regions):
    from openviking.parse.parsers import dwg

    response = {"is_sheet": False}
    if regions is not None:
        response["regions"] = regions
    vlm = Mock()
    vlm.is_available.return_value = True
    vlm.get_vision_completion.return_value = json.dumps(response)
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))

    assert dwg._region_classifier()(b"synthetic-image", {"bbox": [0, 0, 100, 100]}) == {
        "is_sheet": False
    }
    vlm.get_vision_completion.assert_called_once()
    prompt = vlm.get_vision_completion.call_args.kwargs["prompt"]
    assert "whole Sheet candidate" in prompt
    assert "Keep complete drawing-index, notes, and table Sheets" in prompt
    assert "Missing a title or titleblock alone is not grounds for rejection" in prompt
    assert "If uncertain, keep the candidate" in prompt


@pytest.mark.parametrize(
    "detail, flag", [(True, False), (False, True), (False, 0), (False, "false")]
)
def test_live_adapter_ignores_detail_rejection_and_non_false_flags(monkeypatch, detail, flag):
    from openviking.parse.parsers import dwg

    vlm = Mock()
    vlm.is_available.return_value = True
    vlm.get_vision_completion.return_value = json.dumps(
        {"is_sheet": flag, "regions": [{"type": "view", "bbox": [0, 0, 1000, 1000]}]}
    )
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))

    assert dwg._region_classifier()(
        b"synthetic-image", {"bbox": [-50, 10, 150, 70], "detail": detail}
    ) == {"regions": [{"type": "view", "bbox": [-50, 10, 150, 70]}]}
    vlm.get_vision_completion.assert_called_once()
    if detail:
        prompt = vlm.get_vision_completion.call_args.kwargs["prompt"]
        assert "Do not judge whether it is an entire Sheet" in prompt
        assert "whole Sheet candidate" not in prompt


@pytest.mark.parametrize("regions", [[], [{"type": "view", "bbox": [0, 0, 1001, 1000]}]])
def test_live_adapter_detail_rejection_keeps_existing_invalid_region_handling(monkeypatch, regions):
    from openviking.parse.parsers import dwg

    response = {"is_sheet": False}
    if regions:
        response["regions"] = regions
    vlm = Mock()
    vlm.is_available.return_value = True
    vlm.get_vision_completion.return_value = json.dumps(response)
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    classifier = dwg._region_classifier()
    evidence = {"bbox": [0, 0, 100, 100], "detail": True}
    if regions:
        with pytest.raises(ValueError, match="Invalid normalized"):
            classifier(b"synthetic-image", evidence)
    else:
        with pytest.raises(ValueError, match="Missing VLM regions"):
            classifier(b"synthetic-image", evidence)
