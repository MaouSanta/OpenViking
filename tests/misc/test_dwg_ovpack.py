# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""DWG evidence survives packs without becoming searchable during migration."""

import hashlib
import json
import zipfile
from datetime import datetime, timezone

import pytest

from openviking.resource.atomic_bundle import build_dwg_sheet_manifest, build_manifest
from openviking.server.identity import RequestContext, Role
from openviking.storage.index_consistency import build_index_expectations, check_index_consistency
from openviking.storage.ovpack.operations import export_ovpack, import_ovpack
from openviking.storage.ovpack.validation import validate_manifest_content
from openviking_cli.session.user_id import UserIdentifier

ROOT = "viking://resources/drawings"
DOC = f"{ROOT}/example"
SHEET = f"{DOC}/sheets/one"
CONTENT = f"{SHEET}/context.md"
CTX = RequestContext(user=UserIdentifier("acct", "alice"), role=Role.USER)


class MemoryFS:
    def __init__(self, files=None):
        self.files = files or {}
        self.dirs = set()
        for uri in self.files:
            while uri.startswith("viking://resources/"):
                uri = uri.rsplit("/", 1)[0]
                self.dirs.add(uri)

    async def exists(self, uri, ctx=None):
        return uri in self.files or uri in self.dirs

    async def stat(self, uri, ctx=None):
        if not await self.exists(uri):
            raise FileNotFoundError(uri)
        return {"uri": uri, "isDir": uri in self.dirs}

    async def ls(self, uri, ctx=None):
        await self.stat(uri)
        return []

    async def read_file(self, uri, ctx=None):
        return self.files[uri].decode("utf-8")

    async def read_file_bytes(self, uri, ctx=None):
        return self.files[uri]

    async def mkdir(self, uri, exist_ok=False, ctx=None, **kwargs):
        self.dirs.add(uri)

    async def write_file_bytes(self, uri, content, ctx=None, **kwargs):
        self.files[uri] = content

    async def tree(self, uri, **kwargs):
        prefix = f"{uri}/"
        return [
            {
                "uri": path,
                "rel_path": path.removeprefix(prefix),
                "name": path.rsplit("/", 1)[-1],
                "isDir": path in self.dirs,
                "size": len(self.files.get(path, b"")),
            }
            for path in sorted(self.dirs | self.files.keys())
            if path.startswith(prefix)
        ]


class MarkdownVectors:
    def __init__(self):
        self.upserts = []

    async def filter(self, **kwargs):
        uri = kwargs["filter"].value
        if uri != CONTENT:
            return []
        return [
            {
                "uri": uri,
                "level": 2,
                "context_type": "resource",
                "vector": [0.1, 0.2],
                "search_tags": ["kind=dwg-sheet"],
            }
        ]

    async def upsert(self, data, ctx=None):
        self.upserts.append(data)


@pytest.fixture
def dwg_fs():
    files = {
        f"{DOC}/source.dwg": b"source snapshot",
        f"{DOC}/shared/chunk_0001.json": b'{"shared":true}',
        f"{DOC}/unassigned/chunk_0001.json": b'{"unassigned":true}',
        CONTENT: b"# Sheet\nConcrete notes",
        f"{SHEET}/views/view-1/chunk_0001.json": b'{"records":[1,2]}',
        f"{SHEET}/.abstract.md": b"Sheet abstract",
        f"{SHEET}/.overview.md": b"Sheet overview",
    }
    source_hash = hashlib.sha256(files[f"{DOC}/source.dwg"]).hexdigest()
    artifacts = {
        str(i): {
            "path": uri.removeprefix(f"{SHEET}/"),
            "schema": "text/markdown" if uri.endswith(".md") else "test/json",
            "sha256": hashlib.sha256(data).hexdigest(),
            "searchable": uri == CONTENT,
        }
        for i, (uri, data) in enumerate(files.items())
        if uri.startswith(f"{SHEET}/")
    }
    sheet = build_dwg_sheet_manifest(
        document_id="dwg-example",
        sheet_id="one",
        title="Example",
        source_path="../../source.dwg",
        source_sha256=source_hash,
        artifacts=artifacts,
    )
    files[f"{SHEET}/manifest.json"] = json.dumps(sheet).encode()
    document = build_manifest(
        kind="dwg",
        source_path="source.dwg",
        sha256=source_hash,
        ingested_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        artifacts={"source": {"path": "source.dwg", "schema": "dwg", "sha256": source_hash}},
    )
    files[f"{DOC}/manifest.json"] = json.dumps(document).encode()
    return MemoryFS(files)


@pytest.mark.asyncio
@pytest.mark.parametrize("root", [ROOT, DOC, SHEET, f"{SHEET}/views/view-1"])
async def test_dwg_consistency_requires_only_manifest_searchable_files(dwg_fs, root):
    report = await check_index_consistency(
        dwg_fs, MarkdownVectors(), root, await dwg_fs.tree(root), CTX
    )
    assert report.ok
    expected = [] if "/views/" in root else [(CONTENT, 2)]
    assert [(item.uri, item.level) for item in report.expected] == expected


@pytest.mark.asyncio
async def test_dwg_export_restores_only_searchable_vectors_and_preserves_evidence(
    dwg_fs, tmp_path, monkeypatch
):
    metadata = {"provider": "test", "model": "test", "dimensions": 2}
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.embedding_snapshot_metadata", lambda _: metadata
    )
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.current_embedding_metadata", lambda: metadata
    )
    pack = tmp_path / "drawings.ovpack"
    await export_ovpack(dwg_fs, ROOT, str(pack), CTX, MarkdownVectors(), include_vectors=True)
    with zipfile.ZipFile(pack) as archive:
        manifest = json.loads(archive.read("drawings/_ovpack/manifest.json"))
        records = validate_manifest_content(archive, manifest, archive.infolist(), "drawings")
        assert [(r["path"], r["level"]) for r in records] == [("example/sheets/one/context.md", 2)]
    restored = MemoryFS()
    vectors = MarkdownVectors()
    await import_ovpack(
        restored, str(pack), "viking://resources", CTX, vector_mode="require", vector_store=vectors
    )
    assert restored.files == dwg_fs.files
    assert [record["uri"] for record in vectors.upserts] == [CONTENT]
    assert vectors.upserts[0]["search_tags"] == ["kind=dwg-sheet"]
    assert vectors.upserts[0]["vector"] == pytest.approx([0.1, 0.2])


@pytest.mark.asyncio
@pytest.mark.parametrize("vector_mode", ["auto", "recompute"])
async def test_vectorless_dwg_import_does_not_index_json_or_directory_metadata(
    dwg_fs, tmp_path, monkeypatch, vector_mode
):
    pack = tmp_path / "drawings.ovpack"
    await export_ovpack(dwg_fs, ROOT, str(pack), CTX, MarkdownVectors())
    files, directories = [], []

    async def capture_file(**kwargs):
        files.append(kwargs["file_path"])

    async def capture_directory(uri, *args, **kwargs):
        directories.append(uri)

    monkeypatch.setattr("openviking.storage.ovpack.operations.vectorize_file", capture_file)
    monkeypatch.setattr(
        "openviking.storage.ovpack.operations.vectorize_directory_meta", capture_directory
    )
    restored = MemoryFS()
    await import_ovpack(restored, str(pack), "viking://resources", CTX, vector_mode=vector_mode)
    assert files == [CONTENT]
    assert directories == []
    assert restored.files == dwg_fs.files


@pytest.mark.asyncio
async def test_non_dwg_bundle_keeps_ordinary_index_expectations(dwg_fs):
    manifest = json.loads(dwg_fs.files[f"{DOC}/manifest.json"])
    manifest["kind"] = "step"
    dwg_fs.files[f"{DOC}/manifest.json"] = json.dumps(manifest).encode()
    dwg_fs.files.pop(f"{SHEET}/manifest.json")
    expectations = await build_index_expectations(dwg_fs, ROOT, await dwg_fs.tree(ROOT), CTX)
    assert (f"{DOC}/shared/chunk_0001.json", 2) in {(e.uri, e.level) for e in expectations}
    assert {(SHEET, 0), (SHEET, 1)} <= {(e.uri, e.level) for e in expectations}
