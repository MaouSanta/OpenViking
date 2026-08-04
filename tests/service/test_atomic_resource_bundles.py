# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Focused coverage for atomic resource bundles."""

import hashlib
import io
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from PIL import Image

from openviking.parse.vlm import TechnicalDrawingRecognitionError, TechnicalDrawingRegion
from openviking.resource.atomic_bundle import is_resource_bundle_manifest
from openviking.server.identity import RequestContext, Role
from openviking.service import resource_service as resource_service_module
from openviking.service import search_service as search_service_module
from openviking.service.fs_service import FSService
from openviking.service.resource_service import ResourceService
from openviking.service.search_service import SearchService
from openviking.storage.viking_fs import LS_ALL_NODES, VikingFS
from openviking.utils.embedding_utils import _apply_scalar_overrides
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError, InvalidURIError
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


def _write_test_drawing(path, size=(200, 100)):
    Image.new("RGB", size, "white").save(path, format="PNG")


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
            "artifacts": {"source": {"path": "model.step", "schema": "step", "sha256": "a" * 64}},
        }
    )


def test_bundle_kind_tag_is_applied_to_each_embedding_message():
    message = SimpleNamespace(context_data={})

    _apply_scalar_overrides(message, {"search_tags": ["kind=dwg"]})

    assert message.context_data["search_tags"] == ["kind=dwg"]


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
    assert result["kind"] == "step"
    assert result["semantic_status"] == "skipped"
    assert result["vector_status"] == "skipped"
    assert set(viking_fs.files) == {
        "viking://resources/gear/gear.stp",
        "viking://resources/gear/manifest.json",
    }
    assert viking_fs.files["viking://resources/gear/gear.stp"] == b"solid gear"
    manifest = json.loads(viking_fs.files["viking://resources/gear/manifest.json"])
    assert manifest["schema"] == "openviking.resource-bundle/v2"
    assert manifest["kind"] == "step"
    assert manifest["source"]["path"] == "gear.stp"
    assert manifest["source"]["sha256"] == hashlib.sha256(b"solid gear").hexdigest()
    assert manifest["artifacts"]["source"] == {
        "path": "gear.stp",
        "schema": "step",
        "sha256": hashlib.sha256(b"solid gear").hexdigest(),
    }
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_plain_image_bundle_remains_source_only(tmp_path, monkeypatch, ctx):
    source = tmp_path / "photo.png"
    _write_test_drawing(source)
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    root = "viking://resources/photo"
    service._plan_resource_target = AsyncMock(return_value=(root, lock))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    recognize = AsyncMock()
    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(
        "openviking.parse.vlm.VLMProcessor.recognize_technical_drawing_regions",
        recognize,
    )

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to=root,
        bundle=True,
        build_index=False,
    )

    assert result["semantic_status"] == "skipped"
    assert "recognition_status" not in result
    assert {path.removeprefix(f"{root}/") for path in viking_fs.files} == {
        "photo.png",
        "manifest.json",
    }
    manifest = json.loads(viking_fs.files[f"{root}/manifest.json"])
    assert "recognition" not in manifest
    assert set(manifest["artifacts"]) == {"source"}
    recognize.assert_not_awaited()
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_technical_drawing_bundle_publishes_flat_deterministic_regions(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "drawing.png"
    _write_test_drawing(source)
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    root = "viking://resources/drawing"
    service._plan_resource_target = AsyncMock(return_value=(root, lock))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    # Return bottom region first to prove filenames are assigned in reading order.
    recognize = AsyncMock(
        return_value=[
            TechnicalDrawingRegion("detail", "DETAIL A", (50, 600, 400, 900)),
            TechnicalDrawingRegion("section", "SECTION B-B", (500, 100, 900, 500)),
        ]
    )

    async def summarize_staging(staging_uri, **_kwargs):
        viking_fs.files[f"{staging_uri}/.abstract.md"] = b"drawing abstract"
        viking_fs.files[f"{staging_uri}/.overview.md"] = b"drawing overview"
        return {}

    service._summarize_bundle_staging = AsyncMock(side_effect=summarize_staging)
    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(
        "openviking.parse.vlm.VLMProcessor.recognize_technical_drawing_regions",
        recognize,
    )

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to=root,
        bundle=True,
        args={"image_mode": "technical_drawing"},
        build_index=False,
    )

    assert result["recognition_status"] == "complete"
    assert result["semantic_status"] == "complete"
    assert {path.removeprefix(f"{root}/") for path in viking_fs.files} == {
        "drawing.png",
        "region_001.png",
        "region_002.png",
        ".abstract.md",
        ".overview.md",
        "manifest.json",
    }
    manifest = json.loads(viking_fs.files[f"{root}/manifest.json"])
    assert is_resource_bundle_manifest(manifest)
    assert manifest["recognition"] == {
        "schema": "openviking.technical-drawing-regions/v1",
        "status": "complete",
        "source_width": 200,
        "source_height": 100,
    }
    assert manifest["artifacts"]["region_001"]["metadata"] == {
        "type": "section",
        "label": "SECTION B-B",
        "bbox": [98, 8, 182, 52],
    }
    assert manifest["artifacts"]["region_002"]["metadata"] == {
        "type": "detail",
        "label": "DETAIL A",
        "bbox": [8, 58, 82, 92],
    }
    assert set(manifest["artifacts"]) == {
        "source",
        "region_001",
        "region_002",
        "abstract",
        "overview",
    }
    assert (
        manifest["artifacts"]["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    )
    assert (
        manifest["artifacts"]["region_001"]["sha256"]
        == hashlib.sha256(viking_fs.files[f"{root}/region_001.png"]).hexdigest()
    )
    with Image.open(io.BytesIO(viking_fs.files[f"{root}/region_001.png"])) as crop:
        assert crop.format == "PNG"
        assert crop.size == (84, 44)
    recognize.assert_awaited_once_with(source)
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_technical_drawing_recognition_failure_publishes_source_only(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "drawing.png"
    _write_test_drawing(source)
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    root = "viking://resources/drawing"
    service._plan_resource_target = AsyncMock(return_value=(root, lock))
    service._summarize_bundle_staging = AsyncMock()

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(
        "openviking.parse.vlm.VLMProcessor.recognize_technical_drawing_regions",
        AsyncMock(side_effect=TechnicalDrawingRecognitionError("invalid_response")),
    )

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        to=root,
        bundle=True,
        args={"image_mode": "technical_drawing"},
        build_index=False,
    )

    assert result["recognition_status"] == "failed"
    assert result["semantic_status"] == "skipped"
    assert {path.removeprefix(f"{root}/") for path in viking_fs.files} == {
        "drawing.png",
        "manifest.json",
    }
    manifest = json.loads(viking_fs.files[f"{root}/manifest.json"])
    assert manifest["recognition"] == {
        "schema": "openviking.technical-drawing-regions/v1",
        "status": "failed",
        "reason": "invalid_response",
    }
    assert set(manifest["artifacts"]) == {"source"}
    service._summarize_bundle_staging.assert_not_awaited()
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_technical_drawing_crop_write_failure_leaves_no_bundle(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "drawing.png"
    _write_test_drawing(source)
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    root = "viking://resources/drawing"
    service._plan_resource_target = AsyncMock(return_value=(root, lock))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    original_write = viking_fs.write_file_bytes

    async def fail_region_write(uri, content, **kwargs):
        if uri.endswith("/region_001.png"):
            raise RuntimeError("crop write failed")
        await original_write(uri, content, **kwargs)

    viking_fs.write_file_bytes = fail_region_write
    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(
        "openviking.parse.vlm.VLMProcessor.recognize_technical_drawing_regions",
        AsyncMock(return_value=[TechnicalDrawingRegion("view", None, (100, 100, 900, 900))]),
    )

    with pytest.raises(RuntimeError, match="crop write failed"):
        await service.add_resource(
            path=str(source),
            ctx=ctx,
            to=root,
            bundle=True,
            args={"image_mode": "technical_drawing"},
            build_index=False,
        )

    assert not any(path.startswith(root) for path in viking_fs.files)
    assert not any(path.startswith("viking://temp/staging") for path in viking_fs.files)
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_technical_drawing_semantic_failure_leaves_no_bundle(
    tmp_path,
    monkeypatch,
    ctx,
):
    source = tmp_path / "drawing.png"
    _write_test_drawing(source)
    viking_fs = _BundleFS()
    service = _bundle_service(viking_fs)
    lock = _lease()
    root = "viking://resources/drawing"
    service._plan_resource_target = AsyncMock(return_value=(root, lock))
    service._summarize_bundle_staging = AsyncMock(side_effect=RuntimeError("semantic failed"))

    async def no_parent_bundle(*_args, **_kwargs):
        return None

    monkeypatch.setattr(resource_service_module, "find_resource_bundle_root", no_parent_bundle)
    monkeypatch.setattr(
        "openviking.parse.vlm.VLMProcessor.recognize_technical_drawing_regions",
        AsyncMock(return_value=[TechnicalDrawingRegion("view", None, (100, 100, 900, 900))]),
    )

    with pytest.raises(RuntimeError, match="semantic failed"):
        await service.add_resource(
            path=str(source),
            ctx=ctx,
            to=root,
            bundle=True,
            args={"image_mode": "technical_drawing"},
            build_index=False,
        )

    assert not any(path.startswith(root) for path in viking_fs.files)
    assert not any(path.startswith("viking://temp/staging") for path in viking_fs.files)
    service._release_lock_ref.assert_awaited_once_with(lock)


@pytest.mark.asyncio
async def test_technical_drawing_mode_requires_bundle_and_raster_source(tmp_path, ctx):
    source = tmp_path / "drawing.png"
    _write_test_drawing(source)
    service = _bundle_service(_BundleFS())

    with pytest.raises(
        InvalidArgumentError,
        match="requires bundle=true",
    ):
        await service.add_resource(
            path=str(source),
            ctx=ctx,
            to="not-a-viking-uri",
            args={"image_mode": "technical_drawing"},
        )

    non_raster = tmp_path / "drawing.glb"
    non_raster.write_bytes(b"glTF")
    service._plan_resource_target = AsyncMock()
    with pytest.raises(InvalidArgumentError, match="requires a raster source"):
        await service.add_resource(
            path=str(non_raster),
            ctx=ctx,
            to="viking://resources/drawing",
            bundle=True,
            args={"image_mode": "technical_drawing"},
            build_index=False,
        )
    service._plan_resource_target.assert_not_awaited()


@pytest.mark.asyncio
async def test_technical_drawing_rejects_multiframe_raster(tmp_path):
    source = tmp_path / "drawing.tiff"
    first = Image.new("RGB", (20, 20), "white")
    second = Image.new("RGB", (20, 20), "black")
    first.save(source, format="TIFF", save_all=True, append_images=[second])

    with pytest.raises(InvalidArgumentError, match="not a valid raster image"):
        await ResourceService._validate_technical_drawing_source(source)


@pytest.mark.asyncio
async def test_technical_drawing_bundle_indexes_kind_and_image_mode(monkeypatch, ctx):
    service = _bundle_service(_BundleFS())
    index = AsyncMock()
    monkeypatch.setattr(resource_service_module, "index_resource", index)

    queue_status, vector_status = await service._index_atomic_resource_bundle(
        root_uri="viking://resources/drawing",
        kind="jpg",
        image_mode="technical_drawing",
        ctx=ctx,
        wait=False,
        timeout=None,
    )

    assert queue_status is None
    assert vector_status == "queued"
    index.assert_awaited_once_with(
        "viking://resources/drawing",
        ctx,
        scalar_override={"search_tags": ["kind=jpg", "image_mode=technical_drawing"]},
    )


@pytest.mark.asyncio
async def test_dwg_bundle_publishes_complete_artifact_inventory(tmp_path, monkeypatch, ctx):
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
            "beam_topology.json": b'{"schema":"deterministic-dwg-topology-v2"}',
            "drawing_text.json": b'{"schema":"deterministic-dwg-text-v1"}',
            ".abstract.md": b"plan abstract",
            ".overview.md": b"plan overview",
            "preview.png": b"png",
        }
        for name, content in files.items():
            viking_fs.files[f"{staging}/{name}"] = content
        return SimpleNamespace(
            temp_dir_path="viking://temp/job",
            meta={"temp_root_uri": staging},
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
    assert set(viking_fs.files) == {
        f"{root}/plan.dwg",
        f"{root}/beam_topology.json",
        f"{root}/drawing_text.json",
        f"{root}/.abstract.md",
        f"{root}/.overview.md",
        f"{root}/preview.png",
        f"{root}/manifest.json",
    }
    manifest = json.loads(viking_fs.files[f"{root}/manifest.json"])
    assert set(manifest["artifacts"]) == {
        "source",
        "beam_topology",
        "drawing_text",
        "abstract",
        "overview",
        "preview",
    }
    assert all(
        {"path", "schema", "sha256"} <= artifact.keys()
        for artifact in manifest["artifacts"].values()
    )
    assert not any(path.endswith("libredwg.json") for path in viking_fs.files)
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
        viking_fs.files[f"{staging}/plan.dwg"] = b"dwg snapshot"
        return SimpleNamespace(
            temp_dir_path="viking://temp/job",
            meta={"temp_root_uri": staging},
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
async def test_bundle_keeps_its_snapshot_if_indexing_fails(tmp_path, monkeypatch, ctx):
    source = tmp_path / "gear.stp"
    source.write_bytes(b"solid gear")
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
        "viking://resources/gear/gear.stp",
        "viking://resources/gear/manifest.json",
    }
    assert viking_fs.removed == []
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
    temp_uri = viking_fs.create_temp_uri(ctx=ctx)
    viking_fs._ensure_atomic_bundle_write_allowed = AsyncMock()
    viking_fs._ensure_parent_dirs = AsyncMock()
    target_uri = "viking://resources/scene"

    await viking_fs.persist_temp_tree(
        temp_uri,
        target_uri,
        ctx=ctx,
        _allow_atomic_bundle_write=True,
    )

    viking_fs._async_agfs.mv.assert_awaited_once_with(
        viking_fs._uri_to_path(temp_uri, ctx=ctx),
        viking_fs._uri_to_path(target_uri, ctx=ctx),
        fs_ctx={"account_id": ctx.account_id},
    )
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
