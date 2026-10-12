# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Locate native LibreDWG object occurrences without rewriting their fields.

``object_index`` always means the ordinal in the original ``OBJECTS`` array,
not LibreDWG's optional ``index`` field. Bounds cover only supported 2D
geometry; unknown bounds remain explicit. Raw records are owned by the caller.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterator

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_SUPPORT_ENTITIES = {"BLOCK", "ENDBLK", "SEQEND"}
_TEXT_ENTITIES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _is_xy(record: dict[str, Any]) -> bool:
    normal = record.get("extrusion")
    if normal is None:
        return True
    if not isinstance(normal, list) or len(normal) < 3:
        return False
    values = [_number(value) for value in normal[:3]]
    return values[0] == 0.0 and values[1] == 0.0 and values[2] == 1.0


def _location(record: dict[str, Any], matrix: tuple[float, ...]) -> dict[str, Any]:
    from openviking.parse.parsers.dwg_sheet import _bbox, _kind, _matrix_apply, _point

    kind = _kind(record)
    result: dict[str, Any] = {"bbox": None, "point": None, "geometry_status": "unsupported"}
    if not _is_xy(record):
        result["geometry_status"] = "unsupported_ocs"
        return result
    anchor = next(
        (
            point
            for key in ("ins_pt", "alignment_pt", "center", "text_midpt", "def_pt", "pt0")
            if (point := _point(record.get(key))) is not None
        ),
        None,
    )
    if anchor is not None:
        point = _matrix_apply(matrix, anchor)
        if all(math.isfinite(value) for value in point):
            result["point"] = list(point)
    points: list[tuple[float, float]] = []
    if kind == "LINE":
        points = [_point(record.get(key)) for key in ("start", "end")]
    elif kind == "LWPOLYLINE":
        raw = record.get("points")
        if not isinstance(raw, list) or not raw:
            return result
        # Bulged/variable-width polylines need their own curve/width evaluation.
        # A vertex-only bound would silently understate their visible geometry.
        if any(_number(value) != 0.0 for value in record.get("bulges") or []):
            return result
        if record.get("widths"):
            return result
        points = [_point(point) for point in raw]
        width = _number(record.get("const_width", 0.0))
        if width is None or width < 0:
            return result
        if all(point is not None for point in points) and width:
            box = _bbox(points)
            half = width / 2
            points = [
                (box[0] - half, box[1] - half),
                (box[0] - half, box[3] + half),
                (box[2] + half, box[1] - half),
                (box[2] + half, box[3] + half),
            ]
    elif kind in {"CIRCLE", "ARC", "ELLIPSE"}:
        center = _point(record.get("center"))
        radius = _number(record.get("radius"))
        if kind == "ELLIPSE":
            axis = _point(record.get("sm_axis"))
            ratio = _number(record.get("axis_ratio"))
            radius = math.hypot(*axis) * max(1.0, abs(ratio)) if axis and ratio else None
        if center is None or radius is None or radius < 0:
            return result
        x, y = _matrix_apply(matrix, center)
        a, b, c, d, _, _ = matrix
        rx, ry = radius * math.hypot(a, c), radius * math.hypot(b, d)
        box = [x - rx, y - ry, x + rx, y + ry]
        if not all(math.isfinite(value) for value in box):
            result["geometry_status"] = "invalid_coordinates"
            return result
        result.update(
            bbox=box,
            geometry_status="exact" if kind == "CIRCLE" else "conservative",
        )
        return result
    elif kind in {"SOLID", "TRACE", "3DFACE"}:
        points = [_point(record.get(f"corner{index}")) for index in range(1, 5)]
    elif kind == "POINT":
        point = _point(record.get("point"))
        if point is None:
            x, y = _number(record.get("x")), _number(record.get("y"))
            point = (x, y) if x is not None and y is not None else None
        points = [point]
    elif kind in _TEXT_ENTITIES or kind.startswith("DIMENSION"):
        result["geometry_status"] = (
            "anchor_only" if result["point"] is not None else "missing_coordinates"
        )
        return result
    if not points or any(point is None for point in points):
        return result
    box = _bbox(_matrix_apply(matrix, point) for point in points)
    if box and all(math.isfinite(value) for value in box):
        result.update(bbox=list(box), geometry_status="conservative")
    return result


def iter_instances(drawing: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield placed entities, INSERT containers, and otherwise unlocated orphans.

    ``transform`` maps the entity's native coordinates to its containing space.
    ``instance_path`` contains ancestor INSERT handles (an ordinal fallback when
    absent). INSERT itself is a container, not a missing visible primitive; its
    children carry the composed transform. Paper-space occurrences are retained
    but no viewport projection is attempted. Unused block definitions are omitted.
    """
    from openviking.parse.parsers.dwg_sheet import (
        _drawing_graph,
        _insert_matrix,
        _kind,
        _matrix_multiply,
        _point,
        _ref,
    )

    raw = drawing.get("OBJECTS", [])
    if not isinstance(raw, list):
        return
    records = {index: record for index, record in enumerate(raw) if isinstance(record, dict)}
    ordinal = {id(record): index for index, record in records.items()}
    graph = _drawing_graph(drawing)
    blocks = graph["blocks"]
    spaces = {
        handle: "model" if str(block.get("name", "")).casefold() == "*model_space" else "paper"
        for handle, block in blocks.items()
        if str(block.get("name", "")).casefold() == "*model_space"
        or str(block.get("name", "")).casefold().startswith("*paper_space")
    }
    for record in records.values():
        if _kind(record) == "LAYOUT" and (handle := _ref(record.get("block_header"))) in blocks:
            name = str(record.get("layout_name") or record.get("name") or "").casefold()
            spaces[handle] = "model" if name == "model" else "paper"
    members: dict[Any, set[int]] = defaultdict(set)
    for handle, values in graph["members"].items():
        for value in values:
            if record := graph["by_handle"].get(value):
                owner = graph["by_handle"].get(_ref(record.get("ownerhandle")), {})
                if _kind(record) == "ATTRIB" and _kind(owner) == "INSERT":
                    continue
                members[handle].add(ordinal[id(record)])
    for index, record in records.items():
        owner = _ref(record.get("ownerhandle"))
        if owner in blocks:
            members[owner].add(index)
        elif owner is None and record.get("entity"):
            kind = {1: "paper", 2: "model"}.get(_number(record.get("entmode")))
            space = next((handle for handle, value in spaces.items() if value == kind), None)
            if space is not None:
                members[space].add(index)
    placed: set[int] = set()

    def emit(index, matrix, path, space_kind, space_handle, stack):
        record = records[index]
        kind = _kind(record)
        if not record.get("entity") or kind in _SUPPORT_ENTITIES or kind.startswith("VERTEX"):
            return
        placed.add(index)
        occurrence = {
            "object_index": index,
            "instance_path": list(path),
            "transform": list(matrix),
            "space_kind": space_kind,
            "space_handle": space_handle,
            "entity_type": kind,
            "is_container": False,
            **_location(record, matrix),
        }
        if kind != "INSERT":
            yield occurrence
            return
        nested_handle = _ref(record.get("block_header"))
        block = blocks.get(nested_handle)
        try:
            if block is None:
                raise ValueError("missing_block")
            if nested_handle in stack or len(stack) >= 12:
                raise ValueError("cyclic_or_deep_block")
            if not _is_xy(record) or _point(record.get("ins_pt")) is None:
                raise ValueError("invalid_insert_transform")
            if block.get("base_pt") is not None and _point(block["base_pt"]) is None:
                raise ValueError("invalid_insert_transform")
            scale = record.get("scale")
            if scale is not None and (
                not isinstance(scale, list)
                or not scale
                or any(_number(value) is None for value in scale)
            ):
                raise ValueError("invalid_insert_transform")
            local_matrix = _insert_matrix(record, block)
            nested_matrix = _matrix_multiply(matrix, local_matrix)
            if not all(math.isfinite(value) for value in nested_matrix):
                raise ValueError("invalid_insert_transform")
        except (TypeError, ValueError, OverflowError) as exc:
            occurrence["geometry_status"] = str(exc) or "invalid_insert_transform"
        else:
            occurrence.update(is_container=True, geometry_status="container")
        yield occurrence
        handle = _ref(record.get("handle"))
        nested_path = [*path, handle if handle is not None else f"object:{index}"]
        for child in graph["children"].get(handle, []):
            if _kind(child) == "ATTRIB":
                # Attached ATTRIB coordinates already belong to the INSERT's owner.
                yield from emit(
                    ordinal[id(child)], matrix, nested_path, space_kind, space_handle, stack
                )
        if not occurrence["is_container"]:
            return
        yield from visit(
            nested_handle,
            nested_matrix,
            nested_path,
            space_kind,
            space_handle,
            (*stack, nested_handle),
        )

    def visit(handle, matrix, path, space_kind, space_handle, stack):
        for index in sorted(members[handle]):
            yield from emit(index, matrix, path, space_kind, space_handle, stack)

    for handle, kind in spaces.items():
        yield from visit(handle, _IDENTITY, [], kind, handle, (handle,))
    for index, record in records.items():
        if index in placed:
            continue
        owner = _ref(record.get("ownerhandle"))
        if owner in blocks and owner not in spaces:
            continue  # an uninstantiated block definition is not a placed object
        if _kind(graph["by_handle"].get(owner, {})) in {"INSERT", "MINSERT"}:
            continue  # e.g. an attribute of an uninstantiated INSERT
        kind = {1: "paper", 2: "model"}.get(_number(record.get("entmode")), "unknown")
        yield from emit(index, _IDENTITY, [], kind, None, ())
