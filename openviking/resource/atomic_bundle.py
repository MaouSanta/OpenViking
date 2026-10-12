# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Primitives shared by atomic resource-bundle ingestion and retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from openviking.utils.time_utils import format_iso8601
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.utils import VikingURI

if TYPE_CHECKING:
    from openviking.server.identity import RequestContext
    from openviking.storage.viking_fs import VikingFS


RESOURCE_BUNDLE_SCHEMA_V1 = "openviking.resource-bundle/v1"
RESOURCE_BUNDLE_SCHEMA = "openviking.resource-bundle/v2"
RESOURCE_BUNDLE_SCHEMA_V3 = "openviking.resource-bundle/v3"
RESOURCE_BUNDLE_MANIFEST = "manifest.json"
DWG_PROCESSING_MARKER = ".dwg-processing.json"
_DWG_PROCESSING_SCHEMA = "openviking.dwg-processing/v1"
_DWG_MARKDOWN_PATH = re.compile(r"^sheets/[^/]+/(?:context|assets)(?:_[0-9]+)?\.md$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# A kind is the normalized source format, not a broad 3D category.
_KIND_ALIASES = {
    "stp": "step",
    "step": "step",
    "igs": "iges",
    "iges": "iges",
    "jpeg": "jpg",
    "tiff": "tif",
}


def source_basename(path: str, source_name: Optional[str] = None) -> str:
    """Return the URI-safe filename stored as the bundle's immutable source."""
    candidate = source_name or Path(path).name
    # Upload metadata can originate on a different host OS, so normalize both
    # separator styles before choosing the basename.
    candidate = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    if not candidate or candidate in {".", ".."}:
        raise InvalidArgumentError("bundle source must have a file name")
    suffix = Path(candidate).suffix
    if not suffix:
        return VikingURI.sanitize_segment(candidate)

    safe_suffix = "." + VikingURI.sanitize_segment(suffix[1:])
    base = candidate[: -len(suffix)]
    safe_base = VikingURI.sanitize_segment(base)
    max_base_length = 50 - len(safe_suffix)
    if max_base_length < 1:
        return VikingURI.sanitize_segment(candidate)
    safe_base = safe_base[:max_base_length].rstrip("_.") or "source"
    return f"{safe_base}{safe_suffix}"


def kind_from_source_name(source_name: str) -> str:
    """Return a stable source-format kind, rejecting extensionless files."""
    suffix = Path(source_name).suffix.lower().lstrip(".")
    if not suffix:
        raise InvalidArgumentError(
            "bundle source type cannot be determined: the source file needs an extension"
        )
    return _KIND_ALIASES.get(suffix, suffix)


async def read_source_and_sha256(path: Path) -> tuple[bytes, str]:
    """Read one local source snapshot and calculate its content hash off the event loop."""

    def _read() -> tuple[bytes, str]:
        payload = path.read_bytes()
        return payload, hashlib.sha256(payload).hexdigest()

    return await asyncio.to_thread(_read)


def build_manifest(
    *,
    kind: str,
    source_path: str,
    sha256: str,
    ingested_at: Any,
    artifacts: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Build the V2 bundle identity and artifact inventory."""
    return {
        "schema": RESOURCE_BUNDLE_SCHEMA,
        "kind": kind,
        "source": {
            "path": source_path,
            "sha256": sha256,
            "ingested_at": format_iso8601(ingested_at),
        },
        "artifacts": artifacts,
    }


def build_dwg_sheet_manifest(
    *,
    document_id: str,
    sheet_id: str,
    title: str,
    source_path: str,
    source_sha256: str,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build a V3 sheet bundle that references its document-owned DWG."""
    return {
        "schema": RESOURCE_BUNDLE_SCHEMA_V3,
        "kind": "dwg-sheet",
        "owner": {"document_id": document_id},
        "sheet": {"id": sheet_id, "title": title},
        "source_ref": {"path": source_path, "sha256": source_sha256},
        "artifacts": artifacts,
    }


def serialize_manifest(manifest: dict[str, Any]) -> str:
    """Render a stable, human-readable manifest without adding another sidecar."""
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def is_resource_bundle_manifest(payload: Any) -> bool:
    """Return whether *payload* is a complete atomic-bundle marker.

    The schema string alone must not turn an arbitrary directory into an
    immutable bundle. V1 remains readable; V2 owns a source snapshot, while a
    nested V3 DWG sheet references the source owned by its document bundle.
    """
    if not isinstance(payload, dict) or payload.get("schema") not in {
        RESOURCE_BUNDLE_SCHEMA_V1,
        RESOURCE_BUNDLE_SCHEMA,
        RESOURCE_BUNDLE_SCHEMA_V3,
    }:
        return False
    if payload.get("schema") == RESOURCE_BUNDLE_SCHEMA_V3:
        owner = payload.get("owner")
        source_ref = payload.get("source_ref")
        sheet = payload.get("sheet")
        artifacts = payload.get("artifacts")
        if (
            payload.get("kind") != "dwg-sheet"
            or not isinstance(owner, dict)
            or not isinstance(owner.get("document_id"), str)
            or not owner["document_id"]
            or not isinstance(sheet, dict)
            or not isinstance(sheet.get("id"), str)
            or not sheet["id"]
            or not isinstance(source_ref, dict)
            or not isinstance(source_ref.get("path"), str)
            or not source_ref["path"]
            or not isinstance(source_ref.get("sha256"), str)
            or not _SHA256_HEX_RE.fullmatch(source_ref["sha256"])
            or not isinstance(artifacts, dict)
            or not artifacts
        ):
            return False
        for role, artifact in artifacts.items():
            if (
                not isinstance(role, str)
                or not role
                or not isinstance(artifact, dict)
                or not isinstance(artifact.get("path"), str)
                or not artifact["path"]
                or not isinstance(artifact.get("schema"), str)
                or not artifact["schema"]
                or not isinstance(artifact.get("sha256"), str)
                or not _SHA256_HEX_RE.fullmatch(artifact["sha256"])
                or not isinstance(artifact.get("searchable"), bool)
            ):
                return False
        return any(artifact["searchable"] for artifact in artifacts.values())

    source = payload.get("source")
    valid_source = (
        isinstance(payload.get("kind"), str)
        and bool(payload["kind"])
        and isinstance(source, dict)
        and isinstance(source.get("path"), str)
        and bool(source["path"])
        and isinstance(source.get("sha256"), str)
        and bool(_SHA256_HEX_RE.fullmatch(source["sha256"]))
        and isinstance(source.get("ingested_at"), str)
        and bool(source["ingested_at"])
    )
    if not valid_source or payload.get("schema") == RESOURCE_BUNDLE_SCHEMA_V1:
        return valid_source

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        return False
    for role, artifact in artifacts.items():
        if (
            not isinstance(role, str)
            or not role
            or not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or not artifact["path"]
            or not isinstance(artifact.get("schema"), str)
            or not artifact["schema"]
            or not isinstance(artifact.get("sha256"), str)
            or not _SHA256_HEX_RE.fullmatch(artifact["sha256"])
        ):
            return False
    source_artifact = artifacts.get("source")
    return bool(
        source_artifact
        and source_artifact["path"] == source["path"]
        and source_artifact["sha256"] == source["sha256"]
    )


async def read_resource_bundle_manifest(
    viking_fs: "VikingFS",
    root_uri: str,
    *,
    ctx: "RequestContext",
) -> Optional[dict[str, Any]]:
    """Read a valid bundle manifest, or return ``None`` for an ordinary directory."""
    manifest_uri = VikingURI(root_uri).join(RESOURCE_BUNDLE_MANIFEST).uri
    try:
        if not await viking_fs.exists(manifest_uri, ctx=ctx):
            return None
        raw = await viking_fs.read_file(manifest_uri, ctx=ctx)
        payload = json.loads(raw)
    except Exception:
        return None
    return payload if is_resource_bundle_manifest(payload) else None


async def write_dwg_processing_marker(
    viking_fs: "VikingFS", meta: dict[str, Any], *, ctx: "RequestContext" = None
) -> None:
    """Persist the plain DWG parser's processing boundary without making a bundle.

    The fixed-size marker owns no artifact inventory or mutation policy. Parser
    filenames under sheets/ are stable, so it need not repeat every chunk path.
    """
    source_name, sha256 = meta["source_filename"], meta["source_sha256"]
    marker = {
        "schema": _DWG_PROCESSING_SCHEMA,
        "document_id": meta["document_id"],
        "source": {"path": source_name, "sha256": sha256},
    }
    from openviking.utils.embedding_input import estimate_embedding_input_tokens
    from openviking_cli.utils.config import get_openviking_config

    config = get_openviking_config()
    dwg = getattr(config, "dwg", None)
    max_tokens = min(
        getattr(dwg, "json_max_tokens", 2048),
        int(0.8 * getattr(config.embedding, "max_input_tokens", 4096)),
    )
    text = json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
    if (
        len(text) > getattr(dwg, "json_max_chars", 6000)
        or estimate_embedding_input_tokens(text) > max_tokens
    ):
        raise ValueError("DWG JSON budget cannot hold the processing marker")
    await viking_fs.write_file(f"{meta['temp_root_uri']}/{DWG_PROCESSING_MARKER}", text, ctx=ctx)


async def _has_dwg_processing_marker(
    viking_fs: "VikingFS", directory: str, *, ctx: "RequestContext"
) -> bool:
    try:
        marker_uri = f"{directory}/{DWG_PROCESSING_MARKER}"
        if not await viking_fs.exists(marker_uri, ctx=ctx):
            return False
        marker = json.loads(await viking_fs.read_file(marker_uri, ctx=ctx))
        source = marker.get("source", {})
        path, digest = source.get("path"), source.get("sha256")
        return bool(
            marker.get("schema") == _DWG_PROCESSING_SCHEMA
            and isinstance(path, str)
            and "/" not in path
            and "\\" not in path
            and path.lower().endswith(".dwg")
            and isinstance(digest, str)
            and _SHA256_HEX_RE.fullmatch(digest)
            and marker.get("document_id") == f"dwg-{digest[:16]}"
            and await viking_fs.exists(f"{directory}/{path}", ctx=ctx)
        )
    except Exception:
        return False


async def unindexed_dwg_uris(
    viking_fs: "VikingFS",
    root_uri: str,
    entries: list[dict[str, Any]],
    *,
    ctx: "RequestContext",
    include_semantic_directories: bool = False,
) -> set[str]:
    """Exclude DWG evidence and directory metadata using the existing manifests.

    Ordinary resources retain their normal indexing behavior. Ancestor manifests
    also cover single files and nested JSON directories. Semantic traversal may
    retain ancestors of searchable Markdown, while directory vectors stay excluded.
    """
    entry_uris = {
        entry.get("uri") or VikingURI(root_uri).join(str(entry.get("rel_path") or "")).uri
        for entry in entries
    }
    candidates = {
        uri.rsplit("/", 1)[0]
        for uri in entry_uris
        if uri.endswith((f"/{RESOURCE_BUNDLE_MANIFEST}", f"/{DWG_PROCESSING_MARKER}"))
    }
    candidates.update(
        entry.get("uri") or VikingURI(root_uri).join(str(entry.get("rel_path") or "")).uri
        for entry in entries
        if entry.get("isDir")
    )
    current = VikingURI(root_uri)
    while current is not None and current.scope in {"resources", "temp"}:
        candidates.add(current.uri)
        current = current.parent

    searchable: dict[str, set[str]] = {}
    plain_dwg_roots = set()
    visited = set()
    while candidates:
        directory = candidates.pop()
        if directory in visited:
            continue
        visited.add(directory)
        manifest = await read_resource_bundle_manifest(viking_fs, directory, ctx=ctx)
        if not manifest:
            if await _has_dwg_processing_marker(viking_fs, directory, ctx=ctx):
                plain_dwg_roots.add(directory)
            continue
        if manifest["schema"] == RESOURCE_BUNDLE_SCHEMA_V3:
            searchable[directory] = {
                VikingURI(directory).join(artifact["path"]).uri
                for artifact in manifest["artifacts"].values()
                if artifact["searchable"]
                and artifact["schema"] == "text/markdown"
                and artifact["path"].endswith(".md")
                and ".." not in artifact["path"].split("/")
            }
        elif manifest["schema"] == RESOURCE_BUNDLE_SCHEMA and manifest["kind"] == "dwg":
            searchable[directory] = set()
            # Root manifests register their Sheet manifests. Reading these lets
            # a parent refresh distinguish Sheet ancestors from JSON-only trees.
            candidates.update(
                VikingURI(directory).join(artifact["path"]).uri.rsplit("/", 1)[0]
                for artifact in manifest["artifacts"].values()
                if include_semantic_directories
                and artifact["schema"] == RESOURCE_BUNDLE_SCHEMA_V3
                and ".." not in artifact["path"].split("/")
            )

    semantic_files = set().union(*searchable.values()) if searchable else set()
    excluded = set()
    for uri in entry_uris | {root_uri}:
        ancestor = uri
        while ancestor.startswith("viking://"):
            if ancestor in plain_dwg_roots:
                relative = uri.removeprefix(ancestor).lstrip("/")
                allowed = bool(_DWG_MARKDOWN_PATH.fullmatch(relative))
                if include_semantic_directories:
                    allowed = (
                        allowed
                        or relative in {"", "sheets"}
                        or bool(re.fullmatch(r"sheets/[^/]+", relative))
                    )
                if not allowed:
                    excluded.add(uri)
                break
            if ancestor in searchable:
                allowed = uri in searchable[ancestor]
                if include_semantic_directories:
                    allowed = allowed or any(
                        path.startswith(uri.rstrip("/") + "/") for path in semantic_files
                    )
                if not allowed:
                    excluded.add(uri)
                break
            ancestor = ancestor.rsplit("/", 1)[0]
    return excluded


async def find_resource_bundle_root(
    viking_fs: "VikingFS",
    uri: str,
    *,
    ctx: "RequestContext",
    uri_is_directory: bool = False,
) -> Optional[str]:
    """Return the nearest atomic-bundle root containing ``uri``.

    A root itself is included only when ``uri_is_directory`` is true. This lets
    mutation guards distinguish an allowed whole-bundle operation from an
    attempted mutation of a member file.
    """
    current = VikingURI(uri)
    if current.scope != "resources":
        return None
    if not uri_is_directory:
        current = current.parent

    while current is not None and current.scope == "resources":
        if current.uri.rstrip("/") != "viking://resources":
            if await read_resource_bundle_manifest(viking_fs, current.uri, ctx=ctx):
                return current.uri.rstrip("/")
        current = current.parent
    return None


async def find_resource_bundle_owner_root(
    viking_fs: "VikingFS",
    uri: str,
    *,
    ctx: "RequestContext",
    uri_is_directory: bool = False,
) -> Optional[str]:
    """Return the outer bundle that owns mutations for a nested bundle tree."""
    nearest = await find_resource_bundle_root(
        viking_fs,
        uri,
        ctx=ctx,
        uri_is_directory=uri_is_directory,
    )
    if not nearest:
        return None

    outermost = nearest
    current = VikingURI(nearest).parent
    while current is not None and current.scope == "resources":
        if current.uri.rstrip("/") != "viking://resources":
            if await read_resource_bundle_manifest(viking_fs, current.uri, ctx=ctx):
                outermost = current.uri.rstrip("/")
        current = current.parent
    return outermost
