# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deterministic table and option-marker geometry for DWG Sheet parsing."""

from __future__ import annotations

import math
import statistics
import unicodedata
from collections import defaultdict
from typing import Any, Iterable


def _point(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        point = float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    return point if all(math.isfinite(coordinate) for coordinate in point) else None


def _handle(value: Any) -> Any:
    return value[-1] if isinstance(value, list) and value else value


def _source_ref(item: dict[str, Any]) -> dict[str, Any]:
    if isinstance(item.get("source_ref"), dict):
        return dict(item["source_ref"])
    record = item.get("record") if isinstance(item.get("record"), dict) else item
    result = {
        "object_index": record.get("index"),
        "handle": _handle(record.get("handle")),
    }
    path = item.get("instance_path")
    if path:
        result["instance_path"] = list(path)
    return {key: value for key, value in result.items() if value is not None}


def _unique_refs(refs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = repr(ref)
        if ref and key not in seen:
            result.append(ref)
            seen.add(key)
    return result


def _bbox_area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _contains(
    box: tuple[float, float, float, float],
    point: tuple[float, float],
    tolerance: float = 0.0,
) -> bool:
    return (
        box[0] - tolerance <= point[0] <= box[2] + tolerance
        and box[1] - tolerance <= point[1] <= box[3] + tolerance
    )


def _bbox_distance(
    box: tuple[float, float, float, float], point: tuple[float, float]
) -> float:
    return math.hypot(
        max(box[0] - point[0], 0.0, point[0] - box[2]),
        max(box[1] - point[1], 0.0, point[1] - box[3]),
    )


def _text_margin(text: dict[str, Any], tolerance: float) -> float:
    height = text.get("height")
    return max(
        tolerance,
        float(height) * 0.5 if isinstance(height, (int, float)) and height > 0 else 0.0,
    )


def _join_text(parts: Iterable[str]) -> str:
    result = ""
    for part in (part.strip() for part in parts):
        if not part:
            continue
        if result and not (
            unicodedata.category(result[-1]).startswith("P")
            or unicodedata.category(part[0]).startswith("P")
        ):
            result += " "
        result += part
    return result


def _cluster(values: Iterable[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - statistics.mean(groups[-1]) <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [statistics.mean(group) for group in groups]


def _nearest(value: float, levels: list[float]) -> float:
    return min(levels, key=lambda level: abs(level - value))


def _segments(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept exploded segments and the occasional unexpanded polyline."""
    result: list[dict[str, Any]] = []
    for record in records:
        a, b = _point(record.get("a")), _point(record.get("b"))
        pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
        if a and b:
            pairs.append((a, b))
        else:
            points = [point for value in record.get("points") or [] if (point := _point(value))]
            pairs.extend(zip(points, points[1:], strict=False))
            if record.get("closed") and len(points) > 2:
                pairs.append((points[-1], points[0]))
        for start, end in pairs:
            if math.dist(start, end) > 1e-12:
                result.append({**record, "a": start, "b": end, "source_ref": _source_ref(record)})
    return result


def _tolerance(segments: list[dict[str, Any]], texts: list[dict[str, Any]]) -> float:
    points = [segment[key] for segment in segments for key in ("a", "b")]
    span = max(
        max((point[0] for point in points), default=0.0)
        - min((point[0] for point in points), default=0.0),
        max((point[1] for point in points), default=0.0)
        - min((point[1] for point in points), default=0.0),
        1.0,
    )
    heights = [
        float(text["height"])
        for text in texts
        if isinstance(text.get("height"), (int, float)) and text["height"] > 0
    ]
    return max(span * 1e-6, (statistics.median(heights) * 0.02 if heights else 0.0), 1e-7)


def _axis_lines(
    records: list[dict[str, Any]], tolerance: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_horizontal: list[dict[str, Any]] = []
    raw_vertical: list[dict[str, Any]] = []
    for record in records:
        a, b = record["a"], record["b"]
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        length = math.hypot(dx, dy)
        item = {"source_refs": [record["source_ref"]]}
        if dy <= max(tolerance, length * 0.01):
            raw_horizontal.append(
                {
                    **item,
                    "level": (a[1] + b[1]) / 2,
                    "start": min(a[0], b[0]),
                    "end": max(a[0], b[0]),
                }
            )
        elif dx <= max(tolerance, length * 0.01):
            raw_vertical.append(
                {
                    **item,
                    "level": (a[0] + b[0]) / 2,
                    "start": min(a[1], b[1]),
                    "end": max(a[1], b[1]),
                }
            )

    def merge(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not lines:
            return []
        levels = _cluster((line["level"] for line in lines), tolerance)
        grouped: dict[float, list[dict[str, Any]]] = defaultdict(list)
        for line in lines:
            grouped[_nearest(line["level"], levels)].append(line)
        merged: list[dict[str, Any]] = []
        for level, same_level in grouped.items():
            for line in sorted(same_level, key=lambda item: item["start"]):
                if (
                    merged
                    and merged[-1]["level"] == level
                    and line["start"] <= merged[-1]["end"] + tolerance
                ):
                    merged[-1]["end"] = max(merged[-1]["end"], line["end"])
                    merged[-1]["source_refs"] = _unique_refs(
                        merged[-1]["source_refs"] + line["source_refs"]
                    )
                else:
                    merged.append({**line, "level": level})
        return merged

    return merge(raw_horizontal), merge(raw_vertical)


def _components(
    horizontal: list[dict[str, Any]],
    vertical: list[dict[str, Any]],
    tolerance: float,
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    size = len(horizontal) + len(vertical)
    parents = list(range(size))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    for h_index, h_line in enumerate(horizontal):
        for v_index, v_line in enumerate(vertical, len(horizontal)):
            if (
                h_line["start"] - tolerance <= v_line["level"] <= h_line["end"] + tolerance
                and v_line["start"] - tolerance <= h_line["level"] <= v_line["end"] + tolerance
            ):
                union(h_index, v_index)
    grouped_h: dict[int, list[dict[str, Any]]] = defaultdict(list)
    grouped_v: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, line in enumerate(horizontal):
        grouped_h[find(index)].append(line)
    for index, line in enumerate(vertical, len(horizontal)):
        grouped_v[find(index)].append(line)
    roots = set(grouped_h) & set(grouped_v)
    return [(grouped_h[root], grouped_v[root]) for root in roots]


def _covering_refs(
    lines: list[dict[str, Any]], start: float, end: float, tolerance: float
) -> list[dict[str, Any]]:
    refs = [
        ref
        for line in lines
        if line["start"] <= start + tolerance and line["end"] >= end - tolerance
        for ref in line["source_refs"]
    ]
    return _unique_refs(refs)


def _grid_cells(
    horizontal: list[dict[str, Any]],
    vertical: list[dict[str, Any]],
    tolerance: float,
) -> list[dict[str, Any]]:
    by_y: dict[float, list[dict[str, Any]]] = defaultdict(list)
    by_x: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for line in horizontal:
        by_y[line["level"]].append(line)
    for line in vertical:
        by_x[line["level"]].append(line)
    xs, ys = sorted(by_x), sorted(by_y)
    candidates: dict[tuple[float, float, float, float], dict[str, Any]] = {}

    def add(left: float, bottom: float, right: float, top: float) -> None:
        box = left, bottom, right, top
        refs = _unique_refs(
            _covering_refs(by_x[left], bottom, top, tolerance)
            + _covering_refs(by_x[right], bottom, top, tolerance)
            + _covering_refs(by_y[bottom], left, right, tolerance)
            + _covering_refs(by_y[top], left, right, tolerance)
        )
        candidates[box] = {"bbox": box, "source_refs": refs}

    # Row bands recover ordinary cells and column spans.
    for bottom, top in zip(ys, ys[1:], strict=False):
        active_x = [x for x in xs if _covering_refs(by_x[x], bottom, top, tolerance)]
        for left, right in zip(active_x, active_x[1:], strict=False):
            if _covering_refs(by_y[bottom], left, right, tolerance) and _covering_refs(
                by_y[top], left, right, tolerance
            ):
                add(left, bottom, right, top)

    # Column bands recover row spans. A cell spanning both axes is intentionally
    # left for VLM adjudication; guessing it from a sparse drawing is unsafe.
    for left, right in zip(xs, xs[1:], strict=False):
        active_y = [y for y in ys if _covering_refs(by_y[y], left, right, tolerance)]
        for bottom, top in zip(active_y, active_y[1:], strict=False):
            if _covering_refs(by_x[left], bottom, top, tolerance) and _covering_refs(
                by_x[right], bottom, top, tolerance
            ):
                add(left, bottom, right, top)

    cells = list(candidates.values())
    # An outer drawing border can itself form one giant "cell" around a real
    # table. It is never a merged table cell if it contains another closed cell.
    return [
        cell
        for cell in cells
        if not any(
            other is not cell
            and cell["bbox"][0] <= other["bbox"][0]
            and cell["bbox"][1] <= other["bbox"][1]
            and cell["bbox"][2] >= other["bbox"][2]
            and cell["bbox"][3] >= other["bbox"][3]
            and _bbox_area(cell["bbox"]) > _bbox_area(other["bbox"]) + tolerance**2
            for other in cells
        )
    ]


def _cell_groups(cells: list[dict[str, Any]], tolerance: float) -> list[list[dict[str, Any]]]:
    parents = list(range(len(cells)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    for left_index, left in enumerate(cells):
        ax1, ay1, ax2, ay2 = left["bbox"]
        for right_index in range(left_index + 1, len(cells)):
            bx1, by1, bx2, by2 = cells[right_index]["bbox"]
            vertical_touch = (
                min(abs(ax2 - bx1), abs(bx2 - ax1)) <= tolerance
                and min(ay2, by2) - max(ay1, by1) > tolerance
            )
            horizontal_touch = (
                min(abs(ay2 - by1), abs(by2 - ay1)) <= tolerance
                and min(ax2, bx2) - max(ax1, bx1) > tolerance
            )
            if vertical_touch or horizontal_touch:
                union(left_index, right_index)
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, cell in enumerate(cells):
        groups[find(index)].append(cell)
    return list(groups.values())


def _text_ref(text: dict[str, Any]) -> dict[str, Any]:
    return _source_ref(text)


def _table_from_cells(
    cells: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    tolerance: float,
    sheet_bbox: tuple[float, float, float, float] | None,
) -> dict[str, Any] | None:
    if len(cells) < 4:
        return None
    xs = _cluster(
        (coordinate for cell in cells for coordinate in (cell["bbox"][0], cell["bbox"][2])),
        tolerance,
    )
    ys = sorted(
        _cluster(
            (coordinate for cell in cells for coordinate in (cell["bbox"][1], cell["bbox"][3])),
            tolerance,
        ),
        reverse=True,
    )
    if len(xs) < 3 or len(ys) < 3:
        return None
    box = min(xs), min(ys), max(xs), max(ys)
    table_texts = []
    for text in texts:
        point = _point(text.get("point"))
        if point and _bbox_distance(box, point) <= _text_margin(text, tolerance):
            table_texts.append(text)
    if len(table_texts) < 2:
        return None
    if sheet_bbox and _bbox_area(sheet_bbox) > 0:
        area_ratio = _bbox_area(box) / _bbox_area(sheet_bbox)
        if area_ratio >= 0.82 and (
            len(cells) < 8 or len(table_texts) < math.ceil(len(cells) * 0.25)
        ):
            return None
    heights = [
        float(text["height"])
        for text in texts
        if isinstance(text.get("height"), (int, float)) and text["height"] > 0
    ]
    title_gap = max(
        (statistics.median(heights) * 3 if heights else 0.0), (box[3] - box[1]) * 0.08
    )
    titles = [
        text
        for text in texts
        if (point := _point(text.get("point")))
        and box[0] - _text_margin(text, tolerance) * 4
        <= point[0]
        <= box[2] + _text_margin(text, tolerance) * 4
        and box[3] < point[1] <= box[3] + title_gap
    ]
    title_record = min(titles, key=lambda text: _point(text["point"])[1] - box[3], default=None)
    if title_record in table_texts:
        table_texts.remove(title_record)
    if len(table_texts) < 2:
        return None

    matches_by_cell: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for text in table_texts:
        point = _point(text.get("point"))
        choices = [
            (_bbox_distance(cell["bbox"], point), _bbox_area(cell["bbox"]), index)
            for index, cell in enumerate(cells)
            if point
            and _bbox_distance(cell["bbox"], point) <= _text_margin(text, tolerance)
        ]
        if choices:
            _, _, index = min(choices)
            matches_by_cell[index].append(text)

    cell_rows: list[dict[str, Any]] = []
    consumed: set[str] = set()
    for cell_index, cell in enumerate(cells):
        left, bottom, right, top = cell["bbox"]
        row = min(range(len(ys)), key=lambda index: abs(ys[index] - top))
        row_end = min(range(len(ys)), key=lambda index: abs(ys[index] - bottom))
        column = min(range(len(xs)), key=lambda index: abs(xs[index] - left))
        column_end = min(range(len(xs)), key=lambda index: abs(xs[index] - right))
        matches = matches_by_cell[cell_index]
        match_heights = [
            float(text["height"])
            for text in matches
            if isinstance(text.get("height"), (int, float)) and text["height"] > 0
        ]
        line_tolerance = max(
            tolerance, statistics.median(match_heights) * 0.5 if match_heights else 0.0
        )
        line_levels = _cluster((_point(text["point"])[1] for text in matches), line_tolerance)
        matches.sort(
            key=lambda text: (
                -_nearest(_point(text["point"])[1], line_levels),
                _point(text["point"])[0],
            )
        )
        for text in matches:
            if text.get("uid") is not None:
                consumed.add(str(text["uid"]))
        text_value = _join_text(str(text.get("text") or "") for text in matches)
        refs = _unique_refs(cell["source_refs"] + [_text_ref(text) for text in matches])
        cell_rows.append(
            {
                "row": row,
                "column": column,
                "row_span": max(1, row_end - row),
                "column_span": max(1, column_end - column),
                "text": text_value,
                "source_refs": refs,
            }
        )
    occupied = sum(bool(cell["text"]) for cell in cell_rows)
    if occupied < 2:
        return None
    # A drawing frame or notes column can form a valid line grid while holding
    # an entire specification paragraph. Treating that as a table creates a
    # huge first-row header and thousands of useless Markdown fragments.
    if max((len(cell["text"]) for cell in cell_rows), default=0) > 1000:
        return None
    row_count, column_count = len(ys) - 1, len(xs) - 1
    grid_size = row_count * column_count
    if column_count > 48 or row_count > 200:
        return None
    if grid_size >= 12 and occupied / grid_size < 0.1:
        return None
    matrix = [["" for _ in range(column_count)] for _ in range(row_count)]
    cell_source_refs: dict[str, list[dict[str, Any]]] = {}
    for cell in cell_rows:
        matrix[cell["row"]][cell["column"]] = cell["text"]
        cell_source_refs[f"{cell['row']}:{cell['column']}"] = cell["source_refs"]
    spans = [
        {
            "row": cell["row"],
            "column": cell["column"],
            "row_span": cell["row_span"],
            "column_span": cell["column_span"],
        }
        for cell in cell_rows
        if cell["row_span"] > 1 or cell["column_span"] > 1
    ]
    header_rows = (
        2
        if row_count > 1
        and any(
            cell["row"] == 0 and (cell["row_span"] > 1 or cell["column_span"] > 1)
            for cell in cell_rows
        )
        else 1
    )
    if title_record and title_record.get("uid") is not None:
        consumed.add(str(title_record["uid"]))
    source_refs = _unique_refs(ref for cell in cell_rows for ref in cell["source_refs"])
    if title_record:
        source_refs = _unique_refs(source_refs + [_text_ref(title_record)])
    return {
        "title": str(title_record.get("text") or "几何表格") if title_record else "几何表格",
        "rows": matrix,
        "header_rows": header_rows,
        "spans": spans,
        "cells": sorted(cell_rows, key=lambda cell: (cell["row"], cell["column"])),
        "bbox": list(box),
        "anchor": _point(title_record.get("point")) if title_record else (box[0], box[3]),
        "source_kind": "native_line_grid",
        "confidence": "high",
        "source_refs": source_refs,
        "cell_source_refs": cell_source_refs,
        "text_uids": sorted(consumed),
    }


def detect_table_grids(
    segment_records: Iterable[dict[str, Any]],
    text_records: Iterable[dict[str, Any]],
    *,
    sheet_bbox: tuple[float, float, float, float] | list[float] | None = None,
) -> list[dict[str, Any]]:
    """Recover bounded line-grid tables without treating aligned text as a table."""
    segments = _segments(segment_records)
    texts = list(text_records)
    if len(segments) < 4:
        return []
    tolerance = _tolerance(segments, texts)
    horizontal, vertical = _axis_lines(segments, tolerance)
    normalized_sheet_bbox = (
        tuple(map(float, sheet_bbox)) if sheet_bbox and len(sheet_bbox) == 4 else None
    )
    tables: list[dict[str, Any]] = []
    for component_h, component_v in _components(horizontal, vertical, tolerance):
        cells = _grid_cells(component_h, component_v, tolerance)
        for group in _cell_groups(cells, tolerance):
            table = _table_from_cells(group, texts, tolerance, normalized_sheet_bbox)
            if table:
                tables.append(table)
    return sorted(tables, key=lambda table: (-table["bbox"][3], table["bbox"][0]))


def _frame(frame: dict[str, Any]) -> dict[str, Any] | None:
    box = frame.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) == 4:
        try:
            bbox = tuple(map(float, box))
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        center = _point(frame.get("center"))
        radius = frame.get("radius")
        if not center or not isinstance(radius, (int, float)) or radius <= 0:
            return None
        bbox = center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    hint = str(
        frame.get("shape") or frame.get("marker_shape") or frame.get("source_kind") or ""
    ).casefold()
    shape = "circle" if "circle" in hint or "radio" in hint else "square"
    return {**frame, "bbox": bbox, "shape": shape, "source_ref": _source_ref(frame)}


def _segments_intersect(first: dict[str, Any], second: dict[str, Any], tolerance: float) -> bool:
    a, b, c, d = first["a"], first["b"], second["a"], second["b"]
    denominator = (a[0] - b[0]) * (c[1] - d[1]) - (a[1] - b[1]) * (c[0] - d[0])
    if abs(denominator) <= tolerance**2:
        return False
    t = ((a[0] - c[0]) * (c[1] - d[1]) - (a[1] - c[1]) * (c[0] - d[0])) / denominator
    u = -((a[0] - b[0]) * (a[1] - c[1]) - (a[1] - b[1]) * (a[0] - c[0])) / denominator
    return tolerance < t < 1 - tolerance and tolerance < u < 1 - tolerance


def _mark(
    frame: dict[str, Any], segments: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    box = frame["bbox"]
    width, height = box[2] - box[0], box[3] - box[1]
    scale = max(width, height)
    expanded = (
        box[0] - scale * 0.2,
        box[1] - scale * 0.2,
        box[2] + scale * 0.2,
        box[3] + scale * 0.2,
    )
    frame_key = repr(frame["source_ref"])
    diagonals = []
    for segment in segments:
        if repr(segment["source_ref"]) == frame_key:
            continue
        a, b = segment["a"], segment["b"]
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if (
            _contains(expanded, a)
            and _contains(expanded, b)
            and dx >= width * 0.12
            and dy >= height * 0.12
        ):
            diagonals.append(segment)
    if frame.get("filled"):
        return "fill", []
    if not diagonals:
        return "none", []
    endpoint_tolerance = scale * 0.22
    if any(
        _segments_intersect(left, right, 1e-6)
        for index, left in enumerate(diagonals)
        for right in diagonals[index + 1 :]
    ):
        mark = "cross"
    elif any(
        math.dist(a, b) <= endpoint_tolerance
        for index, left in enumerate(diagonals)
        for right in diagonals[index + 1 :]
        for a in (left["a"], left["b"])
        for b in (right["a"], right["b"])
    ):
        mark = "check"
    else:
        mark = "slash" if len(diagonals) == 1 else "ambiguous"
    return mark, diagonals


def _bind_labels(
    frames: list[dict[str, Any]], texts: list[dict[str, Any]]
) -> tuple[dict[int, dict[str, Any]], set[int]]:
    pairs: list[tuple[float, int, int]] = []
    for frame_index, frame in enumerate(frames):
        box = frame["bbox"]
        width, height = box[2] - box[0], box[3] - box[1]
        center_y = (box[1] + box[3]) / 2
        for text_index, text in enumerate(texts):
            point = _point(text.get("point"))
            if not point or _contains(box, point):
                continue
            if (
                box[2] - width * 0.1 <= point[0] <= box[2] + width * 60
                and abs(point[1] - center_y) <= height * 1.5
            ):
                pairs.append(
                    ((point[0] - box[2]) + abs(point[1] - center_y) * 2, frame_index, text_index)
                )
    assigned_frames: dict[int, dict[str, Any]] = {}
    assigned_texts: set[int] = set()
    for _, frame_index, text_index in sorted(pairs):
        if frame_index not in assigned_frames and text_index not in assigned_texts:
            assigned_frames[frame_index] = texts[text_index]
            assigned_texts.add(text_index)
    by_frame: dict[int, list[float]] = defaultdict(list)
    for score, frame_index, _ in pairs:
        by_frame[frame_index].append(score)
    ambiguous = set()
    for frame_index, scores in by_frame.items():
        scores.sort()
        width = frames[frame_index]["bbox"][2] - frames[frame_index]["bbox"][0]
        if len(scores) > 1 and scores[1] - scores[0] <= width * 0.5:
            ambiguous.add(frame_index)
    return assigned_frames, ambiguous


def _marker_groups(frames: list[dict[str, Any]]) -> list[list[int]]:
    parents = list(range(len(frames)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    for left_index, left in enumerate(frames):
        left_box = left["bbox"]
        left_width, left_height = left_box[2] - left_box[0], left_box[3] - left_box[1]
        left_center = (left_box[0] + left_box[2]) / 2, (left_box[1] + left_box[3]) / 2
        for right_index in range(left_index + 1, len(frames)):
            right = frames[right_index]
            if left["shape"] != right["shape"]:
                continue
            right_box = right["bbox"]
            right_width, right_height = right_box[2] - right_box[0], right_box[3] - right_box[1]
            if (
                max(left_width, right_width) > min(left_width, right_width) * 1.3
                or max(left_height, right_height) > min(left_height, right_height) * 1.3
            ):
                continue
            right_center = (right_box[0] + right_box[2]) / 2, (right_box[1] + right_box[3]) / 2
            scale = max(left_width, left_height, right_width, right_height)
            aligned = (
                abs(left_center[0] - right_center[0]) <= scale * 1.5
                or abs(left_center[1] - right_center[1]) <= scale * 1.5
            )
            if aligned and math.dist(left_center, right_center) <= scale * 80:
                union(left_index, right_index)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(frames)):
        groups[find(index)].append(index)
    return list(groups.values())


def detect_marker_groups(
    frame_records: Iterable[dict[str, Any]],
    segment_records: Iterable[dict[str, Any]],
    text_records: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect checkbox/radio groups while keeping empty-state inference gated."""
    frames = [item for record in frame_records if (item := _frame(record))]
    segments = _segments(segment_records)
    texts = list(text_records)
    if not frames:
        return []
    heights = [
        float(text["height"])
        for text in texts
        if isinstance(text.get("height"), (int, float)) and text["height"] > 0
    ]
    typical_height = statistics.median(heights) if heights else 0.0
    frames = [
        frame
        for frame in frames
        if 0.65
        <= (frame["bbox"][2] - frame["bbox"][0]) / (frame["bbox"][3] - frame["bbox"][1])
        <= 1.55
        and (
            not typical_height
            or max(frame["bbox"][2] - frame["bbox"][0], frame["bbox"][3] - frame["bbox"][1])
            <= typical_height * 4
        )
    ]
    nested: set[int] = set()
    for left_index, left in enumerate(frames):
        left_box = left["bbox"]
        left_center = (left_box[0] + left_box[2]) / 2, (left_box[1] + left_box[3]) / 2
        left_size = max(left_box[2] - left_box[0], left_box[3] - left_box[1])
        for right_index in range(left_index + 1, len(frames)):
            right_box = frames[right_index]["bbox"]
            right_center = (right_box[0] + right_box[2]) / 2, (right_box[1] + right_box[3]) / 2
            right_size = max(right_box[2] - right_box[0], right_box[3] - right_box[1])
            if (
                math.dist(left_center, right_center) <= min(left_size, right_size) * 0.1
                and max(left_size, right_size) <= min(left_size, right_size) * 3
            ):
                nested.update((left_index, right_index))
    frames = [frame for index, frame in enumerate(frames) if index not in nested]
    labels, ambiguous_labels = _bind_labels(frames, texts)
    observed = []
    for index, frame in enumerate(frames):
        mark, mark_segments = _mark(frame, segments)
        observed.append(
            {
                "frame": frame,
                "mark": mark,
                "mark_segments": mark_segments,
                "label": labels.get(index),
                "label_ambiguous": index in ambiguous_labels,
            }
        )
    result = []
    marker_number = 1
    for group_number, indexes in enumerate(_marker_groups(frames), 1):
        items = [observed[index] for index in indexes]
        has_positive_mark = any(item["mark"] in {"check", "cross", "fill"} for item in items)
        explicitly_grouped = any(item["frame"].get("marker_group") for item in items)
        if not has_positive_mark and not explicitly_grouped:
            continue
        coverage_complete = (
            len(items) >= 2
            and has_positive_mark
            and all(item["label"] for item in items)
            and not any(item["label_ambiguous"] for item in items)
            and all(item["frame"]["source_ref"] for item in items)
        )
        markers = []
        for item in items:
            mark = item["mark"]
            state = (
                "selected"
                if mark in {"check", "cross", "fill"}
                else "unselected"
                if coverage_complete and mark == "none"
                else "unknown"
            )
            refs = [item["frame"]["source_ref"]]
            refs.extend(segment["source_ref"] for segment in item["mark_segments"])
            if item["label"]:
                refs.append(_text_ref(item["label"]))
            markers.append(
                {
                    "marker_id": f"M{marker_number:04d}",
                    "shape": item["frame"]["shape"],
                    "mark": mark,
                    "state": state,
                    "label": str(item["label"].get("text") or "") if item["label"] else "",
                    "confidence": "exact"
                    if mark not in {"none", "ambiguous"}
                    else "high"
                    if state == "unselected"
                    else "candidate",
                    "evidence": "native_geometry",
                    "source_refs": _unique_refs(refs),
                }
            )
            marker_number += 1
        result.append(
            {
                "group_id": f"G{group_number:04d}",
                "marker_type": "radio"
                if all(marker["shape"] == "circle" for marker in markers)
                else "checkbox",
                "coverage_complete": coverage_complete,
                "confidence": "high" if coverage_complete else "candidate",
                "markers": markers,
                "source_refs": _unique_refs(
                    ref for marker in markers for ref in marker["source_refs"]
                ),
                "text_uids": sorted(
                    str(item["label"]["uid"])
                    for item in items
                    if item["label"] and item["label"].get("uid") is not None
                ),
            }
        )
    return result


__all__ = ["detect_marker_groups", "detect_table_grids"]
