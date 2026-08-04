# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json

import pytest

from openviking.parse.vlm import (
    TechnicalDrawingRecognitionError,
    VLMProcessor,
)


class _FakeVLM:
    def __init__(self, response="", *, available=True, error=None):
        self.response = response
        self.available = available
        self.error = error
        self.calls = []

    def is_available(self):
        return self.available

    async def get_vision_completion_async(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def _processor(monkeypatch, vlm):
    processor = VLMProcessor()
    monkeypatch.setattr(processor, "_get_vlm", lambda: vlm)
    return processor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '{"regions":[{"type":"detail","label":"DETAIL A","bbox":[1,2,300,400]}]}',
        '```json\n{"regions":[{"type":"detail","label":"DETAIL A","bbox":[1,2,300,400]}]}\n```',
    ],
)
async def test_recognizes_strict_json_and_fenced_json_once(monkeypatch, response):
    vlm = _FakeVLM(response)
    regions = await _processor(monkeypatch, vlm).recognize_technical_drawing_regions(b"png")

    assert [(region.type, region.label, region.bbox) for region in regions] == [
        ("detail", "DETAIL A", (1, 2, 300, 400))
    ]
    assert len(vlm.calls) == 1
    assert vlm.calls[0]["images"] == [b"png"]
    assert vlm.calls[0]["thinking"] is False
    assert "normalized integer coordinates" in vlm.calls[0]["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "region",
    [
        {"type": "view", "label": None, "bbox": [1, 2, 3, 4], "extra": "no"},
        {"type": "view", "label": None, "bbox": [True, 2, 3, 4]},
        {"type": "view", "label": None, "bbox": [1.0, 2, 3, 4]},
        {"type": "view", "label": None, "bbox": [-1, 2, 3, 4]},
        {"type": "view", "label": None, "bbox": [1, 2, 1001, 4]},
        {"type": "view", "label": None, "bbox": [3, 2, 1, 4]},
        {"type": "view", "label": "x" * 81, "bbox": [1, 2, 3, 4]},
        {"type": "unknown", "label": None, "bbox": [1, 2, 3, 4]},
    ],
)
async def test_rejects_invalid_region_payloads(monkeypatch, region):
    response = json.dumps({"regions": [region]})

    with pytest.raises(TechnicalDrawingRecognitionError) as exc_info:
        await _processor(monkeypatch, _FakeVLM(response)).recognize_technical_drawing_regions(
            b"png"
        )

    assert exc_info.value.reason == "invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "not json",
        '{"regions":[],"extra":"no"}',
        "[]",
    ],
)
async def test_rejects_invalid_top_level_payloads(monkeypatch, response):
    with pytest.raises(TechnicalDrawingRecognitionError) as exc_info:
        await _processor(monkeypatch, _FakeVLM(response)).recognize_technical_drawing_regions(
            b"png"
        )

    assert exc_info.value.reason == "invalid_response"


@pytest.mark.asyncio
async def test_normalizes_labels_and_removes_exact_duplicates(monkeypatch):
    response = json.dumps(
        {
            "regions": [
                {"type": "detail", "label": "  DETAIL   A ", "bbox": [1, 2, 300, 400]},
                {"type": "detail", "label": "detail a", "bbox": [1, 2, 300, 400]},
                {"type": "detail", "label": "DETAIL A", "bbox": [2, 2, 300, 400]},
            ]
        }
    )

    regions = await _processor(monkeypatch, _FakeVLM(response)).recognize_technical_drawing_regions(
        b"png"
    )

    assert [(region.label, region.bbox) for region in regions] == [
        ("DETAIL A", (1, 2, 300, 400)),
        ("DETAIL A", (2, 2, 300, 400)),
    ]


@pytest.mark.asyncio
async def test_rejects_more_than_32_unique_regions(monkeypatch):
    response = json.dumps(
        {
            "regions": [
                {"type": "view", "label": str(index), "bbox": [1, 2, 300, 400]}
                for index in range(33)
            ]
        }
    )

    with pytest.raises(TechnicalDrawingRecognitionError) as exc_info:
        await _processor(monkeypatch, _FakeVLM(response)).recognize_technical_drawing_regions(
            b"png"
        )

    assert exc_info.value.reason == "invalid_response"


@pytest.mark.asyncio
async def test_reports_stable_empty_unavailable_and_model_error_reasons(monkeypatch):
    with pytest.raises(TechnicalDrawingRecognitionError) as empty:
        await _processor(
            monkeypatch, _FakeVLM('{"regions":[]}')
        ).recognize_technical_drawing_regions(b"png")
    assert empty.value.reason == "no_regions"

    unavailable_vlm = _FakeVLM(available=False)
    with pytest.raises(TechnicalDrawingRecognitionError) as unavailable:
        await _processor(monkeypatch, unavailable_vlm).recognize_technical_drawing_regions(b"png")
    assert unavailable.value.reason == "vlm_unavailable"
    assert unavailable_vlm.calls == []

    with pytest.raises(TechnicalDrawingRecognitionError) as model_error:
        await _processor(
            monkeypatch,
            _FakeVLM(error=RuntimeError("offline")),
        ).recognize_technical_drawing_regions(b"png")
    assert model_error.value.reason == "model_error"
