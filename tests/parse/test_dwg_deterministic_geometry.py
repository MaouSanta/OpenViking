"""Non-model sheet extraction: coordinate conformance and source-backed borders."""

import copy
import math
from unittest.mock import Mock

import pytest

from openviking.parse.parsers.dwg_coordinates import apply3, insert_matrix, multiply
from openviking.parse.parsers.dwg_json import build_json_artifacts, read_json_records
from openviking.parse.parsers.dwg_json_geometry import iter_instances
from openviking.parse.parsers.dwg_sheet import _drawing_graph, _walk_block, build_sheet_artifacts


def block(handle, name, **fields):
    return {"object": "BLOCK_HEADER", "handle": [0, 0, handle], "name": name, **fields}


def text(handle, value, x, y, owner=1, **fields):
    return {
        "entity": "TEXT",
        "handle": [0, 0, handle],
        "ownerhandle": [0, 0, owner],
        "ins_pt": [x, y],
        "height": 2,
        "text_value": value,
        **fields,
    }


def rect(handle, x=0, y=0, w=100, h=80, owner=1, angle=0):
    co, si = math.cos(angle), math.sin(angle)
    return {
        "entity": "LWPOLYLINE",
        "handle": [0, 0, handle],
        "ownerhandle": [0, 0, owner],
        "flag": 512,
        "points": [
            [x + a * co - b * si, y + a * si + b * co] for a, b in [(0, 0), (w, 0), (w, h), (0, h)]
        ],
    }


def drawing(*items):
    return {"OBJECTS": [block(1, "*Model_Space"), *items]}


def build(d, **kwargs):
    return build_sheet_artifacts(
        d, source_name="geometry.dwg", source_sha256="a" * 64, source_size_bytes=1, **kwargs
    )


@pytest.mark.parametrize("normal", [[0, 0, 1], [0, 0, -1], [1, 2, 3], [1, 0, 0]])
def test_insert_transform_matches_independent_ezdxf_ocs(normal):
    import ezdxf

    doc = ezdxf.new()
    base = [4, 5, 6]
    doc.blocks.new("reference", base_point=base)
    record = {"ins_pt": [10, 20, 30], "scale": [-2, 3, 0.5], "rotation": 0.73, "extrusion": normal}
    reference = (
        doc.modelspace()
        .add_blockref(
            "reference",
            record["ins_pt"],
            dxfattribs={
                "xscale": -2,
                "yscale": 3,
                "zscale": 0.5,
                "rotation": math.degrees(0.73),
                "extrusion": normal,
            },
        )
        .matrix44()
    )
    matrix = insert_matrix(record, {"base_pt": base})
    for p in [(0, 0, 0), (1, 2, 3), tuple(base)]:
        assert apply3(matrix, p) == pytest.approx(tuple(reference.transform(p)))


def test_tilted_ancestors_keep_z_until_final_projection():
    import ezdxf

    doc = ezdxf.new()
    doc.blocks.new("inner", base_point=(2, 3, 4))
    outer = doc.blocks.new("outer", base_point=(1, 2, 3))
    child = {"ins_pt": [5, 6, 7], "extrusion": [0, 1, 0], "scale": [1, 2, 3], "rotation": 0.4}
    parent = {"ins_pt": [20, 30, 40], "extrusion": [1, 1, 1], "scale": [-1, 2, 1], "rotation": 0.2}
    c = outer.add_blockref(
        "inner",
        child["ins_pt"],
        dxfattribs={
            "extrusion": child["extrusion"],
            "xscale": 1,
            "yscale": 2,
            "zscale": 3,
            "rotation": math.degrees(0.4),
        },
    )
    p = doc.modelspace().add_blockref(
        "outer",
        parent["ins_pt"],
        dxfattribs={
            "extrusion": parent["extrusion"],
            "xscale": -1,
            "yscale": 2,
            "zscale": 1,
            "rotation": math.degrees(0.2),
        },
    )
    combined = multiply(
        insert_matrix(parent, {"base_pt": [1, 2, 3]}), insert_matrix(child, {"base_pt": [2, 3, 4]})
    )
    target = (9, 8, 7)
    assert apply3(combined, target) == pytest.approx(
        tuple(p.matrix44().transform(c.matrix44().transform(target)))
    )


def test_reflected_polyline_text_and_native_json_use_same_coordinates():
    d = drawing(
        block(2, "reflected"),
        {
            "entity": "INSERT",
            "handle": [0, 0, 3],
            "ownerhandle": [0, 0, 1],
            "block_header": [0, 0, 2],
            "ins_pt": [-100, 0, 0],
            "extrusion": [0, 0, -1],
        },
        rect(10, w=20, h=10, owner=2),
        text(11, "图号：S-01", 10, 5, 2),
    )
    before = copy.deepcopy(d)
    s = build(d)
    assert s["document"]["sheet_count"] == 1
    sheet = s["sheets"][0]
    assert sheet["bbox"] == pytest.approx([80, 0, 100, 10])
    point = next(t["point"] for t in sheet["_texts"] if t["record"]["handle"][-1] == 11)
    occurrence = next(o for o in iter_instances(d) if o["entity_type"] == "TEXT")
    assert point == pytest.approx([90, 5])
    assert occurrence["point"] == pytest.approx(point)
    preserved = build_json_artifacts(d, s["sheets"])
    raw = {
        r["object_index"]: r["value"]
        for r in read_json_records(preserved["files"].values())
        if r["kind"] == "object"
    }
    assert [raw[i] for i in range(len(d["OBJECTS"]))] == d["OBJECTS"]
    assert d == before


def test_text_height_is_transformed_and_attached_attribute_is_not_translated_twice():
    d = drawing(
        block(2, "title"),
        {
            "entity": "INSERT",
            "handle": [0, 0, 3],
            "ownerhandle": [0, 0, 1],
            "block_header": [0, 0, 2],
            "ins_pt": [100, 200],
            "scale": [2, 3, 1],
        },
        text(10, "text", 1, 2, 2),
        {
            "entity": "ATTRIB",
            "handle": [0, 0, 11],
            "ownerhandle": [0, 0, 3],
            "ins_pt": [103, 204],
            "height": 4,
            "text_value": "attribute",
        },
    )
    texts = _walk_block(1, _drawing_graph(d))["texts"]
    t = next(t for t in texts if t["text"] == "text")
    assert t["point"] == [102, 206] or t["point"] == (102, 206)
    assert t["height"] == 6
    a = next(t for t in texts if t["text"] == "attribute")
    assert a["point"] == (103, 204)
    assert a["height"] == 4


def test_extrusion_flag_does_not_invent_a_closing_segment():
    p = rect(10)
    p.update(flag=1, extrusion=[0, 0, 1])
    d = drawing(p, text(11, "部件文字", 50, 20))
    walked = _walk_block(1, _drawing_graph(d))
    assert len(walked["lines"]) == 3
    assert not any(f["source_kind"] in {"closed_polyline", "line_frame"} for f in walked["frames"])
    assert build(d)["document"]["sheet_count"] == 0
    assert p["flag"] == 1


@pytest.mark.parametrize("angle", [0, 0.3, -0.512971])
def test_four_lines_are_closed_frames_at_any_in_plane_rotation(angle):
    objects = []
    for i, x in enumerate([0, 200]):
        p = rect(10 + i, x=x, angle=angle)["points"]
        objects.extend(
            {
                "entity": "LINE",
                "handle": [0, 0, 30 + i * 10 + j],
                "ownerhandle": [0, 0, 1],
                "start": a,
                "end": b,
            }
            for j, (a, b) in enumerate(zip(p, p[1:] + p[:1], strict=False))
        )
        objects.append(text(50 + i, f"图号：S-0{i + 1}", x + 30, 20))
    renderer = Mock(side_effect=AssertionError("no model/render needed"))
    result = build(drawing(*objects), region_classifier=None, sheet_renderer=renderer)
    assert result["document"]["sheet_count"] == 2
    assert len({s["sheet_id"] for s in result["sheets"]}) == 2
    assert all(len(s["boundary_sources"]) >= 4 for s in result["sheets"])
    renderer.assert_not_called()


def test_room_rectangles_do_not_replace_outer_sheet_and_nested_sheets_remain():
    rooms = drawing(
        rect(10), text(11, "图号：S-01", 80, 5), rect(20, 10, 20, 20, 20), rect(30, 50, 20, 20, 20)
    )
    result = build(rooms)
    assert result["document"]["sheet_count"] == 1
    assert result["sheets"][0]["bbox"] == [0, 0, 100, 80]
    nested = drawing(
        block(2, "master"),
        {
            "entity": "INSERT",
            "handle": [0, 0, 3],
            "ownerhandle": [0, 0, 1],
            "block_header": [0, 0, 2],
            "ins_pt": [0, 0],
        },
        rect(10, owner=2),
        text(11, "图号：S-01", 80, 5, 2),
        rect(20, x=150, owner=2),
        text(21, "图号：S-02", 230, 5, 2),
    )
    assert build(nested)["document"]["sheet_count"] == 2


def test_unrelated_giant_component_cannot_set_the_frame_size_threshold():
    d = drawing(
        rect(10),
        text(11, "图号：S-01", 80, 5),
        block(2, "site"),
        {
            "entity": "INSERT",
            "handle": [0, 0, 3],
            "ownerhandle": [0, 0, 1],
            "block_header": [0, 0, 2],
            "ins_pt": [10000, 0],
        },
        {
            "entity": "LINE",
            "handle": [0, 0, 20],
            "ownerhandle": [0, 0, 2],
            "start": [0, 0],
            "end": [1000000, 1000000],
        },
    )
    r = build(d)
    assert r["document"]["sheet_count"] == 1
    assert r["sheets"][0]["bbox"] == [0, 0, 100, 80]


def test_viewport_only_layout_is_not_a_sheet_and_paper_frames_are_separate():
    paper = [
        block(2, "*Paper_Space"),
        {
            "object": "LAYOUT",
            "handle": [0, 0, 3],
            "block_header": [0, 0, 2],
            "layout_name": "布局1",
        },
        {"entity": "VIEWPORT", "handle": [0, 0, 4], "ownerhandle": [0, 0, 2], "id": 1},
    ]
    r = build(drawing(rect(10), text(11, "图号：S-01", 80, 5), *paper))
    assert r["document"]["sheet_count"] == 1
    assert r["document"]["geometry_diagnostics"][0]["reason"] == "viewport_only_or_empty_layout"
    d = drawing(
        *paper,
        rect(10, owner=2),
        text(11, "图号：S-01", 80, 5, 2),
        rect(20, x=150, owner=2),
        text(21, "图号：S-02", 230, 5, 2),
    )
    r = build(d)
    assert r["document"]["sheet_count"] == 2
    assert all(s["kind"] == "layout_frame" for s in r["sheets"])
    files = build_json_artifacts(d, r["sheets"])["files"]
    for sheet in r["sheets"]:
        owned = read_json_records([files[sheet["directory"] + "/drawing.json"]])
        assert any(q.get("value", {}).get("entity") == "TEXT" for q in owned)


def test_synthetic_fallback_is_preserved_but_excluded_from_sheet_count():
    d = drawing(rect(10), text(11, "unknown", 20, 20))
    r = build(
        d, region_classifier=lambda *_: {"is_sheet": False}, sheet_renderer=lambda _: b"image"
    )
    assert r["document"]["sheet_count"] == 0
    assert r["document"]["sheet_bundle_count"] == 1
    assert r["document"]["detection_statistics"]["rejected_candidate_count"] == 1
    assert r["sheets"][0]["kind"] == "synthetic"
    files = build_json_artifacts(
        d, r["sheets"], detection_diagnostics={"statistics": r["document"]["detection_statistics"]}
    )["files"]
    records = read_json_records(files.values())
    assert sum(x["kind"] == "object" for x in records) == len(d["OBJECTS"])
    assert any(x["kind"] == "sheet_detection" for x in records)


def test_loose_paper_space_tables_are_unframed_content_not_extra_sheets():
    d = drawing(
        rect(10),
        text(11, "图号：S-01", 80, 5),
        block(2, "*Paper_Space"),
        {
            "object": "LAYOUT",
            "handle": [0, 0, 3],
            "block_header": [0, 0, 2],
            "layout_name": "布局1",
        },
        rect(20, owner=2),
        text(21, "主要技术经济指标表", 20, 60, 2),
        text(22, "总建筑面积", 20, 40, 2),
        text(23, "1000", 60, 40, 2),
    )
    r = build(d)
    assert r["document"]["sheet_count"] == 1
    assert r["document"]["sheet_bundle_count"] == 2
    assert r["document"]["detection_statistics"]["unframed_bundle_count"] == 1
    assert any(s["kind"] == "layout_unframed" for s in r["sheets"])
    assert any(
        g["reason"] == "paper_boundary_without_sheet_identity"
        for g in r["document"]["geometry_diagnostics"]
    )


def test_rotated_frame_does_not_own_text_in_aabb_corner_outside_polygon():
    d = drawing(
        rect(10, angle=math.pi / 4), text(11, "图号：S-01", 0, 40), text(12, "outside", -50, 5)
    )
    r = build(d)
    s = r["sheets"][0]
    assert not any(t["text"] == "outside" for t in s["_texts"])
    files = build_json_artifacts(d, r["sheets"])["files"]
    root = read_json_records([files["drawing.json"]])
    assert any(q.get("value", {}).get("text_value") == "outside" for q in root)


@pytest.mark.parametrize("regions", [None, [], [{"type": "unknown", "bbox": [0, 0, 100, 80]}]])
def test_no_document_regions_skips_unused_grid_and_marker_reconstruction(monkeypatch, regions):
    from openviking.parse.parsers import dwg_sheet

    grid = Mock(side_effect=AssertionError("Unused table reconstruction"))
    markers = Mock(side_effect=AssertionError("Unused marker reconstruction"))
    monkeypatch.setattr(dwg_sheet, "detect_table_grids", grid)
    monkeypatch.setattr(dwg_sheet, "detect_marker_groups", markers)
    result = build(
        drawing(rect(10), text(11, "图号：S-01", 80, 5)),
        region_classifier=None if regions is None else lambda *_: {"regions": regions},
        sheet_renderer=lambda _: b"image",
    )
    assert result["document"]["sheet_count"] == 1
    grid.assert_not_called()
    markers.assert_not_called()


def test_selected_document_region_still_reconstructs_tables_and_markers(monkeypatch):
    from openviking.parse.parsers import dwg_sheet

    grid, markers = Mock(return_value=[]), Mock(return_value=[])
    monkeypatch.setattr(dwg_sheet, "detect_table_grids", grid)
    monkeypatch.setattr(dwg_sheet, "detect_marker_groups", markers)
    result = build(
        drawing(rect(10), text(11, "图号：S-01", 80, 5), text(12, "施工说明正文。", 20, 30)),
        region_classifier=lambda *_: {"regions": [{"type": "document", "bbox": [0, 20, 100, 70]}]},
        sheet_renderer=lambda _: b"image",
    )
    assert "施工说明正文。" in result["sheets"][0]["content_files"][0][1]
    grid.assert_called_once()
    markers.assert_called_once()
