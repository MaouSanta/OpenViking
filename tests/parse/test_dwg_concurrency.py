# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Concurrent DWG VLM calls retain request attribution and accumulate usage."""

import asyncio
import threading
from types import SimpleNamespace

from openviking.models.vlm.base import VLMBase
from openviking.models.vlm.token_usage import TokenUsageTracker
from openviking.observability import events
from openviking.observability.context import bind_root_context, reset_observability_context
from openviking.parse.parsers import dwg
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts
from openviking.telemetry.context import (
    bind_telemetry,
    bind_telemetry_stage,
    get_current_telemetry,
    get_current_telemetry_stage,
)
from openviking.telemetry.operation import OperationTelemetry
from openviking.telemetry.span_models import RootSpanAttributes


def test_parallel_sheet_classifier_preserves_context_and_usage_on_one_loop(monkeypatch):
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 10], "name": "*Model_Space"},
            *(
                {
                    "entity": "LWPOLYLINE",
                    "handle": [0, 0, 20 + index],
                    "index": 20 + index,
                    "entmode": 2,
                    "flag": 512,
                    "points": [[x, 0], [x + 100, 0], [x + 100, 100], [x, 100]],
                }
                for index, x in enumerate(range(0, 800, 200))
            ),
            *(
                {
                    "entity": "TEXT",
                    "handle": [0, 0, 30 + index],
                    "entmode": 2,
                    "ins_pt": [x + 10, 20],
                    "height": 2,
                    "text_value": f"图号：E-{index}",
                }
                for index, x in enumerate(range(0, 800, 200))
            ),
        ]
    }
    telemetry = OperationTelemetry("resources.add_resource", enabled=True)
    tracker = TokenUsageTracker()
    backend = SimpleNamespace(_token_tracker=tracker, is_available=lambda: True)
    captured_events = []
    event_bus = events.ObservabilityEventBus()
    event_bus.register("dwg-concurrency-test", captured_events.append)
    monkeypatch.setattr(events, "_GLOBAL_EVENT_BUS", event_bus)
    active = peak = 0
    ready = None
    observations = []

    async def completion(*, prompt, images, thinking):
        nonlocal active, peak, ready
        assert prompt and images == [b"synthetic-sheet"] and thinking is False
        if ready is None:
            ready = asyncio.Event()
        active += 1
        peak = max(peak, active)
        if active == 4:
            ready.set()
        try:
            # Rendezvous proves overlap without timing-dependent sleeps.
            await asyncio.wait_for(ready.wait(), timeout=5)
            observations.append(
                (
                    asyncio.get_running_loop(),
                    threading.get_ident(),
                    get_current_telemetry(),
                    get_current_telemetry_stage(),
                )
            )
            VLMBase.update_token_usage(
                backend,
                model_name="synthetic-model",
                provider="synthetic-provider",
                prompt_tokens=1,
                completion_tokens=2,
            )
            return '{"regions":[]}'
        finally:
            active -= 1

    backend.get_vision_completion_async = completion
    monkeypatch.setattr(dwg, "get_openviking_config", lambda: SimpleNamespace(vlm=backend))
    classifier = dwg._region_classifier()
    root = RootSpanAttributes(
        http_method="POST",
        http_route="/api/v1/resources",
        request_id="synthetic-request",
        account_id="synthetic-account",
        user_id="synthetic-user",
    )
    root_token = bind_root_context(root)
    try:
        with bind_telemetry(telemetry), bind_telemetry_stage("dwg_regions"):
            result = build_sheet_artifacts(
                drawing,
                source_name="synthetic.dwg",
                source_sha256="a" * 64,
                source_size_bytes=100,
                region_classifier=classifier,
                sheet_renderer=lambda _: b"synthetic-sheet",
                max_concurrent_regions=4,
            )
    finally:
        reset_observability_context(root_token)

    assert len(result["sheets"]) == 4
    assert all(sheet["status"] == "complete" for sheet in result["sheets"])
    assert peak == 4 and active == 0 and len(observations) == 4
    assert len({loop for loop, _, _, _ in observations}) == 1
    assert len({thread for _, thread, _, _ in observations}) == 1
    assert all(
        handle is telemetry and stage == "dwg_regions" for _, _, handle, stage in observations
    )
    model_usage = tracker.get_model_usage("synthetic-model")
    assert model_usage is not None
    usage = model_usage.total_usage
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, usage.call_count) == (
        4,
        8,
        12,
        4,
    )
    summary = telemetry.finish().summary
    expected_usage = {"input": 4, "output": 8, "total": 12}
    assert summary["tokens"]["total"] == 12
    assert summary["tokens"]["llm"] == expected_usage
    assert summary["tokens"]["stages"]["dwg_regions"]["llm"] == expected_usage
    assert len(captured_events) == 4
    assert all(
        event.event_name == "vlm.call"
        and event.request_id == "synthetic-request"
        and event.account_id == "synthetic-account"
        and event.user_id == "synthetic-user"
        for event in captured_events
    )
    assert sum(event.payload["prompt_tokens"] for event in captured_events) == 4
    assert sum(event.payload["completion_tokens"] for event in captured_events) == 8
