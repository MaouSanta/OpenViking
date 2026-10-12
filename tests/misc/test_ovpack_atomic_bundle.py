"""OVPack restoration exercises the actual VikingFS atomic write guards."""

import zipfile

import pytest

from openviking.storage.ovpack.operations import (
    backup_ovpack,
    export_ovpack,
    import_ovpack,
    restore_ovpack,
)
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import FailedPreconditionError, InvalidArgumentError
from tests.misc import test_dwg_ovpack
from tests.misc.test_dwg_ovpack import (
    CONTENT,
    CTX,
    DOC,
    ROOT,
    SHEET,
    MarkdownVectors,
)
from tests.utils.mock_agfs import MockLocalAGFS

dwg_fs = test_dwg_ovpack.dwg_fs


class LocalAGFS(MockLocalAGFS):
    """Only the backing I/O is fake; VikingFS policy and writes stay real."""

    def ls(self, path, ctx=None, **kwargs):
        if not self._resolve(path).exists():
            raise FileNotFoundError(path)
        return super().ls(path, ctx=ctx, **kwargs)

    def stat(self, path, ctx=None):
        result = super().stat(path, ctx=ctx)
        result["isDir"] = result["is_dir"]
        return result

    def ensure_parent_dirs(self, path, ctx=None):
        self._resolve(path).parent.mkdir(parents=True, exist_ok=True)

    def pathlock_acquire_tree(self, *args, **kwargs):
        return {"lease_ref": "test-tree-lease"}

    def pathlock_release(self, *args, **kwargs):
        return None


@pytest.fixture
def native_fs(tmp_path):
    return VikingFS(agfs=LocalAGFS(root_path=tmp_path / "store"))


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    # This regression restores the resources scope, not user/agent aliases.
    monkeypatch.setattr("openviking.storage.ovpack.operations.PUBLIC_SCOPES", ("resources",))
    metadata = {"provider": "test", "model": "test", "dimensions": 2}
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.embedding_snapshot_metadata", lambda _: metadata
    )
    monkeypatch.setattr(
        "openviking.storage.ovpack.vectors.current_embedding_metadata", lambda: metadata
    )

    async def unexpected(*args, **kwargs):
        pytest.fail("A require restore must not enqueue model work")

    monkeypatch.setattr(
        "openviking.storage.ovpack.operations._enqueue_direct_vectorization", unexpected
    )


async def make_pack(dwg_fs, tmp_path, *, backup=False, marker_first=True):
    pack = tmp_path / "source.ovpack"
    if backup:
        await backup_ovpack(dwg_fs, str(pack), CTX, MarkdownVectors(), include_vectors=True)
    else:
        await export_ovpack(dwg_fs, ROOT, str(pack), CTX, MarkdownVectors(), include_vectors=True)
    with zipfile.ZipFile(pack) as archive:
        members = [(info.filename, archive.read(info)) for info in archive.infolist()]
    # Exercise a marker preceding its child directory/files, and arriving last.
    members.sort(key=lambda item: item[0].endswith("/manifest.json") != marker_first)
    with zipfile.ZipFile(pack, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    return pack


@pytest.mark.asyncio
@pytest.mark.parametrize("backup", [False, True])
@pytest.mark.parametrize("marker_first", [False, True])
async def test_validated_bundle_members_restore_with_real_write_guard(
    dwg_fs, native_fs, tmp_path, backup, marker_first
):
    pack = await make_pack(dwg_fs, tmp_path, backup=backup, marker_first=marker_first)
    vectors = MarkdownVectors()
    if backup:
        await restore_ovpack(native_fs, str(pack), CTX, vector_mode="require", vector_store=vectors)
    else:
        await import_ovpack(
            native_fs,
            str(pack),
            "viking://resources",
            CTX,
            vector_mode="require",
            vector_store=vectors,
        )
    for uri, content in dwg_fs.files.items():
        assert await native_fs.read_file_bytes(uri, ctx=CTX) == content
    assert [record["uri"] for record in vectors.upserts] == [CONTENT]
    with pytest.raises(FailedPreconditionError, match="immutable"):
        await native_fs.write_file_bytes(CONTENT, b"mutation", ctx=CTX)
    with pytest.raises(FailedPreconditionError, match="generic file write"):
        await native_fs.write_file_bytes(
            "viking://resources/forged/manifest.json",
            dwg_fs.files[f"{DOC}/manifest.json"],
            ctx=CTX,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [DOC, SHEET, f"{SHEET}/new-directory"])
async def test_import_under_existing_bundle_is_rejected_before_internal_writes(
    dwg_fs, native_fs, tmp_path, parent
):
    pack = await make_pack(dwg_fs, tmp_path)
    await native_fs.write_file_bytes(
        f"{DOC}/manifest.json",
        dwg_fs.files[f"{DOC}/manifest.json"],
        ctx=CTX,
        _allow_atomic_bundle_write=True,
    )
    before = sorted(
        path.relative_to(native_fs.agfs.root) for path in native_fs.agfs.root.rglob("*")
    )
    with pytest.raises(FailedPreconditionError, match="Cannot import a package"):
        await import_ovpack(
            native_fs,
            str(pack),
            parent,
            CTX,
            vector_mode="require",
            vector_store=MarkdownVectors(),
        )
    assert (
        sorted(path.relative_to(native_fs.agfs.root) for path in native_fs.agfs.root.rglob("*"))
        == before
    )


@pytest.mark.asyncio
async def test_invalid_package_does_not_reach_internal_bundle_writes(dwg_fs, native_fs, tmp_path):
    pack = await make_pack(dwg_fs, tmp_path)
    with zipfile.ZipFile(pack) as archive:
        members = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(pack, "w") as archive:
        for name, data in members:
            if name.endswith("/context.md"):
                data = b"x" * len(data)
            archive.writestr(name, data)
    with pytest.raises(InvalidArgumentError, match="sha256 does not match manifest"):
        await import_ovpack(
            native_fs,
            str(pack),
            "viking://resources",
            CTX,
            vector_mode="require",
            vector_store=MarkdownVectors(),
        )
    assert not list(native_fs.agfs.root.rglob("*"))


@pytest.mark.asyncio
async def test_whole_bundle_overwrite_keeps_existing_delete_policy(dwg_fs, native_fs, tmp_path):
    pack = tmp_path / "document.ovpack"
    await export_ovpack(dwg_fs, DOC, str(pack), CTX, MarkdownVectors(), include_vectors=True)
    await native_fs.write_file_bytes(
        f"{DOC}/manifest.json",
        dwg_fs.files[f"{DOC}/manifest.json"],
        ctx=CTX,
        _allow_atomic_bundle_write=True,
    )
    await native_fs.write_file_bytes(
        f"{DOC}/old.json",
        b"old",
        ctx=CTX,
        _allow_atomic_bundle_write=True,
    )
    await import_ovpack(
        native_fs,
        str(pack),
        ROOT,
        CTX,
        on_conflict="overwrite",
        vector_mode="require",
        vector_store=MarkdownVectors(),
    )
    assert not await native_fs.exists(f"{DOC}/old.json", ctx=CTX)
    for uri, content in dwg_fs.files.items():
        assert await native_fs.read_file_bytes(uri, ctx=CTX) == content
