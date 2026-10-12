# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Preserve LibreDWG records in bounded, spatially grouped JSON files."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

from openviking.utils.embedding_input import estimate_embedding_input_tokens

CHUNK_SCHEMA = "openviking.dwg-json-chunk/v1"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def bounded_json(
    records: Iterable[dict[str, Any]], *, max_tokens: int = 2048, max_chars: int = 6000
) -> list[str]:
    """Pack whole records; split an oversized record's JSON text reversibly.

    Every file, including fragment wrappers and escape characters, satisfies both
    limits. This is the same token *estimate* used by the embedding input guard.
    """

    def encode(items: list[dict[str, Any]]) -> str:
        return _dump({"schema": CHUNK_SCHEMA, "records": items}) + "\n"

    def fits(text: str) -> bool:
        return len(text) <= max_chars and estimate_embedding_input_tokens(text) <= max_tokens

    probe = {"fragment": "0" * 64, "part": 999999999, "total": 999999999, "text": '\\"中'}
    if not fits(encode([probe])):
        raise ValueError("DWG JSON budget cannot hold a fragment envelope")

    files: list[str] = []
    current: list[dict[str, Any]] = []
    for record in records:
        if fits(encode([*current, record])):
            current.append(record)
            continue
        if current:
            files.append(encode(current))
            current = []
        if fits(encode([record])):
            current = [record]
            continue
        serialized = _dump(record)
        identity = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        pieces: list[str] = []
        offset = 0
        while offset < len(serialized):
            low, high = 0, min(max_chars, len(serialized) - offset)
            while low < high:
                mid = (low + high + 1) // 2
                fragment = {
                    "fragment": identity,
                    "part": 999999999,
                    "total": 999999999,
                    "text": serialized[offset : offset + mid],
                }
                if fits(encode([fragment])):
                    low = mid
                else:
                    high = mid - 1
            if not low:
                raise ValueError("DWG JSON budget cannot hold the next escaped character")
            pieces.append(serialized[offset : offset + low])
            offset += low
        for index, piece in enumerate(pieces):
            files.append(
                encode(
                    [
                        {
                            "fragment": identity,
                            "part": index,
                            "total": len(pieces),
                            "text": piece,
                        }
                    ]
                )
            )
    if current:
        files.append(encode(current))
    return files


def read_json_records(contents: Iterable[str]) -> list[dict[str, Any]]:
    """Read chunks and verify/reassemble oversized records, independent of file order."""
    records = []
    fragments: dict[str, dict[int, str]] = defaultdict(dict)
    totals: dict[str, int] = {}
    for content in contents:
        payload = json.loads(content)
        if payload.get("schema") != CHUNK_SCHEMA or not isinstance(payload.get("records"), list):
            raise ValueError("Invalid DWG JSON chunk")
        for record in payload["records"]:
            if not isinstance(record, dict):
                raise ValueError("Invalid DWG JSON record")
            if "fragment" not in record or "kind" in record:
                records.append(record)
                continue
            identity, part, total = record["fragment"], record["part"], record["total"]
            if (
                not isinstance(identity, str)
                or not isinstance(part, int)
                or not isinstance(total, int)
                or not 0 <= part < total
                or not isinstance(record.get("text"), str)
                or identity in totals
                and totals[identity] != total
                or part in fragments[identity]
            ):
                raise ValueError("Invalid or duplicate DWG JSON fragment")
            totals[identity] = total
            fragments[identity][part] = record["text"]
    for identity, pieces in fragments.items():
        if len(pieces) != totals[identity]:
            raise ValueError("Missing DWG JSON fragment")
        serialized = "".join(pieces[index] for index in range(totals[identity]))
        if hashlib.sha256(serialized.encode("utf-8")).hexdigest() != identity:
            raise ValueError("DWG JSON fragment checksum mismatch")
        record = json.loads(serialized)
        if not isinstance(record, dict):
            raise ValueError("Invalid reassembled DWG JSON record")
        records.append(record)
    return records


def _intersects(box: list[float], bounds: list[float]) -> bool:
    return (
        box[0] <= bounds[2] and bounds[0] <= box[2] and box[1] <= bounds[3] and bounds[1] <= box[3]
    )


def _spatial_order(items: list[tuple[tuple[float, float], dict]]) -> list[dict]:
    """Bisect the longest spatial axis; stable ties retain source/instance order."""
    if len(items) <= 1:
        return [record for _, record in items]
    spans = [
        max(point[d] for point, _ in items) - min(point[d] for point, _ in items) for d in (0, 1)
    ]
    if max(spans) == 0:
        return [record for _, record in items]
    axis = 0 if spans[0] >= spans[1] else 1
    items = sorted(items, key=lambda item: (item[0][axis], item[0][1 - axis]))
    middle = len(items) // 2
    return _spatial_order(items[:middle]) + _spatial_order(items[middle:])


def build_json_artifacts(
    drawing: dict[str, Any],
    sheets: list[dict[str, Any]],
    *,
    max_tokens: int = 2048,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """Preserve records once, then map optional assets to the actual chunk files.

    Asset boundaries never affect JSON ownership, order or content. The returned
    ``asset_chunks`` is an in-memory input to Markdown navigation, not another
    persisted catalog. Native objects retain their original OBJECTS ordinals.
    """
    from openviking.parse.parsers.dwg_json_geometry import iter_instances
    from openviking.parse.parsers.dwg_sheet import _ref

    objects = drawing.get("OBJECTS", [])
    if not isinstance(objects, list):
        raise ValueError("LibreDWG OBJECTS must be an array")
    ordinals = {id(record): index for index, record in enumerate(objects)}
    groups: dict[str, list[tuple[tuple[float, float], dict]]] = defaultdict(list)
    placements: dict[int, list[dict]] = defaultdict(list)
    markdown: dict[tuple[int, tuple], list[dict]] = defaultdict(list)
    instances: dict[tuple[int, tuple], dict] = {}
    instance_bounds: dict[tuple[int, tuple], list | None] = {}
    instance_groups: dict[tuple[int, tuple], str] = {}
    centers: dict[tuple[int, tuple], tuple] = {}
    unsupported = False
    sheet_by_directory = {sheet["directory"]: sheet for sheet in sheets}

    for sheet in sheets:
        sheet["json_artifacts"] = []
        sheet.setdefault("status", "complete")
        converted = set(sheet.get("markdown_uids", []))
        for text in sheet.get("_texts", []):
            if text["uid"] in converted and id(text["record"]) in ordinals:
                index = ordinals[id(text["record"])]
                key = (index, tuple(text.get("instance_path", [])))
                markdown[key].append(
                    {"sheet_id": sheet["sheet_id"], "paths": sheet["content_paths"]}
                )

    def add(directory: str, record: dict, point: tuple = (0.0, 0.0)) -> None:
        groups[directory].append((point, record))

    frame_instances = {
        (reference["handle"], tuple(reference.get("instance_path", [])))
        for sheet in sheets
        if (reference := sheet.get("source_locator", {}).get("source_ref", {})).get("handle")
        is not None
    }
    for instance in iter_instances(drawing):
        index = instance["object_index"]
        path = tuple(instance.get("instance_path", []))
        key = (index, path)
        if key in instances:
            continue
        instances[key] = instance
        placements[index].append(instance)
        box = instance.get("bbox")
        point = instance.get("point")
        if box is None and point is not None:
            box = [point[0], point[1], point[0], point[1]]
        instance_bounds[key] = box
        centers[key] = (
            tuple(point or ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)) if box else (0.0, 0.0)
        )
        located = instance.get("geometry_status") in {
            "exact",
            "conservative",
            "anchor_only",
            "container",
        }
        candidates = [
            sheet
            for sheet in sheets
            if instance.get("space_kind") == "model"
            and sheet.get("kind") == "model_frame"
            and sheet.get("bbox")
            and box
            and _intersects(box, sheet["bbox"])
        ]
        target = (
            f"{candidates[0]['directory']}/chunks" if located and len(candidates) == 1 else "chunks"
        )
        if not located or instance.get("space_kind") != "model":
            unsupported = True
            for sheet in candidates:
                sheet["status"] = "partial"
        instance_groups[key] = target
        entry = {"kind": "instance", **instance}
        if key in markdown:
            entry["markdown"] = markdown[key]
        add(target, entry, centers[key])

    # Header/classes, dormant block definitions, unknown objects and non-dict
    # OBJECTS entries are data too. No type allowlist may discard them.
    unresolved_insert = any(
        instance["entity_type"] in {"INSERT", "MINSERT"} and not instance["is_container"]
        for instance in instances.values()
    )
    add(
        "metadata",
        {
            "kind": "header",
            "value": {key: value for key, value in drawing.items() if key != "OBJECTS"},
        },
    )
    for index, original in enumerate(objects):
        uses = placements[index]
        key = (index, tuple(uses[0]["instance_path"])) if len(uses) == 1 else None
        target = instance_groups[key] if key is not None else "metadata"
        value = original
        entry: dict[str, Any] = {"kind": "object", "object_index": index, "value": value}
        # A reused block text is moved only if *every* occurrence is represented
        # in Markdown. Drawing or unassigned instances still need the native text.
        paths = {tuple(instance["instance_path"]) for instance in uses}
        if (
            isinstance(original, dict)
            and paths
            and not unresolved_insert
            and all((index, path) in markdown for path in paths)
        ):
            value = dict(original)
            primary = "text" if str(original.get("entity", "")).upper() == "MTEXT" else "text_value"
            secondary = "text_value" if primary == "text" else "text"
            consumed = primary if original.get(primary) is not None else secondary
            moved = [
                key
                for key in ("text", "text_value")
                if isinstance(value.get(key), str) and value[key] == original.get(consumed)
            ]
            for field in moved:
                del value[field]
            if moved:
                entry["value"] = value
                entry["markdown_fields"] = moved
                entry["markdown"] = [
                    {"instance_path": list(path), **reference}
                    for path in sorted(paths, key=str)
                    for reference in markdown[(index, path)]
                ]
        # Place private objects beside their instances in the spatial order.
        add(target, entry, centers[key] if key is not None else (0.0, 0.0))

    status = (
        "partial"
        if unsupported or any(sheet["status"] != "complete" for sheet in sheets)
        else "complete"
    )
    files: dict[str, str] = {}
    object_files: dict[int, set[str]] = defaultdict(set)
    instance_files: dict[tuple[int, tuple], set[str]] = defaultdict(set)
    for directory, items in sorted(groups.items()):
        fragment_records = {
            hashlib.sha256(_dump(record).encode("utf-8")).hexdigest(): record for _, record in items
        }
        for number, content in enumerate(
            bounded_json(_spatial_order(items), max_tokens=max_tokens, max_chars=max_chars), 1
        ):
            path = (
                f"metadata_{number:05d}.json"
                if directory == "metadata"
                else f"{directory}/chunk_{number:05d}.json"
            )
            files[path] = content
            for record in json.loads(content)["records"]:
                if "fragment" in record:
                    record = fragment_records[record["fragment"]]
                if record.get("kind") == "object":
                    object_files[record["object_index"]].add(path)
                elif record.get("kind") == "instance":
                    key = (record["object_index"], tuple(record["instance_path"]))
                    instance_files[key].add(path)

    # Resolve block ownership and ancestor placements only after storage is fixed.
    # A frame or INSERT anchor is not evidence that an asset contains that whole
    # container; selected child occurrences supply the necessary dependencies.
    handles = {
        _ref(record.get("handle")): index
        for index, record in enumerate(objects)
        if isinstance(record, dict) and _ref(record.get("handle")) is not None
    }

    def dependencies(index: int) -> set[int]:
        result: set[int] = set()
        pending = [index]
        while pending:
            current = pending.pop()
            if current in result:
                continue
            result.add(current)
            record = objects[current]
            if isinstance(record, dict):
                for field in ("ownerhandle", "block_header"):
                    if (dependency := handles.get(_ref(record.get(field)))) is not None:
                        pending.append(dependency)
        return result

    asset_chunks: dict[str, dict[str, dict]] = {}
    for sheet in sheets:
        assets = {}
        regions = [region for region in sheet.get("regions", []) if region["type"] == "view"]
        candidates = []
        all_located = True
        for key, instance in instances.items() if regions else []:
            raw = objects[key[0]]
            if instance["is_container"] or (_ref(raw.get("handle")), key[1]) in frame_instances:
                continue
            if instance.get("space_kind") == "paper":
                continue  # Paper-space objects cannot belong to a Model Space frame.
            box = instance_bounds[key]
            if instance.get("space_kind") != "model" or box is None:
                all_located = False
            elif sheet.get("bbox") and _intersects(box, sheet["bbox"]):
                candidates.append((key, instance, box))
        for region in regions:
            paths: set[str] = set()
            selected = 0
            complete = bool(region.get("bbox")) and all_located
            for key, instance, box in candidates:
                index, instance_path = key
                if not region.get("bbox") or not _intersects(box, region["bbox"]):
                    continue
                selected += 1
                complete &= instance.get("geometry_status") in {
                    "exact",
                    "conservative",
                    "anchor_only",
                }
                paths.update(instance_files[key])
                needed = dependencies(index)
                for depth, handle in enumerate(instance_path):
                    ancestor = handles.get(handle)
                    if ancestor is None and str(handle).startswith("object:"):
                        ancestor = int(str(handle).removeprefix("object:"))
                    if ancestor is not None:
                        needed.update(dependencies(ancestor))
                        ancestor_key = (ancestor, instance_path[:depth])
                        paths.update(instance_files[ancestor_key])
                        complete &= bool(instance_files[ancestor_key])
                    else:
                        complete = False
                for dependency in needed:
                    paths.update(object_files[dependency])
                    complete &= bool(object_files[dependency])
            assets[region["id"]] = {"paths": sorted(paths), "complete": bool(complete and selected)}
        asset_chunks[sheet["sheet_id"]] = assets

    root_artifacts = []
    for path in sorted(files):
        artifact = {"path": path, "schema": CHUNK_SCHEMA}
        sheet = next(
            (
                sheet
                for directory, sheet in sheet_by_directory.items()
                if path.startswith(directory + "/")
            ),
            None,
        )
        (sheet["json_artifacts"] if sheet else root_artifacts).append(artifact)
    return {
        "files": files,
        "json_artifacts": root_artifacts,
        "parse_status": status,
        "asset_chunks": asset_chunks,
    }
