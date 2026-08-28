# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused coverage for atomic resource bundles."""

import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest

from openviking.resource.atomic_bundle import (
    find_resource_bundle_owner_root,
    find_resource_bundle_root,
    is_resource_bundle_manifest,
)
from openviking.server.identity import RequestContext, Role
from openviking.service import resource_service as resource_service_module
from openviking.service import search_service as search_service_module
from openviking.service.fs_service import FSService
from openviking.service.resource_service import ResourceService
from openviking.service.search_service import SearchService
from openviking.storage.viking_fs import LS_ALL_NODES, VikingFS
from openviking.utils.embedding_utils import _apply_scalar_overrides
from openviking_cli.exceptions import FailedPreconditionError, InvalidURIError
from openviking_cli.retrieve.types import ContextType, FindResult, MatchedContext
from openviking_cli.session.user_id import UserIdentifier


@pytest.fixture
def ctx():
    return RequestContext(user=UserIdentifier("acct", "alice"), role=Role.USER)


def _lease(name="bundle-lock"):
    return {"lease_ref": name}


class _BundleFS:
    def __init__(self):
        self.existing = set()
        self.directories = set()
        self.files = {}
        self.removed = []

    async def exists(self, uri, *, ctx):
        del ctx
        return uri in self.existing or uri in self.directories or uri in self.files

    async def mkdir(self, uri, *, ctx, lease_ref=None):
        del ctx, lease_ref
        self.directories.add(uri)

    async def write_file_bytes(self, uri, content, *, ctx=None, lease_ref=None):
        del ctx, lease_ref
        self.files[uri] = content

    async def write_file(self, uri, content, *, ctx=None, lease_ref=None, **kwargs):
        del ctx, lease_ref, kwargs
        self.files[uri] = content.encode("utf-8")

    async def rm(self, uri, *, recursive, ctx, lease_ref=None):
        del recursive, ctx, lease_ref
        self.removed.append(uri)
        self.directories = {path for path in self.directories if not path.startswith(uri)}
        self.files = {path: data for path, data in self.files.items() if not path.startswith(uri)}

    def bind_request_context(self, ctx):
        del ctx
        return nullcontext()

    def create_temp_uri(self, *, ctx=None):
        del ctx
        return "viking://temp/staging"

    async def read_file_bytes(self, uri, *, ctx=None):
        del ctx
        return self.files[uri]

    async def read_file(self, uri, *, ctx=None):
        del ctx
        return self.files[uri]

    async def persist_temp_tree(
        self,
        temp_uri,
        target_uri,
        *,
        ctx=None,
        lease_ref=None,
        _allow_atomic_bundle_write=False,
    ):
        del ctx, lease_ref
        assert _allow_atomic_bundle_write
        prefix = temp_uri.rstrip("/") + "/"
        self.directories.add(target_uri)
        for uri, content in list(self.files.items()):
            if uri.startswith(prefix):
                self.files[target_uri + uri[len(temp_uri) :]] = content

    async def delete_temp(self, temp_uri, *, ctx=None):
        del ctx
        self.directories = {path for path in self.directories if not path.startswith(temp_uri)}
        self.files = {
            path: data for path, data in self.files.items() if not path.startswith(temp_uri)
        }


def _bundle_service(viking_fs):
    service = ResourceService(
        vikingdb=object(),
        viking_fs=viking_fs,
        resource_processor=object(),
        skill_processor=object(),
    )
    service._release_lock_ref = AsyncMock()
    return service


def test_bundle_marker_reads_v1_and_requires_a_v2_artifact_inventory():
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
    assert is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v3",
            "kind": "dwg-sheet",
            "owner": {"document_id": "dwg-123"},
            "sheet": {"id": "S001", "title": "一层平面图"},
            "source_ref": {"path": "../../plan.dwg", "sha256": "a" * 64},
            "artifacts": {
                "content": {
                    "path": "content.md",
                    "schema": "text/markdown",
                    "sha256": "b" * 64,
                    "searchable": True,
                },
                "structure": {
                    "path": "structure.json",
                    "schema": "openviking.dwg-sheet-structure/v1",
                    "sha256": "c" * 64,
                    "searchable": False,
                },
            },
        }
    )
    assert not is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v3",
            "kind": "dwg-sheet",
            "owner": {"document_id": "dwg-123"},
            "sheet": {"id": "S001"},
            "source_ref": {"path": "../../plan.dwg", "sha256": "a" * 64},
            "artifacts": {
                "structure": {
                    "path": "structure.json",
                    "schema": "openviking.dwg-sheet-structure/v1",
                    "sha256": "c" * 64,
                    "searchable": False,
                }
            },
        }
    )
    assert not is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v2",
            "kind": "step",
            "source": {
                "path": "model.step",
                "sha256": "a" * 64,
                "ingested_at": "2026-07-23T00:00:00+00:00",
            },
        }
    )
    assert is_resource_bundle_manifest(
        {
            "schema": "openviking.resource-bundle/v2",
            "kind": "step",
            "source": {
                "path": "model.step",
                "sha256": "a" * 64,
                "ingested_at": "2026-07-23T00:00:00+00:00",
            },
            "artifacts": {
                "source": {
                    "path": "model.step",
                    "schema": "application/step",
                    "sha256": "a" * 64,
                }
            },
        }
    )


def test_bundle_kind_tag_is_applied_to_each_embedding_message():
    message = SimpleNamespace(context_data={})

    _apply_scalar_overrides(message, {"search_tags": ["kind=dwg"]})

    assert message.context_data["search_tags"] == ["kind=dwg"]


@pytest.mark.asyncio
async def test_nested_sheet_searches_nearest_but_mutations_resolve_document_owner(ctx):
    viking_fs = _BundleFS()
    root = "viking://resources/plan"
    sheet = f"{root}/sheets/S001"
    viking_fs.files[f"{root}/manifest.json"] = json.dumps(
        {
            "schema": "openviking.resource-bundle/v2",
            "kind": "dwg",
            "source": {
                "path": "plan.dwg",
                "sha256": "a" * 64,
                "ingested_at": "2026-08-27T00:00:00+00:00",
            },
            "artifacts": {"source": {"path": "plan.dwg", "schema": "dwg", "sha256": "a" * 64}},
        }
    ).encode()
    viking_fs.files[f"{sheet}/manifest.json"] = json.dumps(
        {
            "schema": "openviking.resource-bundle/v3",
            "kind": "dwg-sheet",
            "owner": {"document_id": "dwg-doc-1"},
            "sheet": {"id": "S001", "title": "一层平面图"},
            "source_ref": {"path": "../../plan.dwg", "sha256": "a" * 64},
            "artifacts": {
                "content": {
                    "path": "content.md",
                    "schema": "text/markdown",
                    "sha256": "b" * 64,
                    "searchable": True,
                }
            },
        }
    ).encode()

    content_uri = f"{sheet}/content.md"
    assert await find_resource_bundle_root(viking_fs, content_uri, ctx=ctx) == sheet
    assert await find_resource_bundle_owner_root(viking_fs, content_uri, ctx=ctx) == root


@pytest.mark.asyncio
async def test_add_raw_resource_bundle_writes_only_source_and_manifest(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "gear.glb"
    source.write_bytes(b"glTF gear")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()

    async def reserve_bundle_target(*_args, **_kwargs):
        return "viking://resources/gear", lock

    service._plan_resource_target = AsyncMock(side_effect=reserve_bundle_target)

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
    assert result["kind"] == "glb"
    assert result["semantic_status"] == "skipped"
    assert result["vector_status"] == "skipped"
    assert set(viking_fs.files) == {
        "viking://resources/gear/gear.glb",
        "viking://resources/gear/manifest.json",
    }
    assert viking_fs.files["viking://resources/gear/gear.glb"] == b"glTF gear"
    manifest = json.loads(viking_fs.files["viking://resources/gear/manifest.json"])
    assert manifest["schema"] == "openviking.resource-bundle/v2"
    assert manifest["kind"] == "glb"
    assert manifest["source"]["path"] == "gear.glb"
    assert manifest["source"]["sha256"] == hashlib.sha256(b"glTF gear").hexdigest()
    assert manifest["artifacts"]["source"] == {
        "path": "gear.glb",
        "schema": "glb",
        "sha256": hashlib.sha256(b"glTF gear").hexdigest(),
    }
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_dwg_bundle_publishes_document_and_nested_sheet_bundle(tmp_path, monkeypatch, ctx):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"dwg snapshot")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    service._plan_resource_target = AsyncMock(return_value=("viking://resources/plan", lock))
    service._summarize_bundle_staging = AsyncMock(return_value={})

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    async def parse_dwg(_parser, _source, **_kwargs):
        staging = "viking://temp/job/plan_dwg"
        files = {
            "plan.dwg": b"dwg snapshot",
            "document.json": b'{"schema":"openviking.dwg-document/v1","document_id":"dwg-doc-1"}',
            "beam_topology.json": b'{"schema":"deterministic-dwg-topology-v2"}',
            "sheets/S001/content.md": "# 一层平面图\n\n生产辅助间".encode(),
            "sheets/S001/structure.json": b'{"schema":"openviking.dwg-sheet-structure/v1"}',
            "sheets/S001/sheet.dwg": b"AC1027sheet",
            "preview.png": b"png",
        }
        for name, content in files.items():
            viking_fs.files[f"{staging}/{name}"] = content
        return SimpleNamespace(
            temp_dir_path="viking://temp/job",
            meta={
                "temp_root_uri": staging,
                "document_manifest": "document.json",
                "beam_topology": "beam_topology.json",
                "sheet_bundles": [
                    {
                        "sheet_id": "S001",
                        "directory": "sheets/S001",
                        "title": "一层平面图",
                        "content_paths": ["sheets/S001/content.md"],
                        "structure_path": "sheets/S001/structure.json",
                        "dwg_path": "sheets/S001/sheet.dwg",
                        "kind": "model_frame",
                    }
                ],
            },
        )

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr("openviking.parse.parsers.dwg.DWGParser.parse", parse_dwg)

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to="viking://resources/plan",
        bundle=True,
        build_index=False,
    )

    root = "viking://resources/plan"
    assert result["semantic_status"] == "complete"
    assert result["document_id"] == "dwg-doc-1"
    assert result["sheet_uris"] == [f"{root}/sheets/S001"]
    assert set(viking_fs.files) == {
        f"{root}/plan.dwg",
        f"{root}/document.json",
        f"{root}/beam_topology.json",
        f"{root}/sheets/S001/content.md",
        f"{root}/sheets/S001/structure.json",
        f"{root}/sheets/S001/sheet.dwg",
        f"{root}/sheets/S001/manifest.json",
        f"{root}/manifest.json",
    }
    manifest = json.loads(viking_fs.files[f"{root}/manifest.json"])
    assert set(manifest["artifacts"]) == {
        "source",
        "document",
        "beam_topology",
        "sheet_0000",
    }
    assert all(
        {"path", "schema", "sha256"} <= artifact.keys()
        for artifact in manifest["artifacts"].values()
    )
    sheet_manifest = json.loads(viking_fs.files[f"{root}/sheets/S001/manifest.json"])
    assert sheet_manifest["schema"] == "openviking.resource-bundle/v3"
    assert sheet_manifest["kind"] == "dwg-sheet"
    assert sheet_manifest["owner"] == {"document_id": "dwg-doc-1"}
    assert sheet_manifest["source_ref"] == {
        "path": "../../plan.dwg",
        "sha256": hashlib.sha256(b"dwg snapshot").hexdigest(),
    }
    assert sheet_manifest["artifacts"]["content"]["searchable"] is True
    assert sheet_manifest["artifacts"]["structure"]["searchable"] is False
    assert sheet_manifest["artifacts"]["flat_dwg"]["path"] == "sheet.dwg"
    assert sheet_manifest["artifacts"]["flat_dwg"]["searchable"] is False
    assert not any(path.endswith("libredwg.json") for path in viking_fs.files)
    assert not any("preview" in path for path in viking_fs.files)
    service._summarize_bundle_staging.assert_awaited_once_with(
        "viking://temp/job/plan_dwg/sheets/S001", ctx=ctx, timeout=None
    )
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_dwg_bundle_failure_leaves_no_published_tree(tmp_path, monkeypatch, ctx):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"dwg snapshot")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    service._plan_resource_target = AsyncMock(return_value=("viking://resources/plan", lock))
    service._summarize_bundle_staging = AsyncMock(side_effect=RuntimeError("semantic failed"))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    async def parse_dwg(_parser, _source, **_kwargs):
        staging = "viking://temp/job/plan_dwg"
        files = {
            "plan.dwg": b"dwg snapshot",
            "document.json": b'{"document_id":"dwg-doc-1"}',
            "beam_topology.json": b"{}",
            "sheets/S001/content.md": b"# Sheet",
            "sheets/S001/structure.json": b"{}",
        }
        for name, content in files.items():
            viking_fs.files[f"{staging}/{name}"] = content
        return SimpleNamespace(
            temp_dir_path="viking://temp/job",
            meta={
                "temp_root_uri": staging,
                "document_manifest": "document.json",
                "beam_topology": "beam_topology.json",
                "sheet_bundles": [
                    {
                        "sheet_id": "S001",
                        "directory": "sheets/S001",
                        "content_path": "sheets/S001/content.md",
                        "structure_path": "sheets/S001/structure.json",
                    }
                ],
            },
        )

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr("openviking.parse.parsers.dwg.DWGParser.parse", parse_dwg)

    with pytest.raises(RuntimeError, match="semantic failed"):
        await service.add_resource(
            path=str(source),
            ctx=ctx,
            to="viking://resources/plan",
            bundle=True,
            build_index=False,
        )

    assert not any(path.startswith("viking://resources/plan") for path in viking_fs.files)
    assert not any(path.startswith("viking://temp/job") for path in viking_fs.files)
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_dwg_sheet_directories_cannot_overlap(ctx):
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    service._summarize_bundle_staging = AsyncMock(return_value={})
    staging = "viking://temp/job/plan_dwg"
    files = {
        "plan.dwg": b"dwg snapshot",
        "document.json": b'{"document_id":"dwg-doc-1"}',
        "beam_topology.json": b"{}",
        "sheets/S001/content.md": b"# Sheet",
        "sheets/S001/structure.json": b"{}",
        "sheets/S001/detail/content.md": b"# Nested",
        "sheets/S001/detail/structure.json": b"{}",
    }
    for name, content in files.items():
        viking_fs.files[f"{staging}/{name}"] = content

    with pytest.raises(RuntimeError, match="overlapping sheet directories"):
        await service._assemble_dwg_sheet_bundles(
            staging_uri=staging,
            parsed_meta={
                "document_manifest": "document.json",
                "beam_topology": "beam_topology.json",
                "sheet_bundles": [
                    {
                        "sheet_id": "S001",
                        "directory": "sheets/S001",
                        "content_path": "sheets/S001/content.md",
                        "structure_path": "sheets/S001/structure.json",
                    },
                    {
                        "sheet_id": "S002",
                        "directory": "sheets/S001/detail",
                        "content_path": "sheets/S001/detail/content.md",
                        "structure_path": "sheets/S001/detail/structure.json",
                    },
                ],
            },
            stored_name="plan.dwg",
            source_sha256=hashlib.sha256(b"dwg snapshot").hexdigest(),
            ctx=ctx,
            timeout=None,
        )


@pytest.mark.asyncio
async def test_bundle_indexes_every_searchable_artifact(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "scene.glb"
    source.write_bytes(b"glTF")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    service._plan_resource_target = AsyncMock(return_value=("viking://resources/scene", lock))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    index = AsyncMock()
    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(resource_service_module, "index_resource", index)

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to="viking://resources/scene",
        bundle=True,
        wait=False,
    )

    assert result["vector_status"] == "queued"
    index.assert_awaited_once_with(
        "viking://resources/scene",
        ctx,
        scalar_override={"search_tags": ["kind=glb"]},
    )
    assert set(viking_fs.files) == {
        "viking://resources/scene/scene.glb",
        "viking://resources/scene/manifest.json",
    }


@pytest.mark.asyncio
async def test_dwg_bundle_indexes_only_sheet_markdown(monkeypatch, ctx):
    service = _bundle_service(_BundleFS())
    index = AsyncMock()
    monkeypatch.setattr(resource_service_module, "index_resource", index)

    queue_status, vector_status = await service._index_atomic_resource_bundle(
        root_uri="viking://resources/plan",
        kind="dwg",
        ctx=ctx,
        wait=False,
        timeout=None,
        sheet_bundles=[
            {
                "directory": "sheets/S001",
                "content_paths": [
                    "sheets/S001/content_001.md",
                    "sheets/S001/content_002.md",
                ],
            }
        ],
    )

    assert (queue_status, vector_status) == (None, "queued")
    index.assert_awaited_once_with(
        "viking://resources/plan/sheets/S001",
        ctx,
        scalar_override={"search_tags": ["kind=dwg-sheet"]},
        include_files={"content_001.md", "content_002.md"},
        include_directory_meta=False,
    )


@pytest.mark.asyncio
async def test_bundle_keeps_its_snapshot_if_indexing_fails(tmp_path, monkeypatch, ctx):
    source = tmp_path / "gear.glb"
    source.write_bytes(b"glTF gear")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    service._plan_resource_target = AsyncMock(return_value=("viking://resources/gear", lock))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(
        resource_service_module,
        "index_resource",
        AsyncMock(side_effect=RuntimeError("queue offline")),
    )

    with pytest.raises(RuntimeError, match="queue offline"):
        await service.add_resource(
            path=str(source),
            ctx=ctx,
            to="viking://resources/gear",
            bundle=True,
        )

    assert set(viking_fs.files) == {
        "viking://resources/gear/gear.glb",
        "viking://resources/gear/manifest.json",
    }
    assert viking_fs.removed == []
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_step_bundle_publishes_flat_evidence_and_six_views(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "gear.stp"
    source.write_bytes(b"step snapshot")
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    service._plan_resource_target = AsyncMock(return_value=("viking://resources/gear", lock))
    service._summarize_bundle_staging = AsyncMock(return_value={})

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    async def parse_step(_parser, _source, **_kwargs):
        staging = "viking://temp/job/gear_step"
        files = {
            "gear.stp": b"step snapshot",
            "evidence.json": b'{"schema":"openviking.step-evidence/v1"}',
            ".abstract.md": b"gear abstract",
            ".overview.md": b"gear overview",
            "front.png": b"front",
            "top.png": b"top",
            "right.png": b"right",
            "iso_front_top.png": b"iso_front_top",
            "iso_back_top.png": b"iso_back_top",
            "iso_front_bottom.png": b"iso_front_bottom",
        }
        for name, content in files.items():
            viking_fs.files[f"{staging}/{name}"] = content
        return SimpleNamespace(
            temp_dir_path="viking://temp/job",
            meta={"temp_root_uri": staging},
        )

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr("openviking.parse.parsers.step.STEPParser.parse", parse_step)

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to="viking://resources/gear",
        bundle=True,
        build_index=False,
    )

    assert result["semantic_status"] == "complete"
    root = "viking://resources/gear"
    assert {path.removeprefix(f"{root}/") for path in viking_fs.files} == {
        "gear.stp",
        "evidence.json",
        ".abstract.md",
        ".overview.md",
        "front.png",
        "top.png",
        "right.png",
        "iso_front_top.png",
        "iso_back_top.png",
        "iso_front_bottom.png",
        "manifest.json",
    }
    manifest = json.loads(viking_fs.files[f"{root}/manifest.json"])
    assert manifest["schema"] == "openviking.resource-bundle/v2"
    assert set(manifest["artifacts"]) == {
        "source",
        "evidence",
        "abstract",
        "overview",
        "view_front",
        "view_top",
        "view_right",
        "view_iso_front_top",
        "view_iso_back_top",
        "view_iso_front_bottom",
    }
    assert manifest["artifacts"]["evidence"]["schema"] == "openviking.step-evidence/v1"
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_find_resource_expands_child_hits_and_uses_each_bundle_maximum(monkeypatch, ctx):
    class _SearchFS:
        def __init__(self):
            self.calls = []

        async def find(self, **kwargs):
            self.calls.append(kwargs)
            assert kwargs["filter"] == {
                "op": "must",
                "field": "search_tags",
                "conds": ["kind=step"],
            }
            resources = [
                MatchedContext(
                    uri="viking://resources/a/manifest.json",
                    context_type=ContextType.RESOURCE,
                    score=0.4,
                ),
                MatchedContext(
                    uri="viking://resources/a/preview.png",
                    context_type=ContextType.RESOURCE,
                    score=0.9,
                    match_reason="image match",
                ),
                MatchedContext(
                    uri="viking://resources/b/drawing_text.json",
                    context_type=ContextType.RESOURCE,
                    score=0.7,
                ),
                MatchedContext(
                    uri="viking://resources/plain/readme.md",
                    context_type=ContextType.RESOURCE,
                    score=0.95,
                ),
            ]
            return FindResult(
                memories=[],
                skills=[],
                resources=resources[: kwargs["limit"]],
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
        filter={"op": "must", "field": "search_tags", "conds": ["kind=step"]},
    )

    assert [call["limit"] for call in viking_fs.calls] == [2, 4]
    assert all(call["level"] == [0, 1, 2] for call in viking_fs.calls)
    assert result == {
        "resources": [
            {
                "uri": "viking://resources/a",
                "score": 0.9,
                "matched_uri": "viking://resources/a/preview.png",
                "match_reason": "image match",
            },
            {
                "uri": "viking://resources/b",
                "score": 0.7,
                "matched_uri": "viking://resources/b/drawing_text.json",
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
async def test_temp_copy_cannot_create_an_atomic_bundle_marker(ctx):
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
    ).encode()
    viking_fs.read_file_bytes = AsyncMock(return_value=marker)

    with pytest.raises(FailedPreconditionError, match="add_resource"):
        await viking_fs._copy_for_mv(
            old_uri="viking://temp/staged/marker.json",
            new_uri="viking://resources/scene/manifest.json",
            old_path="/local/acct/temp/staged/marker.json",
            new_path="/local/acct/resources/scene/manifest.json",
            is_dir=False,
            is_temp=True,
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_temp_tree_copy_recursively_rejects_an_atomic_bundle_marker(ctx):
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
    ).encode()
    viking_fs.ls = AsyncMock(
        side_effect=[
            [{"name": "nested", "isDir": True}, {"name": "large.dwg", "isDir": False}],
            [{"name": "manifest.json", "isDir": False}],
        ]
    )
    viking_fs.read_file_bytes = AsyncMock(return_value=marker)

    with pytest.raises(FailedPreconditionError, match="add_resource"):
        await viking_fs._copy_for_mv(
            old_uri="viking://temp/staged/tree",
            new_uri="viking://resources/scene",
            old_path="/local/acct/temp/staged/tree",
            new_path="/local/acct/resources/scene",
            is_dir=True,
            is_temp=True,
            ctx=ctx,
        )

    assert viking_fs.ls.await_args_list == [
        call(
            "viking://temp/staged/tree",
            show_all_hidden=True,
            node_limit=LS_ALL_NODES,
            ctx=ctx,
        ),
        call(
            "viking://temp/staged/tree/nested",
            show_all_hidden=True,
            node_limit=LS_ALL_NODES,
            ctx=ctx,
        ),
    ]
    viking_fs.read_file_bytes.assert_awaited_once_with(
        "viking://temp/staged/tree/nested/manifest.json",
        ctx=ctx,
    )


@pytest.mark.asyncio
async def test_bundle_move_only_bypasses_marker_guard_for_its_root(monkeypatch, ctx):
    viking_fs = VikingFS(agfs=object())

    async def root_for_move(_viking_fs, uri, **_kwargs):
        return "viking://resources/scene" if uri == "viking://resources/scene" else None

    monkeypatch.setattr(
        "openviking.resource.atomic_bundle.find_resource_bundle_root",
        root_for_move,
    )

    assert await viking_fs._ensure_atomic_bundle_move_allowed(
        "viking://resources/scene",
        "viking://resources/moved-scene",
        ctx=ctx,
        source_is_directory=True,
    )
    assert not await viking_fs._ensure_atomic_bundle_move_allowed(
        "viking://temp/staged/marker.json",
        "viking://resources/scene/manifest.json",
        ctx=ctx,
        source_is_directory=False,
    )


@pytest.mark.asyncio
async def test_mv_passes_the_root_only_bundle_allowance_to_the_copy_path(ctx):
    viking_fs = VikingFS.__new__(VikingFS)
    viking_fs._async_agfs = AsyncMock()
    viking_fs._async_agfs.stat.return_value = {"isDir": True}
    lease = _lease("mv-bundle-lock")
    viking_fs._async_agfs.pathlock_acquire_batch.return_value = lease
    viking_fs._collect_uris = AsyncMock(return_value=[])
    viking_fs._copy_for_mv = AsyncMock()
    viking_fs._update_vector_store_uris = AsyncMock()
    viking_fs._ensure_atomic_bundle_move_allowed = AsyncMock(return_value=True)

    await viking_fs.mv(
        "viking://resources/scene",
        "viking://resources/moved-scene",
        ctx=ctx,
    )

    viking_fs._copy_for_mv.assert_awaited_once_with(
        old_uri="viking://resources/scene",
        new_uri="viking://resources/moved-scene",
        old_path="/local/acct/resources/scene",
        new_path="/local/acct/resources/moved-scene",
        is_dir=True,
        is_temp=False,
        ctx=ctx,
        lease_ref=lease,
        allow_atomic_bundle_write=True,
    )
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_temp_non_bundle_manifest_still_uses_raw_mv_copy(ctx):
    viking_fs = VikingFS.__new__(VikingFS)
    viking_fs._async_agfs = AsyncMock()
    viking_fs._async_agfs.stat.return_value = {"isDir": False}
    lease = _lease("mv-manifest-lock")
    viking_fs._async_agfs.pathlock_acquire_batch.return_value = lease
    viking_fs._collect_uris = AsyncMock(return_value=[])
    viking_fs._update_vector_store_uris = AsyncMock()
    viking_fs._ensure_atomic_bundle_move_allowed = AsyncMock(return_value=False)
    viking_fs.read_file_bytes = AsyncMock(return_value=b'{"not": "a bundle"}')
    temp_uri = viking_fs.create_temp_uri(ctx=ctx)
    source_uri = f"{temp_uri}/manifest.json"
    target_uri = "viking://resources/scene/manifest.json"
    source_path = viking_fs._uri_to_path(source_uri, ctx=ctx)
    target_path = viking_fs._uri_to_path(target_uri, ctx=ctx)

    await viking_fs.mv(source_uri, target_uri, ctx=ctx)

    viking_fs.read_file_bytes.assert_awaited_once_with(source_uri, ctx=ctx)
    viking_fs._async_agfs.cp.assert_awaited_once_with(
        source_path,
        target_path,
        recursive=False,
        fs_ctx={"account_id": ctx.account_id, "lease_ref": lease["lease_ref"]},
    )
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_temp_large_ordinary_file_still_uses_raw_mv_copy(ctx):
    viking_fs = VikingFS.__new__(VikingFS)
    viking_fs._async_agfs = AsyncMock()
    viking_fs._async_agfs.stat.return_value = {"isDir": False}
    lease = _lease("mv-large-file-lock")
    viking_fs._async_agfs.pathlock_acquire_batch.return_value = lease
    viking_fs._collect_uris = AsyncMock(return_value=[])
    viking_fs._update_vector_store_uris = AsyncMock()
    viking_fs._ensure_atomic_bundle_move_allowed = AsyncMock(return_value=False)
    viking_fs.read_file_bytes = AsyncMock()
    temp_uri = viking_fs.create_temp_uri(ctx=ctx)
    source_uri = f"{temp_uri}/large.dwg.json"
    target_uri = "viking://resources/scene/large.dwg.json"
    source_path = viking_fs._uri_to_path(source_uri, ctx=ctx)
    target_path = viking_fs._uri_to_path(target_uri, ctx=ctx)

    await viking_fs.mv(source_uri, target_uri, ctx=ctx)

    viking_fs.read_file_bytes.assert_not_awaited()
    viking_fs._async_agfs.cp.assert_awaited_once_with(
        source_path,
        target_path,
        recursive=False,
        fs_ctx={"account_id": ctx.account_id, "lease_ref": lease["lease_ref"]},
    )
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_persist_temp_tree_runs_preflight_then_uses_raw_copy(ctx):
    viking_fs = VikingFS.__new__(VikingFS)
    viking_fs._async_agfs = AsyncMock()
    temp_uri = viking_fs.create_temp_uri(ctx=ctx)
    viking_fs._ensure_atomic_bundle_write_allowed = AsyncMock()
    viking_fs._ensure_temp_copy_cannot_create_atomic_bundle = AsyncMock()
    viking_fs._ensure_parent_dirs = AsyncMock()
    target_uri = "viking://resources/scene"
    temp_path = viking_fs._uri_to_path(temp_uri, ctx=ctx)
    target_path = viking_fs._uri_to_path(target_uri, ctx=ctx)

    await viking_fs.persist_temp_tree(temp_uri, target_uri, ctx=ctx)

    viking_fs._ensure_temp_copy_cannot_create_atomic_bundle.assert_awaited_once_with(
        temp_uri,
        target_uri,
        is_dir=True,
        ctx=ctx,
    )
    viking_fs._async_agfs.cp.assert_awaited_once_with(
        temp_path,
        target_path,
        recursive=True,
        fs_ctx={"account_id": ctx.account_id},
    )


@pytest.mark.asyncio
async def test_persist_atomic_bundle_uses_native_move(ctx):
    viking_fs = VikingFS.__new__(VikingFS)
    viking_fs._async_agfs = AsyncMock()
    reservation_lease = _lease("bundle-reservation")
    publish_lease = _lease("bundle-publish")
    viking_fs._async_agfs.pathlock_acquire_batch.return_value = publish_lease
    staging_uri = viking_fs.create_temp_uri(ctx=ctx)
    temp_uri = f"{staging_uri}/bundle"
    viking_fs._ensure_atomic_bundle_write_allowed = AsyncMock()
    viking_fs._ensure_parent_dirs = AsyncMock()
    target_uri = "viking://resources/scene"
    temp_path = viking_fs._uri_to_path(temp_uri, ctx=ctx)
    staging_path = viking_fs._uri_to_path(staging_uri, ctx=ctx)
    target_path = viking_fs._uri_to_path(target_uri, ctx=ctx)

    await viking_fs.persist_temp_tree(
        temp_uri,
        target_uri,
        ctx=ctx,
        lease_ref=reservation_lease,
        _allow_atomic_bundle_write=True,
    )

    viking_fs._async_agfs.pathlock_acquire_batch.assert_awaited_once_with(
        [
            {"path": staging_path, "kind": "tree"},
            {"path": target_path, "kind": "exact"},
        ],
        owner_lease_ref=reservation_lease,
    )
    viking_fs._async_agfs.mv.assert_awaited_once_with(
        temp_path,
        target_path,
        fs_ctx={"account_id": ctx.account_id, "lease_ref": publish_lease["lease_ref"]},
    )
    viking_fs._async_agfs.pathlock_release.assert_awaited_once_with(publish_lease)
    viking_fs._async_agfs.cp.assert_not_awaited()


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
