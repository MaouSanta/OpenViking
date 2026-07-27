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
RESOURCE_BUNDLE_MANIFEST = "manifest.json"
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


def serialize_manifest(manifest: dict[str, Any]) -> str:
    """Render a stable, human-readable manifest without adding another sidecar."""
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


def is_resource_bundle_manifest(payload: Any) -> bool:
    """Return whether *payload* is a complete atomic-bundle marker.

    The schema string alone must not turn an arbitrary directory into an
    immutable bundle. V1 remains readable; new V2 manifests also require a
    complete artifact inventory.
    """
    if not isinstance(payload, dict) or payload.get("schema") not in {
        RESOURCE_BUNDLE_SCHEMA_V1,
        RESOURCE_BUNDLE_SCHEMA,
    }:
        return False
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
