# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Export compact, flattened Model-Space DWGs for detected physical Sheets."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox, disassemble
from ezdxf.addons import odafc
from ezdxf.addons.importer import Importer
from ezdxf.math import BoundingBox


def _configure_oda_file_converter() -> None:
    configured = os.environ.get("OPENVIKING_ODA_FILE_CONVERTER", "").strip()
    candidates = [configured, shutil.which("ODAFileConverter") or ""]
    if sys.platform == "darwin":
        candidates.append(
            "/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter"
        )
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if executable:
        ezdxf.options.set("odafc-addon", "unix_exec_path", executable)
    if not odafc.is_installed():
        raise RuntimeError(
            "Sheet DWG export requires ODA File Converter; install it or set "
            "OPENVIKING_ODA_FILE_CONVERTER"
        )


def _sheet_bbox(sheet: dict[str, Any]) -> tuple[float, float, float, float] | None:
    raw = sheet.get("structure", {}).get("sheet", {}).get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        box = tuple(float(value) for value in raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _point_inside(entity: Any, box: tuple[float, float, float, float]) -> bool:
    for name in ("insert", "location", "center", "start"):
        point = entity.dxf.get(name)
        if point is None:
            continue
        try:
            return box[0] <= float(point.x) <= box[2] and box[1] <= float(point.y) <= box[3]
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
    return False


def export_flat_sheet_dwgs(
    source_path: Path, sheets: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Return one flattened ``sheet.dwg`` payload for every framed Sheet.

    INSERT and DIMENSION display geometry is materialized in world coordinates.
    An entity is copied whole when its final bounding box intersects the Sheet;
    no geometric clipping is performed.
    """
    targets = [
        (sheet, box)
        for sheet in sheets
        if sheet.get("kind") == "model_frame" and (box := _sheet_bbox(sheet)) is not None
    ]
    if not targets:
        return {}, []

    _configure_oda_file_converter()
    try:
        source = odafc.readfile(source_path, version="R2018", audit=True)
    except Exception as exc:
        raise RuntimeError(f"Could not convert {source_path.name} for Sheet DWG export: {exc}") from exc

    states: list[dict[str, Any]] = []
    source_units = source.header.get("$INSUNITS", 0)
    for sheet, box in targets:
        target = ezdxf.new("R2018")
        target.header["$INSUNITS"] = source_units
        target.header["$LIMMIN"] = (box[0], box[1])
        target.header["$LIMMAX"] = (box[2], box[3])
        states.append(
            {
                "sheet": sheet,
                "box": box,
                "bounds": BoundingBox([(box[0], box[1], -1e99), (box[2], box[3], 1e99)]),
                "doc": target,
                "importer": Importer(source, target),
                "selected": Counter(),
            }
        )

    primitive_count = 0
    no_bbox_count = 0
    for entity in disassemble.recursive_decompose(source.modelspace()):
        primitive_count += 1
        entity_box = bbox.extents([entity], fast=True)
        if not entity_box.has_data:
            no_bbox_count += 1
        for state in states:
            intersects = (
                entity_box.has_data and entity_box.has_intersection(state["bounds"])
            ) or (not entity_box.has_data and _point_inside(entity, state["box"]))
            if not intersects:
                continue
            try:
                state["importer"].import_entity(entity, state["doc"].modelspace())
            except Exception as exc:
                raise RuntimeError(
                    f"Could not copy {entity.dxftype()} into Sheet {state['sheet']['sheet_id']}: {exc}"
                ) from exc
            state["selected"][entity.dxftype()] += 1

    exports: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="openviking-sheet-dwg-") as temp_dir:
        temp_root = Path(temp_dir)
        for state in states:
            sheet = state["sheet"]
            sheet_id = sheet["sheet_id"]
            if not state["selected"]:
                raise RuntimeError(f"Sheet {sheet_id} contains no exportable CAD entities")
            state["importer"].finalize()
            audit = state["doc"].audit()
            output_path = temp_root / f"{sheet_id}.dwg"
            try:
                odafc.export_dwg(
                    state["doc"], output_path, version="R2018", audit=False, replace=True
                )
            except Exception as exc:
                raise RuntimeError(f"Could not write Sheet {sheet_id} DWG: {exc}") from exc
            payload = output_path.read_bytes()
            if len(payload) < 6 or not payload.startswith(b"AC10"):
                raise RuntimeError(f"Sheet {sheet_id} exporter produced an invalid DWG")
            exports[sheet_id] = {
                "bytes": payload,
                "entity_count": sum(state["selected"].values()),
                "entity_types": dict(state["selected"].most_common()),
                "audit_fixes": len(audit.fixes),
                "bbox": list(state["box"]),
            }
            if audit.fixes:
                warnings.append(f"Sheet {sheet_id} DXF audit applied {len(audit.fixes)} fixes")

    if no_bbox_count:
        warnings.append(
            f"{no_bbox_count} flattened CAD primitives had no bbox; anchor points were used when available"
        )
    warnings.append(
        f"Sheet DWGs were flattened from {primitive_count} world-space CAD primitives"
    )
    return exports, warnings
