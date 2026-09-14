# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""DWG affine/OCS coordinates. Compose in 3D; project only at the consumer."""

from __future__ import annotations

import math
from typing import Any, Iterable

IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def point3(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        p = float(value[0]), float(value[1]), float(value[2]) if len(value) > 2 else 0.0
    except (TypeError, ValueError, OverflowError):
        return None
    return p if all(math.isfinite(v) for v in p) else None


def affine(matrix: tuple[float, ...]) -> tuple[float, ...]:
    if len(matrix) == 12:
        return matrix
    a, b, c, d, e, f = matrix  # compatibility with old 2D callers
    return (a, c, 0.0, e, b, d, 0.0, f, 0.0, 0.0, 1.0, 0.0)


def apply3(matrix: tuple[float, ...], point: Iterable[float]) -> tuple[float, float, float]:
    m = affine(matrix)
    p = tuple(point)
    x, y, z = p[0], p[1], p[2] if len(p) > 2 else 0.0
    return tuple(m[r] * x + m[r + 1] * y + m[r + 2] * z + m[r + 3] for r in (0, 4, 8))


def apply2(matrix: tuple[float, ...], point: Iterable[float]) -> tuple[float, float]:
    return apply3(matrix, point)[:2]


def multiply(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    a, b = affine(left), affine(right)
    return tuple(
        sum(a[4 * r + k] * b[4 * k + c] for k in range(3)) + (a[4 * r + 3] if c == 3 else 0.0)
        for r in range(3)
        for c in range(4)
    )


def _unit(v):
    norm = math.hypot(*v)
    if not norm or not math.isfinite(norm):
        raise ValueError("Invalid extrusion vector")
    return tuple(x / norm for x in v)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def ocs(record: dict[str, Any]) -> tuple[float, ...]:
    """Autodesk arbitrary-axis algorithm; axes are matrix columns."""
    raw = record.get("extrusion", [0.0, 0.0, 1.0])
    normal = point3(raw)
    if normal is None:
        raise ValueError("Invalid extrusion vector")
    z = _unit(normal)
    x = _unit(
        _cross((0.0, 1.0, 0.0) if abs(z[0]) < 1 / 64 and abs(z[1]) < 1 / 64 else (0.0, 0.0, 1.0), z)
    )
    y = _cross(z, x)
    return (x[0], y[0], z[0], 0.0, x[1], y[1], z[1], 0.0, x[2], y[2], z[2], 0.0)


def insert_matrix(record: dict[str, Any], block: dict[str, Any]) -> tuple[float, ...]:
    insertion = point3(record.get("ins_pt", [0.0, 0.0, 0.0]))
    base = point3(block.get("base_pt", [0.0, 0.0, 0.0]))
    if insertion is None or base is None:
        raise ValueError("Invalid INSERT point")
    scale = record.get("scale", [1.0, 1.0, 1.0])
    if not isinstance(scale, (list, tuple)) or not scale:
        raise ValueError("Invalid INSERT scale")
    sx = float(scale[0])
    sy = float(scale[1]) if len(scale) > 1 else sx
    sz = float(scale[2]) if len(scale) > 2 else 1.0
    angle = float(record.get("rotation") or 0.0)  # LibreDWG uses radians
    if not all(math.isfinite(v) for v in (sx, sy, sz, angle)):
        raise ValueError("Invalid INSERT transform")
    co, si = math.cos(angle), math.sin(angle)
    local = (
        sx * co,
        -sy * si,
        0.0,
        insertion[0],
        sx * si,
        sy * co,
        0.0,
        insertion[1],
        0.0,
        0.0,
        sz,
        insertion[2],
    )
    result = list(multiply(ocs(record), local))
    for r in (0, 4, 8):
        result[r + 3] -= sum(result[r + k] * base[k] for k in range(3))
    return tuple(result)


def entity_matrix(record, parent):
    kind = str(record.get("entity") or "").upper()
    # LINE/POINT/MTEXT/ELLIPSE coordinates already belong to their owner space.
    return (
        multiply(parent, ocs(record))
        if kind in {"LWPOLYLINE", "TEXT", "ATTRIB", "ATTDEF", "CIRCLE", "ARC", "SOLID", "TRACE"}
        else affine(parent)
    )


def text_point(record):
    kind = str(record.get("entity") or "").upper()
    aligned = kind != "MTEXT" and (
        record.get("horiz_alignment", 0) or record.get("vert_alignment", 0)
    )
    keys = ("alignment_pt", "ins_pt") if aligned else ("ins_pt", "alignment_pt")
    return next((p for key in keys if (p := point3(record.get(key))) is not None), None)


def projected_height(record, parent):
    height = abs(float(record.get("text_height") or record.get("height") or 0.0))
    matrix = entity_matrix(record, parent)
    if str(record.get("entity") or "").upper() == "MTEXT":
        direction = point3(record.get("x_axis_dir", [1.0, 0.0, 0.0])) or (1.0, 0.0, 0.0)
        normal = point3(record.get("extrusion", [0.0, 0.0, 1.0])) or (0.0, 0.0, 1.0)
        up = _unit(_cross(normal, direction))
    else:
        angle = float(record.get("rotation") or 0.0)
        up = (-math.sin(angle), math.cos(angle), 0.0)
    # Apply the linear part only; subtracting translated endpoints loses
    # precision for small text at large survey coordinates.
    return height * math.hypot(
        sum(matrix[k] * up[k] for k in range(3)),
        sum(matrix[4 + k] * up[k] for k in range(3)),
    )
