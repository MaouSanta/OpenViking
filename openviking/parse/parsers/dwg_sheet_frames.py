# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Deterministic, source-backed rectangular drawing boundaries."""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict


def bounds(points):
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def tolerance(points):
    b = bounds(points)
    return max(
        1e-9,
        max(b[2] - b[0], b[3] - b[1]) * 1e-8,
        max(math.ulp(v) for p in points for v in p[:2]) * 16,
    )


def rectangle(points):
    """Verify actual straight rectangle edges, not just a rectangle-shaped AABB."""
    pts = [tuple(p[:2]) for p in points]
    if len(pts) < 4:
        return None
    tol = tolerance(pts)
    if math.dist(pts[0], pts[-1]) <= tol:
        pts.pop()
    changed = True
    while changed and len(pts) > 4:
        changed = False
        for i, p in enumerate(pts):
            a, b = pts[i - 1], pts[(i + 1) % len(pts)]
            u, v = (p[0] - a[0], p[1] - a[1]), (b[0] - p[0], b[1] - p[1])
            if (
                abs(u[0] * v[1] - u[1] * v[0]) <= tol * max(math.hypot(*u), math.hypot(*v))
                and u[0] * v[0] + u[1] * v[1] >= 0
            ):
                pts.pop(i)
                changed = True
                break
    if len(pts) != 4:
        return None
    edges = [(pts[(i + 1) % 4][0] - p[0], pts[(i + 1) % 4][1] - p[1]) for i, p in enumerate(pts)]
    lengths = [math.hypot(*e) for e in edges]
    if min(lengths) <= tol:
        return None
    for i in range(4):
        a, b = edges[i], edges[(i + 1) % 4]
        if abs(a[0] * b[0] + a[1] * b[1]) > tol * max(lengths[i], lengths[(i + 1) % 4]):
            return None
    return pts


def boundary(frame):
    if frame.get("boundary"):
        return frame["boundary"]
    x0, y0, x1, y1 = frame["bbox"]
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def contains_point(frame, point):
    if point is None:
        return False
    x, y = point[:2]
    b = frame["bbox"]
    polygon = boundary(frame)
    tol = frame.get("_tolerance")
    if tol is None:
        tol = tolerance(polygon)
    tol = max(tol, frame.get("_containment_margin", 0.0))
    if not (b[0] - tol <= x <= b[2] + tol and b[1] - tol <= y <= b[3] + tol):
        return False
    signs = []
    for a, c in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        cross = (c[0] - a[0]) * (y - a[1]) - (c[1] - a[1]) * (x - a[0])
        threshold = tol * math.dist(a, c)
        if abs(cross) > threshold:
            signs.append(cross > 0)
    return not signs or all(s == signs[0] for s in signs)


def contains_box(frame, box):
    return all(contains_point(frame, p) for p in boundary({"bbox": box}))


def contains_frame(outer, inner):
    # CAD title strips and outer borders can come from independently placed
    # blocks, with small insertion-rounding differences. This tolerance only
    # resolves containment; it never fabricates or closes geometric edges.
    b = outer["bbox"]
    padded = {**outer, "_containment_margin": max(b[2] - b[0], b[3] - b[1]) * 1e-6}
    return all(contains_point(padded, p) for p in boundary(inner))


def intersects_box(frame, box):
    f = frame["bbox"]
    if f[2] < box[0] or box[2] < f[0] or f[3] < box[1] or box[3] < f[1]:
        return False
    if not frame.get("boundary"):
        return True
    a = boundary(frame)
    b = boundary({"bbox": box})
    # Separating-axis test for two convex polygons (including degenerate point boxes).
    for polygon in (a, b):
        for p, q in zip(polygon, polygon[1:] + polygon[:1], strict=False):
            axis = (-(q[1] - p[1]), q[0] - p[0])
            if axis == (0, 0):
                continue
            aa = [x * axis[0] + y * axis[1] for x, y in a]
            bb = [x * axis[0] + y * axis[1] for x, y in b]
            if max(aa) < min(bb) or max(bb) < min(aa):
                return False
    return True


def _merge(records, tol):
    groups = []
    for coordinate, lo, hi, ref in sorted(records, key=lambda r: r[0]):
        if not groups or coordinate - groups[-1][0] > tol:
            groups.append((coordinate, []))
        groups[-1][1].append((lo, hi, ref))
    merged = []
    for coordinate, values in groups:
        values.sort(key=lambda r: (r[0], r[1]))
        lo, hi, ref = values[0]
        refs = [(lo, hi, ref)]
        for a, b, ref in values[1:]:
            if a <= hi + tol:
                hi = max(hi, b)
                refs.append((a, b, ref))
            else:
                merged.append((coordinate, lo, hi, refs))
                lo, hi, refs = a, b, [(a, b, ref)]
        merged.append((coordinate, lo, hi, refs))
    return merged


def line_frames(lines):
    """Find closed four-edge supports at any in-plane angle and retain source refs."""
    families = defaultdict(list)
    for line in lines:
        a, b = line["a"], line["b"]
        dx, dy = b[0] - a[0], b[1] - a[1]
        if math.hypot(dx, dy) <= 1e-9:
            continue
        angle = math.atan2(dy, dx) % (math.pi / 2)
        if min(angle, math.pi / 2 - angle) < 1e-7:
            angle = 0.0
        families[(tuple(line.get("instance_path", [])), round(angle, 6))].append(line)
    found = []
    seen = set()
    for (_path, angle), members in families.items():
        if len(members) < 4:
            continue
        frame_hint = next((l.get("frame_hint") for l in members if l.get("frame_hint")), None)
        # The bucket only groups directions. Using its rounded angle as the
        # geometry basis would introduce gaps in otherwise exactly closed loops.
        longest = max(members, key=lambda l: math.dist(l["a"], l["b"]))
        angle = math.atan2(longest["b"][1] - longest["a"][1], longest["b"][0] - longest["a"][0]) % (
            math.pi / 2
        )
        if min(angle, math.pi / 2 - angle) < 1e-7:
            angle = 0.0
        co, si = math.cos(angle), math.sin(angle)
        origin = members[0]["a"]

        def project(p, origin=origin, co=co, si=si):
            x, y = p[0] - origin[0], p[1] - origin[1]
            return x * co + y * si, -x * si + y * co

        def unproject(p, origin=origin, co=co, si=si):
            return (origin[0] + p[0] * co - p[1] * si, origin[1] + p[0] * si + p[1] * co)

        hs = []
        vs = []
        tol = max(
            1e-9,
            max(math.dist(l["a"], l["b"]) for l in members) * 1e-8,
            max(math.ulp(v) for l in members for p in (l["a"], l["b"]) for v in p) * 16,
        )
        for line in members:
            a, b = project(line["a"]), project(line["b"])
            ref = line["source_ref"]
            if abs(a[1] - b[1]) <= max(tol, abs(a[0] - b[0]) * 1e-6):
                hs.append(((a[1] + b[1]) / 2, min(a[0], b[0]), max(a[0], b[0]), ref))
            elif abs(a[0] - b[0]) <= max(tol, abs(a[1] - b[1]) * 1e-6):
                vs.append(((a[0] + b[0]) / 2, min(a[1], b[1]), max(a[1], b[1]), ref))
        if len(hs) < 2 or len(vs) < 2:
            continue
        horizontal = _merge(hs, tol)
        vertical = _merge(vs, tol)
        vx = [r[0] for r in vertical]
        # Keep original endpoints too: a shared collinear edge can span several sheets.
        horizontal += [(c, lo, hi, [(lo, hi, ref)]) for c, lo, hi, ref in hs]
        grouped = defaultdict(list)
        quant = tol * 4
        unique_horizontal = {}
        for r in horizontal:
            key = tuple(round(v / quant) for v in r[:3])
            unique_horizontal.setdefault(key, r)
        for r in unique_horizontal.values():
            grouped[(round(r[1] / quant), round(r[2] / quant))].append(r)

        def side(x, y0, y1, vertical=vertical, vx=vx, tol=tol):
            for _coordinate, lo, hi, refs in vertical[
                bisect_left(vx, x - tol * 2) : bisect_right(vx, x + tol * 2)
            ]:
                if lo <= y0 + tol and hi >= y1 - tol:
                    return [ref for a, b, ref in refs if b >= y0 - tol and a <= y1 + tol]
            return None

        for key, values in grouped.items():
            neighbors = []
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    neighbors.extend(grouped.get((key[0] + di, key[1] + dj), []))
            for y0, x0, x1, refs0 in values:
                for y1, a, b, refs1 in neighbors:
                    if (
                        y1 - y0 <= tol
                        or x1 - x0 <= tol
                        or abs(a - x0) > tol * 2
                        or abs(b - x1) > tol * 2
                    ):
                        continue
                    left, right = side(x0, y0, y1), side(x1, y0, y1)
                    if left is None or right is None:
                        continue
                    polygon = [unproject(p) for p in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]
                    k = tuple(round(v / quant) for p in polygon for v in p)
                    if k in seen:
                        continue
                    seen.add(k)
                    horizontal_refs = [
                        r for a, b, r in refs0 + refs1 if b >= x0 - tol and a <= x1 + tol
                    ]
                    refs = {repr(r): r for r in horizontal_refs + left + right}
                    ordered = [refs[k] for k in sorted(refs)]
                    found.append(
                        {
                            "bbox": bounds(polygon),
                            "boundary": polygon,
                            "source_kind": "line_frame",
                            "source_ref": dict(ordered[0]),
                            "boundary_sources": ordered,
                            "confidence": "candidate",
                            "frame_hint": frame_hint,
                        }
                    )
    return found


class _Index:
    def __init__(self, items, point):
        self.items = sorted(
            ((point(item), item) for item in items if point(item) is not None),
            key=lambda pair: pair[0][0],
        )
        self.xs = [p[0] for p, _ in self.items]

    def inside(self, frame):
        return list(self.iter_inside(frame))

    def iter_inside(self, frame):
        b = frame["bbox"]
        for i in range(bisect_left(self.xs, b[0]), bisect_right(self.xs, b[2])):
            p, item = self.items[i]
            if contains_point(frame, p):
                yield item


def _normal(text):
    return re.sub(r"[\s.：:]+", "", text).upper()


_IDENTITY = re.compile(
    r"^(?:图纸编号|图号|图纸名称|图名|DWGNO|DRAWINGNO|DWGTITLE|DRAWINGTITLE|所属装配号)(?:$|[:：])",
    re.I,
)
_CATALOG = re.compile(r"^(?:工程设计|施工图|设计)?图纸目录$|^目录$|^DRAWING(?:LIST|INDEX)$", re.I)
_PLOT_STYLE = re.compile(r"线宽|灰度|LINEWEIGHT|GRAYSCALE", re.I)
_TITLE_FIELDS = {
    "图号",
    "图纸编号",
    "图名",
    "图纸名称",
    "比例",
    "日期",
    "设计",
    "校对",
    "审核",
    "制图",
    "专业",
    "建设单位",
    "工程编号",
    "DWGNO",
    "DRAWINGNO",
    "DWGTITLE",
    "SCALE",
    "DATE",
}


def select_frames(space):
    """Select real boundaries; never promote arbitrary block extents to sheets."""
    frames = [
        dict(f)
        for f in space.get("frames", [])
        if f.get("source_kind") in {"closed_polyline", "polyline_with_line", "line_frame"}
        and f.get("boundary")
    ]
    texts = _Index(space.get("texts", []), lambda t: t.get("point"))
    lines = _Index(
        space.get("lines", []), lambda l: ((l["a"][0] + l["b"][0]) / 2, (l["a"][1] + l["b"][1]) / 2)
    )
    features = []
    for f in frames:
        f["_tolerance"] = tolerance(boundary(f))
        inside = texts.inside(f)
        words = [t["text"] for t in inside]
        normalized = [_normal(t) for t in words]
        identities = [t for t in inside if _IDENTITY.search(re.sub(r"[\s.]", "", t["text"]))]
        catalog = any(_CATALOG.search(t) for t in normalized)
        polygon = boundary(f)
        tol = tolerance(polygon)
        image_content = any(contains_point(f, p["point"]) for p in space.get("images", []))
        if not words and not image_content and not f.get("frame_hint"):
            continue
        heights = [t.get("height", 0) for t in inside if t.get("height", 0) > 0]
        # This is local to the candidate, never a fraction of the largest block
        # or drawing extent. It excludes title/table cells smaller than their text.
        if (
            heights
            and min(
                math.dist(a, b) for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=False)
            )
            < min(heights) * 4
        ):
            continue
        inline_identity = any(
            _IDENTITY.search(re.sub(r"[\s.]", "", t)) and re.search(r"[:：]\s*\S", t) for t in words
        )
        number_labels = [
            t
            for t in identities
            if re.match(r"^(图号|图纸编号|DWGNO|DRAWINGNO|所属装配号)", _normal(t["text"]))
        ]
        independent = inline_identity or (
            bool(number_labels) and len(set(normalized) & _TITLE_FIELDS) >= 3
        )

        def internal(line, polygon=polygon, tol=tol):
            p = ((line["a"][0] + line["b"][0]) / 2, (line["a"][1] + line["b"][1]) / 2)
            return all(
                abs((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]))
                > tol * math.dist(a, b)
                for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=False)
            )

        # Stop once content is established instead of counting millions of lines.
        content = 0
        for line in lines.iter_inside(f):
            if internal(line):
                content += 1
                if content >= 8:
                    break
        prose = any(len(t) >= 12 and re.search(r"[。；;]|[.!?]$", t) for t in words)
        plot_guide = bool(words) and len(words) <= 4 and all(_PLOT_STYLE.search(t) for t in words)
        placeholder = (
            bool(words)
            and len(words) <= 2
            and all(
                re.fullmatch(
                    r"(?:[一二三四五六七八九十\d]+层)?平面图|立面图|剖面图|房间|[\d.㎡²]+|[.。…]+",
                    t.strip(),
                )
                for t in words
            )
        )
        if plot_guide or (
            not identities
            and not catalog
            and not image_content
            and (
                (not words and not (f.get("frame_hint") and content >= 8))
                or (placeholder and not prose and content < 8)
            )
        ):
            continue
        prose_lines = sum(len(t) >= 12 and bool(re.search(r"[。；;]|[.!?]$", t)) for t in words)
        supported = (
            independent
            or catalog
            or bool(f.get("frame_hint"))
            or (prose_lines >= 3 and prose_lines >= len(words) / 2)
        )
        f["detection_evidence"] = {
            "boundary_supported": True,
            "text_count": len(words),
            "identity_label_count": len(identities),
            "catalog": catalog,
            "identity_supported": supported,
        }
        f["_tolerance"] = tol
        origin = polygon[0]
        f["_area"] = (
            abs(
                sum(
                    (a[0] - origin[0]) * (b[1] - origin[1])
                    - (a[1] - origin[1]) * (b[0] - origin[0])
                    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=False)
                )
            )
            / 2
        )
        features.append((f, (number_labels or identities) if independent else []))
    # Once genuine title/catalog-bearing boundaries exist in this space, remote
    # building outlines and symbol palettes are not extra sheets. If no such
    # evidence exists, retain bounded graphical candidates as explicitly weak.
    if any(f["detection_evidence"]["identity_supported"] for f, _ in features):
        features = [
            (f, ids) for f, ids in features if f["detection_evidence"]["identity_supported"]
        ]

    def area(f):
        return f["_area"]

    # Prefer existing polyline identities to assembled copies of the same boundary.
    features.sort(
        key=lambda x: (-area(x[0]), x[0]["source_kind"] == "line_frame", repr(x[0]["source_ref"]))
    )
    unique = []
    buckets = defaultdict(list)
    for f, ids in features:
        b = f["bbox"]
        exponent = math.frexp(max(b[2] - b[0], b[3] - b[1]))[1]
        nearby = []
        for e in (exponent - 1, exponent, exponent + 1):
            q = math.ldexp(1.0, e) * 1e-8
            x, y = round(b[0] / q), round(b[1] / q)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nearby.extend(buckets.get((e, x + dx, y + dy), []))
        if any(
            all(
                abs(a - b) <= max(f["_tolerance"], g["_tolerance"])
                for a, b in zip(f["bbox"], g["bbox"], strict=False)
            )
            and abs(area(f) - area(g)) <= area(f) * 1e-7
            and all(
                any(math.dist(p, q) <= max(f["_tolerance"], g["_tolerance"]) for q in boundary(g))
                for p in boundary(f)
            )
            for g in nearby
        ):
            continue
        unique.append((f, ids))
        q = math.ldexp(1.0, exponent) * 1e-8
        buckets[(exponent, round(b[0] / q), round(b[1] / q))].append(f)
    # Many subrectangles can enclose the same title field. Only its largest
    # boundary is relevant when deciding whether a parent wraps separate sheets.
    by_identity = {}
    for g, gi in unique:
        if gi:
            by_identity.setdefault(tuple(sorted(t["uid"] for t in gi)), (g, gi))
    identified = list(by_identity.values())
    retained = []
    for f, ids in unique:
        children = [
            (g, gi)
            for g, gi in identified
            if g is not f and area(g) < area(f) * 0.98 and contains_frame(f, g)
        ]
        independent = []
        for g, _gi in sorted(children, key=lambda x: -area(x[0])):
            if not any(contains_frame(other, g) for other in independent):
                independent.append(g)
        own_ids = [t for t in ids if not any(contains_point(g, t["point"]) for g in independent)]
        if len(independent) >= 2 and not own_ids:
            continue
        if any(contains_frame(g, f) for g in retained):
            continue
        retained.append(f)
    for f in retained:
        f.pop("_tolerance", None)
        f.pop("_area", None)
    return sorted(retained, key=lambda f: (-f["bbox"][3], f["bbox"][0], repr(f["source_ref"])))
