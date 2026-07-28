# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""STEP parsing: deterministic B-Rep evidence and fixed OCCT views."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import re
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image

from openviking.parse.base import NodeType, ParseResult, ResourceNode
from openviking.parse.parsers.base_parser import BaseParser
from openviking_cli.utils.uri import VikingURI

STEP_EVIDENCE_SCHEMA = "openviking.step-evidence/v1"
VIEW_SIZE = 768
VIEW_DPI = (72, 72)
VIEWS = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "iso_front_top": ((1, -1, 0.8), (-0.4, 0.4, 1)),
    "iso_back_top": ((-1, 1, 0.8), (0.4, -0.4, 1)),
    "iso_front_bottom": ((1, -1, -0.8), (0.4, -0.4, 1)),
}


class STEPParser(BaseParser):
    """Preserve STEP and derive source-only evidence plus six fixed views."""

    @property
    def supported_extensions(self) -> List[str]:
        return [".stp", ".step"]

    async def parse(
        self, source: Union[str, Path], instruction: str = "", **kwargs: Any
    ) -> ParseResult:
        del instruction
        source_path = Path(source)
        if not source_path.is_file() or source_path.suffix.lower() not in {".stp", ".step"}:
            raise ValueError(f"Not a STEP file: {source_path}")

        started_at = time.perf_counter()
        source_bytes = source_path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        source_mtime = datetime.fromtimestamp(
            source_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
        requested_name = kwargs.get("source_name")
        original_filename = Path(requested_name).name if requested_name else source_path.name
        source_filename = VikingURI.sanitize_segment(original_filename) or "model.stp"
        root_name = VikingURI.sanitize_segment(f"{Path(source_filename).stem}_step") or "model_step"

        evidence, views = _build_step_artifacts(source_path, source_filename)
        evidence_bytes = json.dumps(
            evidence, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

        viking_fs = self._get_viking_fs()
        temp_uri = viking_fs.create_temp_uri()
        root_uri = f"{temp_uri}/{root_name}"
        try:
            await viking_fs.mkdir(root_uri, exist_ok=True)
            await viking_fs.write_file_bytes(f"{root_uri}/{source_filename}", source_bytes)
            await viking_fs.write_file_bytes(f"{root_uri}/evidence.json", evidence_bytes)
            for name, payload in views.items():
                await viking_fs.write_file_bytes(f"{root_uri}/{name}.png", payload)
        except BaseException:
            try:
                await viking_fs.delete_temp(temp_uri)
            except Exception:
                pass
            raise

        root_node = ResourceNode(
            type=NodeType.ROOT,
            title=source_path.stem,
            meta={
                "content_type": "model3d",
                "original_filename": original_filename,
                "source_filename": source_filename,
                "source_sha256": source_sha256,
                "source_size_bytes": len(source_bytes),
                "source_modified_at": source_mtime,
                "evidence": "evidence.json",
                "views": [f"{name}.png" for name in VIEWS],
                "temp_root_uri": root_uri,
            },
        )
        return ParseResult(
            root=root_node,
            source_path=str(source_path),
            temp_dir_path=temp_uri,
            source_format="step",
            parser_name="STEPParser",
            parser_version="1.0",
            parse_time=time.perf_counter() - started_at,
            parse_timestamp=datetime.now(timezone.utc),
            meta=root_node.meta,
            warnings=[],
        )

    async def parse_content(
        self,
        content: str,
        source_path: Optional[str] = None,
        instruction: str = "",
        **kwargs: Any,
    ) -> ParseResult:
        del content, source_path, instruction, kwargs
        raise NotImplementedError("STEPParser requires a local .stp or .step file")


def _build_step_artifacts(source_path: Path, source_filename: str) -> Tuple[dict, Dict[str, bytes]]:
    try:
        import cadquery as cq
    except ImportError as exc:  # pragma: no cover - dependency is mandatory in packaged installs
        raise RuntimeError("CadQuery/OCCT is required to parse STEP files") from exc

    imported = cq.importers.importStep(str(source_path))
    shapes = [shape for shape in imported.vals() if not shape.wrapped.IsNull()]
    if not shapes:
        raise RuntimeError(f"OCCT imported no geometry from {source_path.name}")
    shape = shapes[0] if len(shapes) == 1 else cq.Compound.makeCompound(shapes)
    geometry = _shape_summary(shape)
    evidence = _step_evidence(source_path, source_filename, geometry)

    with tempfile.TemporaryDirectory(prefix="openviking-step-") as temp_dir:
        paths = _render_views(shape.wrapped, Path(temp_dir))
        views = {path.stem: path.read_bytes() for path in paths}
    return evidence, views


def _step_header(path: Path) -> dict:
    text = path.read_text(errors="ignore")[:2_000_000]
    file_name = timestamp = None
    block = re.search(r"FILE_NAME\s*\((.*?)\);", text, re.S)
    if block:
        quoted = [
            value.replace("\n", "") for value in re.findall(r"'([^']*)'", block.group(1), re.S)
        ]
        file_name = quoted[0] if quoted else None
        timestamp = quoted[1] if len(quoted) > 1 else None

    def match(pattern: str) -> Optional[str]:
        result = re.search(pattern, text)
        return result.group(1).replace("\n", "") if result else None

    return {
        "file_name": file_name,
        "timestamp": timestamp,
        "originating_system": match(r"/\* originating_system \*/\s*'([^']*)'"),
        "schema": match(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']*)'"),
        "unit_hint": (
            "cm" if re.search(r"SI_UNIT\s*\(\s*\.CENTI\.\s*,\s*\.METRE\.\s*\)", text) else None
        ),
    }


def _step_evidence(source_path: Path, source_filename: str, geometry: dict) -> dict:
    size = geometry["size"]
    return {
        "schema": STEP_EVIDENCE_SCHEMA,
        "source": {"file": source_filename, "step_header": _step_header(source_path)},
        "dimensions": {
            "units": geometry["units"]["length"],
            "axis_aligned_xyz": dict(zip(("x", "y", "z"), size, strict=True)),
            "sorted_extents_desc": sorted(size, reverse=True),
            "center_xyz": geometry["center"],
            "oriented_bbox": geometry["oriented_bbox"],
            "coordinate_note": (
                "axis_aligned_xyz uses the STEP global frame; sorted_extents_desc ignores "
                "axis labels but not arbitrary object rotation."
            ),
        },
        "shape_metrics": {
            key: geometry[key]
            for key in (
                "bbox_fill_ratio",
                "min_bbox_extent",
                "revolution_surface_area_fraction",
                "dominant_cylinder_radius",
                "surface_area_to_volume_ratio",
                "axis_aligned_aspect_ratios_desc",
                "oriented_bbox_aspect_ratios_desc",
                "minimum_cylinder_radius",
                "minimum_analytic_surface_radius",
                "minimum_circular_edge_radius",
                "closed_circular_edge_count",
                "minimum_elliptical_edge_minor_radius",
                "closed_elliptical_edge_count",
                "maximum_cylinder_axial_extent_to_diameter_ratio",
                "minimum_opposed_parallel_planar_face_distance",
                "opposed_parallel_planar_face_pair_count",
            )
        },
        "topology": geometry["total"],
        "mass_properties": {"center_of_mass": geometry["center_of_mass"]},
        "geometry_facts": geometry["geometry_facts"],
        "surfaces": {
            "counts": geometry["surface_counts"],
            "area_by_type": geometry["surface_area_by_type"],
            "area_fraction_by_type": geometry["surface_area_fraction_by_type"],
            "cylinder_radii": geometry["cylinder_radii"],
            "largest_faces": geometry["largest_faces"][:8],
        },
        "edges": {
            "curve_counts": geometry["edge_curve_counts"],
            "length_by_curve": geometry["edge_length_by_curve"],
        },
        "evidence_limits": [
            "No material, tolerance, title-block, or other drawing PMI is present.",
            "External shaded views may not reveal hidden or internal topology.",
            "Opposed planar-face distance is a measured face separation, not a wall-thickness label.",
            "Cylinder axial ratio describes any cylindrical face and does not by itself identify a hole.",
            "Edge normal angle is unsigned and does not by itself identify a concave internal corner.",
            "Oriented-box axis directions are arbitrary for geometrically symmetric shapes.",
        ],
    }


def _shape_summary(shape: Any) -> dict:
    bounding_box = shape.BoundingBox()
    surface_counts: Counter[str] = Counter()
    surface_area_by_type: Counter[str] = Counter()
    cylinder_radii: Counter[float] = Counter()
    cylinder_area_by_radius: Counter[float] = Counter()
    largest_faces = []
    faces = list(shape.Faces())

    for index, face in enumerate(faces, 1):
        kind, radius = _face_surface(face)
        area = float(face.Area())
        surface_counts[kind] += 1
        surface_area_by_type[kind] += area
        if radius is not None:
            rounded_radius = round(radius, 6)
            cylinder_radii[rounded_radius] += 1
            cylinder_area_by_radius[rounded_radius] += area
        face_box = face.BoundingBox()
        largest_faces.append(
            {
                "face": index,
                "surface": kind,
                "area": area,
                "bbox_size": [face_box.xlen, face_box.ylen, face_box.zlen],
                "center": [
                    (face_box.xmin + face_box.xmax) / 2,
                    (face_box.ymin + face_box.ymax) / 2,
                    (face_box.zmin + face_box.zmax) / 2,
                ],
            }
        )

    largest_faces.sort(key=lambda item: item["area"], reverse=True)
    size = [bounding_box.xlen, bounding_box.ylen, bounding_box.zlen]
    volume = float(shape.Volume())
    area = float(shape.Area())
    revolution_area = sum(
        surface_area_by_type[kind] for kind in ("CYLINDER", "CONE", "SPHERE", "TORUS", "REVOLUTION")
    )
    edges = list(shape.Edges())
    edge_curve_counts = Counter(edge.geomType() for edge in edges)
    edge_length_by_curve: Counter[str] = Counter()
    for edge in edges:
        edge_length_by_curve[edge.geomType()] += float(edge.Length())

    geometry_facts = _geometry_facts(shape, faces, edges)
    cylinder_facts = geometry_facts["cylinder_axes_radii_extents"]
    circular_edges = geometry_facts["circular_edges"]
    elliptical_edges = geometry_facts["elliptical_edges"]
    analytic_radii = [item["radius"] for item in cylinder_facts]
    for item in geometry_facts["analytic_faces"]:
        if item["surface"] == "SPHERE":
            analytic_radii.append(item["radius"])
        elif item["surface"] == "TORUS":
            analytic_radii.append(item["minor_radius"])
    opposed_distances = [
        item["distance"] for item in geometry_facts["opposed_parallel_planar_faces"]
    ]
    oriented_box = _oriented_bbox(shape)
    max_extent = max(size)
    oriented_extents = oriented_box["sorted_extents_desc"]
    return {
        "units": {"length": "mm", "area": "mm^2", "volume": "mm^3"},
        "bbox": [
            bounding_box.xmin,
            bounding_box.ymin,
            bounding_box.zmin,
            bounding_box.xmax,
            bounding_box.ymax,
            bounding_box.zmax,
        ],
        "size": size,
        "center": [
            (bounding_box.xmin + bounding_box.xmax) / 2,
            (bounding_box.ymin + bounding_box.ymax) / 2,
            (bounding_box.zmin + bounding_box.zmax) / 2,
        ],
        "bbox_fill_ratio": _bbox_fill_ratio(size, volume),
        "min_bbox_extent": min(size),
        "revolution_surface_area_fraction": revolution_area / area if area else None,
        "dominant_cylinder_radius": (
            max(cylinder_area_by_radius, key=cylinder_area_by_radius.get)
            if cylinder_area_by_radius
            else None
        ),
        "surface_area_to_volume_ratio": round(area / volume, 6) if volume else None,
        "axis_aligned_aspect_ratios_desc": (
            [round(value / max_extent, 6) for value in sorted(size, reverse=True)]
            if max_extent
            else None
        ),
        "oriented_bbox_aspect_ratios_desc": (
            [round(value / oriented_extents[0], 6) for value in oriented_extents]
            if oriented_extents and oriented_extents[0]
            else None
        ),
        "minimum_cylinder_radius": min((item["radius"] for item in cylinder_facts), default=None),
        "minimum_analytic_surface_radius": min(analytic_radii, default=None),
        "minimum_circular_edge_radius": min(
            (item["radius"] for item in circular_edges), default=None
        ),
        "closed_circular_edge_count": sum(item["is_closed"] for item in circular_edges),
        "minimum_elliptical_edge_minor_radius": min(
            (item["minor_radius"] for item in elliptical_edges), default=None
        ),
        "closed_elliptical_edge_count": sum(item["is_closed"] for item in elliptical_edges),
        "maximum_cylinder_axial_extent_to_diameter_ratio": max(
            (
                item["axial_extent_to_diameter_ratio"]
                for item in cylinder_facts
                if item["axial_extent_to_diameter_ratio"] is not None
            ),
            default=None,
        ),
        "minimum_opposed_parallel_planar_face_distance": min(opposed_distances, default=None),
        "opposed_parallel_planar_face_pair_count": len(opposed_distances),
        "center_of_mass": _xyz(shape.centerOfMass(shape)),
        "oriented_bbox": oriented_box,
        "total": {
            "volume": volume,
            "area": area,
            "solids": len(list(shape.Solids())),
            "faces": len(faces),
            "edges": len(edges),
            "vertices": len(list(shape.Vertices())),
            "wires": len(list(shape.Wires())),
            "shells": len(list(shape.Shells())),
        },
        "surface_counts": dict(surface_counts),
        "surface_area_by_type": dict(surface_area_by_type),
        "surface_area_fraction_by_type": (
            {kind: value / area for kind, value in surface_area_by_type.items()} if area else {}
        ),
        "cylinder_radii": {str(key): value for key, value in cylinder_radii.items()},
        "edge_curve_counts": dict(edge_curve_counts),
        "edge_length_by_curve": {
            kind: round(length, 6) for kind, length in edge_length_by_curve.items()
        },
        "geometry_facts": geometry_facts,
        "largest_faces": largest_faces[:20],
    }


def _geometry_facts(shape: Any, faces: list, edges: list) -> dict:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    analytic_faces = []
    cylinders = []
    boundary_loops = []
    analytic_types = {"PLANE", "CYLINDER", "CONE", "SPHERE", "TORUS"}

    for face_id, face in enumerate(faces, 1):
        kind = face.geomType()
        if kind in analytic_types:
            adaptor = BRepAdaptor_Surface(face.wrapped)
            item = {"face": face_id, "surface": kind, "area": round(float(face.Area()), 6)}
            if kind == "PLANE":
                item["normal"] = _xyz(adaptor.Plane().Axis().Direction())
            elif kind in {"CYLINDER", "CONE", "TORUS"}:
                surface = getattr(adaptor, kind.title())()
                item |= {
                    "axis_origin": _xyz(surface.Axis().Location()),
                    "axis_direction": _xyz(surface.Axis().Direction()),
                }
                if kind == "CYLINDER":
                    item |= {
                        "radius": round(float(surface.Radius()), 6),
                        "axial_parameter_bounds": [
                            round(float(adaptor.FirstVParameter()), 6),
                            round(float(adaptor.LastVParameter()), 6),
                        ],
                        "axial_extent": round(
                            abs(float(adaptor.LastVParameter() - adaptor.FirstVParameter())), 6
                        ),
                    }
                    item["axial_extent_to_diameter_ratio"] = (
                        round(item["axial_extent"] / (2 * item["radius"]), 6)
                        if item["radius"]
                        else None
                    )
                    cylinders.append(
                        {
                            key: value
                            for key, value in item.items()
                            if key not in {"surface", "area"}
                        }
                    )
                    item = {key: item[key] for key in ("face", "surface", "area")}
                elif kind == "CONE":
                    item |= {
                        "reference_radius": round(float(surface.RefRadius()), 6),
                        "semi_angle_degrees": round(math.degrees(float(surface.SemiAngle())), 6),
                    }
                else:
                    item |= {
                        "major_radius": round(float(surface.MajorRadius()), 6),
                        "minor_radius": round(float(surface.MinorRadius()), 6),
                    }
            elif kind == "SPHERE":
                surface = adaptor.Sphere()
                item |= {
                    "center": _xyz(surface.Location()),
                    "radius": round(float(surface.Radius()), 6),
                }
            analytic_faces.append(item)

        try:
            outer = face.outerWire()
            inner = list(face.innerWires())
            boundary_loops.append(
                {
                    "face": face_id,
                    "outer": {
                        "edge_count": len(outer.Edges()),
                        "length": round(float(outer.Length()), 6),
                    },
                    "inner": [
                        {
                            "edge_count": len(wire.Edges()),
                            "length": round(float(wire.Length()), 6),
                        }
                        for wire in inner
                    ],
                }
            )
        except Exception:
            pass

    face_adjacency = []
    normal_angles = []
    circular_edges = []
    elliptical_edges = []
    for edge_id, edge in enumerate(edges, 1):
        adjacent = list(edge.ancestors(shape, "Face").Faces())
        face_ids = sorted(
            index
            for index, candidate in enumerate(faces, 1)
            if any(candidate.isSame(item) for item in adjacent)
        )
        face_adjacency.append(
            {
                "edge": edge_id,
                "faces": face_ids,
                "curve": edge.geomType(),
                "length": round(float(edge.Length()), 6),
            }
        )
        if edge.geomType() == "CIRCLE":
            adaptor = edge._geomAdaptor()
            circle = adaptor.Circle()
            circular_edges.append(
                {
                    "edge": edge_id,
                    "center": _xyz(circle.Location()),
                    "axis_direction": _xyz(circle.Axis().Direction()),
                    "radius": round(float(circle.Radius()), 6),
                    "sweep_degrees": round(
                        math.degrees(abs(adaptor.LastParameter() - adaptor.FirstParameter())), 6
                    ),
                    "is_closed": bool(edge.IsClosed()),
                }
            )
        elif edge.geomType() == "ELLIPSE":
            adaptor = edge._geomAdaptor()
            ellipse = adaptor.Ellipse()
            elliptical_edges.append(
                {
                    "edge": edge_id,
                    "center": _xyz(ellipse.Location()),
                    "axis_direction": _xyz(ellipse.Axis().Direction()),
                    "major_radius": round(float(ellipse.MajorRadius()), 6),
                    "minor_radius": round(float(ellipse.MinorRadius()), 6),
                    "sweep_degrees": round(
                        math.degrees(abs(adaptor.LastParameter() - adaptor.FirstParameter())), 6
                    ),
                    "is_closed": bool(edge.IsClosed()),
                }
            )
        if len(face_ids) == 2:
            try:
                point = edge.positionAt(0.5)
                normals = [_unit(faces[index - 1].normalAt(point).toTuple()) for index in face_ids]
                cosine = max(-1.0, min(1.0, _dot(normals[0], normals[1])))
                normal_angles.append(
                    {
                        "edge": edge_id,
                        "normal_angle_degrees": round(math.degrees(math.acos(cosine)), 6),
                    }
                )
            except Exception:
                pass

    planes = []
    for face_id, face in enumerate(faces, 1):
        if face.geomType() == "PLANE":
            plane = BRepAdaptor_Surface(face.wrapped).Plane()
            planes.append(
                (face_id, face, _unit(face.normalAt().toTuple()), plane.Location().Coord())
            )
    opposed_faces = []
    for index, (face_a_id, face_a, normal_a, origin_a) in enumerate(planes):
        for face_b_id, face_b, normal_b, origin_b in planes[index + 1 :]:
            if _dot(normal_a, normal_b) > -0.999999:
                continue
            plane_distance = abs(
                _dot(
                    tuple(
                        value_b - value_a
                        for value_a, value_b in zip(origin_a, origin_b, strict=True)
                    ),
                    normal_a,
                )
            )
            if plane_distance <= 1e-6:
                continue
            try:
                actual_distance = float(face_a.distance(face_b))
            except Exception:
                continue
            tolerance = max(1e-6, plane_distance * 1e-6)
            if abs(actual_distance - plane_distance) <= tolerance:
                opposed_faces.append(
                    {
                        "faces": [face_a_id, face_b_id],
                        "distance": round(actual_distance, 6),
                        "areas": [round(float(face_a.Area()), 6), round(float(face_b.Area()), 6)],
                    }
                )

    return {
        "analytic_faces": analytic_faces,
        "face_adjacency": face_adjacency,
        "edge_face_normal_angles": normal_angles,
        "boundary_loops": boundary_loops,
        "cylinder_axes_radii_extents": cylinders,
        "circular_edges": circular_edges,
        "elliptical_edges": elliptical_edges,
        "opposed_parallel_planar_faces": opposed_faces,
    }


def _face_surface(face: Any) -> Tuple[str, Optional[float]]:
    try:
        kind = face.geomType()
    except Exception:
        kind = face.__class__.__name__
    try:
        radius = float(face._geomAdaptor().Cylinder().Radius())
    except Exception:
        radius = None
    return kind, radius


def _oriented_bbox(shape: Any) -> dict:
    from OCP.Bnd import Bnd_OBB
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_OBB()
    BRepBndLib.AddOBB_s(shape.wrapped, box, False, True, False)
    extents = [2 * box.XHSize(), 2 * box.YHSize(), 2 * box.ZHSize()]
    return {
        "center": _xyz(box.Center()),
        "axes": [
            {"direction": _xyz(direction), "extent": round(float(extent), 6)}
            for direction, extent in zip(
                (box.XDirection(), box.YDirection(), box.ZDirection()),
                extents,
                strict=True,
            )
        ],
        "sorted_extents_desc": sorted((round(float(value), 6) for value in extents), reverse=True),
    }


def _bbox_fill_ratio(size: List[float], volume: float) -> Optional[float]:
    bbox_volume = math.prod(size) if len(size) == 3 and all(length > 0 for length in size) else 0
    return volume / bbox_volume if bbox_volume else None


def _xyz(value: Any) -> List[float]:
    coordinates = (
        value.toTuple() if hasattr(value, "toTuple") else (value.X(), value.Y(), value.Z())
    )
    return [round(float(coordinate), 6) for coordinate in coordinates]


def _unit(vector: Tuple[float, float, float]) -> Tuple[float, float, float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / norm for value in vector)  # type: ignore[return-value]


def _dot(left: Tuple[float, float, float], right: Tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _init_macos_app() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("STEP rendering currently supports macOS Cocoa only")
    objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    objc.objc_msgSend.restype = ctypes.c_void_p
    objc.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    objc.objc_msgSend(
        objc.objc_getClass(b"NSApplication"),
        objc.sel_registerName(b"sharedApplication"),
    )


def _render_views(shape: Any, out_dir: Path) -> List[Path]:
    from OCP.AIS import AIS_InteractiveContext, AIS_Shape
    from OCP.Aspect import Aspect_DisplayConnection
    from OCP.Cocoa import Cocoa_Window
    from OCP.Graphic3d import (
        Graphic3d_MaterialAspect,
        Graphic3d_NameOfMaterial,
        Graphic3d_TypeOfShadingModel,
    )
    from OCP.Image import Image_AlienPixMap
    from OCP.OpenGl import OpenGl_GraphicDriver
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.V3d import V3d_Viewer

    _init_macos_app()
    driver = OpenGl_GraphicDriver(Aspect_DisplayConnection())
    viewer = V3d_Viewer(driver)
    viewer.SetDefaultLights()
    viewer.SetLightOn()
    view = viewer.CreateView()
    window = Cocoa_Window("OCCT offscreen", 0, 0, VIEW_SIZE, VIEW_SIZE)
    window.SetVirtual(True)
    view.SetWindow(window)
    view.SetBgGradientColors(
        Quantity_Color(0.95, 0.96, 0.98, Quantity_TOC_RGB),
        Quantity_Color(0.75, 0.80, 0.88, Quantity_TOC_RGB),
    )
    params = view.ChangeRenderingParams()
    params.ShadingModel = Graphic3d_TypeOfShadingModel.Graphic3d_TypeOfShadingModel_Phong
    params.NbMsaaSamples = 8

    context = AIS_InteractiveContext(viewer)
    presentation = AIS_Shape(shape)
    presentation.SetColor(Quantity_Color(0.33, 0.52, 0.72, Quantity_TOC_RGB))
    presentation.SetMaterial(
        Graphic3d_MaterialAspect(Graphic3d_NameOfMaterial.Graphic3d_NameOfMaterial_Satin)
    )
    presentation.Attributes().SetFaceBoundaryDraw(True)
    context.Display(presentation, 1, -1, False)

    outputs = []
    for name, (direction, up) in VIEWS.items():
        view.SetProj(*direction)
        view.SetUp(*up)
        view.FitAll(0.08, False)
        pixmap = Image_AlienPixMap()
        if not view.ToPixMap(pixmap, VIEW_SIZE, VIEW_SIZE):
            raise RuntimeError(f"OCCT failed to render {name}")
        output = out_dir / f"{name}.png"
        if not pixmap.Save(str(output)):
            raise RuntimeError(f"OCCT failed to save {output.name}")
        with Image.open(output) as rendered:
            image = rendered.copy()
        image.save(output, format="PNG", dpi=VIEW_DPI, optimize=True)
        outputs.append(output)
    return outputs
