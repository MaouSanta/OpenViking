# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import copy
import hashlib
import json

import pytest

from openviking.parse.parsers.dwg_json import (
    CHUNK_SCHEMA,
    DATA_SCHEMA,
    build_json_artifacts,
    read_json_records,
)
from openviking.parse.parsers.dwg_sheet import _drawing_graph, _inside, _walk_block


def drawing_fixture():
    return {
        "FILEHEADER": {"version": "AC1032"},
        "CLASSES": [{"custom": "原样保留"}],
        "extra": {"empty": [], "nil": None, "bool": True, "float": 1.0},
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            {
                "entity": "LWPOLYLINE",
                "handle": [0, 0, 2],
                "ownerhandle": [0, 0, 1],
                "flag": 512,
                "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
            },
            {
                "entity": "TEXT",
                "handle": [0, 0, 3],
                "ownerhandle": [0, 0, 1],
                "ins_pt": [15, 75],
                "height": 2,
                "text_value": "图中标注 KL1 300x500",
            },
            {
                "entity": "MTEXT",
                "handle": [0, 0, 4],
                "ownerhandle": [0, 0, 1],
                "ins_pt": [15, 15],
                "text_height": 2,
                "text": "1. 施工前应核对现场尺寸。",
            },
            {
                "entity": "LINE",
                "handle": [0, 0, 5],
                "ownerhandle": [0, 0, 1],
                "start": [15, 55],
                "end": [85, 55],
                "extra": {"keep": "all"},
            },
            {"object": "PROXY_OBJECT", "data": [None, False, 4.5]},
            None,
        ],
    }


def partition(_image, _evidence):
    return {
        "regions": [
            {"type": "document", "bbox": [0, 0, 100, 40]},
        ]
    }


def fixed_sheet(drawing, *, bbox=(0, 0, 100, 100), sheet_id="S001", prose=True):
    # JSON storage tests fix Sheet/Markdown decisions without invoking any model.
    texts = [
        record
        for record in _walk_block(1, _drawing_graph(drawing))["texts"]
        if _inside(bbox, record["point"])
    ]
    selected = [record for record in texts if prose and record["point"][1] <= 40]
    directory = f"sheets/{sheet_id}"
    return {
        "sheet_id": sheet_id,
        "directory": directory,
        "kind": "model_frame",
        "status": "complete",
        "bbox": list(bbox),
        "source_locator": {"kind": "closed_polyline", "source_ref": {"handle": 2}},
        "content_paths": [f"{directory}/context.md"],
        "content_files": [("context.md", "\n".join(record["text"] for record in selected))],
        "_texts": texts,
        "markdown_uids": [record["uid"] for record in selected],
    }


def build(drawing, *, prose=True):
    sheets = [fixed_sheet(drawing, prose=prose)]
    return sheets, build_json_artifacts(drawing, sheets)


def scoped_objects(result, prefix):
    return {
        item["object_index"]: item
        for item in read_json_records(
            content for path, content in result["files"].items() if path.startswith(prefix)
        )
        if item.get("kind") == "object"
    }


def records(result):
    return read_json_records(result["files"].values())


def test_json_preserves_every_object_and_header_and_moves_only_prose():
    drawing = drawing_fixture()
    before = copy.deepcopy(drawing)
    sheets, result = build(drawing)
    assert drawing == before
    restored = records(result)
    objects = {item["object_index"]: item for item in restored if item.get("kind") == "object"}
    assert set(objects) == set(range(len(drawing["OBJECTS"])))
    assert next(item["value"] for item in restored if item.get("kind") == "header") == {
        key: value for key, value in drawing.items() if key != "OBJECTS"
    }
    for index, original in enumerate(drawing["OBJECTS"]):
        if index == 3:
            assert objects[index]["value"] == {
                key: value for key, value in original.items() if key != "text"
            }
            assert objects[index]["markdown_fields"] == ["text"]
            assert objects[index]["markdown"]
        else:
            assert objects[index]["value"] == original
    markdown = "\n".join(content for sheet in sheets for _, content in sheet["content_files"])
    assert "施工前应核对现场尺寸" in markdown
    assert "图中标注 KL1" not in markdown
    assert any(path.startswith("metadata.json") for path in result["files"])
    assert any(path.startswith("sheets/S001/drawing.json") for path in result["files"])
    assert not any(
        name in path
        for path in result["files"]
        for name in ("views/", "shared/", "catalog/", "unassigned/")
    )
    assert {item["kind"] for item in restored} == {"object", "document", "header", "sheet"}
    assert scoped_objects(result, "sheets/S001/drawing.json")[4]["value"] == drawing["OBJECTS"][4]
    assert set(scoped_objects(result, "metadata.json")) == {0, 5, 6}
    assert result["parse_status"] == "complete"


def test_ablation_without_vlm_keeps_all_native_fields_in_scope_files():
    drawing = drawing_fixture()
    _, baseline = build(drawing, prose=False)
    _, partitioned = build(drawing)
    objects = sorted(
        (item for item in records(baseline) if item.get("kind") == "object"),
        key=lambda item: item["object_index"],
    )
    assert [item["value"] for item in objects] == drawing["OBJECTS"]
    assert baseline["parse_status"] == "complete"
    assert any(path.startswith("sheets/S001/drawing.json") for path in baseline["files"])
    assert not any("/views/" in path for path in partitioned["files"])
    assert len(objects) == sum(item.get("kind") == "object" for item in records(partitioned))


def test_large_objects_and_metadata_stay_whole_in_pretty_scope_json():
    drawing = drawing_fixture()
    drawing["extra"]["巨大/~字段" * 100] = '中文😀\\"' * 1200
    drawing["OBJECTS"][4]["vertices"] = [[i, i / 3, None] for i in range(300)]
    _, result = build(drawing)
    restored = records(result)
    assert (
        next(item["value"] for item in restored if item.get("kind") == "header")["extra"]
        == drawing["extra"]
    )
    assert (
        next(
            item["value"]
            for item in restored
            if item.get("kind") == "object" and item["object_index"] == 4
        )
        == drawing["OBJECTS"][4]
    )
    assert not any(item.get("kind") in {"locator", "instance", "view"} for item in restored)
    assert set(result["files"]) == {"metadata.json", "drawing.json", "sheets/S001/drawing.json"}
    assert len(result["files"]["metadata.json"]) > 6000
    assert len(result["files"]["sheets/S001/drawing.json"]) > 6000
    for content in result["files"].values():
        payload = json.loads(content)
        assert payload["schema"] == DATA_SCHEMA
        assert not any(
            "fragment" in record and "kind" not in record for record in payload["records"]
        )
        assert content == json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    assert "budget" not in next(row for row in restored if row.get("kind") == "document")


def legacy_fragments(record):
    # Fixed legacy wire shape for backward-reader coverage; there is no v8 writer in production.
    serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    pieces = [serialized[offset : offset + 100] for offset in range(0, len(serialized), 100)]
    identity = hashlib.sha256(serialized.encode()).hexdigest()
    return [
        json.dumps(
            {
                "schema": CHUNK_SCHEMA,
                "records": [
                    {
                        "fragment": identity,
                        "part": index,
                        "total": len(pieces),
                        "text": piece,
                    }
                ],
            },
            ensure_ascii=False,
        )
        for index, piece in enumerate(pieces)
    ]


def test_legacy_fragment_reader_keeps_missing_corrupt_and_duplicate_guards():
    original = {"kind": "object", "object_index": 0, "value": "中文" * 1000}
    chunks = legacy_fragments(original)
    assert read_json_records(reversed(chunks)) == [original]
    assert read_json_records([json.dumps({"schema": DATA_SCHEMA, "records": [original]})]) == [
        original
    ]
    with pytest.raises(ValueError, match="Missing"):
        read_json_records(chunks[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        read_json_records([*chunks, chunks[0]])
    corrupted = json.loads(chunks[0])
    corrupted["records"][0]["text"] += "x"
    with pytest.raises(ValueError, match="checksum"):
        read_json_records([json.dumps(corrupted), *chunks[1:]])


def test_reader_accepts_new_scope_files_and_unfragmented_v8_records():
    expected = [
        {"kind": "object", "object_index": 0, "value": {"integer": 1, "float": 1.0, "bool": True}},
        {"kind": "header", "value": {"CLASS": [None, "原始值"]}},
    ]
    old = json.dumps({"schema": CHUNK_SCHEMA, "records": expected}, ensure_ascii=False)
    new = json.dumps({"schema": DATA_SCHEMA, "records": expected}, ensure_ascii=False, indent=2)
    assert read_json_records([old]) == read_json_records([new]) == expected


def test_unused_text_alias_is_not_deleted():
    drawing = drawing_fixture()
    drawing["OBJECTS"][3]["text_value"] = "备用字段也必须保留"
    _, result = build(drawing)
    raw = next(
        item["value"]
        for item in records(result)
        if item.get("kind") == "object" and item["object_index"] == 3
    )
    assert "text" not in raw
    assert raw["text_value"] == "备用字段也必须保留"


def test_shared_block_text_in_view_and_prose_stays_in_native_json():
    drawing = drawing_fixture()
    drawing["OBJECTS"].extend(
        [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "Repeated", "base_pt": [0, 0]},
            {
                "entity": "TEXT",
                "handle": [0, 0, 21],
                "ownerhandle": [0, 0, 20],
                "ins_pt": [0, 0],
                "height": 2,
                "text_value": "重复块文字保持原样",
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 22],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [20, 20],
                "scale": [1, 1, 1],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 23],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [70, 70],
                "scale": [1, 1, 1],
            },
        ]
    )
    _, result = build(drawing)
    raw = next(
        item
        for item in records(result)
        if item.get("kind") == "object" and item["object_index"] == 8
    )
    assert raw["value"] == drawing["OBJECTS"][8]
    assert "markdown_fields" not in raw
    assert raw["markdown"][0]["instance_path"] == [22]
    assert len(raw["markdown"]) == 1
    assert 8 in scoped_objects(result, "metadata.json")
    placed = scoped_objects(result, "sheets/S001/drawing.json")
    assert placed[9]["value"] == drawing["OBJECTS"][9]
    assert placed[10]["value"] == drawing["OBJECTS"][10]
    assert "bbox" not in raw


def test_unknown_and_outside_entities_are_retained():
    drawing = drawing_fixture()
    drawing["OBJECTS"].extend(
        [
            {
                "entity": "ACAD_PROXY_ENTITY",
                "handle": [0, 0, 33],
                "ownerhandle": [0, 0, 1],
                "bytes": "FF",
            },
            {
                "entity": "LINE",
                "handle": [0, 0, 34],
                "ownerhandle": [0, 0, 1],
                "start": [1000, 1000],
                "end": [2000, 2000],
            },
        ]
    )
    _, result = build(drawing)
    assert result["parse_status"] == "partial"
    unassigned = scoped_objects(result, "drawing.json")
    assert set(unassigned) == {7, 8}
    assert unassigned[7]["status"] == "partial"
    assert "bbox" not in unassigned[7]
    assert unassigned[8]["bbox"] == [1000.0, 1000.0, 2000.0, 2000.0]


@pytest.mark.asyncio
async def test_production_parser_writes_whole_json_independent_of_embedding_budget(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from openviking.parse.parsers import dwg
    from openviking.parse.parsers.base_parser import BaseParser
    from tests.parse.dwg_test_helpers import _FakeVikingFS

    source = tmp_path / "source.dwg"
    source.write_bytes(b"source snapshot")
    drawing = drawing_fixture()
    drawing["OBJECTS"][4]["payload"] = "原始大对象" * 4000
    fs = _FakeVikingFS()
    original_mkdir = fs.mkdir

    async def mkdir_once(uri, exist_ok=False):
        assert uri not in fs.dirs
        await original_mkdir(uri, exist_ok=exist_ok)

    monkeypatch.setattr(fs, "mkdir", mkdir_once)
    parser = dwg.DWGParser()
    monkeypatch.setattr(
        dwg,
        "get_openviking_config",
        lambda: SimpleNamespace(
            markdown=SimpleNamespace(max_section_size=2048, max_section_chars=6000),
            vlm=SimpleNamespace(max_concurrent=4),
            embedding=SimpleNamespace(max_input_tokens=1),
        ),
    )
    monkeypatch.setattr(BaseParser, "_get_viking_fs", lambda _: fs)

    async def export(_path):
        return drawing, b"{}", []

    monkeypatch.setattr(parser, "_export_dwg_json", export)
    result = await parser.parse(
        source, region_classifier=partition, sheet_renderer=lambda _: b"image"
    )
    root = result.meta["temp_root_uri"]
    json_paths = {uri.removeprefix(root + "/") for uri in fs.files if uri.endswith(".json")}
    described = {item["path"] for item in result.meta["json_artifacts"]}
    for sheet in result.meta["sheet_bundles"]:
        described.update(item["path"] for item in sheet["json_artifacts"])
    assert json_paths == described
    assert len(json_paths) == 3
    assert result.parser_version == "10.1"
    assert result.meta["parse_status"] == "complete"
    assert any(len(fs.files[f"{root}/{path}"]) > 6000 for path in json_paths)
    restored = read_json_records(fs.files[f"{root}/{path}"].decode() for path in json_paths)
    assert result.meta["sheet_count"] == result.meta["sheet_bundle_count"] == 1
    diagnostics = next(row for row in restored if row["kind"] == "sheet_detection")
    assert diagnostics["statistics"] == result.meta["detection_statistics"]
    assert diagnostics["statistics"]["detected_sheet_count"] == 1
    assert diagnostics["candidates"][0]["region_diagnostic"]["status"] == "used"
    assert (
        next(
            row["value"]
            for row in restored
            if row.get("kind") == "object" and row["object_index"] == 4
        )
        == drawing["OBJECTS"][4]
    )
    for path in json_paths:
        content = fs.files[f"{root}/{path}"].decode("utf-8")
        assert json.loads(content)["schema"] == DATA_SCHEMA
        assert len(content.splitlines()) > 2
    assert fs.files[f"{root}/source.dwg"] == source.read_bytes()
    assert not any(path.endswith("sheet.dwg") for path in fs.files)


def add_text_block(drawing, positions):
    drawing["OBJECTS"].extend(
        [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "Note", "base_pt": [0, 0]},
            {
                "entity": "TEXT",
                "handle": [0, 0, 21],
                "ownerhandle": [0, 0, 20],
                "ins_pt": [0, 0],
                "height": 2,
                "text_value": "每个实例均需保全",
            },
        ]
    )
    for index, point in enumerate(positions, 22):
        drawing["OBJECTS"].append(
            {
                "entity": "INSERT",
                "handle": [0, 0, index],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": list(point),
                "scale": [1, 1, 1],
            }
        )


def by_handle(rows):
    return {
        row["value"]["handle"][-1]: row
        for row in rows.values()
        if isinstance(row["value"], dict) and row["value"].get("handle")
    }


def test_every_markdown_use_is_required_and_only_its_paths_are_persisted():
    drawing = drawing_fixture()
    add_text_block(drawing, [(20, 20), (70, 20)])
    _, result = build(drawing)
    metadata = by_handle(scoped_objects(result, "metadata.json"))
    note = metadata[21]
    assert "text_value" not in note["value"]
    assert note["markdown_fields"] == ["text_value"]
    assert {tuple(ref["instance_path"]) for ref in note["markdown"]} == {(22,), (23,)}
    assert "bbox" not in note
    placed = by_handle(scoped_objects(result, "sheets/S001/drawing.json"))
    assert {22, 23} <= placed.keys()
    assert placed[22]["value"]["block_header"] == [0, 0, 20]
    assert not any(row["kind"] == "instance" for row in records(result))


def test_unknown_insert_use_prevents_removing_shared_definition_text():
    drawing = drawing_fixture()
    add_text_block(drawing, [(20, 20)])
    sheet = fixed_sheet(drawing)
    # The undecodable transform means the second use cannot appear in _texts.
    drawing["OBJECTS"].append(
        {
            "entity": "INSERT",
            "handle": [0, 0, 23],
            "ownerhandle": [0, 0, 1],
            "block_header": [0, 0, 20],
            "ins_pt": [70, 20],
            "scale": ["invalid", 1, 1],
        }
    )
    before = copy.deepcopy(drawing)
    result = build_json_artifacts(drawing, [sheet])
    note = by_handle(scoped_objects(result, "metadata.json"))[21]
    assert note["value"]["text_value"] == "每个实例均需保全"
    assert "markdown_fields" not in note
    assert note["markdown"][0]["instance_path"] == [22]
    invalid = by_handle(scoped_objects(result, "drawing.json"))[23]
    assert invalid["value"]["scale"] == ["invalid", 1, 1]
    assert invalid["status"] == result["parse_status"] == "partial"
    assert drawing == before


def test_unused_definition_members_and_attributes_are_metadata_without_failure():
    drawing = drawing_fixture()
    drawing["OBJECTS"].extend(
        [
            {
                "object": "BLOCK_HEADER",
                "handle": [0, 0, 30],
                "name": "Unused",
                "entities": [[0, 0, 31], [0, 0, 32]],
            },
            {"entity": "LINE", "handle": [0, 0, 31], "start": [9000, 9000], "end": [9500, 9000]},
            {
                "entity": "INSERT",
                "handle": [0, 0, 32],
                "ownerhandle": [0, 0, 30],
                "block_header": [0, 0, 40],
                "ins_pt": [0, 0],
            },
            {
                "entity": "ATTRIB",
                "handle": [0, 0, 33],
                "ownerhandle": [0, 0, 32],
                "ins_pt": [0, 0],
                "height": 2,
                "text_value": "未使用的属性",
            },
            {"object": "BLOCK_HEADER", "handle": [0, 0, 40], "name": "NestedUnused"},
            {
                "entity": "TEXT",
                "handle": [0, 0, 41],
                "ownerhandle": [0, 0, 40],
                "ins_pt": [0, 0],
                "text_value": "未使用的正文",
            },
        ]
    )
    _, result = build(drawing, prose=False)
    metadata = by_handle(scoped_objects(result, "metadata.json"))
    assert {30, 31, 32, 33, 40, 41} <= metadata.keys()
    assert metadata[31]["value"] == drawing["OBJECTS"][8]
    assert metadata[41]["value"]["text_value"] == "未使用的正文"
    assert all("bbox" not in metadata[handle] for handle in (31, 32, 33, 41))
    assert result["parse_status"] == "complete"
    assert not scoped_objects(result, "drawing.json")


def test_cross_sheet_objects_and_containers_stay_at_document_scope():
    drawing = drawing_fixture()
    drawing["OBJECTS"].append(
        {
            "entity": "LWPOLYLINE",
            "handle": [0, 0, 6],
            "ownerhandle": [0, 0, 1],
            "flag": 512,
            "points": [[200, 0], [300, 0], [300, 100], [200, 100]],
        }
    )
    add_text_block(drawing, [(20, 20), (220, 20)])
    drawing["OBJECTS"].extend(
        [
            {
                "entity": "LINE",
                "handle": [0, 0, 50],
                "ownerhandle": [0, 0, 1],
                "start": [50, 50],
                "end": [250, 50],
            },
            {"object": "BLOCK_HEADER", "handle": [0, 0, 40], "name": "Crossing"},
            {
                "entity": "LINE",
                "handle": [0, 0, 41],
                "ownerhandle": [0, 0, 40],
                "start": [0, 0],
                "end": [220, 0],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 42],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 40],
                "ins_pt": [20, 50],
            },
        ]
    )
    sheets = [fixed_sheet(drawing), fixed_sheet(drawing, bbox=(200, 0, 300, 100), sheet_id="S002")]
    result = build_json_artifacts(drawing, sheets)
    document = by_handle(scoped_objects(result, "drawing.json"))
    assert set(document) == {42, 50}
    assert document[50]["bbox"] == [50.0, 50.0, 250.0, 50.0]
    assert document[42]["value"]["ins_pt"] == [20, 50]
    assert 22 in by_handle(scoped_objects(result, "sheets/S001/drawing.json"))
    assert 23 in by_handle(scoped_objects(result, "sheets/S002/drawing.json"))
    metadata = by_handle(scoped_objects(result, "metadata.json"))
    assert {20, 21, 40, 41} <= metadata.keys()
    assert {ref["sheet_id"] for ref in metadata[21]["markdown"]} == {"S001", "S002"}
    assert "text_value" not in metadata[21]["value"]
    assert result["parse_status"] == "complete"


def test_scope_file_keeps_spatial_order_without_markdown_or_view_regions():
    drawing = drawing_fixture()
    for index in range(20):
        drawing["OBJECTS"].append(
            {
                "entity": "POINT",
                "handle": [0, 0, 100 + index],
                "ownerhandle": [0, 0, 1],
                "point": [10 + index // 2 + (70 if index % 2 else 0), 50],
                "payload": "x" * 80,
            }
        )
    sheets, result = build(drawing, prose=False)
    spatial = [
        content
        for path, content in result["files"].items()
        if path.startswith("sheets/S001/drawing.json")
    ]
    assert len(spatial) == 1
    handles = [
        row["value"]["handle"][-1]
        for row in read_json_records(spatial)
        if row.get("kind") == "object" and row["value"].get("entity") == "POINT"
    ]
    assert handles == [*range(100, 120, 2), *range(101, 120, 2)]
    assert len(result["files"]) == 3
    assert "regions" not in sheets[0]
    assert result["parse_status"] == "complete"
    assert build(drawing, prose=False)[1]["files"] == result["files"]


def test_native_table_keeps_its_rows_and_other_fields_when_markdown_exists():
    drawing = drawing_fixture()
    table = {
        "entity": "TABLE",
        "handle": [0, 0, 80],
        "ownerhandle": [0, 0, 1],
        "ins_pt": [20, 20],
        "rows": [["项目", "数量"], ["梁", 2]],
        "text_value": "原生表格正文",
    }
    drawing["OBJECTS"].append(table)
    sheet = fixed_sheet(drawing)
    sheet["content_files"][0] = ("context.md", "| 项目 | 数量 |\n| --- | --- |\n| 梁 | 2 |")
    result = build_json_artifacts(drawing, [sheet])
    record = next(
        row for row in records(result) if row.get("kind") == "object" and row["object_index"] == 7
    )
    assert record["value"] == table
    assert "markdown_fields" not in record


def test_unknown_block_geometry_marks_its_container_and_sheet_partial():
    drawing = drawing_fixture()
    drawing["OBJECTS"].extend(
        [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 60], "name": "UnknownGeometry"},
            {
                "entity": "HATCH",
                "handle": [0, 0, 61],
                "ownerhandle": [0, 0, 60],
                "paths": ["native-data"],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 62],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 60],
                "ins_pt": [30, 30],
            },
        ]
    )
    sheets, result = build(drawing, prose=False)
    assert result["parse_status"] == sheets[0]["status"] == "partial"
    assert by_handle(scoped_objects(result, "drawing.json"))[62]["status"] == "partial"
    assert by_handle(scoped_objects(result, "metadata.json"))[61]["value"] == drawing["OBJECTS"][8]
