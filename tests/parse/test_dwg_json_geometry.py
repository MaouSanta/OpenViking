import copy
import math

import pytest

from openviking.parse.parsers.dwg_json_geometry import iter_instances


def _block(handle, name, **kwargs):
    return {"object": "BLOCK_HEADER", "handle": [0, 0, handle], "name": name, **kwargs}


def test_instance_identity_uses_original_array_ordinal_and_preserves_raw_fields():
    drawing = {
        "OBJECTS": [
            None,
            _block(10, "*Model_Space"),
            {
                "entity": "LINE",
                "index": 999,
                "ownerhandle": [0, 0, 10],
                "start": [1, 2],
                "end": [3, 4],
            },
            {"entity": "HATCH", "ownerhandle": [0, 0, 10]},
        ]
    }
    before = copy.deepcopy(drawing)
    instances = list(iter_instances(drawing))
    assert [item["object_index"] for item in instances] == [2, 3]
    assert instances[0]["bbox"] == [1, 2, 3, 4]
    assert instances[1]["bbox"] is None
    assert instances[1]["space_kind"] == "model"
    assert drawing == before


def test_nested_repeated_inserts_compose_base_rotation_and_scale():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            _block(20, "outer", base_pt=[10, 0]),
            _block(30, "inner", base_pt=[1, 1]),
            {
                "entity": "INSERT",
                "handle": [0, 0, 100],
                "ownerhandle": [0, 0, 10],
                "block_header": [0, 0, 20],
                "ins_pt": [100, 0],
                "scale": [2, 1],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 200],
                "ownerhandle": [0, 0, 10],
                "block_header": [0, 0, 20],
                "ins_pt": [200, 0],
                "scale": [-2, 1],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 300],
                "ownerhandle": [0, 0, 20],
                "block_header": [0, 0, 30],
                "ins_pt": [10, 5],
                "rotation": math.pi / 2,
            },
            {
                "entity": "LINE",
                "handle": [0, 0, 400],
                "ownerhandle": [0, 0, 30],
                "start": [1, 1],
                "end": [3, 2],
            },
        ]
    }
    instances = list(iter_instances(drawing))
    lines = [item for item in instances if item["entity_type"] == "LINE"]
    assert [item["object_index"] for item in lines] == [6, 6]
    assert [item["instance_path"] for item in lines] == [[100, 300], [200, 300]]
    assert lines[0]["bbox"] == pytest.approx([98, 5, 100, 7])
    assert lines[1]["bbox"] == pytest.approx([200, 5, 202, 7])
    assert all(item["is_container"] for item in instances if item["entity_type"] == "INSERT")


def test_rotated_nonuniform_circle_bounds_cover_transformed_circle():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            _block(20, "circle"),
            {
                "entity": "INSERT",
                "handle": [0, 0, 30],
                "ownerhandle": [0, 0, 10],
                "block_header": [0, 0, 20],
                "ins_pt": [10, 20],
                "scale": [3, 1],
                "rotation": math.pi / 4,
            },
            {"entity": "CIRCLE", "ownerhandle": [0, 0, 20], "center": [0, 0], "radius": 2},
        ]
    }
    circle = next(item for item in iter_instances(drawing) if item["entity_type"] == "CIRCLE")
    radius = 2 * math.sqrt(5)
    assert circle["bbox"] == pytest.approx([10 - radius, 20 - radius, 10 + radius, 20 + radius])


def test_attached_attribute_uses_owner_coordinates_and_keeps_instance_path():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            _block(20, "symbol"),
            {
                "entity": "INSERT",
                "handle": [0, 0, 30],
                "ownerhandle": [0, 0, 10],
                "block_header": [0, 0, 20],
                "ins_pt": [100, 200],
            },
            {"entity": "ATTRIB", "ownerhandle": [0, 0, 30], "ins_pt": [103, 204]},
        ]
    }
    attribute = next(item for item in iter_instances(drawing) if item["entity_type"] == "ATTRIB")
    assert attribute["instance_path"] == [30]
    assert attribute["point"] == [103, 204]
    assert attribute["bbox"] is None
    assert attribute["geometry_status"] == "anchor_only"


def test_orphans_paper_and_unused_definitions_are_distinguished():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            _block(20, "*Paper_Space"),
            _block(30, "unused"),
            {"entity": "LINE", "ownerhandle": [0, 0, 30], "start": [0, 0], "end": [1, 1]},
            {"entity": "TEXT", "text_value": "orphan"},
            {"entity": "LINE", "ownerhandle": [0, 0, 20], "start": [0, 0], "end": [1, 1]},
            {"entity": "HATCH", "ownerhandle": [0, 0, 10]},
        ]
    }
    instances = {item["object_index"]: item for item in iter_instances(drawing)}
    assert set(instances) == {4, 5, 6}
    assert instances[4]["space_kind"] == "unknown"
    assert instances[4]["geometry_status"] == "missing_coordinates"
    assert instances[5]["space_kind"] == "paper"
    assert instances[6]["space_kind"] == "model"


def test_invalid_and_cyclic_inserts_remain_visible_unresolved_occurrences():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            _block(20, "cycle"),
            {
                "entity": "INSERT",
                "handle": [0, 0, 30],
                "ownerhandle": [0, 0, 10],
                "block_header": [0, 0, 20],
                "ins_pt": [0, 0],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 40],
                "ownerhandle": [0, 0, 20],
                "block_header": [0, 0, 20],
                "ins_pt": [0, 0],
            },
            {
                "entity": "INSERT",
                "ownerhandle": [0, 0, 10],
                "block_header": [0, 0, 999],
                "ins_pt": [0, 0],
            },
            {"entity": "INSERT", "ownerhandle": [0, 0, 10], "block_header": [0, 0, 20]},
        ]
    }
    instances = {item["object_index"]: item for item in iter_instances(drawing)}
    assert instances[2]["is_container"] is True
    assert instances[3]["is_container"] is False
    assert instances[3]["geometry_status"] == "cyclic_or_deep_block"
    assert instances[4]["geometry_status"] == "missing_block"
    assert instances[5]["geometry_status"] == "invalid_insert_transform"


def test_non_xy_and_bulged_geometry_does_not_get_vertex_only_bounds():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            {
                "entity": "CIRCLE",
                "entmode": 2,
                "center": [0, 0],
                "radius": 1,
                "extrusion": [0, 1, 0],
            },
            {"entity": "LWPOLYLINE", "entmode": 2, "points": [[0, 0], [1, 1]], "bulges": [1, 0]},
        ]
    }
    instances = list(iter_instances(drawing))
    assert len(instances) == 2
    assert all(item["bbox"] is None for item in instances)
    assert instances[0]["geometry_status"] == "unsupported_ocs"


def test_numeric_overflow_and_unknown_entity_owner_are_preserved_without_infinite_bounds():
    drawing = {
        "OBJECTS": [
            _block(10, "*Model_Space"),
            _block(20, "huge"),
            {
                "entity": "INSERT",
                "handle": [0, 0, 30],
                "entmode": 2,
                "block_header": [0, 0, 20],
                "ins_pt": [0, 0],
                "scale": [1e200, 1e200],
            },
            {
                "entity": "CIRCLE",
                "ownerhandle": [0, 0, 20],
                "center": [1e200, 1e200],
                "radius": 1e200,
            },
            {"object": "PROXY_OBJECT", "handle": [0, 0, 90]},
            {"entity": "LINE", "ownerhandle": [0, 0, 90], "start": [0, 0], "end": [1, 1]},
        ]
    }
    instances = {item["object_index"]: item for item in iter_instances(drawing)}
    assert instances[3]["bbox"] is None
    assert instances[3]["point"] is None
    assert instances[3]["geometry_status"] == "invalid_coordinates"
    assert instances[5]["space_kind"] == "unknown"
