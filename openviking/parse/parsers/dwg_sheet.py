# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Build bounded, searchable Sheet artifacts from normalized LibreDWG JSON."""

from __future__ import annotations

import hashlib
import math
import re
import statistics
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from typing import Any, Callable, Iterable

from openviking.parse.parsers.dwg_coordinates import (
    IDENTITY,
    apply3,
    entity_matrix,
    point3,
    projected_height,
    text_point,
)
from openviking.parse.parsers.dwg_coordinates import (
    apply2 as _matrix_apply,
)
from openviking.parse.parsers.dwg_coordinates import (
    insert_matrix as _insert_matrix,
)
from openviking.parse.parsers.dwg_coordinates import (
    multiply as _matrix_multiply,
)
from openviking.parse.parsers.dwg_sheet_frames import (
    contains_box as _frame_contains_box,
)
from openviking.parse.parsers.dwg_sheet_frames import (
    contains_point as _inside_frame,
)
from openviking.parse.parsers.dwg_sheet_frames import (
    line_frames,
    rectangle,
    select_frames,
)
from openviking.parse.parsers.dwg_sheet_frames import (
    tolerance as _frame_tolerance,
)
from openviking.parse.parsers.dwg_sheet_geometry import (
    detect_marker_groups,
    detect_table_grids,
)
from openviking.parse.parsers.dwg_titleblock import extract_titleblock_meta
from openviking_cli.utils.config.parser_config import ParserConfig
from openviking_cli.utils.uri import VikingURI

SHEET_DOCUMENT_SCHEMA = "openviking.dwg-document/v1"
DWG_PARSER_VERSION = "10.2"

_FORMAT_RE = re.compile(r"\\(?:A\d+|C\d+|F[^;]*|f[^;]*|H[^;]*|Q[^;]*|T[^;]*|W[^;]*);", re.I)
_FIELD_RE = re.compile(
    r"^(项目名称|建设单位|设计单位|图纸名称|图名|图号|专业|比例|日期|出图日期|"
    r"修订|版本|页码|设计|校对|审核|批准)\s*[:：]\s*(.+)$"
)
_META_KEYS = tuple(
    "项目名称 建设单位 设计单位 图纸名称 图名 图号 专业 比例 日期 出图日期 修订 版本 页码 设计 校对 审核 批准".split()
)
_META_HEADER_LABELS = set(_META_KEYS) | {"图别", "序号", "图幅", "备注", "图纸目录"}
_MARKER_RE = re.compile(r"^\s*([□■☐☑☒○●◯✓√×✕])\s*(.*)$")
_FRAME_NAME_RE = re.compile(
    r"(?:^A[0-4](?:\b|[-_]|\d)|图框|图签|图纸目录|title\s*block|border|sheet\s*frame)", re.I
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_NONSPACE_RE = re.compile(r"[^\s]")
_NUMBERED_RE = re.compile(
    r"^\s*((?:[1-9]\d?(?:\.\d+)+)(?=[^\d.])|"
    r"(?:[1-9]\d?(?:\.\d+){2,})$|(?:[1-9]\d?)[.．](?=\D))"
)
_FORM_SLOT_RE = re.compile(r"(?<=\S) {2,}(?=\S)")

RegionClassifier = Callable[[bytes, dict[str, Any]], dict[str, Any] | None]
SheetRenderer = Callable[[tuple[float, float, float, float]], bytes]


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


def _decode_text_codes(text: str) -> str:
    text = text.replace("%%1403%%141", "³").replace("%%%", "%")
    text = re.sub(r"%%P", "±", text, flags=re.I)
    text = re.sub(r"%%D", "°", text, flags=re.I)
    text = re.sub(r"%%C", "⌀", text, flags=re.I)
    return "".join(
        f"⟦SHX:0x{ord(char):02X}⟧" if 0x80 <= ord(char) <= 0x9F else char for char in text
    )


def _clean_text(value: Any) -> str:
    """Remove MTEXT presentation codes while preserving its paragraph structure."""
    text = _decode_text_codes(unicodedata.normalize("NFC", "" if value is None else str(value)))
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda match: chr(int(match.group(1), 16)), text)
    text = text.replace("\\P", "\n").replace("\\~", " ").replace("\r", "\n")
    text = _FORMAT_RE.sub("", text).replace("{", "").replace("}", "")
    lines = [re.sub(r"[ \t\x00-\x1f\x7f]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _compact_cjk_spacing(value: str) -> str:
    return re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value).strip()


def _kind(record: dict[str, Any]) -> str:
    return str(record.get("entity") or record.get("object") or "").upper()


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
    member_sets: dict[Any, set[Any]] = defaultdict(set)

    def add_member(owner, handle):
        if handle not in member_sets[owner]:
            members[owner].append(handle)
            member_sets[owner].add(handle)

    for block_handle, block in blocks.items():
        for value in block.get("entities") or []:
            handle = _ref(value)
            if handle in by_handle:
                add_member(block_handle, handle)
    for record in objects:
        handle, owner = _ref(record.get("handle")), _ref(record.get("ownerhandle"))
        if handle is not None and owner in blocks:
            add_member(owner, handle)
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
            add_member(block_handle, handle)
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


def _block_bbox3(block_handle, graph, seen=frozenset()):
    if block_handle in seen or len(seen) >= 12:
        return None
    cache = graph.setdefault("bbox_cache", {})
    key = block_handle, seen
    if key in cache:
        return cache[key]
    points = []
    for handle in graph["members"].get(block_handle, []):
        record = graph["by_handle"].get(handle, {})
        kind = _kind(record)
        try:
            matrix = entity_matrix(record, IDENTITY)
            if kind == "LINE":
                points.extend(
                    p for name in ("start", "end") if (p := point3(record.get(name))) is not None
                )
            elif kind == "LWPOLYLINE":
                for value in record.get("points") or []:
                    if (p := point3(value)) is not None:
                        points.append(
                            apply3(matrix, (p[0], p[1], float(record.get("elevation") or 0.0)))
                        )
            elif kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
                if (
                    _clean_text(record.get("text_value") or record.get("text"))
                    and (p := text_point(record)) is not None
                ):
                    points.append(apply3(matrix, p))
            elif kind == "INSERT":
                nested_handle = _ref(record.get("block_header"))
                nested = graph["blocks"].get(nested_handle)
                box = _block_bbox3(nested_handle, graph, seen | {block_handle}) if nested else None
                if box:
                    transform = _insert_matrix(record, nested)
                    points.extend(
                        apply3(transform, (x, y, z))
                        for x in (box[0], box[3])
                        for y in (box[1], box[4])
                        for z in (box[2], box[5])
                    )
        except (ValueError, TypeError, OverflowError):
            continue  # bounds are only auxiliary; malformed primitives remain native JSON
    box = (
        tuple(min(p[d] for p in points) for d in range(3))
        + tuple(max(p[d] for p in points) for d in range(3))
        if points
        else None
    )
    cache[key] = box
    return box


def _block_bbox(block_handle, graph, seen=frozenset()):
    box = _block_bbox3(block_handle, graph, seen)
    return (box[0], box[1], box[3], box[4]) if box else None


def _walk_block(block_handle: Any, graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result = {
        "texts": [],
        "lines": [],
        "frames": [],
        "objects": [],
        "diagnostics": [],
        "images": [],
    }
    open_rectangles = []
    frame_hints = {}

    def add_text(record, matrix, path):
        raw = record.get("text") if _kind(record) == "MTEXT" else record.get("text_value")
        text = _clean_text(
            raw if raw is not None else record.get("text_value") or record.get("text")
        )
        if not text:
            return
        point = text_point(record)
        result["texts"].append(
            {
                "uid": f"{_ref(record.get('handle'))}@{'/'.join(map(str, path))}",
                "record": record,
                "text": text,
                "point": _matrix_apply(entity_matrix(record, matrix), point)
                if point is not None
                else None,
                "height": projected_height(record, matrix),
                "layer": graph["layers"].get(_ref(record.get("layer")), ""),
                "instance_path": list(path),
            }
        )

    def add_line(a, b, record, path):
        if math.dist(a, b) <= 1e-9:
            return
        result["lines"].append(
            {
                "a": a,
                "b": b,
                "record": record,
                "layer": graph["layers"].get(_ref(record.get("layer")), ""),
                "instance_path": list(path),
                "source_ref": _source_ref(record, path),
                "frame_hint": frame_hints.get(tuple(path)),
            }
        )

    def visit(current, matrix, path, stack):
        if current in stack or len(stack) >= 12:
            result["diagnostics"].append(
                {"block_handle": current, "reason": "cyclic_or_deep_block"}
            )
            return
        name = str(graph["blocks"].get(current, {}).get("name") or "")
        inherited = frame_hints.get(tuple(path[:-1])) if path else None
        frame_hints[tuple(path)] = name if _FRAME_NAME_RE.search(name) else inherited
        for handle in graph["members"].get(current, []):
            record = graph["by_handle"].get(handle)
            if not record:
                continue
            kind = _kind(record)
            result["objects"].append(record)
            try:
                if kind in {"TEXT", "MTEXT", "ATTRIB"}:
                    add_text(record, matrix, path)
                elif kind == "LINE":
                    a, b = point3(record.get("start")), point3(record.get("end"))
                    if a is not None and b is not None:
                        add_line(_matrix_apply(matrix, a), _matrix_apply(matrix, b), record, path)
                elif kind == "LWPOLYLINE":
                    vertices = [
                        p
                        for value in record.get("points") or []
                        if (p := point3(value)) is not None
                    ]
                    if len(vertices) != len(record.get("points") or []):
                        raise ValueError("invalid_polyline_points")
                    if len(vertices) < 2:
                        continue
                    local = [(p[0], p[1], float(record.get("elevation") or 0.0)) for p in vertices]
                    transform = entity_matrix(record, matrix)
                    points = [_matrix_apply(transform, p) for p in local]
                    tol = max(
                        1e-9,
                        max(math.ulp(v) for p in points for v in p) * 16,
                        max(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))
                        * 1e-8,
                    )
                    repeated = len(points) >= 3 and math.dist(points[0], points[-1]) <= tol
                    # LibreDWG JSON stores the DWG flag: 512 closed, 1 extrusion.
                    closed = bool(int(record.get("flag") or 0) & 512) or repeated
                    pairs = list(zip(points, points[1:], strict=False))
                    if closed and not repeated:
                        pairs.append((points[-1], points[0]))
                    bulges = record.get("bulges") or []
                    for i, (a, b) in enumerate(pairs):
                        if i >= len(bulges) or not bulges[i]:
                            add_line(a, b, record, path)
                    local_polygon = rectangle(local) if not any(bulges) else None
                    if local_polygon:
                        # Retain the projected polygon, even when a tilted OCS makes its AABB misleading.
                        polygon = [
                            _matrix_apply(transform, (p[0], p[1], local[0][2]))
                            for p in local_polygon
                        ]
                        box = _bbox(polygon)
                        origin = polygon[0]
                        projected_area = (
                            abs(
                                sum(
                                    (a[0] - origin[0]) * (b[1] - origin[1])
                                    - (a[1] - origin[1]) * (b[0] - origin[0])
                                    for a, b in zip(
                                        polygon, polygon[1:] + polygon[:1], strict=False
                                    )
                                )
                            )
                            / 2
                        )
                        if box and projected_area > tol * tol:
                            frame = {
                                "bbox": box,
                                "boundary": polygon,
                                "source_kind": "closed_polyline",
                                "source_ref": _source_ref(record, path),
                                "confidence": "candidate",
                                "frame_hint": frame_hints.get(tuple(path)),
                            }
                            if closed:
                                result["frames"].append(frame)
                            elif len(local) == 4:
                                open_rectangles.append((frame, points[0], points[-1], tol))
                elif kind == "CIRCLE":
                    p = point3(record.get("center"))
                    radius = float(record.get("radius") or 0.0)
                    if p is not None and radius > 0:
                        transform = entity_matrix(record, matrix)
                        center = _matrix_apply(transform, p)
                        a = _matrix_apply(transform, (p[0] + radius, p[1], p[2]))
                        b = _matrix_apply(transform, (p[0], p[1] + radius, p[2]))
                        rx = math.hypot(a[0] - center[0], b[0] - center[0])
                        ry = math.hypot(a[1] - center[1], b[1] - center[1])
                        result["frames"].append(
                            {
                                "bbox": (
                                    center[0] - rx,
                                    center[1] - ry,
                                    center[0] + rx,
                                    center[1] + ry,
                                ),
                                "center": center,
                                "radius": max(rx, ry),
                                "source_kind": "circle",
                                "source_ref": _source_ref(record, path),
                                "confidence": "candidate",
                            }
                        )
                elif kind == "INSERT":
                    insertion_handle = _ref(record.get("handle"))
                    for attr in graph["children"].get(insertion_handle, []):
                        if _kind(attr) == "ATTRIB":
                            add_text(attr, matrix, path + [insertion_handle])
                    nested_handle = _ref(record.get("block_header"))
                    nested = graph["blocks"].get(nested_handle)
                    if nested:
                        transform = _matrix_multiply(matrix, _insert_matrix(record, nested))
                        # Extents are auxiliary geometry, never standalone sheet candidates.
                        box = _block_bbox3(nested_handle, graph)
                        if box:
                            world = _bbox(
                                _matrix_apply(transform, (x, y, z))
                                for x in (box[0], box[3])
                                for y in (box[1], box[4])
                                for z in (box[2], box[5])
                            )
                            result["frames"].append(
                                {
                                    "bbox": world,
                                    "source_kind": "block_bbox",
                                    "block_name": str(nested.get("name") or ""),
                                    "source_ref": _source_ref(record, path),
                                    "confidence": "extent_only",
                                }
                            )
                        visit(
                            nested_handle, transform, path + [insertion_handle], stack | {current}
                        )
                elif kind in {"IMAGE", "OLE2FRAME"}:
                    p = point3(record.get("ins_pt")) or point3(record.get("pt0"))
                    if p is not None:
                        result["images"].append(
                            {
                                "point": _matrix_apply(matrix, p),
                                "source_ref": _source_ref(record, path),
                            }
                        )
            except (TypeError, ValueError, OverflowError) as exc:
                result["diagnostics"].append(
                    {"source_ref": _source_ref(record, path), "reason": str(exc)}
                )
        listed = set(graph["members"].get(current, []))
        for record in graph["children"].get(current, []):
            if _ref(record.get("handle")) not in listed and _kind(record) in {
                "TEXT",
                "MTEXT",
                "ATTRIB",
            }:
                try:
                    add_text(record, matrix, path)
                except (TypeError, ValueError, OverflowError) as exc:
                    result["diagnostics"].append(
                        {"source_ref": _source_ref(record, path), "reason": str(exc)}
                    )

    visit(block_handle, IDENTITY, [], frozenset())
    # Preserve the older, specific source locator when a native LINE closes an open U.
    for frame, start, end, tol in open_rectangles:
        for line in result["lines"]:
            if _kind(line["record"]) != "LINE" or line["instance_path"] != frame["source_ref"].get(
                "instance_path", []
            ):
                continue
            if (
                min(
                    max(math.dist(start, line["a"]), math.dist(end, line["b"])),
                    max(math.dist(start, line["b"]), math.dist(end, line["a"])),
                )
                <= tol
            ):
                frame["source_kind"] = "polyline_with_line"
                frame["source_ref"]["closing_line"] = line["source_ref"]
                result["frames"].append(frame)
                break
    result["frames"].extend(line_frames(result["lines"]))
    result["texts"] = list({record["uid"]: record for record in result["texts"]}.values())
    return result


def _select_frames(space):
    return select_frames(space)


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
                    "anchor": position,
                }
            )
    return tables


def _extract_meta(
    records: list[dict[str, Any]],
    summary: dict[str, Any],
    sheet_bbox: tuple[float, float, float, float] | None,
) -> tuple[dict[str, str], set[str]]:
    titleblock, titleblock_uids = extract_titleblock_meta(records, sheet_bbox)
    if titleblock:
        return titleblock, titleblock_uids

    fields = {
        str(key): _clean_text(value)
        for key, value in summary.items()
        if value not in (None, "", [], {})
        and isinstance(value, (str, int, float))
        and str(key).casefold() != "lastsavedby"
        and not str(key).casefold().startswith("unknown")
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
            if _compact_cjk_spacing(value["text"]) in _META_HEADER_LABELS:
                continue
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
    *,
    extract_document_geometry: bool = True,
) -> dict[str, Any]:
    meta, used = _extract_meta(records, summary, sheet_bbox)
    if not extract_document_geometry:
        return {"meta": meta, "tables": [], "markers": []}
    tables = _native_tables(objects, sheet_bbox)
    # ponytail: dense plans contain hundreds of thousands of orthogonal segments;
    # exhaustive grid reconstruction is for document/table sheets, not floor plans.
    if not tables and len(lines) <= 25_000:
        tables = detect_table_grids(
            lines,
            [record for record in records if record["uid"] not in used],
            sheet_bbox=sheet_bbox,
        )
    for table in tables:
        table_uids = table.pop("text_uids", [])
        table["_text_uids"] = table_uids
        used.update(table_uids)
    option_groups = detect_marker_groups(frames, lines, records)
    markers = [marker for group in option_groups for marker in group["markers"]]
    text_uids_by_ref = {
        repr(_source_ref(record["record"], record["instance_path"])): record["uid"]
        for record in records
    }
    for marker in markers:
        marker["_label_uid"] = next(
            (
                text_uids_by_ref[repr(source)]
                for source in reversed(marker["source_refs"])
                if repr(source) in text_uids_by_ref
            ),
            None,
        )
    marker_label_uids = {uid for group in option_groups for uid in group.pop("text_uids", [])}
    used.update(marker_label_uids)
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
                    "_label_uid": record["uid"],
                }
            )
    return {"meta": meta, "tables": tables, "markers": markers}


def _document_column_anchors(
    records: list[dict[str, Any]],
    sheet_bbox: tuple[float, float, float, float] | None,
    excluded_uids: set[str],
) -> list[float]:
    numbered = [
        record
        for record in records
        if record["uid"] not in excluded_uids
        and record.get("point")
        and _NUMBERED_RE.match(record["text"])
    ]
    width = (sheet_bbox[2] - sheet_bbox[0]) if sheet_bbox else 0.0
    heights = [record["height"] for record in records if record.get("height", 0) > 0]
    tolerance = max(statistics.median(heights) * 8 if heights else 0.0, width * 0.04, 1.0)

    def cluster(items: list[dict[str, Any]], minimum: int) -> list[float]:
        groups: list[list[float]] = []
        for x in sorted(record["point"][0] for record in items):
            if not groups or x - groups[-1][-1] > tolerance:
                groups.append([x])
            else:
                groups[-1].append(x)
        return [statistics.median(group) for group in groups if len(group) >= minimum]

    anchors = cluster(numbered, 2)
    if len(anchors) >= 2:
        return anchors
    narrative = [
        record
        for record in records
        if record["uid"] not in excluded_uids
        and record.get("point")
        and len(record["text"].replace("\n", "")) >= 12
    ]
    fallback = cluster(narrative, max(3, math.ceil(len(narrative) * 0.04)))
    if 2 <= len(fallback) <= 6:
        return fallback
    positioned = [record["point"][0] for record in records if record.get("point")]
    return anchors or ([min(positioned)] if positioned else [])


def _boxes_intersect(first: tuple[float, ...], second: tuple[float, ...]) -> bool:
    return not (
        first[2] < second[0] or second[2] < first[0] or first[3] < second[1] or second[3] < first[1]
    )


def _functional_regions(
    candidate: dict[str, Any],
    classifier: RegionClassifier | None,
    renderer: SheetRenderer | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select prose/table areas once; unselected drawing content stays native JSON."""
    sheet_box = candidate.get("bbox")
    if candidate["kind"] != "model_frame" or not sheet_box:
        return [], {"status": "unsupported_sheet"}
    if classifier is None:
        return [], {"status": "unavailable"}
    if renderer is None:
        return [], {"status": "unavailable", "render_error": "sheet_renderer_not_supplied"}
    render_error = None

    def classify(box: tuple[float, ...]) -> dict[str, Any]:
        nonlocal render_error
        try:
            image = renderer(box)
            if not image:
                raise ValueError("empty_sheet_render")
        except Exception as exc:
            # Only renderer errors are retained, never provider response details.
            render_error = str(exc).replace("\n", " ")[:240]
            raise
        result = classifier(
            image,
            {
                "bbox": list(box),
                "sheet_bbox": list(sheet_box),
                "coordinate_system": "CAD world coordinates; x right, y up",
            },
        )
        if isinstance(result, dict) and result.get("is_sheet") is False:
            return {"is_sheet": False}
        values = result.get("regions") if isinstance(result, dict) else None
        if not isinstance(values, list):
            raise ValueError("missing_regions")
        regions = []
        for value in values:
            if not isinstance(value, dict) or value.get("type") not in {
                "document",
                "table",
                "unknown",
            }:
                raise ValueError("invalid_region_type")
            raw_box = value.get("bbox")
            if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
                raise ValueError("invalid_region_bbox")
            region_box = tuple(float(coordinate) for coordinate in raw_box)
            if (
                not all(math.isfinite(coordinate) for coordinate in region_box)
                or _box_area(region_box) <= 0
                or not _contains(box, region_box)
            ):
                raise ValueError("invalid_region_bbox")
            regions.append(
                {
                    "type": value["type"],
                    "bbox": list(region_box),
                    "title": str(value.get("title") or ""),
                }
            )
        return {"regions": regions}

    try:
        result = classify(tuple(sheet_box))
    except Exception as exc:
        diagnostic = {"status": "failed", "error": type(exc).__name__}
        if render_error:
            diagnostic["render_error"] = render_error
        return [], diagnostic
    if result.get("is_sheet") is False:
        return [], {"status": "not_sheet"}
    regions = result["regions"]
    for index, region in enumerate(regions, 1):
        region["id"] = f"R{index:03d}"
    diagnostic = {"status": "used", "region_count": len(regions)}
    return regions, diagnostic


def _functional_documents(
    regions: list[dict[str, Any]],
    records: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Select only text/table instances uniquely inside a prose region."""
    selected: dict[str, set[str]] = defaultdict(set)
    ambiguous = False
    for record in records:
        matches = [region for region in regions if _inside(region["bbox"], record["point"])]
        if len(matches) > 1:
            ambiguous = True
        elif matches and matches[0]["type"] in {"document", "table"}:
            selected[matches[0]["id"]].add(record["uid"])
    documents = []
    for region in regions:
        if region["type"] not in {"document", "table"}:
            continue
        uids = selected[region["id"]]
        table_indexes = []
        for index, table in enumerate(tables):
            box, anchor = table.get("bbox"), table.get("anchor")
            inside = _contains(region["bbox"], box) if box else _inside(region["bbox"], anchor)
            if not inside or not set(table.get("_text_uids", [])) <= uids:
                continue
            # Native tables may have only an insertion point, without TEXT records.
            matches = [
                other
                for other in regions
                if (_boxes_intersect(other["bbox"], box) if box else _inside(other["bbox"], anchor))
            ]
            if len(matches) == 1:
                table_indexes.append(index)
        documents.append(
            {
                "region_id": region["id"],
                "bbox": region["bbox"],
                "scope": "local",
                "record_uids": uids,
                "table_indexes": table_indexes,
            }
        )
    return documents, ambiguous


def _document_column_cuts(
    records: list[dict[str, Any]], anchors: list[float], excluded_uids: set[str]
) -> list[float]:
    xs = sorted(
        {
            record["point"][0]
            for record in records
            if record["uid"] not in excluded_uids and record.get("point")
        }
    )
    cuts: list[float] = []
    for left, right in zip(anchors, anchors[1:], strict=False):
        between = [x for x in xs if left <= x <= right]
        if len(between) < 2:
            cuts.append((left + right) / 2)
            continue
        gap_left, gap_right = max(
            zip(between, between[1:], strict=False), key=lambda pair: pair[1] - pair[0]
        )
        # Keep right-aligned form values with the column that owns their template.
        cuts.append(max((gap_left + gap_right) / 2, right - (right - left) * 0.1))
    return cuts


def _form_raw_text(event: dict[str, Any]) -> str | None:
    record = event.get("record")
    if not isinstance(record, dict) or _kind(record) != "TEXT":
        return None
    try:
        rotation = float(record.get("rotation") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(rotation) or abs(rotation) > 1e-6:
        return None
    value = record.get("text_value")
    if not isinstance(value, str) or "\n" in value or "\r" in value or "\\P" in value:
        return None
    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda match: chr(int(match.group(1), 16)), text)
    text = _decode_text_codes(text)
    text = _FORMAT_RE.sub("", text).replace("{", "").replace("}", "")
    return re.sub(r"[\t\x00-\x1f\x7f]+", " ", text)


def _form_advance(text: str, height: float, width_factor: float) -> float:
    # ponytail: approximate SHX advances; use real font metrics only if fail-closed misses matter.
    unit = max(height * width_factor, 1e-6)
    result = 0.0
    for char in text:
        if char == " ":
            result += unit * 0.5
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            result += unit
        elif unicodedata.category(char).startswith("P"):
            result += unit * 0.5
        else:
            result += unit * 0.6
    return result


def _roman_i_glyph(segments: list[dict[str, Any]], height: float) -> str | None:
    """Recognize only the exploded I/II/III shape observed in T0202.

    This is a temporary, sample-specific bridge—not a general exploded-text
    recognizer and not a pattern to extend with more hard-coded glyphs. If a
    second exploded-font family appears, replace this path with generic text-row
    candidate detection plus SHX template matching or local OCR/VLM.
    """
    # ponytail: T0202-only bridge; replace it instead of adding another glyph family.
    vertical = []
    horizontal = []
    for segment in segments:
        a, b = segment["a"], segment["b"]
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if dx <= height * 0.05 and height * 0.45 <= dy <= height * 1.1:
            vertical.append(segment)
        elif dy <= height * 0.05 and height * 0.08 <= dx <= height * 0.5:
            horizontal.append(segment)
        else:
            return None
    if not 1 <= len(vertical) <= 3 or len(horizontal) != len(vertical) * 2:
        return None

    consumed: set[int] = set()
    glyphs = []
    for stem in sorted(vertical, key=lambda line: (line["a"][0] + line["b"][0]) / 2):
        x = (stem["a"][0] + stem["b"][0]) / 2
        bottom, top = sorted((stem["a"][1], stem["b"][1]))
        caps = []
        for index, cap in enumerate(horizontal):
            if index in consumed:
                continue
            left, right = sorted((cap["a"][0], cap["b"][0]))
            y = (cap["a"][1] + cap["b"][1]) / 2
            if (
                left - height * 0.05 <= x <= right + height * 0.05
                and min(abs(y - bottom), abs(y - top)) <= height * 0.06
            ):
                caps.append((index, y))
        if len(caps) != 2 or abs(caps[0][1] - caps[1][1]) < height * 0.4:
            return None
        consumed.update(index for index, _ in caps)
        glyphs.append((x, bottom, top))
    if len(consumed) != len(horizontal):
        return None
    first = glyphs[0]
    if any(
        abs(bottom - first[1]) > height * 0.08
        or abs(top - first[2]) > height * 0.08
        or not 0 <= x - previous[0] <= height * 0.35
        for previous, (x, bottom, top) in zip(glyphs, glyphs[1:], strict=False)
    ):
        return None
    return {1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ"}[len(glyphs)]


def _exploded_form_glyphs(
    records: list[dict[str, Any]], lines: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply the temporary T0202 Roman-numeral patch inside explicit form slots.

    Slot containment keeps this workaround from pretending that arbitrary CAD
    linework is text. Do not use its success as evidence of general exploded-text
    support; another case should trigger a redesign, not another glyph branch.
    """
    result = []
    used: set[str] = set()
    for record in records:
        if record.get("instance_path") or not record.get("point") or record.get("height", 0) <= 0:
            continue
        event = {
            "record": record["record"],
            "point": record["point"],
            "height": record["height"],
        }
        raw = _form_raw_text(event)
        slots = list(_FORM_SLOT_RE.finditer(raw or ""))
        if not raw or not slots:
            continue
        try:
            width_factor = float(record["record"].get("width_factor") or 1.0)
        except (TypeError, ValueError, OverflowError):
            continue
        for slot_number, slot in enumerate(slots, 1):
            height = float(record["height"])
            left = record["point"][0] + _form_advance(raw[: slot.start()], height, width_factor)
            right = record["point"][0] + _form_advance(raw[: slot.end()], height, width_factor)
            if any(
                other["uid"] != record["uid"]
                and other.get("point")
                and abs(other["point"][1] - record["point"][1]) <= height * 0.2
                and left - height * 0.5 <= other["point"][0] <= right + height * 0.5
                for other in records
            ):
                continue
            candidates = [
                line
                for line in lines
                if repr(line["source_ref"]) not in used
                and not line.get("instance_path")
                and left - height * 0.5
                <= min(line["a"][0], line["b"][0])
                <= max(line["a"][0], line["b"][0])
                <= right + height * 0.5
                and record["point"][1] - height * 0.1
                <= min(line["a"][1], line["b"][1])
                <= max(line["a"][1], line["b"][1])
                <= record["point"][1] + height * 1.1
            ]
            if not candidates or len({line.get("layer") for line in candidates}) != 1:
                continue
            glyph = _roman_i_glyph(candidates, height)
            if not glyph:
                continue
            refs = [line["source_ref"] for line in candidates]
            used.update(map(repr, refs))
            result.append(
                {
                    "kind": "text",
                    "uid": f"glyph:{record['uid']}:{slot_number}",
                    "text": glyph,
                    "point": (
                        min(min(line["a"][0], line["b"][0]) for line in candidates),
                        record["point"][1],
                    ),
                    "height": height,
                    "layer": candidates[0].get("layer") or "",
                    "record": {"entity": "GEOMETRY_GLYPH"},
                    "form_fill_eligible": True,
                    "source_refs": refs,
                }
            )
    return result


def _restore_form_slots(group: list[dict[str, Any]]) -> str:
    """Restore independent TEXT values into explicit whitespace slots, or do nothing."""
    if len(group) < 2:
        return _join_fragments([item["text"] for item in group])
    templates: list[dict[str, Any]] = []
    template_indexes: set[int] = set()
    for index, item in enumerate(group):
        raw = _form_raw_text(item)
        slots = list(_FORM_SLOT_RE.finditer(raw or ""))
        if not raw or not slots or not item.get("form_fill_eligible", True):
            continue
        record = item["record"]
        try:
            height = float(item.get("height") or 0.0)
            width_factor = float(record.get("width_factor") or 1.0)
        except (TypeError, ValueError, OverflowError):
            continue
        if height <= 0 or width_factor <= 0:
            continue
        template_indexes.add(index)
        templates.append(
            {
                "index": index,
                "item": item,
                "raw": raw,
                "height": height,
                "width_factor": width_factor,
                "slots": slots,
            }
        )
    if not templates:
        return _join_fragments([item["text"] for item in group])
    templates_by_index = {template["index"]: template for template in templates}

    slot_specs = []
    for template in templates:
        item, raw = template["item"], template["raw"]
        for match in template["slots"]:
            slot_specs.append(
                {
                    "template": template,
                    "span": match.span(),
                    "left": item["point"][0]
                    + _form_advance(
                        raw[: match.start()], template["height"], template["width_factor"]
                    ),
                    "right": item["point"][0]
                    + _form_advance(
                        raw[: match.end()], template["height"], template["width_factor"]
                    ),
                }
            )
    slot_specs.sort(key=lambda slot: slot["left"])
    for index, slot in enumerate(slot_specs):
        center = (slot["left"] + slot["right"]) / 2
        slot["partition_left"] = (
            -math.inf
            if index == 0
            else ((slot_specs[index - 1]["left"] + slot_specs[index - 1]["right"]) / 2 + center) / 2
        )
        slot["partition_right"] = (
            math.inf
            if index + 1 == len(slot_specs)
            else (center + (slot_specs[index + 1]["left"] + slot_specs[index + 1]["right"]) / 2) / 2
        )

    def matches(slot: dict[str, Any], candidate: dict[str, Any]) -> bool:
        template = slot["template"]
        if (
            not candidate.get("form_fill_eligible", True)
            or not candidate.get("layer")
            or not template["item"].get("layer")
        ):
            return False
        try:
            candidate_height = float(candidate.get("height") or 0.0)
        except (TypeError, ValueError, OverflowError):
            return False
        if candidate_height <= 0:
            return False
        height = template["height"]
        if not 0.6 <= candidate_height / height <= 1.6:
            return False
        if (
            abs(candidate["point"][1] - template["item"]["point"][1])
            > max(height, candidate_height) * 0.2
        ):
            return False
        margin = max(height, candidate_height) * (
            0.35 if candidate["layer"] == template["item"].get("layer") else 3.0
        )
        record = candidate.get("record") or {}
        try:
            candidate_width = _form_advance(
                candidate["text"], candidate_height, float(record.get("width_factor") or 1.0)
            )
        except (TypeError, ValueError, OverflowError):
            return False
        if candidate_width > slot["right"] - slot["left"] + margin * 2:
            return False
        boundary_margin = max(height, candidate_height) * 0.1
        x = candidate["point"][0]
        return (
            slot["left"] - margin <= x <= slot["right"] + margin
            and slot["partition_left"] + boundary_margin < x
            and x < slot["partition_right"] - boundary_margin
        )

    candidates = [
        item
        for index, item in enumerate(group)
        if index not in template_indexes and any(matches(slot, item) for slot in slot_specs)
    ]
    candidates.sort(key=lambda item: item["point"][0])
    compatibility = [
        [index for index, slot in enumerate(slot_specs) if matches(slot, candidate)]
        for candidate in candidates
    ]
    mapped_slots = [indexes[0] for indexes in compatibility if len(indexes) == 1]
    if (
        not candidates
        or any(len(indexes) != 1 for indexes in compatibility)
        or len(set(mapped_slots)) != len(mapped_slots)
    ):
        return _join_fragments([item["text"] for item in group])

    assignments: dict[int, list[tuple[tuple[int, int], str]]] = defaultdict(list)
    used = {id(candidate) for candidate in candidates}
    for candidate, indexes in zip(candidates, compatibility, strict=True):
        slot = slot_specs[indexes[0]]
        assignments[slot["template"]["index"]].append((slot["span"], candidate["text"]))
    values = []
    for index, item in enumerate(group):
        if id(item) in used:
            continue
        text = item["text"]
        if index in assignments:
            text = templates_by_index[index]["raw"]
            for (start, end), value in sorted(assignments[index], reverse=True):
                value = value.strip()
                replacement = (
                    value
                    if _CJK_RE.fullmatch(text[start - 1]) and _CJK_RE.fullmatch(text[end])
                    else f" {value} "
                )
                text = f"{text[:start]}{replacement}{text[end:]}"
            text = _clean_text(text)
        values.append(text)
    return _join_fragments(values)


def _join_fragments(values: list[str]) -> str:
    result = ""
    for value in values:
        value = value.strip()
        if not value:
            continue
        if not result or result[-1] in "（(【[、/" or value[0] in "，。；：、）)】]！？,.!?;:%‰":
            result += value
        else:
            result += " " + value
    return result


def _document_text(text: str) -> str:
    compact = text.strip()
    compact = re.sub(
        r"((?:kN|N|kg)/m(?:m)?)\s+([23])\b",
        lambda match: match.group(1) + {"2": "²", "3": "³"}[match.group(2)],
        compact,
        flags=re.I,
    )
    if len(compact) <= 24 and re.search(r"(?:总说明|设计说明)$", compact):
        return f"## {compact}"
    return compact


def _marker_prefix(marker: dict[str, Any]) -> str:
    return (
        "[x]"
        if marker["state"] == "selected"
        else "[ ]"
        if marker["state"] == "unselected"
        else "[?]"
    )


def _document_flow(
    records: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    excluded_uids: set[str],
    anchors: list[float],
    markers: list[dict[str, Any]] | None = None,
    lines: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    table_uids = {uid for table in tables for uid in table.get("_text_uids", [])}
    excluded = excluded_uids | table_uids
    markers_by_uid = {
        marker["_label_uid"]: marker
        for marker in markers or []
        if marker.get("_label_uid") is not None
    }
    records_by_uid = {record["uid"]: record for record in records}
    source_keys_by_uid = {
        record["uid"]: repr(_source_ref(record["record"], record["instance_path"]))
        for record in records
    }
    inlined_marker_uids: set[str] = set()
    events: list[dict[str, Any]] = []
    for record in records:
        if record["uid"] in excluded or not record.get("point"):
            continue
        text = record["text"]
        if marker := markers_by_uid.get(record["uid"]):
            text = f"{_marker_prefix(marker)} {marker['label'] or text}"
            inlined_marker_uids.add(record["uid"])
        events.append(
            {
                "kind": "text",
                "point": record["point"],
                "height": record.get("height", 0.0),
                "text": text,
                "uid": record["uid"],
                "record": record["record"],
                "layer": record.get("layer") or "",
                # ponytail: nested INSERT metrics are unscaled here; fail closed until normalized.
                "form_fill_eligible": marker is None and not record["instance_path"],
                "source_refs": [_source_ref(record["record"], record["instance_path"])],
            }
        )
    events.extend(
        _exploded_form_glyphs(
            [record for record in records if record["uid"] not in excluded], lines or []
        )
    )
    for fallback_index, table in enumerate(tables, 1):
        index = int(table.get("_table_index", fallback_index))
        box, anchor = table.get("bbox"), table.get("anchor")
        if anchor and len(anchor) >= 2:
            point = (float(anchor[0]), float(anchor[1]))
        elif box and len(box) == 4:
            point = (float(box[0]), float(box[3]))
        else:
            point = (anchors[0] if anchors else 0.0, -math.inf)
        title = table["title"]
        rows = [row[:] for row in table["rows"]]
        for cell, refs in table.get("cell_source_refs", {}).items():
            source_keys = {repr(ref) for ref in refs}
            row, column = map(int, cell.split(":"))
            for uid, marker in markers_by_uid.items():
                if uid in inlined_marker_uids or source_keys_by_uid.get(uid) not in source_keys:
                    continue
                prefix = _marker_prefix(marker)
                label = marker.get("label") or ""
                value = rows[row][column]
                raw = records_by_uid.get(uid, {}).get("text", "")
                glyph = _MARKER_RE.match(raw)
                replacement = f"{prefix} {label or (glyph.group(2) if glyph else raw)}".strip()
                if glyph and raw in value:
                    rows[row][column] = value.replace(raw, replacement, 1)
                elif label and label in value:
                    rows[row][column] = value.replace(label, f"{prefix} {label}", 1)
                else:
                    rows[row][column] = f"{prefix} {value}".strip()
                inlined_marker_uids.add(uid)
        markdown = _table_markdown(rows, header_rows=table["header_rows"], spans=table["spans"])
        if title and title != "几何表格":
            markdown = f"{_document_text(title)}\n\n{markdown}"
        events.append(
            {
                "kind": "table",
                "point": point,
                "height": 0.0,
                "text": markdown,
                "table_index": index,
                "source_refs": table["source_refs"],
            }
        )
    for uid, marker in markers_by_uid.items():
        if (
            uid in inlined_marker_uids
            or not (record := records_by_uid.get(uid))
            or not record.get("point")
        ):
            continue
        events.append(
            {
                "kind": "text",
                "point": record["point"],
                "height": record.get("height", 0.0),
                "text": f"{_marker_prefix(marker)} {marker['label'] or record['text']}",
                "form_fill_eligible": False,
                "source_refs": [_source_ref(record["record"], record["instance_path"])],
            }
        )
    if not events:
        return [], []
    anchors = anchors or [min(event["point"][0] for event in events)]
    cuts = _document_column_cuts(records, anchors, excluded_uids)
    columns: list[list[dict[str, Any]]] = [[] for _ in anchors]
    for event in events:
        column = bisect_right(cuts, event["point"][0])
        columns[column].append(event)

    blocks: list[str] = []
    flow: list[dict[str, Any]] = []
    for column in columns:
        ordered = sorted(column, key=lambda item: (-item["point"][1], item["point"][0]))
        index = 0
        while index < len(ordered):
            event = ordered[index]
            if event["kind"] == "table":
                group = [event]
            else:
                group = [event]
                while (
                    index + len(group) < len(ordered)
                    and ordered[index + len(group)]["kind"] == "text"
                    and abs(ordered[index + len(group)]["point"][1] - event["point"][1])
                    <= max(
                        float(event.get("height") or 0.0),
                        float(ordered[index + len(group)].get("height") or 0.0),
                        1e-6,
                    )
                    * 0.55
                ):
                    group.append(ordered[index + len(group)])
                group.sort(key=lambda item: item["point"][0])
            text = (
                event["text"]
                if event["kind"] == "table"
                else _document_text(_restore_form_slots(group))
            )
            blocks.append(text)
            source_refs = [ref for item in group for ref in item["source_refs"]]
            item = {"order": len(flow) + 1, "kind": event["kind"], "source_refs": source_refs}
            if event["kind"] == "table":
                item["table_id"] = f"T{event['table_index']:03d}"
            flow.append(item)
            index += len(group)
    return blocks, flow


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
        column for column in range(width) if header[column] or any(row[column] for row in body)
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
    if sheet.get("layout_name"):
        meta.setdefault("布局", sheet["layout_name"])
    sections: list[tuple[str, str]] = [
        ("图纸元数据", "\n".join(f"- {key}：{value}" for key, value in meta.items()))
    ]
    if sheet["document_blocks"]:
        sections.append(("说明", "\n\n".join(sheet["document_blocks"])))
    return sections


def _tokens(text: str) -> int:
    cjk = len(_CJK_RE.findall(text))
    return int(cjk * 0.7 + (len(_NONSPACE_RE.findall(text)) - cjk) * 0.3)


def _fits(text: str, max_tokens: int, max_chars: int) -> bool:
    return len(text) <= max_chars and _tokens(text) <= max_tokens


def _split_body(heading: str, body: str, prefix: str, max_tokens: int, max_chars: int) -> list[str]:
    lead = f"{prefix}\n\n" + (f"## {heading}\n\n" if heading else "")
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
        len(repeated) > max_chars // 2 or not _fits(f"{lead}{repeated}", max_tokens, max_chars)
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


def _sheet_id(source_sha256: str, locator: str) -> str:
    return "sheet-" + hashlib.sha256(f"{source_sha256}:{locator}".encode()).hexdigest()[:12]


def build_sheet_artifacts(
    drawing: dict[str, Any],
    *,
    source_name: str,
    source_sha256: str,
    source_size_bytes: int,
    max_section_tokens: int | None = None,
    max_section_chars: int | None = None,
    region_classifier: RegionClassifier | None = None,
    sheet_renderer: SheetRenderer | None = None,
    max_concurrent_regions: int = 1,
) -> dict[str, Any]:
    """Build Sheet Markdown from VLM-selected regions, preserving unresolved text."""
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
    geometry_diagnostics = []

    def add_frames(space, frames, space_kind, block_handle, layout=None):
        for frame in frames:
            # Reuse the boundary tolerance for all objects tested against this frame.
            frame = {**frame, "_tolerance": _frame_tolerance(frame["boundary"])}
            source_ref = frame["source_ref"]
            locator = f"frame:{source_ref.get('handle')}:{'/'.join(map(str, source_ref.get('instance_path', [])))}"
            if frame["source_kind"] == "line_frame":
                # Several adjacent frames may share the same long source LINE.
                signature = repr((frame["boundary"], frame.get("boundary_sources", [])))
                locator += ":" + hashlib.sha256(signature.encode()).hexdigest()[:16]
            source_locator = {
                "kind": frame["source_kind"],
                "source_ref": source_ref,
                "space_kind": space_kind,
                "block_handle": block_handle,
            }
            if layout is not None:
                source_locator["layout_handle"] = _ref(layout.get("handle"))
                locator = f"layout:{source_locator['layout_handle']}:" + locator
            candidates.append(
                {
                    "kind": "model_frame" if space_kind == "model" else "layout_frame",
                    "layout_name": None
                    if layout is None
                    else str(layout.get("layout_name") or layout.get("name") or "Layout"),
                    "locator": locator,
                    "source_locator": source_locator,
                    "source_refs": frame.get("boundary_sources", [source_ref]),
                    "texts": [r for r in space["texts"] if _inside_frame(frame, r["point"])],
                    "objects": space["objects"],
                    "geometry_frames": [
                        g for g in space["frames"] if _frame_contains_box(frame, g["bbox"])
                    ],
                    "lines": [
                        line
                        for line in space["lines"]
                        if _inside_frame(frame, line["a"]) and _inside_frame(frame, line["b"])
                    ],
                    "bbox": frame["bbox"],
                    "boundary": frame["boundary"],
                    "boundary_sources": frame.get("boundary_sources", [source_ref]),
                    "detection_evidence": frame.get("detection_evidence", {}),
                    "viewport_projection": "unresolved"
                    if layout is not None and any(_kind(r) == "VIEWPORT" for r in space["objects"])
                    else "not_applicable",
                }
            )

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
        viewports = sorted(
            {
                _ref(r.get("handle"))
                for r in space["objects"]
                if _kind(r) == "VIEWPORT" and _ref(r.get("handle")) is not None
            },
            key=str,
        )
        meaningful = [
            r
            for r in space["objects"]
            if _kind(r) not in {"BLOCK", "ENDBLK", "SEQEND", "VIEWPORT", "DICTIONARY"}
            and r.get("entity")
        ]
        if not space["texts"] and not meaningful:
            geometry_diagnostics.append(
                {
                    "space_kind": "paper",
                    "block_handle": block_handle,
                    "layout_handle": _ref(layout.get("handle")),
                    "reason": "viewport_only_or_empty_layout",
                    "viewport_handles": viewports,
                }
            )
            continue
        selected = _select_frames(space)
        # Paper-space table cells and loose title strips have real rectangular
        # edges too. Without sheet identity evidence or viewport projection they
        # cannot establish independent sheets; retain the layout as unresolved.
        frames = [f for f in selected if f["detection_evidence"]["identity_supported"]]
        for frame in selected:
            if not frame["detection_evidence"]["identity_supported"]:
                geometry_diagnostics.append(
                    {
                        "space_kind": "paper",
                        "block_handle": block_handle,
                        "reason": "paper_boundary_without_sheet_identity",
                        "bbox": list(frame["bbox"]),
                        "source_ref": frame["source_ref"],
                    }
                )
        add_frames(space, frames, "paper", block_handle, layout)
        geometry_diagnostics.extend(space["diagnostics"])
        if not frames:
            candidates.append(
                {
                    "kind": "layout_unframed",
                    "layout_name": name,
                    "locator": f"layout:{_ref(layout.get('handle'))}:{block_handle}:unframed",
                    "source_locator": {
                        "kind": "layout",
                        "layout_handle": _ref(layout.get("handle")),
                        "block_handle": block_handle,
                        "viewport_handles": viewports,
                        "space_kind": "paper",
                    },
                    "source_refs": [_source_ref(layout, [])],
                    "texts": space["texts"],
                    "objects": space["objects"],
                    "geometry_frames": [],
                    "lines": [],
                    "viewport_projection": "unresolved" if viewports else "not_applicable",
                }
            )
    for block_handle in sorted(model_blocks, key=str):
        if block_handle not in graph["blocks"]:
            continue
        space = _walk_block(block_handle, graph)
        frames = _select_frames(space)
        add_frames(space, frames, "model", block_handle)
        geometry_diagnostics.extend(space["diagnostics"])
        if space["texts"] and not frames:
            candidates.append(
                {
                    "kind": "model_unframed",
                    "layout_name": "Model",
                    "locator": f"model:{block_handle}:unframed",
                    "source_locator": {
                        "kind": "model_space",
                        "block_handle": block_handle,
                        "space_kind": "model",
                    },
                    "source_refs": [{"block_handle": block_handle}],
                    "texts": space["texts"],
                    "objects": space["objects"],
                    "geometry_frames": [],
                    "lines": [],
                }
            )
    with ThreadPoolExecutor(
        max_workers=max(1, min(max_concurrent_regions, len(candidates)))
    ) as pool:
        pending = [
            pool.submit(
                copy_context().run,
                _functional_regions,
                candidate,
                region_classifier,
                sheet_renderer,
            )
            for candidate in candidates
        ]
        for candidate, future in zip(candidates, pending, strict=True):
            candidate["_functional_regions"], candidate["_region_diagnostic"] = future.result()
    candidate_diagnostics = [
        {
            "kind": c["kind"],
            "source_locator": c["source_locator"],
            "bbox": c.get("bbox"),
            "region_diagnostic": c["_region_diagnostic"],
        }
        for c in candidates
    ]
    candidates = [
        candidate
        for candidate in candidates
        if candidate["_region_diagnostic"]["status"] != "not_sheet"
    ]
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
                "_functional_regions": [],
                "_region_diagnostic": {"status": "unsupported_sheet"},
            }
        )
    sheets = []
    for ordinal, candidate in enumerate(candidates, 1):
        sheet_id = _sheet_id(source_sha256, candidate["locator"])
        functional_regions = candidate.pop("_functional_regions")
        classified = _classify_sheet(
            candidate["texts"],
            candidate["objects"],
            summary,
            candidate.get("geometry_frames", []),
            candidate.get("lines", []),
            candidate.get("bbox"),
            extract_document_geometry=any(
                region["type"] in {"document", "table"} for region in functional_regions
            ),
        )
        fallback = candidate.get("layout_name") or f"Sheet {ordinal:03d}"
        title = next(
            (
                classified["meta"][key]
                for key in ("图纸名称", "图名")
                if classified["meta"].get(key)
            ),
            fallback,
        )
        region_diagnostic = candidate.pop("_region_diagnostic")
        document_regions, ambiguous = _functional_documents(
            functional_regions, candidate["texts"], classified["tables"]
        )
        partition_status = (
            "partial"
            if region_diagnostic["status"] != "used"
            or ambiguous
            or any(region["type"] == "unknown" for region in functional_regions)
            else "complete"
        )
        slug = VikingURI.sanitize_segment(title) or f"sheet-{ordinal:03d}"
        directory = f"sheets/{sheet_id}__{slug}"
        sheet = {
            **candidate,
            "sheet_id": sheet_id,
            "title": title,
            "directory": directory,
            "source_name": source_name,
            "source_sha256": source_sha256,
        }
        blocks: list[str] = []
        flow: list[dict[str, Any]] = []
        emitted_uids: set[str] = set()
        emitted_tables: set[int] = set()
        for region in document_regions:
            region_uids = region["record_uids"] - emitted_uids
            region_table_indexes = [
                index for index in region["table_indexes"] if index not in emitted_tables
            ]
            emitted_uids.update(region_uids)
            emitted_tables.update(region_table_indexes)
            region_tables = [
                {**classified["tables"][index], "_table_index": index + 1}
                for index in region_table_indexes
            ]
            region_table_uids = {
                uid for table in region_tables for uid in table.get("_text_uids", [])
            }
            region_records = [
                record
                for record in candidate["texts"]
                if record["uid"] in region_uids | region_table_uids
            ]
            anchors = _document_column_anchors(region_records, region["bbox"], set())
            region_lines = [
                line
                for line in candidate.get("lines", [])
                if _inside(region["bbox"], line["a"]) and _inside(region["bbox"], line["b"])
            ]
            region_blocks, region_flow = _document_flow(
                region_records,
                region_tables,
                set(),
                anchors,
                classified["markers"],
                region_lines,
            )
            blocks.extend(region_blocks)
            for item in region_flow:
                flow.append({**item, "order": len(flow) + 1, "region_id": region["region_id"]})
        sheet["document_blocks"] = blocks
        parts = _bounded_markdown(
            title, sheet_id, _sections(classified, sheet), max_tokens, max_chars
        )
        names = (
            ["context.md"]
            if len(parts) == 1
            else [f"context_{index:03d}.md" for index in range(1, len(parts) + 1)]
        )
        content_paths = [f"{directory}/{name}" for name in names]
        source_keys = {repr(ref) for item in flow for ref in item["source_refs"]}
        markdown_uids = [
            record["uid"]
            for record in candidate["texts"]
            if repr(_source_ref(record["record"], record["instance_path"])) in source_keys
        ]
        sheets.append(
            {
                "sheet_id": sheet_id,
                "directory": directory,
                "title": title,
                "content_paths": content_paths,
                "content_files": list(zip(names, parts, strict=True)),
                "region_diagnostic": region_diagnostic,
                "kind": candidate["kind"],
                "source_locator": candidate["source_locator"],
                "bbox": list(candidate["bbox"]) if candidate.get("bbox") else None,
                "boundary": candidate.get("boundary"),
                "boundary_sources": candidate.get("boundary_sources", []),
                "detection_evidence": candidate.get("detection_evidence", {}),
                "viewport_projection": candidate.get("viewport_projection", "not_applicable"),
                "regions": functional_regions,
                "_texts": candidate["texts"],
                "markdown_uids": markdown_uids,
                "status": partition_status,
            }
        )
    bounded_kinds = {"model_frame", "layout_frame"}
    statistics = {
        "candidate_count": sum(c["kind"] in bounded_kinds for c in candidate_diagnostics),
        "detected_sheet_count": sum(s["kind"] in bounded_kinds for s in sheets),
        "rejected_candidate_count": sum(
            c["region_diagnostic"]["status"] == "not_sheet" for c in candidate_diagnostics
        ),
        "unframed_bundle_count": sum(
            s["kind"] in {"model_unframed", "layout_unframed"} for s in sheets
        ),
        "synthetic_fallback_count": sum(s["kind"] == "synthetic" for s in sheets),
        "boundary_only_sheet_count": sum(
            s["kind"] in bounded_kinds
            and not s.get("detection_evidence", {}).get("identity_supported")
            for s in sheets
        ),
        "sheet_bundle_count": len(sheets),
    }
    document_id = "dwg-" + source_sha256[:16]
    document = {
        "schema": SHEET_DOCUMENT_SCHEMA,
        "document_id": document_id,
        "source": {"file": source_name, "sha256": source_sha256, "size_bytes": source_size_bytes},
        "summary_info": summary,
        "sheet_count": statistics["detected_sheet_count"],
        "sheet_bundle_count": len(sheets),
        "detection_statistics": statistics,
        "candidate_diagnostics": candidate_diagnostics,
        "geometry_diagnostics": geometry_diagnostics,
        "sheets": [
            {
                key: sheet[key]
                for key in (
                    "sheet_id",
                    "directory",
                    "title",
                    "content_paths",
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
            "sheet_text_instances": sum(len(sheet["_texts"]) for sheet in sheets),
            "fallback_sheet_used": any(sheet["kind"] == "synthetic" for sheet in sheets),
        },
    }
    return {"document": document, "sheets": sheets}
