# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Prose selection is independent from spatial JSON storage."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
        drawing, lambda *_: {"regions": [{"type": "document", "bbox": [0, 0, 40, 100]}]}
    )
    assert baseline["status"] == "partial"
    assert not baseline["markdown_uids"]
    assert "安装前应核对尺寸" not in _markdown(baseline)
    assert selected["status"] == "complete"
    assert selected["markdown_uids"] == ["20@"]
    assert "安装前应核对尺寸" in _markdown(selected)
    assert "设备接口 A42" not in _markdown(selected)
    assert "图形目录" not in _markdown(selected)
    assert selected["_texts"][0]["record"] is drawing["OBJECTS"][2]
    assert drawing == original


def test_empty_prose_selection_keeps_a_graphical_sheet_complete():
    classifier = Mock(return_value={"regions": []})
    sheet = _build(_drawing(_text(20, "图内尺寸 A42", 60, 50)), classifier)
    classifier.assert_called_once()
    assert classifier.call_args.args[1]["bbox"] == [0, 0, 100, 100]
    assert sheet["kind"] == "model_frame"
    assert sheet["status"] == "complete"
    assert sheet["regions"] == [] and not sheet["markdown_uids"]
    assert "图内尺寸 A42" not in _markdown(sheet)


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"regions": None},
        {"regions": [{"type": "view", "bbox": [0, 0, 100, 100]}]},
        {"regions": [{"type": "document", "bbox": [-1, 0, 100, 100]}]},
        {"regions": [{"type": "table", "bbox": [0, 0, float("nan"), 100]}]},
        {"regions": [{"type": "document", "bbox": [0, 0, 0, 100]}]},
    ],
)
def test_invalid_regions_fail_closed(response):
    sheet = _build(_drawing(_text(20, "不得遗失这段文字。", 10, 80)), lambda *_: response)
    assert sheet["status"] == "partial"
    assert not sheet["regions"] and not sheet["markdown_uids"]
    assert "不得遗失" not in _markdown(sheet)
    assert len(sheet["_texts"]) == 1


def test_timeout_and_empty_render_keep_native_text():
    def timeout(*_):
        raise TimeoutError("test")

    for classifier, renderer in [(timeout, lambda _: b"image"), (lambda *_: {}, lambda _: b"")]:
        sheet = _build(_drawing(_text(20, "施工要求。", 10, 80)), classifier, renderer)
        assert sheet["status"] == "partial" and not sheet["markdown_uids"]


def test_missing_renderer_does_not_invoke_classifier():
    classifier = Mock(side_effect=AssertionError("no approximate image"))
    sheet = _build(_drawing(_text(20, "施工要求。", 10, 80)), classifier, None)
    classifier.assert_not_called()
    assert sheet["status"] == "partial"
    assert sheet["region_diagnostic"]["render_error"] == "sheet_renderer_not_supplied"


def test_each_candidate_uses_one_image_call_without_view_refinement():
    classifier = Mock(
        return_value={
            "regions": [{"type": "document", "bbox": [0, 0, 100, 100], "needs_detail": True}]
        }
    )
    renderer = Mock(return_value=b"image")
    sheet = _build(_drawing(_text(20, "施工要求。", 10, 80)), classifier, renderer)
    classifier.assert_called_once()
    renderer.assert_called_once()
    assert "detail" not in classifier.call_args.args[1]
    assert sheet["status"] == "complete" and sheet["markdown_uids"] == ["20@"]


@pytest.mark.parametrize("other_type", ["document", "unknown"])
def test_overlapping_areas_keep_ambiguous_text_in_json(other_type):
    sheet = _build(
        _drawing(_text(20, "交界处说明", 50, 80)),
        lambda *_: {
            "regions": [
                {"type": "document", "bbox": [0, 0, 60, 100]},
                {"type": other_type, "bbox": [40, 0, 100, 100]},
            ]
        },
    )
    assert sheet["status"] == "partial" and not sheet["markdown_uids"]
    assert "交界处说明" not in _markdown(sheet)


def test_native_standalone_table_uses_existing_markdown_formatter():
    table = {
        "entity": "TABLE",
        "handle": [0, 0, 20],
        "index": 20,
        "entmode": 2,
        "ins_pt": [10, 80],
        "rows": [["项目", "值"], ["压力", "20"]],
    }
    sheet = _build(
        _drawing(table, _text(21, "图内标注", 70, 80)),
        lambda *_: {"regions": [{"type": "table", "bbox": [0, 0, 40, 100]}]},
    )
    assert "| 项目 | 值 |" in _markdown(sheet) and "| 压力 | 20 |" in _markdown(sheet)
    assert "图内标注" not in _markdown(sheet)
    assert table["rows"] == [["项目", "值"], ["压力", "20"]]


def test_repeated_insert_selects_only_the_prose_occurrence():
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
    sheet = _build(drawing, lambda *_: {"regions": [{"type": "document", "bbox": [0, 0, 40, 100]}]})
    assert sheet["markdown_uids"] == ["31@40"]
    assert {r["uid"] for r in sheet["_texts"]} == {"31@40", "31@41"}
    assert sheet["_texts"][0]["record"] is sheet["_texts"][1]["record"]


def test_unframed_input_does_not_claim_sheet_partition_success():
    drawing = _drawing(_text(20, "没有图框的说明", 10, 80))
    del drawing["OBJECTS"][1]
    sheet = _build(drawing, lambda *_: pytest.fail("unsupported Sheet must not invoke VLM"))
    assert sheet["status"] == "partial" and not sheet["markdown_uids"]


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
    sheet = _build(drawing, lambda *_: {"regions": []})
    assert sheet["kind"] == "model_frame" and sheet["bbox"] == [0, 0, 100, 100]
    assert sheet["status"] == "complete"


def test_render_failure_excludes_provider_error_contents():
    def unavailable(_):
        raise RuntimeError("ODA File Converter not found")

    assert (
        _build(_drawing(), lambda *_: {}, unavailable)["region_diagnostic"]["render_error"]
        == "ODA File Converter not found"
    )

    def provider_error(*_):
        raise RuntimeError("provider request with credential-like details")

    diagnostic = _build(_drawing(), provider_error)["region_diagnostic"]
    assert "render_error" not in diagnostic and "credential" not in repr(diagnostic)


def _adapter(monkeypatch, response):
    from openviking.parse.parsers import dwg

    vlm = Mock()
    vlm.is_available.return_value = True
    vlm.get_vision_completion_async = AsyncMock(return_value=json.dumps(response))
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=vlm))
    return dwg._region_classifier(), vlm


def test_adapter_maps_only_prose_areas_in_normalized_image_coordinates(monkeypatch):
    classify, vlm = _adapter(
        monkeypatch, {"regions": [{"type": "document", "bbox": [250, 100, 750, 600]}]}
    )
    assert classify(b"synthetic-image", {"bbox": [-50, 10, 150, 70]}) == {
        "regions": [{"type": "document", "bbox": [0, 34, 100, 64]}]
    }
    kwargs = vlm.get_vision_completion_async.call_args.kwargs
    assert kwargs["images"] == [b"synthetic-image"]
    assert (
        "top-left=(0,0)" in kwargs["prompt"]
        and "Do not split or name drawing views" in kwargs["prompt"]
    )
    assert 'regions":[]' in kwargs["prompt"]


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
def test_adapter_rejects_invalid_normalized_bounds(monkeypatch, bbox):
    classify, _ = _adapter(monkeypatch, {"regions": [{"type": "document", "bbox": bbox}]})
    with pytest.raises(ValueError, match="Invalid normalized"):
        classify(b"image", {"bbox": [-50, 10, 150, 70]})


@pytest.mark.parametrize(
    "response", [{"is_sheet": False}, {"is_sheet": False, "regions": [{"bbox": "invalid"}]}]
)
def test_adapter_explicit_sheet_rejection(monkeypatch, response):
    classify, vlm = _adapter(monkeypatch, response)
    assert classify(b"image", {"bbox": [0, 0, 100, 100]}) == {"is_sheet": False}
    vlm.get_vision_completion_async.assert_awaited_once()
    assert (
        "Keep complete drawing-index, notes, table and graphical Sheets"
        in vlm.get_vision_completion_async.call_args.kwargs["prompt"]
    )


@pytest.mark.parametrize("flag", [True, 0, "false", None])
def test_adapter_empty_prose_is_valid_without_false_boolean(monkeypatch, flag):
    classify, vlm = _adapter(monkeypatch, {"is_sheet": flag, "regions": []})
    assert classify(b"image", {"bbox": [0, 0, 100, 100]}) == {"regions": []}
    vlm.get_vision_completion_async.assert_awaited_once()


@pytest.mark.parametrize("kind", ["view", "titleblock", "invented"])
def test_adapter_has_no_view_partitioning_contract(monkeypatch, kind):
    classify, _ = _adapter(monkeypatch, {"regions": [{"type": kind, "bbox": [0, 0, 1000, 1000]}]})
    with pytest.raises(ValueError, match="Invalid prose region type"):
        classify(b"image", {"bbox": [0, 0, 100, 100]})
