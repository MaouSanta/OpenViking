# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Preserve native records in one readable JSON file per drawing scope."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any, Iterable

DATA_SCHEMA = "openviking.dwg-json/v1"
CHUNK_SCHEMA = "openviking.dwg-json-chunk/v1"


def read_json_records(contents: Iterable[str]) -> list[dict[str, Any]]:
    """Read complete scope files or reassemble legacy v8 chunks without losing records."""
    records = []
    fragments: dict[str, dict[int, str]] = defaultdict(dict)
    totals: dict[str, int] = {}
    for content in contents:
        payload = json.loads(content)
        if payload.get("schema") not in {DATA_SCHEMA, CHUNK_SCHEMA} or not isinstance(
            payload.get("records"), list
        ):
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
) -> dict[str, Any]:
    """Keep native objects once: definitions in metadata, placed records by drawing scope.

    Instance traversal is temporary. Persist native INSERT references and only the
    instance paths needed to trace Markdown; readers recover source order by ordinal.
    """
    from openviking.parse.parsers.dwg_json_geometry import iter_instances
    from openviking.parse.parsers.dwg_sheet import _contains, _drawing_graph, _kind, _ref

    objects = drawing.get("OBJECTS", [])
    if not isinstance(objects, list):
        raise ValueError("LibreDWG OBJECTS must be an array")
    ordinals = {id(record): index for index, record in enumerate(objects)}
    graph = _drawing_graph(drawing)
    space_blocks = {
        handle
        for handle, block in graph["blocks"].items()
        if str(block.get("name", "")).casefold() == "*model_space"
        or str(block.get("name", "")).casefold().startswith("*paper_space")
    }
    space_blocks.update(
        _ref(record.get("block_header")) for record in graph["objects"] if _kind(record) == "LAYOUT"
    )
    definition_blocks = set(graph["blocks"]) - space_blocks

    def definitions_for(block_handles: Iterable[Any]) -> set[int]:
        # Ownership includes attached attributes and old entities lists. Following
        # native INSERT references also covers nested/unused definitions and cycles.
        pending = [
            graph["blocks"][handle] for handle in block_handles if handle in definition_blocks
        ]
        found = set()
        while pending:
            record = pending.pop()
            index = ordinals[id(record)]
            if index in found:
                continue
            found.add(index)
            handle = _ref(record.get("handle"))
            pending.extend(graph["children"].get(handle, []))
            if _kind(record) == "BLOCK_HEADER":
                pending.extend(
                    graph["by_handle"][member] for member in graph["members"].get(handle, [])
                )
            elif _kind(record) in {"INSERT", "MINSERT"}:
                nested = _ref(record.get("block_header"))
                if nested in definition_blocks:
                    pending.append(graph["blocks"][nested])
        return found

    definitions = definitions_for(definition_blocks)
    metadata = {
        index
        for index, record in enumerate(objects)
        if index in definitions
        or not isinstance(record, dict)
        or not record.get("entity")
        or _kind(record) in {"BLOCK", "ENDBLK", "SEQEND"}
    }
    sheet_by_directory = {sheet["directory"]: sheet for sheet in sheets}
    markdown: dict[tuple[int, tuple], list[dict]] = defaultdict(list)
    for sheet in sheets:
        sheet["json_artifacts"] = []
        sheet.setdefault("status", "partial")
        converted = set(sheet.get("markdown_uids", []))
        for text in sheet.get("_texts", []):
            if text["uid"] in converted and id(text["record"]) in ordinals:
                markdown[
                    (ordinals[id(text["record"])], tuple(text.get("instance_path", [])))
                ].append({"sheet_id": sheet["sheet_id"], "paths": sheet["content_paths"]})

    markdown_by_object: dict[int, list[dict]] = defaultdict(list)
    for (index, path), references in markdown.items():
        markdown_by_object[index].extend(
            {"instance_path": list(path), **reference} for reference in references
        )

    owners: dict[int, set[str | None]] = defaultdict(set)
    uses: dict[int, int] = defaultdict(int)
    covered: dict[int, bool] = {}
    bounds: dict[int, list[float]] = {}
    anchors: dict[int, tuple[float, float]] = {}
    unknown: set[int] = set()
    unsafe_blocks = set()
    insert_ordinals = {
        handle if (handle := _ref(record.get("handle"))) is not None else f"object:{index}": index
        for index, record in enumerate(objects)
        if isinstance(record, dict) and _kind(record) in {"INSERT", "MINSERT"}
    }
    for instance in iter_instances(drawing):
        index = instance["object_index"]
        path = tuple(instance["instance_path"])
        if index in definitions and not path and instance.get("space_handle") is None:
            continue  # Unused entities-list members can lack an ownerhandle.
        uses[index] += 1
        point, box = instance.get("point"), instance.get("bbox")
        location = box or ([*point, *point] if point is not None else None)
        reliable = (
            instance.get("geometry_status") in {"exact", "conservative", "anchor_only", "container"}
            and location is not None
            and instance.get("space_kind") == "model"
        )
        matches = [
            sheet
            for sheet in sheets
            if sheet.get("kind") == "model_frame"
            and sheet.get("bbox")
            and location is not None
            and _intersects(location, sheet["bbox"])
        ]
        target = (
            matches[0]["directory"]
            if reliable and len(matches) == 1 and _contains(matches[0]["bbox"], location)
            else None
        )
        owners[index].add(target)
        if not reliable:
            unknown.add(index)
            for sheet in matches:
                sheet["status"] = "partial"
        if box is not None and reliable:
            bounds[index] = box
        if point is not None or box is not None:
            anchors[index] = tuple(point or ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
        refs = markdown.get((index, path), [])
        covered[index] = (
            covered.get(index, True)
            and target is not None
            and any(ref["sheet_id"] == sheet_by_directory[target]["sheet_id"] for ref in refs)
        )
        # A container whose contents cross a Sheet or have unknown placement must
        # not be assigned using its insertion anchor alone.
        for ancestor in path:
            if ancestor in insert_ordinals:
                parent = insert_ordinals[ancestor]
                owners[parent].add(target)
                if not reliable:
                    unknown.add(parent)
        if instance["entity_type"] in {"INSERT", "MINSERT"} and not instance.get("is_container"):
            unsafe_blocks.add(_ref(objects[index].get("block_header")))

    unsafe_definitions = definitions_for(unsafe_blocks)
    unknown.update(
        index for index in range(len(objects)) if index not in metadata and not uses[index]
    )
    for index in unknown:
        for owner in owners[index]:
            if owner is not None:
                sheet_by_directory[owner]["status"] = "partial"
    status = (
        "partial"
        if unknown or any(sheet["status"] != "complete" for sheet in sheets)
        else "complete"
    )
    groups: dict[str, list[tuple[tuple[float, float], dict]]] = defaultdict(list)
    groups[""] = []
    for directory in sheet_by_directory:
        groups[directory] = []

    def add(scope: str, record: dict, point: tuple = (0.0, 0.0)) -> None:
        groups[scope].append((point, record))

    add(
        "metadata",
        {
            "kind": "document",
            "status": status,
            "object_count": len(objects),
        },
    )
    add(
        "metadata",
        {
            "kind": "header",
            "value": {key: value for key, value in drawing.items() if key != "OBJECTS"},
        },
    )
    for sheet in sheets:
        add(
            "metadata",
            {
                "kind": "sheet",
                "sheet_id": sheet["sheet_id"],
                "status": sheet["status"],
                "bbox": sheet.get("bbox"),
                "source_locator": sheet.get("source_locator"),
            },
        )
    for index, original in enumerate(objects):
        targets = owners[index]
        destination = next(iter(targets)) if len(targets) == 1 else None
        scope = (
            "metadata"
            if index in metadata
            else destination
            if destination is not None and uses[index] == 1 and index not in unknown
            else ""
        )
        entry: dict[str, Any] = {"kind": "object", "object_index": index, "value": original}
        refs = markdown_by_object.get(index, [])
        if refs:
            entry["markdown"] = refs
        if (
            isinstance(original, dict)
            and _kind(original) in {"TEXT", "MTEXT", "ATTRIB"}
            and covered.get(index)
            and index not in unsafe_definitions
        ):
            primary = "text" if _kind(original) == "MTEXT" else "text_value"
            secondary = "text_value" if primary == "text" else "text"
            consumed = primary if original.get(primary) is not None else secondary
            moved = [
                key
                for key in ("text", "text_value")
                if isinstance(original.get(key), str) and original[key] == original.get(consumed)
            ]
            if moved:
                entry["value"] = {key: value for key, value in original.items() if key not in moved}
                entry["markdown_fields"] = moved
        if index not in metadata:
            if index in unknown:
                entry["status"] = "partial"
            elif uses[index] == 1 and index in bounds:
                entry["bbox"] = bounds[index]
        add(
            scope,
            entry,
            anchors.get(index, (0.0, 0.0))
            if scope != "metadata" and uses[index] == 1
            else (0.0, 0.0),
        )

    files = {}
    root_artifacts = []
    for scope, items in sorted(groups.items()):
        path = (
            "metadata.json"
            if scope == "metadata"
            else f"{scope}/drawing.json"
            if scope
            else "drawing.json"
        )
        files[path] = (
            json.dumps(
                {"schema": DATA_SCHEMA, "records": _spatial_order(items)},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        artifact = {"path": path, "schema": DATA_SCHEMA}
        owner = sheet_by_directory.get(scope)
        (owner["json_artifacts"] if owner is not None else root_artifacts).append(artifact)
    return {
        "files": files,
        "json_artifacts": root_artifacts,
        "parse_status": status,
    }
