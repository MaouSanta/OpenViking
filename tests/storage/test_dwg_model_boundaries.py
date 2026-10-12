# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""DWG JSON stays readable but never becomes model or embedding input."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openviking.core.context import ContextLevel
from openviking.resource.atomic_bundle import (
    DWG_PROCESSING_MARKER,
    RESOURCE_BUNDLE_SCHEMA_V3,
    build_dwg_sheet_manifest,
    build_manifest,
    unindexed_dwg_uris,
    write_dwg_processing_marker,
)
from openviking.server.identity import RequestContext, Role
from openviking.service.reindex_executor import ReindexExecutor, _ReindexCounters
from openviking.storage.queuefs.semantic_dag import SemanticDagExecutor
from openviking_cli.session.user_id import UserIdentifier

ROOT = "viking://resources/project"
DOC = f"{ROOT}/drawing"
SHEET = f"{DOC}/sheets/S01"
CONTEXT = f"{SHEET}/context.md"
ASSETS = f"{SHEET}/assets.md"
CHUNK = f"{SHEET}/chunks/0001.json"
CTX = RequestContext(user=UserIdentifier("test", "alice"), role=Role.USER)


class MemoryFS:
    def __init__(self, plain=False):
        self.files = {
            f"{DOC}/source.dwg": "DWG_SOURCE_SECRET",
            f"{DOC}/metadata_0001.json": "DWG_METADATA_SECRET",
            CONTEXT: "Concrete notes",
            ASSETS: "Beam detail - chunks/0001.json",
            CHUNK: "DWG_CHUNK_SECRET",
            f"{SHEET}/chunks/.abstract.md": "STALE_DWG_CHUNK_SECRET",
            f"{SHEET}/.abstract.md": "Sheet notes and beam detail",
            f"{SHEET}/.overview.md": "Sheet notes and beam detail",
            f"{ROOT}/ordinary.json": '{"ordinary":"keeps normal processing"}',
        }
        digest = hashlib.sha256(b"source").hexdigest()
        sheet = build_dwg_sheet_manifest(
            document_id="dwg-example",
            sheet_id="S01",
            title="Sheet 1",
            source_path="../../source.dwg",
            source_sha256=digest,
            artifacts={
                name: {
                    "path": path,
                    "schema": "text/markdown" if path.endswith(".md") else "dwg/json",
                    "sha256": digest,
                    "searchable": name in {"context", "assets"},
                }
                for name, path in {
                    "context": "context.md",
                    "assets": "assets.md",
                    "chunk": "chunks/0001.json",
                }.items()
            },
        )
        self.files[f"{SHEET}/manifest.json"] = json.dumps(sheet)
        self.files[f"{DOC}/manifest.json"] = json.dumps(
            build_manifest(
                kind="dwg",
                source_path="source.dwg",
                sha256=digest,
                ingested_at=datetime.now(timezone.utc),
                artifacts={
                    "source": {"path": "source.dwg", "schema": "dwg", "sha256": digest},
                    "sheet": {
                        "path": "sheets/S01/manifest.json",
                        "schema": RESOURCE_BUNDLE_SCHEMA_V3,
                        "sha256": digest,
                    },
                },
            )
        )
        if plain:
            del self.files[f"{DOC}/manifest.json"]
            del self.files[f"{SHEET}/manifest.json"]
            self.files[f"{DOC}/{DWG_PROCESSING_MARKER}"] = json.dumps(
                {
                    "schema": "openviking.dwg-processing/v1",
                    "document_id": f"dwg-{digest[:16]}",
                    "source": {"path": "source.dwg", "sha256": digest},
                }
            )
        self.dirs = set()
        for path in self.files:
            while path.startswith("viking://resources/"):
                path = path.rsplit("/", 1)[0]
                self.dirs.add(path)
        self._async_agfs = SimpleNamespace(
            pathlock_acquire_exact_batch=AsyncMock(return_value={"lease_ref": "test"}),
            pathlock_release=AsyncMock(),
        )
        self.listed_dirs = []

    async def exists(self, uri, **kwargs):
        return uri in self.files or uri in self.dirs

    async def stat(self, uri, **kwargs):
        return {"isDir": uri in self.dirs}

    async def read_file(self, uri, **kwargs):
        return self.files[uri]

    async def write_file(self, uri, content, **kwargs):
        self.files[uri] = content

    def _uri_to_path(self, uri, **kwargs):
        return "/test/" + uri.removeprefix("viking://")

    async def tree(self, uri, **kwargs):
        return [
            {"uri": path, "name": path.rsplit("/", 1)[-1], "isDir": path in self.dirs}
            for path in sorted(self.files.keys() | self.dirs)
            if path.startswith(uri + "/")
        ]

    async def ls(self, uri, **kwargs):
        self.listed_dirs.append(uri)
        return [
            entry
            for entry in await self.tree(uri)
            if "/" not in entry["uri"].removeprefix(uri + "/")
        ]


class CapturingProcessor:
    def __init__(self, fs):
        self.fs = fs
        self.model_inputs = []
        self.embeddings = []

    async def _generate_single_file_summary(self, file_path, **kwargs):
        content = await self.fs.read_file(file_path)
        self.model_inputs.append((file_path, content))
        return {"name": file_path.rsplit("/", 1)[-1], "summary": content}

    async def _generate_overview(self, uri, files, children):
        prompt = json.dumps([files, children])
        self.model_inputs.append((uri, prompt))
        return prompt

    def _normalize_overview_generation(self, overview):
        return overview, overview

    async def _vectorize_single_file(self, file_path, summary_dict, **kwargs):
        self.embeddings.append((file_path, summary_dict["summary"]))

    async def _vectorize_directory(self, uri, context_type, abstract, overview, **kwargs):
        self.embeddings.append((uri, overview))


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("target,recursive", [(ROOT, True), (SHEET, True), (SHEET, False)])
async def test_semantics_and_parent_refresh_only_receive_dwg_markdown(
    monkeypatch, target, recursive, plain
):
    fs = MemoryFS(plain=plain)
    processor = CapturingProcessor(fs)
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=2,
        ctx=CTX,
        recursive=recursive,
    )
    await executor.run(target)

    payloads = repr(processor.model_inputs + processor.embeddings)
    assert "DWG_CHUNK_SECRET" not in payloads
    assert "DWG_METADATA_SECRET" not in payloads
    assert "DWG_SOURCE_SECRET" not in payloads
    assert "Concrete notes" in payloads
    assert "Beam detail" in payloads
    assert {CONTEXT, ASSETS} <= {uri for uri, _ in processor.embeddings}
    assert all(
        not uri.startswith(DOC) or uri in {CONTEXT, ASSETS} for uri, _ in processor.embeddings
    )
    assert f"{SHEET}/chunks" not in fs.listed_dirs
    if target == ROOT:
        assert f"{ROOT}/ordinary.json" in {uri for uri, _ in processor.embeddings}


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("target", [CHUNK, f"{SHEET}/chunks", f"{DOC}/metadata_0001.json"])
async def test_direct_json_or_json_only_directory_has_no_model_calls(monkeypatch, target, plain):
    fs = MemoryFS(plain=plain)
    processor = CapturingProcessor(fs)
    monkeypatch.setattr("openviking.storage.queuefs.semantic_dag.get_viking_fs", lambda: fs)
    executor = SemanticDagExecutor(
        processor=processor,
        context_type="resource",
        max_concurrent_llm=1,
        ctx=CTX,
    )
    await executor.run(target)
    assert processor.model_inputs == []
    assert processor.embeddings == []


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [False, True])
async def test_reindex_keeps_assets_but_never_rebuilds_dwg_directory_or_json_vectors(
    monkeypatch, plain
):
    fs = MemoryFS(plain=plain)
    monkeypatch.setattr("openviking.service.reindex_executor.get_viking_fs", lambda: fs)
    executor = ReindexExecutor()
    executor._best_file_summary = AsyncMock(return_value="")
    executor._read_directory_abstract = AsyncMock(return_value="ordinary parent")
    executor._read_directory_overview = AsyncMock(return_value="ordinary parent")
    executor._best_resource_file_vector_text = AsyncMock(
        side_effect=lambda uri, summary, **kwargs: fs.files[uri]
    )
    executor._upsert_context = AsyncMock()
    entries = await fs.tree(ROOT)
    await executor._reindex_resource_vectors_from_entries(
        root_uri=ROOT,
        directories=[ROOT] + [e["uri"] for e in entries if e["isDir"]],
        files=[e["uri"] for e in entries if not e["isDir"]],
        counters=_ReindexCounters(),
        ctx=CTX,
    )
    calls = [call.kwargs for call in executor._upsert_context.await_args_list]
    assert {call["uri"] for call in calls} == {ROOT, CONTEXT, ASSETS, f"{ROOT}/ordinary.json"}
    assert "DWG_" not in repr(calls)
    executor._upsert_context.reset_mock()
    await executor._reindex_resource_vectors(uri=CHUNK, counters=_ReindexCounters(), ctx=CTX)
    executor._upsert_context.assert_not_awaited()
    executor.delete_uri_level = AsyncMock()
    await executor.reindex_directory_marker(dir_uri=SHEET, level=ContextLevel.ABSTRACT, ctx=CTX)
    executor.delete_uri_level.assert_awaited_once_with(
        uri=SHEET, level=ContextLevel.ABSTRACT, ctx=CTX
    )
    executor._upsert_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_manifest_cannot_opt_json_into_embedding(monkeypatch):
    fs = MemoryFS()
    manifest_uri = f"{SHEET}/manifest.json"
    manifest = json.loads(fs.files[manifest_uri])
    manifest["artifacts"]["chunk"]["searchable"] = True
    fs.files[manifest_uri] = json.dumps(manifest)
    assert CHUNK in await unindexed_dwg_uris(fs, CHUNK, [], ctx=CTX)
    assert ASSETS not in await unindexed_dwg_uris(fs, ASSETS, [], ctx=CTX)


@pytest.mark.asyncio
async def test_plain_marker_is_small_source_bound_and_does_not_make_atomic_bundle():
    from openviking.resource.atomic_bundle import find_resource_bundle_root

    fs = MemoryFS(plain=True)
    digest = hashlib.sha256(b"source").hexdigest()
    await write_dwg_processing_marker(
        fs,
        {
            "source_filename": "source.dwg",
            "source_sha256": digest,
            "document_id": f"dwg-{digest[:16]}",
            "temp_root_uri": DOC,
        },
        ctx=CTX,
    )
    assert len(fs.files[f"{DOC}/{DWG_PROCESSING_MARKER}"]) < 400
    assert await find_resource_bundle_root(fs, CHUNK, ctx=CTX) is None
    assert CHUNK in await unindexed_dwg_uris(fs, CHUNK, [], ctx=CTX)
    assert ASSETS not in await unindexed_dwg_uris(fs, ASSETS, [], ctx=CTX)


@pytest.mark.asyncio
async def test_long_source_filename_keeps_parser_marker_effective(tmp_path, monkeypatch):
    from openviking.parse.parsers import dwg
    from tests.parse.test_dwg_json import drawing_fixture, partition

    source = tmp_path / ("long-drawing-name-" * 4 + ".dwg")
    source.write_bytes(b"fixed DWG source")
    fs = MemoryFS()
    fs.files = {}
    fs.dirs = set()
    fs.create_temp_uri = lambda: "viking://temp/long-filename"

    async def mkdir(uri, **kwargs):
        fs.dirs.add(uri)

    async def write_bytes(uri, value, **kwargs):
        fs.files[uri] = value

    fs.mkdir = mkdir
    fs.write_file_bytes = write_bytes
    parser = dwg.DWGParser()
    monkeypatch.setattr(parser, "_get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        parser, "_export_dwg_json", AsyncMock(return_value=(drawing_fixture(), b"{}", []))
    )
    parsed = await parser.parse(
        source, region_classifier=partition, sheet_renderer=lambda _: b"image"
    )
    await write_dwg_processing_marker(fs, parsed.meta, ctx=CTX)

    assert len(parsed.meta["source_filename"]) <= 50
    assert parsed.meta["source_filename"].endswith(".dwg")
    root = parsed.meta["temp_root_uri"]
    assert fs.files[f"{root}/{parsed.meta['source_filename']}"] == source.read_bytes()
    chunk = next(path for path in fs.files if "/chunks/" in path and path.endswith(".json"))
    markdown = f"{root}/{parsed.meta['sheet_bundles'][0]['content_paths'][0]}"
    assert chunk in await unindexed_dwg_uris(fs, chunk, [], ctx=CTX)
    assert markdown not in await unindexed_dwg_uris(fs, markdown, [], ctx=CTX)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["no_marker", "wrong_schema", "missing_source", "wrong_owner"])
async def test_plain_json_named_chunks_is_not_suppressed_by_invalid_markers(invalid):
    fs = MemoryFS(plain=True)
    marker_uri = f"{DOC}/{DWG_PROCESSING_MARKER}"
    marker = json.loads(fs.files[marker_uri])
    if invalid == "no_marker":
        del fs.files[marker_uri]
    elif invalid == "wrong_schema":
        marker["schema"] = "example"
        fs.files[marker_uri] = json.dumps(marker)
    elif invalid == "missing_source":
        del fs.files[f"{DOC}/source.dwg"]
    else:
        marker["document_id"] = "dwg-unrelated"
        fs.files[marker_uri] = json.dumps(marker)
    assert await unindexed_dwg_uris(fs, CHUNK, [], ctx=CTX) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("plain", [False, True])
async def test_low_level_embedding_enqueue_uses_only_markdown_and_ordinary_json(monkeypatch, plain):
    from openviking.utils import embedding_utils

    fs = MemoryFS(plain=plain)
    queue = SimpleNamespace(enqueue=AsyncMock(return_value="queued"))
    monkeypatch.setattr(embedding_utils, "get_viking_fs", lambda: fs)
    monkeypatch.setattr(
        embedding_utils,
        "get_queue_manager",
        lambda: SimpleNamespace(
            EMBEDDING="embedding",
            get_queue=lambda _name: queue,
        ),
    )
    monkeypatch.setattr(
        embedding_utils,
        "get_openviking_config",
        lambda: SimpleNamespace(
            embedding=SimpleNamespace(text_source="content_only"),
        ),
    )
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        embedding_utils, "_resolve_context_timestamps", AsyncMock(return_value=(now, now))
    )
    for uri in [CONTEXT, ASSETS, CHUNK, f"{DOC}/metadata_0001.json", f"{ROOT}/ordinary.json"]:
        await embedding_utils.vectorize_file(
            file_path=uri,
            parent_uri=uri.rsplit("/", 1)[0],
            summary_dict={"name": uri.rsplit("/", 1)[-1]},
            context_type="resource",
            ctx=CTX,
        )
    await embedding_utils.vectorize_directory_meta(
        f"{SHEET}/chunks",
        "STALE_DWG_CHUNK_SECRET",
        "STALE_DWG_CHUNK_SECRET",
        ctx=CTX,
    )
    messages = [call.args[0] for call in queue.enqueue.await_args_list]
    assert {message.context_data["uri"] for message in messages} == {
        CONTEXT,
        ASSETS,
        f"{ROOT}/ordinary.json",
    }
    assert "DWG_" not in repr([message.message for message in messages])


@pytest.mark.asyncio
async def test_mixed_directory_merge_preserves_dwg_marker_and_processing_policy(
    tmp_path, monkeypatch
):
    from openviking.parse.parsers.directory import DirectoryParser

    fs = MemoryFS(plain=True)
    # Exercise the real recursive merge with a marked parser-produced temp tree.
    fs.files = {
        uri.replace(DOC, "viking://temp/job/drawing"): data
        for uri, data in fs.files.items()
        if uri.startswith(DOC + "/")
    }
    fs.dirs = {"viking://temp/job", "viking://temp/job/drawing"}
    for path in list(fs.files):
        while path.startswith("viking://temp/job/"):
            path = path.rsplit("/", 1)[0]
            fs.dirs.add(path)

    async def mkdir(uri, **kwargs):
        fs.dirs.add(uri)

    async def move_file(source, destination, **kwargs):
        fs.files[destination] = fs.files.pop(source)

    fs.mkdir = mkdir
    fs.move_file = move_file
    fs.delete_temp = AsyncMock()
    digest = hashlib.sha256(b"source").hexdigest()
    parser = SimpleNamespace(
        parse=AsyncMock(
            return_value=SimpleNamespace(
                source_format="dwg",
                temp_dir_path="viking://temp/job",
                meta={
                    "temp_root_uri": "viking://temp/job/drawing",
                    "source_filename": "source.dwg",
                    "source_sha256": digest,
                    "document_id": f"dwg-{digest[:16]}",
                },
            )
        )
    )
    warnings = []
    result = await DirectoryParser._process_single_file(
        SimpleNamespace(rel_path="source.dwg", path=tmp_path / "source.dwg"),
        parser,
        ROOT,
        fs,
        warnings,
    )
    assert result is True and warnings == []
    assert f"{DOC}/{DWG_PROCESSING_MARKER}" in fs.files
    assert CHUNK in await unindexed_dwg_uris(fs, CHUNK, [], ctx=CTX)
    assert ASSETS not in await unindexed_dwg_uris(fs, ASSETS, [], ctx=CTX)
    fs.files[f"{ROOT}/chunks/0001.json"] = '{"ordinary":"JSON beside the DWG"}'
    assert await unindexed_dwg_uris(fs, f"{ROOT}/chunks/0001.json", [], ctx=CTX) == set()


@pytest.mark.asyncio
async def test_processing_marker_obeys_json_budget_before_writing(monkeypatch):
    fs = MemoryFS(plain=True)
    fs.write_file = AsyncMock()
    monkeypatch.setattr(
        "openviking_cli.utils.config.get_openviking_config",
        lambda: SimpleNamespace(
            dwg=SimpleNamespace(json_max_tokens=2048, json_max_chars=100),
            embedding=SimpleNamespace(max_input_tokens=4096),
        ),
    )
    with pytest.raises(ValueError, match="budget cannot hold the processing marker"):
        await write_dwg_processing_marker(
            fs,
            {
                "source_filename": "source.dwg",
                "source_sha256": "a" * 64,
                "document_id": "dwg-" + "a" * 16,
                "temp_root_uri": DOC,
            },
            ctx=CTX,
        )
    fs.write_file.assert_not_awaited()
