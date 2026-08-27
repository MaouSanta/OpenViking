# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Build bounded, searchable Sheet artifacts from normalized LibreDWG JSON."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable

from openviking.parse.parsers.dwg_sheet_geometry import (
    detect_marker_groups,
    detect_table_grids,
)
from openviking_cli.utils.config.parser_config import ParserConfig
from openviking_cli.utils.uri import VikingURI

SHEET_DOCUMENT_SCHEMA = "openviking.dwg-document/v1"
SHEET_STRUCTURE_SCHEMA = "openviking.dwg-sheet-structure/v1"

_FORMAT_RE = re.compile(r"\\(?:A\d+|C\d+|F[^;]*|f[^;]*|H[^;]*|Q[^;]*|T[^;]*|W[^;]*);", re.I)
_FIELD_RE = re.compile(
    r"^(项目名称|建设单位|设计单位|图纸名称|图名|图号|专业|比例|日期|出图日期|"
    r"修订|版本|页码|设计|校对|审核|批准)\s*[:：]\s*(.+)$"
)
_META_KEYS = tuple(
    "项目名称 建设单位 设计单位 图纸名称 图名 图号 专业 比例 日期 出图日期 修订 版本 页码 设计 校对 审核 批准".split()
)
_DIRECTIVE_RE = re.compile(
    r"(?:不得|严禁|应当|应按|应采用|须|封堵|预留|待.+后|施工要求|安装要求|注意事项)"
)
_ROOM_RE = re.compile(r"(?:室|间|厅|房|井|库|站|区|平台|楼梯|走道|通道)$")
_IDENTIFIER_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9_.-]{3,}$")
_DIMENSION_RE = re.compile(
    r"^(?:[±+-]?\d+(?:\.\d+)?|[ΦØRr]\s*\d+|.*(?:标高|尺寸|\d\s*[xX×]\s*\d).*)$"
)
_MARKER_RE = re.compile(r"^\s*([□■☐☑☒○●◯✓√×✕])\s*(.*)$")
_FRAME_NAME_RE = re.compile(r"(?:图框|图签|title\s*block|border|sheet\s*frame)", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_NONSPACE_RE = re.compile(r"[^\s]")


def _ref(value: Any) -> Any:
    return value[-1] if isinstance(value, list) and value else value


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _clean_text(value: Any) -> str:
    """Remove MTEXT presentation codes while preserving its paragraph structure."""
    text = unicodedata.normalize("NFC", "" if value is None else str(value))
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda match: chr(int(match.group(1), 16)), text)
    text = text.replace("\\P", "\n").replace("\\~", " ").replace("\r", "\n")
    text = _FORMAT_RE.sub("", text).replace("{", "").replace("}", "")
    lines = [re.sub(r"[ \t\x00-\x1f\x7f]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _kind(record: dict[str, Any]) -> str:
    return str(record.get("entity") or record.get("object") or "").upper()


def _matrix_apply(matrix: tuple[float, ...], point: tuple[float, float]) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * point[0] + c * point[1] + e, b * point[0] + d * point[1] + f


def _matrix_multiply(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = left
    g, h, i, j, k, l = right
    return (
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * l + e,
        b * k + d * l + f,
    )


def _insert_matrix(record: dict[str, Any], block: dict[str, Any]) -> tuple[float, ...]:
    insertion = _point(record.get("ins_pt")) or (0.0, 0.0)
    base = _point(block.get("base_pt")) or (0.0, 0.0)
    scale = record.get("scale")
    sx = float(scale[0]) if isinstance(scale, list) and scale else 1.0
    sy = float(scale[1]) if isinstance(scale, list) and len(scale) > 1 else sx
    angle = float(record.get("rotation") or 0.0)
    cosine, sine = math.cos(angle), math.sin(angle)
    return (
        sx * cosine,
        sx * sine,
        -sy * sine,
        sy * cosine,
        insertion[0] - sx * cosine * base[0] + sy * sine * base[1],
        insertion[1] - sx * sine * base[0] - sy * cosine * base[1],
    )


def _bbox(points: Iterable[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    points = list(points)
    if not points:
        return None
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(box: tuple[float, ...], point: tuple[float, float] | None) -> bool:
    return bool(point and box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3])


def _box_area(box: tuple[float, ...]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _contains(outer: tuple[float, ...], inner: tuple[float, ...], tolerance: float = 0.0) -> bool:
    return (
        outer[0] - tolerance <= inner[0]
        and outer[1] - tolerance <= inner[1]
        and outer[2] + tolerance >= inner[2]
        and outer[3] + tolerance >= inner[3]
    )


def _source_ref(record: dict[str, Any], instance_path: list[Any]) -> dict[str, Any]:
    result = {"object_index": record.get("index"), "handle": _ref(record.get("handle"))}
    if instance_path:
        result["instance_path"] = instance_path
    return result


def _drawing_graph(drawing: dict[str, Any]) -> dict[str, Any]:
    objects = [record for record in drawing.get("OBJECTS", []) if isinstance(record, dict)]
    by_handle = {
        _ref(record.get("handle")): record
        for record in objects
        if _ref(record.get("handle")) is not None
    }
    blocks = {
        handle: record for handle, record in by_handle.items() if _kind(record) == "BLOCK_HEADER"
    }
    members: dict[Any, list[Any]] = defaultdict(list)
    for block_handle, block in blocks.items():
        for value in block.get("entities") or []:
            handle = _ref(value)
            if handle in by_handle:
                members[block_handle].append(handle)
    for record in objects:
        handle, owner = _ref(record.get("handle")), _ref(record.get("ownerhandle"))
        if handle is not None and owner in blocks and handle not in members[owner]:
            members[owner].append(handle)
    model_blocks = [
        handle
        for handle, block in blocks.items()
        if str(block.get("name") or "").casefold() == "*model_space"
    ]
    paper_blocks = [
        handle
        for handle, block in blocks.items()
        if str(block.get("name") or "").casefold().startswith("*paper_space")
    ]
    # LibreDWG can omit BLOCK_HEADER.entities for top-level space entities.
    # entmode is the remaining authoritative Model/Paper discriminator.
    space_by_mode = {2: model_blocks[:1], 1: paper_blocks[:1]}
    for record in objects:
        handle = _ref(record.get("handle"))
        if (
            handle is None
            or record.get("ownerhandle") is not None
            or not record.get("entity")
            or _kind(record) in {"BLOCK", "ENDBLK"}
        ):
            continue
        try:
            targets = space_by_mode.get(int(record.get("entmode")), [])
        except (TypeError, ValueError):
            targets = []
        for block_handle in targets:
            if handle not in members[block_handle]:
                members[block_handle].append(handle)
    children: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for record in objects:
        owner = _ref(record.get("ownerhandle"))
        if owner in by_handle:
            children[owner].append(record)
    layers = {
        handle: str(record.get("name") or "")
        for handle, record in by_handle.items()
        if _kind(record) == "LAYER"
    }
    return {
        "objects": objects,
        "by_handle": by_handle,
        "blocks": blocks,
        "members": members,
        "children": children,
        "layers": layers,
    }


def _block_bbox(
    block_handle: Any,
    graph: dict[str, Any],
    seen: frozenset[Any] = frozenset(),
) -> tuple[float, float, float, float] | None:
    if block_handle in seen or len(seen) >= 12:
        return None
    points: list[tuple[float, float]] = []
    for handle in graph["members"].get(block_handle, []):
        record = graph["by_handle"].get(handle, {})
        kind = _kind(record)
        if kind == "LINE":
            points.extend(point for key in ("start", "end") if (point := _point(record.get(key))))
        elif kind == "LWPOLYLINE":
            points.extend(point for value in record.get("points") or [] if (point := _point(value)))
        elif kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            point = _point(record.get("ins_pt")) or _point(record.get("alignment_pt"))
            if point:
                points.append(point)
        elif kind == "INSERT":
            nested_handle = _ref(record.get("block_header"))
            nested = graph["blocks"].get(nested_handle)
            nested_box = (
                _block_bbox(nested_handle, graph, seen | {block_handle}) if nested else None
            )
            if nested_box:
                matrix = _insert_matrix(record, nested)
                points.extend(
                    _matrix_apply(matrix, point)
                    for point in (
                        (nested_box[0], nested_box[1]),
                        (nested_box[0], nested_box[3]),
                        (nested_box[2], nested_box[1]),
                        (nested_box[2], nested_box[3]),
                    )
                )
    return _bbox(points)


def _walk_block(block_handle: Any, graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {
        "texts": [],
        "lines": [],
        "frames": [],
        "objects": [],
    }
    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def visit(
        current: Any, matrix: tuple[float, ...], path: list[Any], stack: frozenset[Any]
    ) -> None:
        if current in stack or len(stack) >= 12:
            return
        for handle in graph["members"].get(current, []):
            record = graph["by_handle"].get(handle)
            if not record:
                continue
            kind = _kind(record)
            result["objects"].append(record)
            if kind in {"TEXT", "MTEXT", "ATTRIB"}:
                raw = record.get("text") if kind == "MTEXT" else record.get("text_value")
                if raw is None:
                    raw = record.get("text_value") if kind == "MTEXT" else record.get("text")
                text = _clean_text(raw)
                if not text:
                    continue
                local_point = _point(record.get("ins_pt")) or _point(record.get("alignment_pt"))
                height = record.get("text_height") if kind == "MTEXT" else record.get("height")
                result["texts"].append(
                    {
                        "uid": f"{handle}@{'/'.join(map(str, path))}",
                        "record": record,
                        "text": text,
                        "point": _matrix_apply(matrix, local_point) if local_point else None,
                        "height": abs(float(height)) if isinstance(height, (int, float)) else 0.0,
                        "layer": graph["layers"].get(_ref(record.get("layer")), ""),
                        "instance_path": list(path),
                    }
                )
            elif kind == "LINE":
                start, end = _point(record.get("start")), _point(record.get("end"))
                if start and end:
                    result["lines"].append(
                        {
                            "a": _matrix_apply(matrix, start),
                            "b": _matrix_apply(matrix, end),
                            "record": record,
                            "instance_path": list(path),
                            "source_ref": _source_ref(record, path),
                        }
                    )
            elif kind == "LWPOLYLINE":
                points = [
                    _matrix_apply(matrix, point)
                    for value in record.get("points") or []
                    if (point := _point(value))
                ]
                if len(points) >= 2:
                    tolerance = 1e-9 * max(
                        1.0,
                        *(abs(coordinate) for point in points for coordinate in point),
                    )
                    repeated_endpoint = (
                        len(points) >= 3 and math.dist(points[0], points[-1]) <= tolerance
                    )
                    closed = bool(int(record.get("flag") or 0) & (1 | 512)) or repeated_endpoint
                    vertices = points[:-1] if repeated_endpoint else points
                    pairs = list(zip(points, points[1:], strict=False))
                    if closed and not repeated_endpoint:
                        pairs.append((points[-1], points[0]))
                    pairs = [(a, b) for a, b in pairs if math.dist(a, b) > 1e-9]
                    result["lines"].extend(
                        {
                            "a": a,
                            "b": b,
                            "record": record,
                            "instance_path": list(path),
                            "source_ref": _source_ref(record, path),
                        }
                        for a, b in pairs
                    )
                    box = _bbox(points)
                    orthogonal = all(
                        abs(a[0] - b[0]) <= 1e-6 * max(1.0, abs(a[0]), abs(b[0]))
                        or abs(a[1] - b[1]) <= 1e-6 * max(1.0, abs(a[1]), abs(b[1]))
                        for a, b in pairs
                    )
                    if box and closed and len(vertices) == 4 and orthogonal:
                        result["frames"].append(
                            {
                                "bbox": box,
                                "source_kind": "closed_polyline",
                                "source_ref": _source_ref(record, path),
                                "confidence": "candidate",
                            }
                        )
            elif kind == "CIRCLE":
                local_center = _point(record.get("center"))
                radius = record.get("radius")
                if local_center and isinstance(radius, (int, float)) and radius > 0:
                    center = _matrix_apply(matrix, local_center)
                    edge_x = _matrix_apply(matrix, (local_center[0] + float(radius), local_center[1]))
                    edge_y = _matrix_apply(matrix, (local_center[0], local_center[1] + float(radius)))
                    radius_x, radius_y = math.dist(center, edge_x), math.dist(center, edge_y)
                    result["frames"].append(
                        {
                            "bbox": (
                                center[0] - radius_x,
                                center[1] - radius_y,
                                center[0] + radius_x,
                                center[1] + radius_y,
                            ),
                            "center": center,
                            "radius": max(radius_x, radius_y),
                            "source_kind": "circle",
                            "source_ref": _source_ref(record, path),
                            "confidence": "candidate",
                        }
                    )
            elif kind == "INSERT":
                insert_handle = _ref(record.get("handle"))
                for attribute in graph["children"].get(insert_handle, []):
                    if _kind(attribute) != "ATTRIB":
                        continue
                    text = _clean_text(attribute.get("text_value") or attribute.get("text"))
                    point = _point(attribute.get("ins_pt")) or _point(attribute.get("alignment_pt"))
                    if text:
                        result["texts"].append(
                            {
                                "uid": f"{_ref(attribute.get('handle'))}@{'/'.join(map(str, path + [insert_handle]))}",
                                "record": attribute,
                                "text": text,
                                "point": _matrix_apply(matrix, point) if point else None,
                                "height": abs(float(attribute.get("height") or 0.0)),
                                "layer": graph["layers"].get(_ref(attribute.get("layer")), ""),
                                "instance_path": path + [insert_handle],
                            }
                        )
                nested_handle = _ref(record.get("block_header"))
                nested = graph["blocks"].get(nested_handle)
                if not nested:
                    continue
                nested_matrix = _matrix_multiply(matrix, _insert_matrix(record, nested))
                name = str(nested.get("name") or "")
                local_box = _block_bbox(nested_handle, graph)
                if local_box:
                    transformed = _bbox(
                        _matrix_apply(nested_matrix, point)
                        for point in (
                            (local_box[0], local_box[1]),
                            (local_box[0], local_box[3]),
                            (local_box[2], local_box[1]),
                            (local_box[2], local_box[3]),
                        )
                    )
                    if transformed:
                        result["frames"].append(
                            {
                                "bbox": transformed,
                                "source_kind": "frame_block"
                                if _FRAME_NAME_RE.search(name)
                                else "block_bbox",
                                "block_name": name,
                                "source_ref": _source_ref(record, path),
                                "confidence": "exact"
                                if _FRAME_NAME_RE.search(name)
                                else "candidate",
                            }
                        )
                visit(nested_handle, nested_matrix, path + [insert_handle], stack | {current})

        # Attached ATTRIBs and older LibreDWG records are not always listed in entities.
        listed = set(graph["members"].get(current, []))
        for record in graph["children"].get(current, []):
            if _ref(record.get("handle")) in listed:
                continue
            kind = _kind(record)
            if kind not in {"TEXT", "MTEXT", "ATTRIB"}:
                continue
            raw = record.get("text") if kind == "MTEXT" else record.get("text_value")
            text = _clean_text(raw if raw is not None else record.get("text"))
            point = _point(record.get("ins_pt")) or _point(record.get("alignment_pt"))
            if text:
                result["texts"].append(
                    {
                        "uid": f"{_ref(record.get('handle'))}@{'/'.join(map(str, path))}",
                        "record": record,
                        "text": text,
                        "point": _matrix_apply(matrix, point) if point else None,
                        "height": abs(
                            float(record.get("text_height") or record.get("height") or 0.0)
                        ),
                        "layer": graph["layers"].get(_ref(record.get("layer")), ""),
                        "instance_path": list(path),
                    }
                )

    visit(block_handle, identity, [], frozenset())
    deduplicated = {record["uid"]: record for record in result["texts"]}
    result["texts"] = list(deduplicated.values())
    return result


def _select_frames(space: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    frames = [
        frame
        for frame in space["frames"]
        if frame.get("source_kind") in {"frame_block", "block_bbox", "closed_polyline"}
    ]
    if not frames:
        return []
    candidates = frames
    extent = _bbox(
        [record["point"] for record in space["texts"] if record["point"]]
        + [line[endpoint] for line in space["lines"] for endpoint in ("a", "b")]
    )
    span_x = max(1.0, (extent[2] - extent[0]) if extent else 1.0)
    span_y = max(1.0, (extent[3] - extent[1]) if extent else 1.0)
    filtered = []
    for frame in candidates:
        box = frame["bbox"]
        width, height = box[2] - box[0], box[3] - box[1]
        text_count = sum(_inside(box, record["point"]) for record in space["texts"])
        if frame["source_kind"] == "frame_block" or (
            0.28 <= width / max(height, 1e-12) <= 3.6
            and width >= span_x * 0.06
            and height >= span_y * 0.06
        ):
            frame = {**frame, "text_count": text_count}
            filtered.append(frame)
    dimension_groups: list[list[dict[str, Any]]] = []
    for frame in filtered:
        box = frame["bbox"]
        width, height = box[2] - box[0], box[3] - box[1]
        group = next(
            (
                group
                for group in dimension_groups
                if abs((group[0]["bbox"][2] - group[0]["bbox"][0]) - width) <= width * 0.02
                and abs((group[0]["bbox"][3] - group[0]["bbox"][1]) - height) <= height * 0.02
            ),
            None,
        )
        if group is None:
            dimension_groups.append([frame])
        else:
            group.append(frame)
    repeated = [frame for group in dimension_groups if len(group) >= 2 for frame in group]
    if repeated:
        repeated_ids = {id(frame) for frame in repeated}
        retained_unique = []
        for frame in filtered:
            if id(frame) in repeated_ids or frame["text_count"] < 1:
                continue
            contained_centers = {
                (
                    round((other["bbox"][0] + other["bbox"][2]) / 2, 6),
                    round((other["bbox"][1] + other["bbox"][3]) / 2, 6),
                )
                for other in repeated
                if _contains(frame["bbox"], other["bbox"])
                and _box_area(other["bbox"]) < _box_area(frame["bbox"])
            }
            if len(contained_centers) < 2:
                retained_unique.append(frame)
        filtered = repeated + retained_unique
    else:
        filtered = [frame for frame in filtered if frame["text_count"] >= 1]
    accepted: list[dict[str, Any]] = []
    for frame in sorted(filtered, key=lambda item: _box_area(item["bbox"]), reverse=True):
        box = frame["bbox"]
        if any(
            _contains(existing["bbox"], box) and _box_area(box) < _box_area(existing["bbox"])
            for existing in accepted
        ):
            continue
        accepted.append(frame)
    # De-duplicate coincident frame geometry without rounding drawing units.
    unique: list[dict[str, Any]] = []
    tolerance = 1e-5 * max(span_x, span_y)
    for frame in accepted:
        if any(
            all(abs(a - b) <= tolerance for a, b in zip(frame["bbox"], item["bbox"], strict=True))
            for item in unique
        ):
            continue
        unique.append(frame)
    return sorted(unique, key=lambda item: (-item["bbox"][3], item["bbox"][0]))


def _native_tables(
    objects: list[dict[str, Any]],
    sheet_bbox: tuple[float, float, float, float] | None = None,
) -> list[dict[str, Any]]:
    tables = []
    for record in objects:
        if _kind(record) not in {"TABLE", "ACAD_TABLE"}:
            continue
        position = _point(record.get("ins_pt")) or _point(record.get("position"))
        if sheet_bbox and position and not _inside(sheet_bbox, position):
            continue
        rows = record.get("rows")
        if isinstance(rows, list) and rows and all(isinstance(row, list) for row in rows):
            matrix = [[_clean_text(value) for value in row] for row in rows]
        else:
            cells = record.get("cells")
            matrix = []
            if isinstance(cells, list) and cells and all(isinstance(cell, dict) for cell in cells):
                row_count = max(int(cell.get("row", 0)) for cell in cells) + 1
                col_count = max(int(cell.get("column", cell.get("col", 0))) for cell in cells) + 1
                matrix = [["" for _ in range(col_count)] for _ in range(row_count)]
                for cell in cells:
                    row, column = (
                        int(cell.get("row", 0)),
                        int(cell.get("column", cell.get("col", 0))),
                    )
                    matrix[row][column] = _clean_text(
                        cell.get("text") or cell.get("text_value") or cell.get("value")
                    )
        if matrix and max(map(len, matrix), default=0) >= 2:
            width = max(map(len, matrix))
            matrix = [row + [""] * (width - len(row)) for row in matrix]
            tables.append(
                {
                    "title": _clean_text(record.get("name")) or "原生表格",
                    "rows": matrix,
                    "header_rows": 1,
                    "spans": record.get("merged_cells") or [],
                    "source_kind": "native_table",
                    "confidence": "exact",
                    "source_refs": [_source_ref(record, [])],
                    "text_uids": [],
                }
            )
    return tables


def _extract_meta(
    records: list[dict[str, Any]], summary: dict[str, Any]
) -> tuple[dict[str, str], set[str]]:
    fields = {
        str(key): _clean_text(value)
        for key, value in summary.items()
        if value not in (None, "", [], {}) and isinstance(value, (str, int, float))
    }
    used: set[str] = set()
    for record in records:
        match = _FIELD_RE.match(record["text"].replace("\n", " "))
        if match:
            fields[match.group(1)] = match.group(2).strip()
            used.add(record["uid"])
    exact = {
        record["text"]: record
        for record in records
        if record["text"] in _META_KEYS and record["point"]
    }
    for key, label in exact.items():
        x, y = label["point"]
        height = max(label["height"], 1.0)
        candidates = [
            record
            for record in records
            if record["uid"] != label["uid"]
            and record["point"]
            and record["point"][0] > x
            and abs(record["point"][1] - y) <= height * 2
        ]
        if candidates:
            value = min(candidates, key=lambda item: item["point"][0] - x)
            if value["point"][0] - x <= height * 80:
                fields[key] = value["text"].replace("\n", " ")
                used.update({label["uid"], value["uid"]})
    return fields, used


def _classify_sheet(
    records: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    summary: dict[str, Any],
    frames: list[dict[str, Any]],
    lines: list[dict[str, Any]],
    sheet_bbox: tuple[float, float, float, float] | None,
) -> dict[str, Any]:
    meta, used = _extract_meta(records, summary)
    category_uids: dict[str, set[str]] = defaultdict(set)
    category_uids["meta"].update(used)
    tables = _native_tables(objects, sheet_bbox)
    if not tables:
        tables = detect_table_grids(
            lines,
            [record for record in records if record["uid"] not in used],
            sheet_bbox=sheet_bbox,
        )
    for table in tables:
        table_uids = table.pop("text_uids", [])
        used.update(table_uids)
        category_uids["tables"].update(table_uids)
    notes, directives = [], []
    option_groups = detect_marker_groups(frames, lines, records)
    markers = [marker for group in option_groups for marker in group["markers"]]
    marker_label_uids = {
        uid for group in option_groups for uid in group.pop("text_uids", [])
    }
    used.update(marker_label_uids)
    category_uids["markers"].update(marker_label_uids)
    labels: Counter[tuple[str, str]] = Counter()
    unassigned: Counter[str] = Counter()
    refs: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unassigned_refs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["uid"] in used:
            continue
        text = record["text"]
        source = _source_ref(record["record"], record["instance_path"])
        marker = _MARKER_RE.match(text)
        if marker:
            glyph, label = marker.groups()
            state = "selected" if glyph in "■●☑✓√☒×✕" else "unknown"
            markers.append(
                {
                    "marker_id": f"M{len(markers) + 1:04d}",
                    "shape": "circle"
                    if glyph in "○●◯"
                    else "square"
                    if glyph in "□■☐☑☒"
                    else "none",
                    "mark": "check"
                    if glyph in "☑✓√"
                    else "cross"
                    if glyph in "☒×✕"
                    else "fill"
                    if glyph in "■●"
                    else "none",
                    "state": state,
                    "label": label,
                    "evidence": "text_glyph",
                    "source_refs": [source],
                }
            )
            category_uids["markers"].add(record["uid"])
        elif _DIRECTIVE_RE.search(text):
            directives.append(
                {
                    "directive_id": f"D{len(directives) + 1:04d}",
                    "text": text,
                    "target": None,
                    "binding": "unresolved",
                    "source_refs": [source],
                }
            )
            category_uids["directives"].add(record["uid"])
        elif len(text) >= 100 or "\n" in text or re.match(r"^\d+(?:\.\d+)+[.、．\s]", text):
            notes.append({"text": text, "source_refs": [source]})
            category_uids["notes"].add(record["uid"])
        else:
            compact = text.replace("\n", " ").strip()
            if _ROOM_RE.search(compact):
                role = "spatial_label"
            elif _IDENTIFIER_RE.match(compact):
                role = "identifier"
            elif _DIMENSION_RE.match(compact):
                role = "dimension"
            elif len(compact) <= 40:
                role = "label"
            else:
                unassigned[compact] += 1
                unassigned_refs[compact].append(source)
                category_uids["unassigned"].add(record["uid"])
                continue
            labels[(compact, role)] += 1
            refs[(compact, role)].append(source)
            category_uids["labels"].add(record["uid"])
    label_rows = [
        {"text": text, "role": role, "count": count, "source_refs": refs[(text, role)]}
        for (text, role), count in sorted(labels.items(), key=lambda item: (item[0][1], item[0][0]))
    ]
    unresolved = [
        {"text": text, "count": count, "source_refs": unassigned_refs[text]}
        for text, count in sorted(unassigned.items())
    ]
    return {
        "meta": meta,
        "tables": tables,
        "notes": notes,
        "directives": directives,
        "markers": markers,
        "option_groups": option_groups,
        "labels": label_rows,
        "unassigned": unresolved,
        "_category_uids": {key: sorted(values) for key, values in category_uids.items()},
    }


def _markdown_cell(value: Any) -> str:
    return _clean_text(value).replace("|", "\\|").replace("\n", "<br>")


def _table_markdown(
    rows: list[list[str]],
    *,
    header_rows: int = 1,
    spans: list[dict[str, Any]] | None = None,
) -> str:
    width = max(map(len, rows), default=0)
    if not width:
        return ""
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header_rows = min(max(1, header_rows), len(normalized))
    expanded_headers = [row[:] for row in normalized[:header_rows]]
    for span in spans or []:
        if not isinstance(span, dict):
            continue
        row = int(span.get("row", 0))
        column = int(span.get("column", span.get("col", 0)))
        column_span = int(span.get("column_span", span.get("colspan", 1)))
        if not (0 <= row < header_rows and 0 <= column < width):
            continue
        value = expanded_headers[row][column]
        for target in range(column, min(width, column + max(1, column_span))):
            if value and not expanded_headers[row][target]:
                expanded_headers[row][target] = value
    header = []
    for column in range(width):
        path = []
        for row in range(header_rows):
            value = expanded_headers[row][column]
            if value and value not in path:
                path.append(value)
        header.append(" / ".join(path))
    body = normalized[header_rows:]
    active_columns = [
        column
        for column in range(width)
        if header[column] or any(row[column] for row in body)
    ]
    if not active_columns:
        return ""
    header = [header[column] for column in active_columns]
    body = [
        [row[column] for column in active_columns]
        for row in body
        if any(row[column] for column in active_columns)
    ]
    lines = [
        "| " + " | ".join(map(_markdown_cell, header)) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(map(_markdown_cell, row)) + " |" for row in body)
    return "\n".join(lines)


def _sections(classified: dict[str, Any], sheet: dict[str, Any]) -> list[tuple[str, str]]:
    meta = dict(classified["meta"])
    meta.setdefault("来源文件", sheet["source_name"])
    meta.setdefault("Sheet ID", sheet["sheet_id"])
    if sheet.get("layout_name"):
        meta.setdefault("布局", sheet["layout_name"])
    sections: list[tuple[str, str]] = [
        ("图纸元数据", "\n".join(f"- {key}：{value}" for key, value in meta.items()))
    ]
    for index, table in enumerate(classified["tables"], 1):
        sections.append(
            (
                f"表格 {index}：{table['title']}",
                _table_markdown(
                    table["rows"],
                    header_rows=table["header_rows"],
                    spans=table["spans"],
                ),
            )
        )
    if classified["notes"]:
        sections.append(("说明", "\n\n".join(item["text"] for item in classified["notes"])))
    if classified["markers"]:
        marker_lines = []
        for marker in classified["markers"]:
            box = (
                "[x]"
                if marker["state"] == "selected"
                else "[ ]"
                if marker["state"] == "unselected"
                else "[?]"
            )
            marker_lines.append(f"- {box} {marker['label'] or '未命名选项'}")
        sections.append(("选项与标记", "\n".join(marker_lines)))
    if classified["directives"]:
        sections.append(
            (
                "局部施工要求",
                "\n".join(
                    f"- [{item['directive_id']}] {item['text']}"
                    for item in classified["directives"]
                ),
            )
        )
    if classified["labels"]:
        rows = [["文字", "次数", "类型"]] + [
            [item["text"], str(item["count"]), item["role"]] for item in classified["labels"]
        ]
        sections.append(("文字标签（聚合，无坐标）", _table_markdown(rows)))
    if classified["unassigned"]:
        rows = [["文字", "次数"]] + [
            [item["text"], str(item["count"])] for item in classified["unassigned"]
        ]
        sections.append(("未分类文字", _table_markdown(rows)))
    return sections


def _tokens(text: str) -> int:
    cjk = len(_CJK_RE.findall(text))
    return int(cjk * 0.7 + (len(_NONSPACE_RE.findall(text)) - cjk) * 0.3)


def _fits(text: str, max_tokens: int, max_chars: int) -> bool:
    return len(text) <= max_chars and _tokens(text) <= max_tokens


def _split_body(heading: str, body: str, prefix: str, max_tokens: int, max_chars: int) -> list[str]:
    lead = f"{prefix}\n\n## {heading}\n\n"
    if _fits(lead + body, max_tokens, max_chars):
        return [lead + body]
    lines = body.splitlines()
    table_header = (
        lines[:2]
        if len(lines) >= 2 and lines[0].startswith("|") and lines[1].startswith("|")
        else []
    )
    repeated = "\n".join(table_header)
    if table_header and (
        len(repeated) > max_chars // 2
        or not _fits(f"{lead}{repeated}", max_tokens, max_chars)
    ):
        table_header = []
        repeated = ""
    items = lines[2:] if table_header else re.split(r"\n\s*\n|\n", body)
    parts, current = [], repeated
    for item in items:
        candidate = f"{current}\n{item}".strip()
        if _fits(lead + candidate, max_tokens, max_chars):
            current = candidate
            continue
        if current and current != repeated:
            parts.append(lead + current)
            current = repeated
        remaining = item
        allowance = max(1, max_chars - len(lead) - len(repeated) - 2)
        while remaining and not _fits(
            lead + (repeated + "\n" if repeated else "") + remaining, max_tokens, max_chars
        ):
            cut = min(
                allowance, max(1, int(max_tokens / max(_tokens(remaining), 1) * len(remaining)))
            )
            chunk, remaining = remaining[:cut], remaining[cut:]
            parts.append(lead + ((repeated + "\n") if repeated else "") + chunk)
        current = ((repeated + "\n") if repeated else "") + remaining if remaining else repeated
    if current and current != repeated:
        parts.append(lead + current)
    return parts or [lead.rstrip()]


def _bounded_markdown(
    title: str, sheet_id: str, sections: list[tuple[str, str]], max_tokens: int, max_chars: int
) -> list[str]:
    prefix = f"# {title}\n\n> Sheet ID: `{sheet_id}`"
    pieces = [
        piece
        for heading, body in sections
        for piece in _split_body(heading, body, prefix, max_tokens, max_chars)
    ]
    parts: list[str] = []
    current = ""
    for piece in pieces:
        body = piece[len(prefix) :].lstrip()
        candidate = f"{current}\n\n{body}".strip() if current else piece
        if _fits(candidate, max_tokens, max_chars):
            current = candidate
        else:
            if current:
                parts.append(current.rstrip())
            current = piece
    if current:
        parts.append(current.rstrip())
    parts = parts or [prefix]
    index = 0
    while index < len(parts):
        if _tokens(parts[index]) >= 512:
            index += 1
            continue
        body = parts[index][len(prefix) :].lstrip()
        if index > 0:
            candidate = f"{parts[index - 1]}\n\n{body}".strip()
            if _fits(candidate, max_tokens, max_chars):
                parts[index - 1] = candidate
                parts.pop(index)
                continue
        if index + 1 < len(parts):
            next_body = parts[index + 1][len(prefix) :].lstrip()
            candidate = f"{prefix}\n\n{body}\n\n{next_body}".strip()
            if _fits(candidate, max_tokens, max_chars):
                parts[index] = candidate
                parts.pop(index + 1)
        index += 1
    return parts


def _title(classified: dict[str, Any], fallback: str) -> str:
    for key in ("图纸名称", "图名"):
        if classified["meta"].get(key):
            return classified["meta"][key]
    boilerplate = {
        "制图",
        "制 图",
        "设计",
        "校对",
        "审核",
        "批准",
        "图名",
        "图纸名称",
        "图号",
    }
    candidates = [
        item
        for item in classified["labels"] + classified["unassigned"]
        if re.search(r"(?:图|说明|目录|清单|表)$", item["text"])
        and item["text"].strip() not in boilerplate
        and not re.search(r"[，。；：,;]", item["text"])
        and 3 <= len(item["text"]) <= 48
    ]
    if not candidates:
        return fallback
    candidates.sort(
        key=lambda item: (
            3
            if re.search(r"(?:总说明|设计说明|图纸目录|清单|表)$", item["text"])
            else 2
            if re.search(r"(?:平面图|立面图|剖面图|详图)$", item["text"])
            else 1,
            len(item["text"]),
            item["count"],
        ),
        reverse=True,
    )
    return candidates[0]["text"]


def _sheet_id(source_sha256: str, locator: str) -> str:
    return "sheet-" + hashlib.sha256(f"{source_sha256}:{locator}".encode()).hexdigest()[:12]


def _table_cells(table: dict[str, Any]) -> list[dict[str, Any]]:
    spans = {
        (int(span.get("row", 0)), int(span.get("column", span.get("col", 0)))): span
        for span in table["spans"]
        if isinstance(span, dict)
    }
    cells = []
    for row_index, row in enumerate(table["rows"]):
        for column_index, text in enumerate(row):
            if not text:
                continue
            span = spans.get((row_index, column_index), {})
            cells.append(
                {
                    "row": row_index,
                    "column": column_index,
                    "row_span": int(span.get("row_span", span.get("rowspan", 1))),
                    "column_span": int(span.get("column_span", span.get("colspan", 1))),
                    "role": "column_header" if row_index < table["header_rows"] else "body",
                    "text": text,
                    "source_refs": table.get("cell_source_refs", {}).get(
                        f"{row_index}:{column_index}", table["source_refs"]
                    ),
                }
            )
    return cells


def _structure(
    sheet: dict[str, Any], classified: dict[str, Any], content_paths: list[str]
) -> dict[str, Any]:
    region_specs = (
        ("meta", len(classified["meta"]), "图纸元数据"),
        ("table", len(classified["tables"]), "表格"),
        ("notes", len(classified["notes"]), "说明"),
        ("markers", len(classified["markers"]), "选项与标记"),
        ("directives", len(classified["directives"]), "局部施工要求"),
        ("labels", len(classified["labels"]), "文字标签（聚合，无坐标）"),
        ("unassigned", len(classified["unassigned"]), "未分类文字"),
    )
    regions = [
        {
            "region_id": f"R{index:03d}",
            "type": kind,
            "item_count": count,
            "markdown_heading": heading,
        }
        for index, (kind, count, heading) in enumerate(region_specs, 1)
        if count
    ]
    category_counts = {key: len(values) for key, values in classified["_category_uids"].items()}
    assigned_instances = len(
        {uid for values in classified["_category_uids"].values() for uid in values}
    )
    return {
        "schema": SHEET_STRUCTURE_SCHEMA,
        "sheet": {
            "sheet_id": sheet["sheet_id"],
            "title": sheet["title"],
            "kind": sheet["kind"],
            "layout_name": sheet.get("layout_name"),
            "bbox": list(sheet["bbox"]) if sheet.get("bbox") else None,
            "viewport_projection": sheet.get("viewport_projection", "not_applicable"),
            "source_locator": sheet["source_locator"],
            "content_paths": content_paths,
        },
        "source": {
            "file": sheet["source_name"],
            "sha256": sheet["source_sha256"],
            "sheet_refs": sheet["source_refs"],
        },
        "regions": regions,
        "meta": classified["meta"],
        "notes": classified["notes"],
        "tables": [
            {
                "table_id": f"T{index:03d}",
                "title": table["title"],
                "source_kind": table["source_kind"],
                "confidence": table["confidence"],
                "row_count": len(table["rows"]),
                "column_count": max(map(len, table["rows"]), default=0),
                "header_rows": table["header_rows"],
                "spans": table["spans"],
                "cells": _table_cells(table),
                "source_refs": table["source_refs"],
            }
            for index, table in enumerate(classified["tables"], 1)
        ],
        "markers": classified["markers"],
        "option_groups": classified["option_groups"],
        "directives": classified["directives"],
        "labels": classified["labels"],
        "unassigned": classified["unassigned"],
        "coverage": {
            "source_text_instances": sheet["source_text_count"],
            "classified_text_instances": assigned_instances,
            "category_source_instances": category_counts,
            "unclassified_text_instances": sheet["source_text_count"] - assigned_instances,
            "viewport_projection_complete": sheet.get("viewport_projection") != "unresolved",
            "complete": assigned_instances == sheet["source_text_count"]
            and sheet.get("viewport_projection") != "unresolved",
            "no_per_label_coordinates": True,
        },
    }


def build_sheet_artifacts(
    drawing: dict[str, Any],
    *,
    source_name: str,
    source_sha256: str,
    source_size_bytes: int,
    max_section_tokens: int | None = None,
    max_section_chars: int | None = None,
) -> dict[str, Any]:
    """Return one document manifest and one bounded Markdown/structure pair per Sheet."""
    defaults = ParserConfig()
    max_tokens = max_section_tokens or defaults.max_section_size
    max_chars = max_section_chars or defaults.max_section_chars
    graph = _drawing_graph(drawing)
    summary = drawing.get("SummaryInfo") if isinstance(drawing.get("SummaryInfo"), dict) else {}
    layouts = [record for record in graph["objects"] if _kind(record) == "LAYOUT"]
    model_blocks = {
        _ref(record.get("block_header"))
        for record in layouts
        if str(record.get("layout_name") or record.get("name") or "").casefold() == "model"
    }
    model_blocks.update(
        handle
        for handle, block in graph["blocks"].items()
        if str(block.get("name") or "").casefold() == "*model_space"
    )
    candidates: list[dict[str, Any]] = []
    for layout in layouts:
        name = str(layout.get("layout_name") or layout.get("name") or "Layout")
        block_handle = _ref(layout.get("block_header"))
        if (
            name.casefold() == "model"
            or block_handle in model_blocks
            or block_handle not in graph["blocks"]
        ):
            continue
        space = _walk_block(block_handle, graph)
        meaningful = [
            record for record in space["objects"] if _kind(record) not in {"BLOCK", "ENDBLK"}
        ]
        if not space["texts"] and not meaningful:
            continue
        viewport_handles = {
            _ref(value) for value in layout.get("viewports") or [] if _ref(value) is not None
        }
        viewport_handles.update(
            _ref(record.get("handle"))
            for record in meaningful
            if _kind(record) == "VIEWPORT" and _ref(record.get("handle")) is not None
        )
        candidates.append(
            {
                "kind": "layout",
                "layout_name": name,
                "locator": f"layout:{_ref(layout.get('handle'))}:{block_handle}",
                "source_locator": {
                    "kind": "layout",
                    "layout_handle": _ref(layout.get("handle")),
                    "block_handle": block_handle,
                    "viewport_handles": sorted(viewport_handles, key=str),
                },
                "source_refs": [_source_ref(layout, [])],
                "texts": space["texts"],
                "objects": space["objects"],
                "geometry_frames": space["frames"],
                "lines": space["lines"],
                "viewport_projection": "unresolved" if viewport_handles else "not_applicable",
            }
        )
    for block_handle in model_blocks:
        if block_handle not in graph["blocks"]:
            continue
        space = _walk_block(block_handle, graph)
        frames = _select_frames(space)
        assigned: set[str] = set()
        for frame in frames:
            texts = [record for record in space["texts"] if _inside(frame["bbox"], record["point"])]
            assigned.update(record["uid"] for record in texts)
            source_ref = frame["source_ref"]
            locator = f"frame:{source_ref.get('handle')}:{'/'.join(map(str, source_ref.get('instance_path', [])))}"
            candidates.append(
                {
                    "kind": "model_frame",
                    "layout_name": None,
                    "locator": locator,
                    "source_locator": {"kind": frame["source_kind"], "source_ref": source_ref},
                    "source_refs": [source_ref],
                    "texts": texts,
                    "objects": space["objects"],
                    "geometry_frames": [
                        item for item in space["frames"] if _contains(frame["bbox"], item["bbox"])
                    ],
                    "lines": [
                        line
                        for line in space["lines"]
                        if _inside(frame["bbox"], line["a"]) and _inside(frame["bbox"], line["b"])
                    ],
                    "bbox": frame["bbox"],
                }
            )
        outside = [record for record in space["texts"] if record["uid"] not in assigned]
        if outside:
            candidates.append(
                {
                    "kind": "model_unframed",
                    "layout_name": "Model",
                    "locator": f"model:{block_handle}:unframed",
                    "source_locator": {"kind": "model_space", "block_handle": block_handle},
                    "source_refs": [{"block_handle": block_handle}],
                    "texts": outside,
                    "objects": space["objects"],
                    "geometry_frames": [],
                    "lines": [],
                }
            )
    if not candidates:
        all_texts = []
        for record in graph["objects"]:
            if _kind(record) not in {"TEXT", "MTEXT", "ATTRIB"}:
                continue
            raw = record.get("text") if _kind(record) == "MTEXT" else record.get("text_value")
            text = _clean_text(raw if raw is not None else record.get("text"))
            if text:
                all_texts.append(
                    {
                        "uid": str(_ref(record.get("handle")) or record.get("index")),
                        "record": record,
                        "text": text,
                        "point": _point(record.get("ins_pt")) or _point(record.get("alignment_pt")),
                        "height": abs(
                            float(record.get("text_height") or record.get("height") or 0.0)
                        ),
                        "layer": graph["layers"].get(_ref(record.get("layer")), ""),
                        "instance_path": [],
                    }
                )
        candidates.append(
            {
                "kind": "synthetic",
                "layout_name": None,
                "locator": "synthetic:all",
                "source_locator": {"kind": "synthetic"},
                "source_refs": [],
                "texts": all_texts,
                "objects": graph["objects"],
                "geometry_frames": [],
                "lines": [],
            }
        )
    sheets = []
    for ordinal, candidate in enumerate(candidates, 1):
        sheet_id = _sheet_id(source_sha256, candidate["locator"])
        classified = _classify_sheet(
            candidate["texts"],
            candidate["objects"],
            summary,
            candidate.get("geometry_frames", []),
            candidate.get("lines", []),
            candidate.get("bbox"),
        )
        fallback = candidate.get("layout_name") or f"Sheet {ordinal:03d}"
        title = _title(classified, fallback)
        slug = VikingURI.sanitize_segment(title) or f"sheet-{ordinal:03d}"
        directory = f"sheets/{sheet_id}__{slug}"
        sheet = {
            **candidate,
            "sheet_id": sheet_id,
            "title": title,
            "directory": directory,
            "source_name": source_name,
            "source_sha256": source_sha256,
            "source_text_count": len(candidate["texts"]),
        }
        parts = _bounded_markdown(
            title, sheet_id, _sections(classified, sheet), max_tokens, max_chars
        )
        names = (
            ["content.md"]
            if len(parts) == 1
            else [f"content_{index:03d}.md" for index in range(1, len(parts) + 1)]
        )
        content_paths = [f"{directory}/{name}" for name in names]
        structure_path = f"{directory}/structure.json"
        structure = _structure(sheet, classified, content_paths)
        sheets.append(
            {
                "sheet_id": sheet_id,
                "directory": directory,
                "title": title,
                "content_paths": content_paths,
                "content_files": list(zip(names, parts, strict=True)),
                "structure_path": structure_path,
                "structure": structure,
                "kind": candidate["kind"],
                "source_locator": candidate["source_locator"],
            }
        )
    document_id = "dwg-" + source_sha256[:16]
    document = {
        "schema": SHEET_DOCUMENT_SCHEMA,
        "document_id": document_id,
        "source": {"file": source_name, "sha256": source_sha256, "size_bytes": source_size_bytes},
        "summary_info": summary,
        "sheet_count": len(sheets),
        "sheets": [
            {
                key: sheet[key]
                for key in (
                    "sheet_id",
                    "directory",
                    "title",
                    "content_paths",
                    "structure_path",
                    "kind",
                    "source_locator",
                )
            }
            for sheet in sheets
        ],
        "coverage": {
            "unique_source_text_records": len(
                {
                    _ref(record.get("handle")) or record.get("index")
                    for record in graph["objects"]
                    if _kind(record) in {"TEXT", "MTEXT", "ATTRIB"}
                }
            ),
            "sheet_text_instances": sum(
                sheet["structure"]["coverage"]["source_text_instances"] for sheet in sheets
            ),
            "fallback_sheet_used": any(sheet["kind"] == "synthetic" for sheet in sheets),
        },
    }
    return {"document": document, "sheets": sheets}


def _demo() -> None:
    drawing = {
        "OBJECTS": [
            {
                "object": "BLOCK_HEADER",
                "handle": [0, 1, 10],
                "name": "*Model_Space",
                "entities": [[0, 1, 20]],
            },
            {
                "entity": "TEXT",
                "handle": [0, 1, 20],
                "ins_pt": [1, 2],
                "text_value": "项目名称：示例",
            },
        ]
    }
    result = build_sheet_artifacts(
        drawing, source_name="demo.dwg", source_sha256="a" * 64, source_size_bytes=1
    )
    assert result["document"]["sheet_count"] == 1
    assert result["sheets"][0]["structure"]["coverage"]["no_per_label_coordinates"] is True


if __name__ == "__main__":
    _demo()
