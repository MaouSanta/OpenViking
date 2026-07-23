# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused coverage for V1 atomic resource bundles."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.resource.atomic_bundle import is_resource_bundle_manifest
from openviking.server.identity import RequestContext, Role
from openviking.service import resource_service as resource_service_module
from openviking.service import search_service as search_service_module
from openviking.service.fs_service import FSService
from openviking.service.resource_service import ResourceService
from openviking.service.search_service import SearchService
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import FailedPreconditionError, InvalidURIError
from openviking_cli.retrieve.types import ContextType, FindResult, MatchedContext
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture
def ctx():
    return RequestContext(user=UserIdentifier("acct", "alice"), role=Role.USER)


class _Lock:
    def __init__(self):
        self.handle = object()
        self.close = AsyncMock()


class _BundleFS:
    def __init__(self):
        self.existing = set()
        self.directories = set()
        self.files = {}
        self.removed = []

    async def exists(self, uri, *, ctx):
        del ctx
        return uri in self.existing or uri in self.directories or uri in self.files

    async def mkdir(self, uri, *, ctx):
        del ctx
        self.directories.add(uri)

    async def write_file_bytes(self, uri, content, *, ctx, lock_handle):
        del ctx, lock_handle
        self.files[uri] = content

    async def write_file(self, uri, content, *, ctx, lock_handle, **kwargs):
        del ctx, lock_handle, kwargs
        self.files[uri] = content.encode("utf-8")

    async def rm(self, uri, *, recursive, ctx, lock_handle):
        del recursive, ctx, lock_handle
        self.removed.append(uri)


def _bundle_service(viking_fs):
    return ResourceService(
        vikingdb=object(),
        viking_fs=viking_fs,
        resource_processor=object(),
        skill_processor=object(),
    )


def test_bundle_marker_requires_the_v1_required_fields():
    assert not is_resource_bundle_manifest({"schema": "openviking.resource-bundle/v1"})
    assert not is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v1",
            "kind": "step",
            "source": {"path": "model.step"},
        }
    )
    assert not is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v1",
            "kind": "step",
            "source": {
                "path": "model.step",
                "sha256": "not-a-sha256",
                "ingested_at": "2026-07-23T00:00:00+00:00",
            },
        }
    )
    assert is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v1",
            "kind": "step",
            "source": {
                "path": "model.step",
                "sha256": "a" * 64,
                "ingested_at": "2026-07-23T00:00:00+00:00",
            },
        }
    )


@pytest.mark.asyncio
async def test_add_resource_bundle_writes_only_source_and_manifest(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "gear.stp"
    source.write_bytes(b"solid gear")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _Lock()

    service._plan_resource_target = AsyncMock(
        return_value=("viking://resources/gear", lock)
    )

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to="viking://resources/gear",
        bundle=True,
        build_index=False,
    )

    assert result["root_uri"] == "viking://resources/gear"
    assert result["kind"] == "step"
    assert result["semantic_status"] == "skipped"
    assert result["vector_status"] == "skipped"
    assert set(viking_fs.files) == {
        "viking://resources/gear/gear.stp",
        "viking://resources/gear/manifest.json",
    }
    assert viking_fs.files["viking://resources/gear/gear.stp"] == b"solid gear"
    manifest = json.loads(viking_fs.files["viking://resources/gear/manifest.json"])
    assert manifest["schema"] == "openviking.resource-bundle/v1"
    assert manifest["kind"] == "step"
    assert manifest["source"]["path"] == "gear.stp"
    assert manifest["source"]["sha256"] == hashlib.sha256(b"solid gear").hexdigest()
    lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_bundle_index_tags_manifest_without_sidecars(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "scene.glb"
    source.write_bytes(b"glTF")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _Lock()
    service._plan_resource_target = AsyncMock(
        return_value=("viking://resources/scene", lock)
    )

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    queue = SimpleNamespace(enqueue=AsyncMock())
    queue_manager = SimpleNamespace(EMBEDDING="embedding", get_queue=lambda _name: queue)
    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(resource_service_module, "get_queue_manager", lambda: queue_manager)

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to="viking://resources/scene",
        bundle=True,
        wait=False,
    )

    assert result["vector_status"] == "queued"
    assert queue.enqueue.await_count == 1
    messages = [call.args[0] for call in queue.enqueue.await_args_list]
    assert [message.context_data["uri"] for message in messages] == [
        "viking://resources/scene/manifest.json",
    ]
    assert [message.context_data["level"] for message in messages] == [2]
    assert all(message.context_data["search_tags"] == ["kind=glb"] for message in messages)
    assert set(viking_fs.files) == {
        "viking://resources/scene/scene.glb",
        "viking://resources/scene/manifest.json",
    }


@pytest.mark.asyncio
async def test_bundle_keeps_its_snapshot_if_indexing_fails(tmp_path, monkeypatch, ctx):
    source = tmp_path / "gear.stp"
    source.write_bytes(b"solid gear")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _Lock()
    service._plan_resource_target = AsyncMock(
        return_value=("viking://resources/gear", lock)
    )

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    queue = SimpleNamespace(enqueue=AsyncMock(side_effect=RuntimeError("queue offline")))
    queue_manager = SimpleNamespace(EMBEDDING="embedding", get_queue=lambda _name: queue)
    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(resource_service_module, "get_queue_manager", lambda: queue_manager)

    with pytest.raises(RuntimeError, match="queue offline"):
        await service.add_resource(
            path=str(source),
            ctx=ctx,
            to="viking://resources/gear",
            bundle=True,
        )

    assert set(viking_fs.files) == {
        "viking://resources/gear/gear.stp",
        "viking://resources/gear/manifest.json",
    }
    assert viking_fs.removed == []
    lock.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_find_resource_groups_child_hits_by_highest_score(monkeypatch, ctx):
    class _SearchFS:
        def __init__(self):
            self.calls = []

        async def find(self, **kwargs):
            self.calls.append(kwargs)
            return FindResult(
                memories=[],
                skills=[],
                resources=[
                    MatchedContext(
                        uri="viking://resources/a/manifest.json",
                        context_type=ContextType.RESOURCE,
                        score=0.4,
                    ),
                    MatchedContext(
                        uri="viking://resources/a/render.png",
                        context_type=ContextType.RESOURCE,
                        score=0.9,
                        match_reason="image match",
                    ),
                    MatchedContext(
                        uri="viking://resources/b/manifest.json",
                        context_type=ContextType.RESOURCE,
                        score=0.7,
                    ),
                    MatchedContext(
                        uri="viking://resources/plain/readme.md",
                        context_type=ContextType.RESOURCE,
                        score=0.95,
                    ),
                ],
            )

    viking_fs = _SearchFS()

    async def root_for_match(_viking_fs, uri, **_kwargs):
        if "/a/" in uri:
            return "viking://resources/a"
        if "/b/" in uri:
            return "viking://resources/b"
        return None

    monkeypatch.setattr(search_service_module, "find_resource_bundle_root", root_for_match)
    result = await SearchService(viking_fs=viking_fs).find_resource(
        query="blue gear",
        ctx=ctx,
        limit=2,
    )

    assert viking_fs.calls[0]["level"] == [2]
    assert viking_fs.calls[0]["limit"] == 2
    assert result == {
        "resources": [
            {
                "uri": "viking://resources/a",
                "score": 0.9,
                "matched_uri": "viking://resources/a/render.png",
                "match_reason": "image match",
            },
            {
                "uri": "viking://resources/b",
                "score": 0.7,
                "matched_uri": "viking://resources/b/manifest.json",
                "match_reason": "",
            },
        ],
        "total": 2,
    }


@pytest.mark.asyncio
async def test_find_resource_is_scoped_to_resources(ctx):
    with pytest.raises(InvalidURIError, match="resources"):
        await SearchService(viking_fs=object()).find_resource(
            query="blue gear",
            ctx=ctx,
            target_uri="viking://skills",
        )


@pytest.mark.asyncio
async def test_raw_write_cannot_create_an_atomic_bundle_marker(ctx):
    viking_fs = VikingFS(agfs=object())
    marker = json.dumps(
        {
            "schema": "openviking.resource-bundle/v1",
            "kind": "step",
            "source": {
                "path": "model.step",
                "sha256": "a" * 64,
                "ingested_at": "2026-07-23T00:00:00+00:00",
            },
        }
    )

    with pytest.raises(FailedPreconditionError, match="add_resource"):
        await viking_fs.write(
            "viking://resources/scene/manifest.json",
            marker,
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_bundle_member_mutation_guard_requires_the_root(monkeypatch, ctx):
    viking_fs = VikingFS(agfs=object())

    async def containing_bundle(*_args, **_kwargs):
        return "viking://resources/scene"

    monkeypatch.setattr(
        "openviking.resource.atomic_bundle.find_resource_bundle_root",
        containing_bundle,
    )

    with pytest.raises(FailedPreconditionError, match="complete bundle root"):
        await viking_fs._ensure_atomic_bundle_write_allowed(
            "viking://resources/scene/scene.glb",
            ctx=ctx,
        )
    with pytest.raises(FailedPreconditionError, match="complete package"):
        await viking_fs._ensure_atomic_bundle_delete_allowed(
            "viking://resources/scene/scene.glb",
            ctx=ctx,
            uri_is_directory=False,
        )
    await viking_fs._ensure_atomic_bundle_delete_allowed(
        "viking://resources/scene",
        ctx=ctx,
        uri_is_directory=True,
    )
    with pytest.raises(FailedPreconditionError, match="complete bundle root"):
        await viking_fs.link(
            "viking://resources/scene",
            "viking://resources/other",
            ctx=ctx,
        )
    with pytest.raises(FailedPreconditionError, match="complete bundle root"):
        await viking_fs.write_context(
            "viking://resources/scene/renders",
            content="unexpected sidecar",
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_recursive_tags_cannot_cross_a_bundle_boundary(monkeypatch, ctx):
    class _TagFS:
        def __init__(self):
            self.tree_kwargs = None

        async def tree(self, *_args, **_kwargs):
            self.tree_kwargs = _kwargs
            return [{"uri": "viking://resources/scene/manifest.json", "isDir": False}]

    async def root_for_manifest(_viking_fs, uri, **_kwargs):
        return "viking://resources/scene" if uri.endswith("manifest.json") else None

    monkeypatch.setattr(
        "openviking.service.fs_service.find_resource_bundle_root",
        root_for_manifest,
    )
    viking_fs = _TagFS()
    service = FSService(viking_fs=viking_fs)

    with pytest.raises(FailedPreconditionError, match="system-owned"):
        await service.set_tags(
            "viking://resources",
            tags=["team=design"],
            mode="replace",
            recursive=True,
            ctx=ctx,
        )

    assert viking_fs.tree_kwargs["node_limit"] is None
    assert viking_fs.tree_kwargs["level_limit"] is None
