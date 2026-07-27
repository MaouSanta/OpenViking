# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deterministic searchable artifacts derived from LibreDWG JSON."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from typing import Any

BEAM_TOPOLOGY_SCHEMA = "deterministic-dwg-topology-v2"
DRAWING_TEXT_SCHEMA = "deterministic-dwg-text-v1"
BEAM_TOPOLOGY_ITERATION = "i2_pair_center"

_BEAM_RE = re.compile(
    r"(?P<id>(?:W?KL|LL|L|KZL|XL)\d+[A-Z]?)(?:\((?P<spans>\d+)\))?\s*"
    r"(?P<w>\d{2,4})\s*[xX×]\s*(?P<h>\d{2,4})"
)
_MTEXT_FORMAT_RE = re.compile(r"\\(?:A\d+|C\d+|F[^;]*|f[^;]*|H[^;]*|Q[^;]*|T[^;]*|W[^;]*);", re.I)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def _point2(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None


def _point3(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    try:
        result = [round(float(value[0]), 9), round(float(value[1]), 9)]
        if len(value) >= 3:
            result.append(round(float(value[2]), 9))
        return result
    except (TypeError, ValueError, OverflowError):
        return None


def _handle(record: dict[str, Any], key: str = "handle") -> Any:
    value = record.get(key, [])
    return value[-1] if isinstance(value, list) and value else None


def _angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi


def _angle_diff(a: float, b: float) -> float:
    difference = abs(a - b) % math.pi
    return min(difference, math.pi - difference)


def _distance_to_segment(
    point: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
) -> float:
    vx, vy = b[0] - a[0], b[1] - a[1]
    denominator = vx * vx + vy * vy
    if denominator <= 1e-12:
        return math.dist(point, a)
    ratio = max(
        0.0,
        min(1.0, ((point[0] - a[0]) * vx + (point[1] - a[1]) * vy) / denominator),
    )
    nearest = a[0] + ratio * vx, a[1] + ratio * vy
    return math.dist(point, nearest)


def _project(point: tuple[float, float], theta: float) -> float:
    return point[0] * math.cos(theta) + point[1] * math.sin(theta)


def _normal_project(point: tuple[float, float], theta: float) -> float:
    return -point[0] * math.sin(theta) + point[1] * math.cos(theta)


def _center(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2


def _endpoint_cost(a, b, c, d) -> float:
    return min(
        (math.dist(a, c) + math.dist(b, d)) / 2,
        (math.dist(a, d) + math.dist(b, c)) / 2,
    )


def _bbox_contains(box, point) -> bool:
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def _layer_kind(name: str) -> tuple[bool, bool]:
    upper = name.upper()
    beam = any(key in upper for key in ("BEAM", "梁")) and not any(
        key in upper for key in ("TEXT", "标注", "DIM", "吊筋")
    )
    axis = any(key in upper for key in ("AXIS", "GRID", "轴线")) and not any(
        key in upper for key in ("TEXT", "NUM", "DIM")
    )
    return beam, axis


def _segment(record, a, b, layer, source="LINE") -> dict[str, Any]:
    return {
        "id": record.get("index"),
        "handle": _handle(record),
        "layer": layer,
        "a": a,
        "b": b,
        "length": math.dist(a, b),
        "angle": _angle(a, b),
        "source": source,
    }


def _extract_facts(drawing: dict[str, Any]) -> dict[str, Any]:
    objects = drawing.get("OBJECTS", [])
    if not isinstance(objects, list):
        objects = []
    layers = {
        _handle(record): str(record.get("name", ""))
        for record in objects
        if isinstance(record, dict) and record.get("object") == "LAYER"
    }
    beams: list[dict[str, Any]] = []
    axes: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    rectangles: list[tuple[Any, ...]] = []

    for record in objects:
        if not isinstance(record, dict):
            continue
        entity = str(record.get("entity", ""))
        layer = layers.get(_handle(record, "layer"), "")
        beam_layer, axis_layer = _layer_kind(layer)
        if entity == "LINE":
            a, b = _point2(record.get("start")), _point2(record.get("end"))
            if a and b and math.dist(a, b) > 1e-6:
                segment = _segment(record, a, b, layer)
                if beam_layer:
                    beams.append(segment)
                if axis_layer:
                    axes.append(segment)
        elif entity == "LWPOLYLINE":
            points = [point for value in record.get("points", []) if (point := _point2(value))]
            closed = bool(int(record.get("flag", 0)) & 1)
            if len(points) >= 2:
                pairs = list(zip(points, points[1:], strict=False))
                if closed:
                    pairs.append((points[-1], points[0]))
                for index, (a, b) in enumerate(pairs):
                    if math.dist(a, b) <= 1e-6:
                        continue
                    segment = _segment(record, a, b, layer, "LWPOLYLINE")
                    segment["part"] = index
                    if beam_layer:
                        beams.append(segment)
                    if axis_layer:
                        axes.append(segment)
                if closed and len(points) in (4, 5):
                    xs = [point[0] for point in points]
                    ys = [point[1] for point in points]
                    width, height = max(xs) - min(xs), max(ys) - min(ys)
                    orthogonal = all(
                        min(_angle_diff(_angle(a, b), 0), _angle_diff(_angle(a, b), math.pi / 2))
                        <= math.radians(2)
                        for a, b in pairs
                        if math.dist(a, b) > 1e-6
                    )
                    if orthogonal and width > 1000 and height > 1000:
                        rectangles.append(
                            (
                                min(xs),
                                min(ys),
                                max(xs),
                                max(ys),
                                width * height,
                                record.get("index"),
                                layer,
                            )
                        )
        if entity.startswith("DIMENSION_"):
            a = _point2(record.get("xline1_pt"))
            b = _point2(record.get("xline2_pt"))
            measurement = record.get("act_measurement")
            if (
                a
                and b
                and isinstance(measurement, (int, float))
                and math.isfinite(float(measurement))
                and float(measurement) > 0
            ):
                dimensions.append(
                    {
                        "id": record.get("index"),
                        "handle": _handle(record),
                        "type": entity,
                        "layer": layer,
                        "a": a,
                        "b": b,
                        "def_pt": _point2(record.get("def_pt")),
                        "measurement": float(measurement),
                        "angle": _angle(a, b),
                        "center": _center(a, b),
                    }
                )
        if entity in ("TEXT", "MTEXT"):
            text = str(record.get("text_value") or record.get("text") or "").strip()
            match = _BEAM_RE.search(text)
            position = _point2(record.get("ins_pt")) or _point2(record.get("alignment_pt"))
            if match and position:
                rotation = record.get("rotation")
                if not isinstance(rotation, (int, float)):
                    direction = _point2(record.get("x_axis_dir"))
                    rotation = math.atan2(direction[1], direction[0]) if direction else 0.0
                labels.append(
                    {
                        "object": record,
                        "match": match,
                        "pos": position,
                        "angle": float(rotation) % math.pi,
                        "layer": layer,
                    }
                )
        if entity == "INSERT" and _point2(record.get("ins_pt")):
            inserts.append(record)

    label_points = [label["pos"] for label in labels]
    if label_points:
        span = max(
            max(point[0] for point in label_points) - min(point[0] for point in label_points),
            max(point[1] for point in label_points) - min(point[1] for point in label_points),
            1,
        )
        rectangles = [
            rectangle
            for rectangle in rectangles
            if rectangle[2] - rectangle[0] >= 0.04 * span
            and rectangle[3] - rectangle[1] >= 0.04 * span
        ]
    return {
        "object_count": len(objects),
        "beams": beams,
        "axes": axes,
        "dimensions": dimensions,
        "labels": labels,
        "inserts": inserts,
        "rectangles": rectangles,
    }


def _region_of(point, rectangles):
    hits = [rectangle for rectangle in rectangles if _bbox_contains(rectangle, point)]
    return min(hits, key=lambda rectangle: rectangle[4])[5] if hits else None


def _same_region(a, b, rectangles) -> bool:
    region_a, region_b = _region_of(a, rectangles), _region_of(b, rectangles)
    return region_a is None or region_b is None or region_a == region_b


def _pair_centers(segments, section_width):
    result = []
    buckets = defaultdict(list)
    cell = max(1000.0, section_width * 4)
    for index, segment in enumerate(segments):
        midpoint = _center(segment["a"], segment["b"])
        key = (
            round(segment["angle"] / math.radians(5)),
            int(midpoint[0] // cell),
            int(midpoint[1] // cell),
        )
        buckets[key].append(index)
    seen = set()
    for (angle_bucket, x_bucket, y_bucket), base_ids in buckets.items():
        nearby = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                nearby.extend(buckets.get((angle_bucket, x_bucket + dx, y_bucket + dy), []))
        for left_index in base_ids:
            left = segments[left_index]
            for right_index in nearby:
                pair = left_index, right_index
                if right_index <= left_index or pair in seen:
                    continue
                seen.add(pair)
                right = segments[right_index]
                if _angle_diff(left["angle"], right["angle"]) > math.radians(1.5):
                    continue
                theta = left["angle"]
                spacing = abs(
                    _normal_project(_center(left["a"], left["b"]), theta)
                    - _normal_project(_center(right["a"], right["b"]), theta)
                )
                tolerance = max(25.0, 0.12 * section_width)
                if abs(spacing - section_width) > tolerance:
                    continue
                left_start, left_end = sorted(
                    (_project(left["a"], theta), _project(left["b"], theta))
                )
                right_start, right_end = sorted(
                    (_project(right["a"], theta), _project(right["b"], theta))
                )
                overlap = max(0, min(left_end, right_end) - max(left_start, right_start))
                union = max(left_end, right_end) - min(left_start, right_start)
                if overlap / max(min(left_end - left_start, right_end - right_start), 1) < 0.72:
                    continue
                normal = -math.sin(theta), math.cos(theta)
                normal_offset = (
                    _normal_project(_center(left["a"], left["b"]), theta)
                    + _normal_project(_center(right["a"], right["b"]), theta)
                ) / 2
                low, high = max(left_start, right_start), min(left_end, right_end)
                a = (
                    low * math.cos(theta) + normal_offset * normal[0],
                    low * math.sin(theta) + normal_offset * normal[1],
                )
                b = (
                    high * math.cos(theta) + normal_offset * normal[0],
                    high * math.sin(theta) + normal_offset * normal[1],
                )
                result.append(
                    {
                        "id": f"pair:{left['id']}:{right['id']}",
                        "handle": None,
                        "layer": left["layer"],
                        "a": a,
                        "b": b,
                        "length": math.dist(a, b),
                        "angle": theta,
                        "source": "PARALLEL_EDGE_CENTER",
                        "edge_ids": [left["id"], right["id"]],
                        "edge_handles": [left["handle"], right["handle"]],
                        "edge_spacing": spacing,
                        "overlap_ratio": overlap / max(union, 1),
                        "width_error": abs(spacing - section_width),
                    }
                )
    return result


def _dimension_candidates(label, beam, dimensions, section_width, rectangles):
    result = []
    reference = beam["angle"] if beam else label["angle"]
    for dimension in dimensions:
        if not _same_region(label["pos"], dimension["center"], rectangles):
            continue
        direction_error = _angle_diff(reference, dimension["angle"])
        if direction_error > math.radians(5):
            continue
        center = _center(beam["a"], beam["b"]) if beam else label["pos"]
        spatial_distance = math.dist(center, dimension["center"])
        scale = max(dimension["measurement"], beam["length"] if beam else 0, 1000)
        if spatial_distance > max(15000, 2.0 * scale):
            continue
        relative_error = abs(
            dimension["measurement"] - (beam["length"] if beam else dimension["measurement"])
        ) / max(dimension["measurement"], beam["length"] if beam else 1, 1)
        endpoint_error = (
            _endpoint_cost(beam["a"], beam["b"], dimension["a"], dimension["b"]) if beam else 1e99
        )
        chord = math.dist(dimension["a"], dimension["b"])
        intrinsic_error = abs(chord - dimension["measurement"]) / max(
            chord, dimension["measurement"], 1
        )
        conditions = {
            "direction": direction_error <= math.radians(2),
            "measurement": relative_error <= 0.025,
            "extension_endpoints": endpoint_error <= max(120.0, section_width * 0.75),
            "native_dimension_scale": intrinsic_error <= 0.025,
            "two_axis_ends": False,
        }
        confidence = (
            0.20 * conditions["direction"]
            + 0.25 * conditions["measurement"]
            + 0.20 * conditions["extension_endpoints"]
            + 0.10 * conditions["native_dimension_scale"]
            + 0.10 * (spatial_distance <= scale)
        )
        result.append(
            {
                "dimension_id": dimension["id"],
                "dimension_handle": dimension["handle"],
                "type": dimension["type"],
                "layer": dimension["layer"],
                "measurement_mm": round(dimension["measurement"], 3),
                "xline1_pt": [round(value, 3) for value in dimension["a"]],
                "xline2_pt": [round(value, 3) for value in dimension["b"]],
                "def_pt": (
                    None
                    if dimension["def_pt"] is None
                    else [round(value, 3) for value in dimension["def_pt"]]
                ),
                "angle_diff_deg": round(math.degrees(direction_error), 4),
                "length_relative_error": round(relative_error, 6),
                "endpoint_mean_distance": round(endpoint_error, 3),
                "native_chord_relative_error": round(intrinsic_error, 6),
                "spatial_distance": round(spatial_distance, 3),
                "conditions": conditions,
                "confidence": round(confidence, 4),
            }
        )
    return sorted(
        result,
        key=lambda item: (
            -item["confidence"],
            item["length_relative_error"],
            item["endpoint_mean_distance"],
            item["dimension_id"] or -1,
        ),
    )[:5]


def build_beam_topology(
    drawing: dict[str, Any],
    *,
    source_name: str,
    source_sha256: str,
    source_size_bytes: int,
) -> dict[str, Any]:
    """Build the report's best deterministic i2 beam-topology artifact."""
    facts = _extract_facts(drawing)
    rectangles = facts["rectangles"]
    records = []
    for label in facts["labels"]:
        match = label["match"]
        width = float(match.group("w"))
        direct = []
        for segment in facts["beams"]:
            direction_error = _angle_diff(label["angle"], segment["angle"])
            distance = _distance_to_segment(label["pos"], segment["a"], segment["b"])
            if (
                segment["source"] == "LINE"
                and direction_error <= math.radians(12)
                and distance <= 5000
                and segment["length"] >= 500
            ):
                direct.append((distance + direction_error * 3000, segment))
        direct.sort(key=lambda item: (item[0], str(item[1]["id"])))
        legacy_beam = direct[0][1] if direct else None
        legacy_dimensions = []
        if legacy_beam:
            for dimension in facts["dimensions"]:
                direction_error = _angle_diff(legacy_beam["angle"], dimension["angle"])
                spatial_distance = math.dist(label["pos"], dimension["center"])
                if direction_error > math.radians(12) or spatial_distance > 10000:
                    continue
                endpoint_error = _endpoint_cost(
                    legacy_beam["a"], legacy_beam["b"], dimension["a"], dimension["b"]
                )
                relative_error = abs(dimension["measurement"] - legacy_beam["length"]) / max(
                    dimension["measurement"], legacy_beam["length"], 1
                )
                score = (
                    direction_error / math.radians(12) * 0.25
                    + min(relative_error / 0.2, 1) * 0.35
                    + min(endpoint_error / 2000, 1) * 0.3
                    + min(spatial_distance / 10000, 1) * 0.1
                )
                legacy_dimensions.append(
                    (score, dimension, direction_error, relative_error, endpoint_error)
                )
        legacy_dimensions.sort(key=lambda item: (item[0], item[1]["id"] or -1))
        legacy_exact = bool(
            legacy_dimensions
            and legacy_dimensions[0][2] <= math.radians(5)
            and legacy_dimensions[0][3] <= 0.15
            and legacy_dimensions[0][4] <= 1200
        )
        legacy_evidence = None
        if legacy_dimensions:
            best_legacy = legacy_dimensions[0]
            legacy_evidence = {
                "dimension_id": best_legacy[1]["id"],
                "angle_diff_deg": round(math.degrees(best_legacy[2]), 4),
                "length_relative_error": round(best_legacy[3], 6),
                "endpoint_mean_distance": round(best_legacy[4], 3),
                "rule": (
                    "unchanged baseline: angle<=5deg AND relative length<=15% "
                    "AND endpoint mean<=1200"
                ),
            }

        local_edges = [
            segment
            for segment in facts["beams"]
            if _angle_diff(segment["angle"], label["angle"]) <= math.radians(5)
            and _distance_to_segment(label["pos"], segment["a"], segment["b"])
            <= max(5000, width * 8)
            and _same_region(label["pos"], _center(segment["a"], segment["b"]), rectangles)
        ]
        pairs = _pair_centers(local_edges, width)
        candidates = []
        for geometry in pairs + facts["beams"]:
            direction_error = _angle_diff(label["angle"], geometry["angle"])
            distance = _distance_to_segment(label["pos"], geometry["a"], geometry["b"])
            if (
                direction_error <= math.radians(8)
                and distance <= max(5000, width * 10)
                and geometry["length"] >= max(500, width * 2)
                and _same_region(label["pos"], _center(geometry["a"], geometry["b"]), rectangles)
            ):
                pair_bonus = -2000 if geometry["source"] == "PARALLEL_EDGE_CENTER" else 0
                candidates.append((distance + direction_error * 3000 + pair_bonus, geometry))
        candidates.sort(key=lambda item: (item[0], str(item[1]["id"])))
        beam = candidates[0][1] if candidates else None
        second_margin = candidates[1][0] - candidates[0][0] if len(candidates) > 1 else 1e99
        dimension_candidates = _dimension_candidates(
            label, beam, facts["dimensions"], width, rectangles
        )
        best_dimension = dimension_candidates[0] if dimension_candidates else None
        pair_ok = bool(
            beam
            and beam["source"] == "PARALLEL_EDGE_CENTER"
            and beam["width_error"] <= max(25, 0.12 * width)
            and beam["overlap_ratio"] >= 0.72
        )
        confidence = 0.0
        if beam:
            confidence = 0.34 + (0.24 if pair_ok else 0)
            if second_margin < 1e90:
                confidence += 0.08
        confidence = min(
            1.0, confidence + 0.20 * (best_dimension["confidence"] if best_dimension else 0)
        )
        if legacy_exact:
            confidence = 1.0
        candidate = bool(beam and (pair_ok or best_dimension))
        status = "exact" if legacy_exact else ("candidate" if candidate else "unresolved")
        if status == "unresolved":
            confidence = 0.0

        selected_beam = legacy_beam if legacy_exact else beam
        beam_geometry = None
        if selected_beam:
            beam_geometry = {
                key: selected_beam.get(key)
                for key in ("id", "handle", "layer", "source", "edge_ids", "edge_handles")
                if key in selected_beam
            }
            beam_geometry.update(
                {
                    "start": [round(value, 3) for value in selected_beam["a"]],
                    "end": [round(value, 3) for value in selected_beam["b"]],
                    "length": round(selected_beam["length"], 3),
                    "angle_deg": round(math.degrees(selected_beam["angle"]), 4),
                }
            )
            if "edge_spacing" in selected_beam:
                beam_geometry.update(
                    {
                        "edge_spacing": round(selected_beam["edge_spacing"], 3),
                        "section_width_error": round(selected_beam["width_error"], 3),
                        "overlap_ratio": round(selected_beam["overlap_ratio"], 4),
                    }
                )
        source_record = label["object"]
        records.append(
            {
                "beam_id": match.group("id"),
                "spans": int(match.group("spans")) if match.group("spans") else None,
                "section_mm": [int(match.group("w")), int(match.group("h"))],
                "label_object_id": source_record.get("index"),
                "label_handle": _handle(source_record),
                "label_layer": label["layer"],
                "label_position": [round(value, 3) for value in label["pos"]],
                "label_angle_deg": round(math.degrees(label["angle"]), 4),
                "space_region": _region_of(label["pos"], rectangles),
                "beam_geometry": beam_geometry,
                "axis_relations": [],
                "binding": {
                    "status": status,
                    "confidence": round(confidence, 4),
                    "exact_rule": (
                        "legacy non-regression OR (paired_edges_width+overlap AND dimension "
                        "direction+measurement+extension endpoints AND (two axis ends OR native "
                        "dimension scale) AND uniqueness guard(if enabled))"
                    ),
                    "exact_source": "legacy_non_regression" if legacy_exact else None,
                    "legacy_evidence": legacy_evidence,
                    "geometry_unique": True,
                    "geometry_second_margin": (
                        None if second_margin > 1e90 else round(second_margin, 3)
                    ),
                    "local_pair_count": len(pairs),
                    "candidates": dimension_candidates,
                },
            }
        )

    stats = {
        "beam_labels": len(records),
        "exact": sum(record["binding"]["status"] == "exact" for record in records),
        "candidate": sum(record["binding"]["status"] == "candidate" for record in records),
        "unresolved": sum(record["binding"]["status"] == "unresolved" for record in records),
        "confidence_sum": round(sum(record["binding"]["confidence"] for record in records), 6),
    }
    stats["weighted_confidence"] = (
        round(stats["confidence_sum"] / len(records), 6) if records else None
    )
    return {
        "schema_version": BEAM_TOPOLOGY_SCHEMA,
        "source": source_name,
        "source_sha256": source_sha256,
        "source_size_bytes": source_size_bytes,
        "facts": {
            "objects": facts["object_count"],
            "beam_segments": len(facts["beams"]),
            "axis_segments": len(facts["axes"]),
            "native_dimensions": len(facts["dimensions"]),
            "closed_space_rectangles": len(facts["rectangles"]),
            "inserts": len(facts["inserts"]),
        },
        "iteration": BEAM_TOPOLOGY_ITERATION,
        "stats": stats,
        "records": records,
    }


def _clean_text(raw: Any) -> str:
    if raw is None:
        return ""
    text = unicodedata.normalize("NFC", str(raw))
    text = text.replace("%%d", "°").replace("%%D", "°")
    text = text.replace("%%p", "±").replace("%%P", "±")
    text = text.replace("%%c", "∅").replace("%%C", "∅")
    text = text.replace(r"\P", "\n").replace(r"\p", "\n").replace(r"\~", " ")
    text = _MTEXT_FORMAT_RE.sub("", text)
    text = re.sub(r"\\S([^;]*?)[#^/]([^;]*?);", r"\1/\2", text)
    text = re.sub(r"\\[LlOoKk]", "", text)
    text = text.replace("{", "").replace("}", "")
    return _WS_RE.sub(" ", _CONTROL_RE.sub("", text)).strip()


def _text_rotation(record: dict[str, Any]) -> float:
    rotation = record.get("rotation")
    if isinstance(rotation, (int, float)) and math.isfinite(float(rotation)):
        return round(math.degrees(float(rotation)) % 360.0, 9)
    direction = record.get("x_axis_dir")
    if isinstance(direction, list) and len(direction) >= 2:
        try:
            return round(
                math.degrees(math.atan2(float(direction[1]), float(direction[0]))) % 360.0,
                9,
            )
        except (TypeError, ValueError, OverflowError):
            pass
    return 0.0


def _text_chunks(records: list[dict[str, Any]], max_chars: int = 800):
    groups = defaultdict(list)
    for index, record in enumerate(records):
        groups[record["l"]].append((index, record))
    chunks = []
    for layer in sorted(groups):
        rows = groups[layer]
        heights = sorted(record["z"] for _, record in rows if record["z"] > 0)
        median = heights[len(heights) // 2] if heights else 1.0
        distance_limit = max(100.0, median * 40.0)
        rows.sort(key=lambda item: (-item[1]["p"][1], item[1]["p"][0], item[1]["i"]))
        current: list[int] = []
        characters = 0
        previous = None
        for record_index, record in rows:
            position = record["p"]
            gap = math.dist(position[:2], previous[:2]) if previous is not None else 0.0
            added = len(record["txt"]) + (1 if current else 0)
            if current and (gap > distance_limit or characters + added > max_chars):
                chunks.append((layer, distance_limit, current))
                current = []
                characters = 0
            current.append(record_index)
            characters += len(record["txt"]) + (1 if len(current) > 1 else 0)
            previous = position
        if current:
            chunks.append((layer, distance_limit, current))
    result = []
    for chunk_id, (layer, distance_limit, record_ids) in enumerate(chunks):
        selected = [records[index] for index in record_ids]
        xs = [record["p"][0] for record in selected]
        ys = [record["p"][1] for record in selected]
        result.append(
            {
                "id": chunk_id,
                "l": layer,
                "b": [min(xs), min(ys), max(xs), max(ys)],
                "r": record_ids,
                "txt": "\n".join(record["txt"] for record in selected),
                "rule": {"d": round(distance_limit, 9), "m": max_chars},
            }
        )
    return result


def build_drawing_text(
    drawing: dict[str, Any],
    *,
    source_name: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Build the deterministic TEXT/MTEXT/ATTRIB artifact from the report script."""
    objects = drawing.get("OBJECTS", [])
    if not isinstance(objects, list):
        objects = []
    layers = {
        _handle(record): str(record.get("name", ""))
        for record in objects
        if isinstance(record, dict) and record.get("object") == "LAYER"
    }
    extracted = []
    for record in objects:
        if not isinstance(record, dict):
            continue
        entity = str(record.get("entity", ""))
        if entity not in ("TEXT", "MTEXT", "ATTRIB"):
            continue
        raw_value = record.get("text_value") if entity in ("TEXT", "ATTRIB") else record.get("text")
        if raw_value is None:
            raw_value = (
                record.get("text") if entity in ("TEXT", "ATTRIB") else record.get("text_value")
            )
        raw = "" if raw_value is None else str(raw_value)
        text = _clean_text(raw)
        position = _point3(record.get("ins_pt")) or _point3(record.get("alignment_pt"))
        if not text or position is None:
            continue
        height = record.get("height") if entity in ("TEXT", "ATTRIB") else record.get("text_height")
        height = (
            float(height)
            if isinstance(height, (int, float)) and math.isfinite(float(height))
            else 0.0
        )
        extracted.append(
            {
                "i": record.get("index"),
                "h": record.get("handle"),
                "t": entity,
                "l": layers.get(_handle(record, "layer"), ""),
                "p": position,
                "r": _text_rotation(record),
                "z": round(height, 9),
                "raw": raw,
                "txt": text,
            }
        )
    deduplicated = {}
    for record in extracted:
        key = repr(tuple(record[field] for field in ("t", "l", "p", "r", "z", "raw", "txt")))
        if key not in deduplicated:
            record["refs"] = [[record["i"], record["h"]]]
            deduplicated[key] = record
        else:
            deduplicated[key]["refs"].append([record["i"], record["h"]])
    records = list(deduplicated.values())
    records.sort(
        key=lambda record: (record["l"], -record["p"][1], record["p"][0], str(record["i"]))
    )
    for record in records:
        if len(record["refs"]) == 1:
            record.pop("refs")
    return {
        "schema": DRAWING_TEXT_SCHEMA,
        "source": source_name,
        "source_sha256": source_sha256,
        "units": {"p": "DWG drawing units", "r": "degrees", "z": "DWG drawing units"},
        "fields": {
            "i": "object index",
            "h": "original LibreDWG handle array",
            "t": "entity type",
            "l": "layer",
            "p": "insertion/alignment coordinate",
            "r": "rotation",
            "z": "text height",
            "raw": "original text",
            "txt": "clean text",
            "refs": "all [index,handle] identities for exact duplicates",
        },
        "rules": {
            "entities": ["TEXT", "MTEXT", "ATTRIB"],
            "cleaning": (
                "NFC; strip C0/DEL; collapse whitespace; deterministic MTEXT formatting "
                "removal; no OCR/VLM/semantic inference"
            ),
            "dedup": (
                "exact equality of type, layer, coordinate, rotation, height, raw and clean text"
            ),
            "chunking": (
                "same layer; coordinate order; split when consecutive distance > "
                "max(100,40*layer median text height) or text length would exceed 800 Unicode characters"
            ),
        },
        "records": records,
        "chunks": _text_chunks(records),
    }
