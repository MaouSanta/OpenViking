# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""DWG data stays readable but never becomes automatic model input on rescans."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openviking.core.context import ContextLevel
from openviking.parse.base import NodeType, ResourceNode, create_parse_result
from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.directory import DirectoryParser
from openviking.resource.atomic_bundle import is_dwg_parser_data
from openviking.server.identity import RequestContext, Role
from openviking.service import reindex_executor as reindex_module
from openviking.service.reindex_executor import ReindexExecutor, _ReindexCounters
from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
from openviking.storage.queuefs.semantic_processor import SemanticProcessor
from openviking_cli.session.user_id import UserIdentifier
from tests.misc.test_media_processor_zip_root import _FakeVikingFS as MergeFS
from tests.storage.test_semantic_dag_skip_files import _DummyTracker, _FakeAgfs

ROOT = "viking://resources/mixed"
CTX = RequestContext(user=UserIdentifier("acc1", "user1"), role=Role.USER)
SENTINEL = "DWG_DATA_MUST_NEVER_BE_MODEL_INPUT"
DWG = json.dumps({"schema": "openviking.dwg-json/v1", "records": [{"value": SENTINEL}]}, indent=2)
DWG_MANIFEST = json.dumps(
    {
        "schema": "openviking.resource-bundle/v2",
        "kind": "dwg",
        "note": SENTINEL,
        "source": {"path": "source.dwg", "sha256": "a" * 64, "ingested_at": "2026-09-10T00:00:00Z"},
        "artifacts": {"source": {"path": "source.dwg", "schema": "dwg", "sha256": "a" * 64}},
    }
)


class MemoryFS(MergeFS):
    def __init__(self, files=None):
        super().__init__()
        self.files.update({path: text.encode() for path, text in (files or {}).items()})
        self._async_agfs = _FakeAgfs()

    async def write_file(self, uri, content, **kwargs):
        await super().write_file(uri, content)

    async def abstract(self, uri, **kwargs):
        return await self.read_file(uri + "/.abstract.md")

    async def overview(self, uri, **kwargs):
        return await self.read_file(uri + "/.overview.md")

    def _uri_to_path(self, uri, **kwargs):
        return uri.replace("viking://", "/local/acc1/")


class CapturingProcessor(SemanticProcessor):
    def __init__(self, vlm):
        super().__init__(max_concurrent_llm=2)
        self.vlm = vlm
        self.vector_inputs = []

    async def _generate_overview(self, uri, file_summaries, children_abstracts):
        return await self.vlm.get_completion_async(
            json.dumps(
                {
                    "uri": uri,
                    "files": file_summaries,
                    "children": children_abstracts,
                }
            )
        )

    async def _vectorize_single_file(self, **kwargs):
        self.vector_inputs.append(kwargs)

    async def _vectorize_directory(self, uri, context_type, abstract, overview, **kwargs):
        self.vector_inputs.append({"uri": uri, "abstract": abstract, "overview": overview})


def setup_processor(monkeypatch, fs):
    config = MagicMock()
    config.output_language_override = "en"
    config.semantic.max_file_content_chars = 10000
    config.semantic.max_skeleton_chars = 10000
    config.semantic.overview_max_chars = 10000
    config.semantic.abstract_max_chars = 10000
    config.vlm.is_available.return_value = True
    config.vlm.get_completion_async = AsyncMock(return_value="# Summary\nSafe content")
    monkeypatch.setattr(
        "openviking.storage.queuefs.semantic_processor.get_openviking_config", lambda: config
    )
    monkeypatch.setattr("openviking.storage.queuefs.semantic_processor.get_viking_fs", lambda: fs)
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        "openviking.storage.queuefs.embedding_tracker.EmbeddingTaskTracker.get_instance",
        lambda: _DummyTracker(),
    )
    return CapturingProcessor(config.vlm), config


@pytest.mark.asyncio
@pytest.mark.parametrize("recursive", [True, False])
async def test_generic_rescan_and_parent_refresh_do_not_use_json_or_its_stale_sidecars(
    monkeypatch, recursive
):
    fs = MemoryFS(
        {
            ROOT + "/context.md": "VISIBLE_MARKDOWN",
            ROOT + "/normal.json": '{"business":"VISIBLE_ORDINARY_JSON"}',
            ROOT + "/metadata_0001.json": DWG,
            ROOT + "/manifest.json": DWG_MANIFEST,
            ROOT + "/damaged.json": DWG[:-2],
            ROOT + "/arbitrary-folder/evidence.json": DWG,
            ROOT + "/arbitrary-folder/.abstract.md": SENTINEL,
            ROOT + "/arbitrary-folder/.overview.md": SENTINEL,
            ROOT + "/ordinary/context.md": "VISIBLE_CHILD_MARKDOWN",
            ROOT + "/ordinary/.abstract.md": "VISIBLE_CHILD_ABSTRACT",
        }
    )
    before = dict(fs.files)
    processor, config = setup_processor(monkeypatch, fs)
    await SemanticDagExecutor(processor, "resource", 2, ctx=CTX, recursive=recursive).run(ROOT)
    prompts = "\n".join(
        str(call.args[0]) for call in config.vlm.get_completion_async.await_args_list
    )
    assert "VISIBLE_MARKDOWN" in prompts and "VISIBLE_ORDINARY_JSON" in prompts
    assert SENTINEL not in prompts
    assert SENTINEL not in json.dumps(processor.vector_inputs, default=str)
    assert not any(
        "arbitrary-folder" in json.dumps(item, default=str) for item in processor.vector_inputs
    )
    assert all(fs.files[path] == value for path, value in before.items() if path.endswith(".json"))
    assert await fs.read_file(ROOT + "/metadata_0001.json") == DWG  # Read access remains intact.


@pytest.mark.asyncio
@pytest.mark.parametrize("root_suffix", ["", "/unusual.json"])
@pytest.mark.parametrize("payload", [DWG, DWG[:-2]])
async def test_direct_data_directory_and_file_summary_make_no_model_calls(
    monkeypatch, root_suffix, payload
):
    fs = MemoryFS({ROOT + "/unusual.json": payload, ROOT + "/.abstract.md": SENTINEL})
    processor, config = setup_processor(monkeypatch, fs)
    await SemanticDagExecutor(processor, "resource", 2, ctx=CTX).run(ROOT + root_suffix)
    result = await processor._generate_single_file_summary(ROOT + "/unusual.json", ctx=CTX)
    assert result == {"name": "unusual.json", "summary": ""}
    config.vlm.get_completion_async.assert_not_awaited()
    assert processor.vector_inputs == []


@pytest.mark.asyncio
async def test_reindex_vectors_skips_json_even_with_old_summary_and_keeps_ordinary_json(
    monkeypatch,
):
    fs = MemoryFS(
        {
            ROOT + "/context.md": "VISIBLE_MARKDOWN",
            ROOT + "/normal.json": '{"business":"VISIBLE_ORDINARY_JSON"}',
            ROOT + "/metadata_0001.json": DWG,
            ROOT + "/damaged.json": DWG[:-2],
            ROOT + "/chunks/evidence.json": DWG,
            ROOT + "/chunks/.abstract.md": SENTINEL,
            ROOT + "/chunks/.overview.md": SENTINEL,
        }
    )
    monkeypatch.setattr(reindex_module, "get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        reindex_module,
        "get_openviking_config",
        lambda: SimpleNamespace(
            embedding=SimpleNamespace(text_source="summary_first", max_input_chars=10000)
        ),
    )
    executor = ReindexExecutor()
    executor._fetch_existing_record = AsyncMock(return_value={"abstract": SENTINEL})
    executor._best_file_summary = AsyncMock(
        side_effect=lambda uri, **kwargs: (
            SENTINEL if uri.endswith("evidence.json") or "metadata" in uri else ""
        )
    )
    executor._upsert_context = AsyncMock()
    await executor._reindex_resource_vectors_from_entries(
        root_uri=ROOT,
        directories=[ROOT + "/chunks"],
        files=[
            ROOT + "/context.md",
            ROOT + "/normal.json",
            ROOT + "/metadata_0001.json",
            ROOT + "/damaged.json",
            ROOT + "/chunks/evidence.json",
        ],
        counters=_ReindexCounters(),
        ctx=CTX,
    )
    calls = [call.kwargs for call in executor._upsert_context.await_args_list]
    assert {call["uri"] for call in calls} == {ROOT + "/context.md", ROOT + "/normal.json"}
    assert SENTINEL not in json.dumps(calls, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [ContextLevel.ABSTRACT, ContextLevel.OVERVIEW])
async def test_restoring_data_directory_markers_cannot_embed_stale_json_summary(monkeypatch, level):
    fs = MemoryFS(
        {
            ROOT + "/evidence.json": DWG,
            ROOT + "/.abstract.md": SENTINEL,
            ROOT + "/.overview.md": SENTINEL,
        }
    )
    monkeypatch.setattr(reindex_module, "get_viking_fs", lambda: fs)
    executor = ReindexExecutor()
    executor._upsert_context = AsyncMock()
    await executor.reindex_directory_marker(dir_uri=ROOT, level=level, ctx=CTX)
    executor._upsert_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_directory_parser_keeps_json_bytes_but_schema_guard_prevents_model_input(
    tmp_path, monkeypatch
):
    (tmp_path / "drawing.dwg").write_bytes(b"synthetic DWG placeholder")
    (tmp_path / "business.json").write_text('{"business":"VISIBLE_ORDINARY_JSON"}')
    fs = MemoryFS()
    temporary = fs.create_temp_uri()
    await fs.mkdir(temporary)
    await fs.mkdir(temporary + "/drawing")
    await fs.write_file(temporary + "/drawing/metadata_0001.json", DWG)
    await fs.write_file(temporary + "/drawing/chunks/a.json", DWG)
    await fs.write_file(temporary + "/drawing/sheets/S1/context.md", "VISIBLE_MARKDOWN")
    parsed = create_parse_result(
        root=ResourceNode(type=NodeType.ROOT),
        source_path="drawing.dwg",
        source_format="dwg",
        parser_name="DWGParser",
        parse_time=0,
    )
    parsed.temp_dir_path = temporary
    fake_parser = SimpleNamespace(parse=AsyncMock(return_value=parsed))
    monkeypatch.setattr(BaseParser, "_get_viking_fs", lambda self: fs)
    monkeypatch.setattr(
        DirectoryParser,
        "_assign_parser",
        staticmethod(lambda file, registry: fake_parser if file.path.suffix == ".dwg" else None),
    )
    directory_result = await DirectoryParser().parse(tmp_path, preserve_structure=True)
    assert directory_result.meta["file_count"] == 2
    root = directory_result.temp_dir_path + "/" + tmp_path.name
    protected = {path: value for path, value in fs.files.items() if SENTINEL.encode() in value}
    assert len(protected) == 2
    processor, config = setup_processor(monkeypatch, fs)
    await SemanticDagExecutor(processor, "resource", 2, ctx=CTX).run(root)
    prompts = "\n".join(
        str(call.args[0]) for call in config.vlm.get_completion_async.await_args_list
    )
    assert "VISIBLE_MARKDOWN" in prompts and "VISIBLE_ORDINARY_JSON" in prompts
    assert SENTINEL not in prompts and SENTINEL not in json.dumps(
        processor.vector_inputs, default=str
    )
    assert all(fs.files[path] == value for path, value in protected.items())
    assert not await is_dwg_parser_data(fs, root + "/business.json", ctx=CTX)
