# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openviking.parse.base import NodeType, ParseResult, ResourceNode
from openviking.server.identity import RequestContext, Role
from openviking.utils.ingest_options import IngestOptions
from openviking.utils.resource_processor import ResourceProcessor
from openviking_cli.session.user_id import UserIdentifier


class _FakeVikingDB:
    def get_embedder(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_preexisting", [False, True])
@pytest.mark.parametrize("narrow_selection", [False, True])
async def test_standalone_dwg_carries_markdown_allowlist_through_deferred_processing(
    monkeypatch, ctx, target_preexisting, narrow_selection
):
    root_uri = "viking://resources/plan"
    temp_uri = "viking://temp/job/plan_dwg"
    content_paths = ["sheets/S001/context.md", "sheets/S002/context_0000.md"]
    viking_fs = SimpleNamespace(
        bind_request_context=lambda _ctx: nullcontext(),
        exists=AsyncMock(return_value=target_preexisting),
        stat=AsyncMock(return_value={"isDir": False}),
        persist_temp_tree=AsyncMock(),
        delete_temp=AsyncMock(),
        _async_agfs=SimpleNamespace(pathlock_handoff=AsyncMock(), pathlock_release=AsyncMock()),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    monkeypatch.setattr("openviking.utils.resource_processor.rewrite_image_uris", AsyncMock())
    processor = ResourceProcessor(_FakeVikingDB())
    parse_result = ParseResult(
        root=ResourceNode(type=NodeType.ROOT, title="plan"),
        source_path="plan.dwg",
        source_format="dwg",
        temp_dir_path="viking://temp/job",
        meta={
            "temp_root_uri": temp_uri,
            "sheet_bundles": [{"content_paths": [path]} for path in content_paths],
            "json_artifacts": [{"path": "shared/chunk_00001.json"}],
        },
    )
    processor._get_media_processor = Mock(
        return_value=SimpleNamespace(process=AsyncMock(return_value=parse_result))
    )
    processor.tree_builder.finalize_from_temp = AsyncMock(
        return_value=SimpleNamespace(root=SimpleNamespace(uri=root_uri, temp_uri=temp_uri))
    )
    processor._summarizer = SimpleNamespace(
        summarize=AsyncMock(return_value={"status": "success", "enqueued_count": 1})
    )
    lock = {"lease_ref": "lock-1"}
    result = await processor.process_resource(
        "plan.dwg", ctx, resource_lock=lock, defer_post_processing=True
    )

    prepared = result["_post_process"]
    assert prepared["summary_files"] == content_paths
    assert prepared["temp_uri"] == (temp_uri if target_preexisting else root_uri)
    processor._summarizer.summarize.assert_not_awaited()
    selection = (
        {"summary_files": [content_paths[1], "shared/chunk_00001.json"]} if narrow_selection else {}
    )
    await processor.finish_prepared_resource(
        prepared, ctx=ctx, resource_lock=lock, build_index=True, **selection
    )

    args = processor._summarizer.summarize.await_args.kwargs
    assert args["summary_files"] == ([content_paths[1]] if narrow_selection else content_paths)
    assert args["temp_uris"] == [temp_uri if target_preexisting else root_uri]
    assert args["resource_uris"] == [root_uri]
    assert args["skip_vectorization"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("has_lock", [False, True])
async def test_standalone_dwg_vectors_only_indexes_markdown_without_scanning_json(
    monkeypatch, ctx, has_lock
):
    viking_fs = SimpleNamespace(
        tree=AsyncMock(side_effect=AssertionError("DWG JSON tree must not be scanned")),
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    vectorize_file = AsyncMock()
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", vectorize_file)
    processor = ResourceProcessor(_FakeVikingDB())
    processor._get_summarizer = Mock(side_effect=AssertionError("summarizer should not run"))
    content_paths = ["sheets/S001/context.md", "sheets/S002/context_0000.md"]

    await processor.finish_prepared_resource(
        {
            "root_uri": "viking://resources/plan",
            "temp_uri": "viking://resources/plan",
            "source_committed": True,
            "summary_files": content_paths,
        },
        ctx=ctx,
        resource_lock={"lease_ref": "lock-1"} if has_lock else None,
        processing_mode="vectors_only",
        build_index=True,
        summary_files=[content_paths[1], "shared/chunk_00001.json"],
    )

    vectorize_file.assert_awaited_once()
    assert vectorize_file.await_args.kwargs["file_path"] == (
        "viking://resources/plan/sheets/S002/context_0000.md"
    )
    viking_fs.tree.assert_not_awaited()


class _RecordingVikingDB:
    def __init__(self):
        self.lookup_calls = []
        self.filter_calls = []
        self.delete_calls = []

    def get_embedder(self):
        return None

    async def get_context_by_uri(self, *, uri, level, limit, ctx):
        self.lookup_calls.append((uri, level, limit, ctx))
        return [{"id": f"{uri}:{level}"}]

    async def delete(self, ids, *, ctx):
        self.delete_calls.append((ids, ctx))
        return len(ids)

    async def filter(self, *, filter, limit, output_fields, ctx):
        self.filter_calls.append((filter, limit, output_fields, ctx))
        return [{"id": "recursive-child-detail"}]


@pytest.fixture
def ctx() -> RequestContext:
    return RequestContext(
        user=UserIdentifier("account-1", "user-1"),
        role=Role.USER,
    )


@pytest.mark.asyncio
async def test_vectors_only_persists_tree_and_vectorizes_files_only(monkeypatch, ctx):
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
        persist_temp_tree=AsyncMock(),
        delete_temp=AsyncMock(),
        tree=AsyncMock(
            return_value=[
                {"uri": "viking://resources/demo/section", "isDir": True},
                {
                    "uri": "viking://resources/demo/section/page.md",
                    "isDir": False,
                    "name": "page.md",
                },
                {
                    "uri": "viking://resources/demo/section/.abstract.md",
                    "isDir": False,
                    "name": ".abstract.md",
                },
            ]
        ),
    )
    vectorize_file = AsyncMock()
    rewrite_image_uris = AsyncMock()
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    monkeypatch.setattr("openviking.utils.resource_processor.rewrite_image_uris", rewrite_image_uris)
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", vectorize_file)
    processor = ResourceProcessor(_FakeVikingDB())
    processor._get_summarizer = Mock(side_effect=AssertionError("summarizer should not run"))
    processor._delete_resource_semantic_markers = AsyncMock()
    processor._delete_resource_semantic_vectors = AsyncMock()
    processor._delete_removed_resource_vectors = AsyncMock()
    lock = {"lease_ref": "lock-1"}

    result = await processor.finish_prepared_resource(
        {
            "root_uri": "viking://resources/demo",
            "temp_uri": "viking://temp/demo",
            "temp_dir_path": "tmp/demo",
            "source_committed": False,
        },
        ctx=ctx,
        resource_lock=lock,
        build_index=True,
        processing_mode="vectors_only",
        ingest_options=IngestOptions.from_search_tags(["team=search"], mode="append"),
    )

    assert result == {"status": "success", "root_uri": "viking://resources/demo"}
    viking_fs.persist_temp_tree.assert_awaited_once_with(
        "viking://temp/demo",
        "viking://resources/demo",
        ctx=ctx,
        lease_ref=lock,
    )
    rewrite_image_uris.assert_awaited_once_with(
        "viking://resources/demo",
        ctx=ctx,
        lease_ref=lock,
    )
    viking_fs.delete_temp.assert_awaited_once_with("tmp/demo", ctx=ctx)
    vectorize_file.assert_awaited_once()
    assert vectorize_file.await_args.kwargs["file_path"] == (
        "viking://resources/demo/section/page.md"
    )
    assert vectorize_file.await_args.kwargs["parent_uri"] == "viking://resources/demo/section"
    assert vectorize_file.await_args.kwargs["summary_dict"] == {
        "name": "page.md",
        "summary": "",
    }
    assert vectorize_file.await_args.kwargs["ingest_options"] == IngestOptions(
        search_tags=["team=search"],
        search_tag_mode="append",
    )
    assert vectorize_file.await_args.kwargs["register_request_wait"] is True
    processor._delete_resource_semantic_markers.assert_not_awaited()
    processor._delete_resource_semantic_vectors.assert_not_awaited()
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_vectors_only_skips_vectorization_when_build_index_false(monkeypatch, ctx):
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
        persist_temp_tree=AsyncMock(),
        delete_temp=AsyncMock(),
        tree=AsyncMock(return_value=[]),
    )
    vectorize_file = AsyncMock()
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    monkeypatch.setattr("openviking.utils.resource_processor.rewrite_image_uris", AsyncMock())
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", vectorize_file)
    processor = ResourceProcessor(_FakeVikingDB())
    processor._delete_resource_semantic_markers = AsyncMock()
    processor._delete_resource_semantic_vectors = AsyncMock()
    lock = {"lease_ref": "lock-1"}

    await processor.finish_prepared_resource(
        {
            "root_uri": "viking://resources/demo",
            "temp_uri": "viking://temp/demo",
            "source_committed": False,
        },
        ctx=ctx,
        resource_lock=lock,
        build_index=False,
        processing_mode="vectors_only",
    )

    processor._delete_resource_semantic_markers.assert_not_awaited()
    processor._delete_resource_semantic_vectors.assert_not_awaited()
    vectorize_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_vectors_only_syncs_preexisting_target_instead_of_merging(monkeypatch, ctx):
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
        persist_temp_tree=AsyncMock(),
        delete_temp=AsyncMock(),
        tree=AsyncMock(return_value=[]),
    )
    sync = AsyncMock()
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    monkeypatch.setattr("openviking.utils.resource_processor.SemanticProcessor", Mock())
    monkeypatch.setattr(
        "openviking.utils.resource_processor.SemanticProcessor.return_value._sync_topdown_recursive",
        sync,
    )
    monkeypatch.setattr("openviking.utils.resource_processor.rewrite_image_uris", AsyncMock())
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", AsyncMock())
    processor = ResourceProcessor(_FakeVikingDB())
    processor._delete_resource_semantic_vectors = AsyncMock()
    lock = {"lease_ref": "lock-1"}

    await processor.finish_prepared_resource(
        {
            "root_uri": "viking://resources/demo",
            "temp_uri": "viking://temp/demo",
            "temp_dir_path": "viking://temp/demo",
            "source_committed": False,
            "target_preexisting": True,
        },
        ctx=ctx,
        resource_lock=lock,
        build_index=True,
        processing_mode="vectors_only",
    )

    viking_fs.persist_temp_tree.assert_not_awaited()
    sync.assert_awaited_once_with(
        "viking://temp/demo",
        "viking://resources/demo",
        ctx=ctx,
        lock=lock,
    )


@pytest.mark.asyncio
async def test_vectors_only_leaves_existing_semantic_vectors_untouched(monkeypatch, ctx):
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
        persist_temp_tree=AsyncMock(),
        delete_temp=AsyncMock(),
        tree=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    monkeypatch.setattr("openviking.utils.resource_processor.rewrite_image_uris", AsyncMock())
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", AsyncMock())
    processor = ResourceProcessor(_FakeVikingDB())
    processor._delete_resource_semantic_vectors = AsyncMock()
    processor._delete_removed_resource_vectors = AsyncMock()
    lock = {"lease_ref": "lock-1"}

    await processor.finish_prepared_resource(
        {
            "root_uri": "viking://resources/demo",
            "temp_uri": "viking://temp/demo",
            "source_committed": False,
        },
        ctx=ctx,
        resource_lock=lock,
        build_index=True,
        processing_mode="vectors_only",
    )

    processor._delete_resource_semantic_vectors.assert_not_awaited()


@pytest.mark.asyncio
async def test_vectors_only_deletes_sync_removed_detail_vectors(monkeypatch, ctx):
    viking_fs = SimpleNamespace(
        _async_agfs=SimpleNamespace(pathlock_release=AsyncMock()),
        persist_temp_tree=AsyncMock(),
        delete_temp=AsyncMock(),
        tree=AsyncMock(return_value=[]),
    )
    diff = SimpleNamespace(
        deleted_files=["viking://resources/demo/old.md"],
        deleted_dirs=["viking://resources/demo/old-dir"],
    )
    monkeypatch.setattr("openviking.utils.resource_processor.get_viking_fs", lambda: viking_fs)
    monkeypatch.setattr("openviking.utils.resource_processor.SemanticProcessor", Mock())
    monkeypatch.setattr(
        "openviking.utils.resource_processor.SemanticProcessor.return_value._sync_topdown_recursive",
        AsyncMock(return_value=diff),
    )
    monkeypatch.setattr("openviking.utils.resource_processor.rewrite_image_uris", AsyncMock())
    monkeypatch.setattr("openviking.utils.resource_processor.vectorize_file", AsyncMock())
    processor = ResourceProcessor(_FakeVikingDB())
    processor._delete_resource_semantic_vectors = AsyncMock()
    processor._delete_removed_resource_vectors = AsyncMock()
    lock = {"lease_ref": "lock-1"}

    await processor.finish_prepared_resource(
        {
            "root_uri": "viking://resources/demo",
            "temp_uri": "viking://temp/demo",
            "source_committed": False,
            "target_preexisting": True,
        },
        ctx=ctx,
        resource_lock=lock,
        build_index=True,
        processing_mode="vectors_only",
    )

    processor._delete_removed_resource_vectors.assert_awaited_once_with(
        files=["viking://resources/demo/old.md"],
        dirs=["viking://resources/demo/old-dir"],
        ctx=ctx,
    )


@pytest.mark.asyncio
async def test_delete_removed_resource_vectors_deletes_detail_records(ctx):
    vikingdb = _RecordingVikingDB()
    processor = ResourceProcessor(vikingdb)

    await processor._delete_removed_resource_vectors(
        files=["viking://resources/demo/old.md"],
        dirs=["viking://resources/demo/old-dir"],
        ctx=ctx,
    )

    assert vikingdb.lookup_calls == [
        ("viking://resources/demo/old.md", 2, 100, ctx),
    ]
    assert len(vikingdb.filter_calls) == 1
    assert vikingdb.filter_calls[0][1] == 100_000
    assert vikingdb.delete_calls == [
        (["viking://resources/demo/old.md:2"], ctx),
        (["recursive-child-detail"], ctx),
    ]
