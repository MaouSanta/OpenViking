# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import copy
import json

import pytest

from openviking.parse.parsers.dwg_json import bounded_json, build_json_artifacts, read_json_records
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts
from openviking.utils.embedding_input import estimate_embedding_input_tokens
from openviking_cli.utils.config.open_viking_config import OpenVikingConfig


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
                "flag": 1,
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
            {"type": "view", "bbox": [0, 40, 50, 100], "title": "平面图"},
            {"type": "view", "bbox": [50, 40, 100, 100], "title": "局部图"},
        ]
    }


def build(drawing, classifier=partition, **budgets):
    sheets = build_sheet_artifacts(
        drawing,
        source_name="source.dwg",
        source_sha256="a" * 64,
        source_size_bytes=1,
        region_classifier=classifier,
        sheet_renderer=lambda _: b"test-render",
    )["sheets"]
    return sheets, build_json_artifacts(drawing, sheets, **budgets)


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
    assert any("/chunks/" in path for path in result["files"])
    assert all(
        not any(part in {"views", "shared", "unassigned", "catalog"} for part in path.split("/"))
        for path in result["files"]
    )
    cross = [
        item for item in restored if item.get("kind") == "instance" and item["object_index"] == 4
    ]
    assert len(cross) == 1
    assert not any(item["kind"] in {"locator", "view", "sheet", "document"} for item in restored)
    assets = result["asset_chunks"][sheets[0]["sheet_id"]]
    assert len(assets) == 2
    assert all(asset["complete"] for asset in assets.values())
    assert result["parse_status"] == "complete"


def test_ablation_without_vlm_keeps_all_native_fields_and_reports_partial():
    drawing = drawing_fixture()
    _, baseline = build(drawing, classifier=None)
    _, partitioned = build(drawing)
    objects = sorted(
        (item for item in records(baseline) if item.get("kind") == "object"),
        key=lambda item: item["object_index"],
    )
    assert [item["value"] for item in objects] == drawing["OBJECTS"]
    assert baseline["parse_status"] == "partial"
    assert not any("/views/" in path for path in baseline["files"])
    assert any(partitioned["asset_chunks"].values())
    assert len(objects) == sum(item.get("kind") == "object" for item in records(partitioned))


def test_oversized_objects_and_header_roundtrip_and_every_file_bounded():
    drawing = drawing_fixture()
    drawing["extra"]["巨大/~字段" * 100] = '中文😀\\"' * 400
    drawing["OBJECTS"][4]["vertices"] = [[i, i / 3, None] for i in range(300)]
    _, result = build(drawing, max_tokens=180, max_chars=500)
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
    assert not any(item.get("kind") == "locator" for item in restored)
    object_chunks = {
        path
        for path, content in result["files"].items()
        if any("fragment" in record for record in json.loads(content)["records"])
        and "/chunks/" in path
    }
    assert len(object_chunks) > 2
    for sheet_assets in result["asset_chunks"].values():
        for asset in sheet_assets.values():
            assert object_chunks <= set(asset["paths"])
    for content in result["files"].values():
        assert len(content) <= 500
        assert estimate_embedding_input_tokens(content) <= 180
        assert isinstance(json.loads(content), dict)


def test_fragment_reader_rejects_missing_corrupt_and_duplicate_parts():
    chunks = bounded_json(
        [{"kind": "object", "value": "中文" * 1000}], max_tokens=180, max_chars=500
    )
    assert len(chunks) > 2
    with pytest.raises(ValueError, match="Missing"):
        read_json_records(chunks[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        read_json_records([*chunks, chunks[0]])
    corrupted = json.loads(chunks[0])
    corrupted["records"][0]["text"] += "x"
    with pytest.raises(ValueError, match="checksum"):
        read_json_records([json.dumps(corrupted), *chunks[1:]])
    with pytest.raises(ValueError, match="budget"):
        bounded_json([], max_tokens=10, max_chars=30)


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


def test_unresolved_insert_cannot_claim_all_text_placements_are_in_markdown():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "Repeat"},
            {
                "entity": "TEXT",
                "handle": [0, 0, 21],
                "ownerhandle": [0, 0, 20],
                "ins_pt": [0, 0],
                "text_value": "原正文保留直到所有实例都可确认",
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 22],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [10, 10],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 23],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [70, 70],
                "extrusion": [0, 1, 0],
            },
        ]
    }
    sheet = spatial_sheet()
    sheet["markdown_uids"] = ["written"]
    sheet["_texts"] = [{"uid": "written", "record": drawing["OBJECTS"][2], "instance_path": [22]}]
    result = build_json_artifacts(drawing, [sheet])
    source = next(
        record
        for record in records(result)
        if record["kind"] == "object" and record["object_index"] == 2
    )
    assert source["value"] == drawing["OBJECTS"][2]
    assert "markdown_fields" not in source
    assert result["parse_status"] == "partial"


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
    unassigned = read_json_records(
        content for path, content in result["files"].items() if path.startswith("chunks/")
    )
    assert {item["object_index"] for item in unassigned if item.get("kind") == "object"} == {7, 8}


def spatial_sheet(*, regions=None, bounds=None, sheet_id="sheet-a"):
    return {
        "sheet_id": sheet_id,
        "directory": f"sheets/{sheet_id}",
        "kind": "model_frame",
        "bbox": bounds or [0, 0, 100, 100],
        "status": "complete",
        "regions": regions or [],
        "source_locator": {},
        "content_paths": [f"sheets/{sheet_id}/context.md"],
    }


def test_asset_boundaries_titles_and_presence_do_not_change_json_bytes():
    drawing = drawing_fixture()
    sheets, initial = build(drawing)
    sheets[0]["regions"] = [
        {"type": "view", "id": "entire-sheet", "bbox": [0, 0, 100, 100], "title": "新标题"}
    ]
    regrouped = build_json_artifacts(drawing, sheets)
    sheets[0]["regions"] = []
    no_navigation = build_json_artifacts(drawing, sheets)
    assert initial["files"] == regrouped["files"] == no_navigation["files"]
    assert regrouped["asset_chunks"] != initial["asset_chunks"]
    assert no_navigation["asset_chunks"] == {sheets[0]["sheet_id"]: {}}
    assert no_navigation["parse_status"] == "complete"


def test_noncontiguous_asset_files_and_shared_chunks_are_actual_paths():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            *[
                {
                    "entity": "LINE",
                    "handle": [0, 0, index + 2],
                    "ownerhandle": [0, 0, 1],
                    "start": [index * 10, 0 if index % 2 == 0 else 10],
                    "end": [index * 10 + 1, 0 if index % 2 == 0 else 10],
                    "payload": "X" * 120,
                }
                for index in range(12)
            ],
        ]
    }
    sheet = spatial_sheet(
        bounds=[-1, -1, 120, 20],
        regions=[
            {"type": "view", "id": "bottom", "bbox": [-1, -1, 120, 1]},
            {"type": "view", "id": "bottom-left", "bbox": [-1, -1, 30, 1]},
        ],
    )
    result = build_json_artifacts(drawing, [sheet], max_chars=560, max_tokens=200)
    assets = result["asset_chunks"][sheet["sheet_id"]]
    bottom = assets["bottom"]["paths"]
    assert set(assets["bottom-left"]["paths"]) & set(bottom)
    numbers = [
        int(path.rsplit("_", 1)[1].removesuffix(".json")) for path in bottom if "/chunks/" in path
    ]
    assert any(right - left > 1 for left, right in zip(numbers, numbers[1:], strict=False))
    assert set(bottom) <= set(result["files"])
    assert all(asset["complete"] for asset in assets.values())
    selected = read_json_records(result["files"][path] for path in bottom)
    selected_objects = {record["object_index"] for record in selected if record["kind"] == "object"}
    assert set(range(1, 13, 2)) <= selected_objects


def test_repeated_definition_is_metadata_and_all_fragments_resolve_for_each_asset():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "Repeat"},
            {
                "entity": "LINE",
                "handle": [0, 0, 21],
                "ownerhandle": [0, 0, 20],
                "start": [0, 0],
                "end": [5, 5],
                "original_array": list(range(500)),
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 22],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [20, 20],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 23],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [70, 70],
            },
        ]
    }
    sheet = spatial_sheet(
        regions=[
            {"type": "view", "id": "first", "bbox": [10, 10, 30, 30]},
            {"type": "view", "id": "second", "bbox": [60, 60, 80, 80]},
        ]
    )
    result = build_json_artifacts(drawing, [sheet], max_chars=500, max_tokens=180)
    raw = records(result)
    instances = [record for record in raw if record["kind"] == "instance"]
    assert len(instances) == 4
    assert {
        tuple(record["instance_path"]) for record in instances if record["object_index"] == 2
    } == {(22,), (23,)}
    metadata = read_json_records(
        content for path, content in result["files"].items() if path.startswith("metadata_")
    )
    assert (
        next(record["value"] for record in metadata if record.get("object_index") == 2)
        == drawing["OBJECTS"][2]
    )
    definition_fragments = {
        path
        for path, content in result["files"].items()
        if path.startswith("metadata_") and "fragment" in json.loads(content)["records"][0]
    }
    assert len(definition_fragments) > 2
    for asset in result["asset_chunks"][sheet["sheet_id"]].values():
        assert definition_fragments <= set(asset["paths"])
        assert asset["complete"]
        resolved = read_json_records(result["files"][path] for path in asset["paths"])
        assert any(
            record.get("object_index") == 2 and record["kind"] == "object" for record in resolved
        )


def test_container_anchor_and_frame_do_not_bind_unrelated_assets():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "Offset"},
            {
                "entity": "LINE",
                "handle": [0, 0, 21],
                "ownerhandle": [0, 0, 20],
                "start": [70, 70],
                "end": [80, 80],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 22],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [0, 0],
            },
            {
                "entity": "LWPOLYLINE",
                "handle": [0, 0, 23],
                "ownerhandle": [0, 0, 1],
                "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
                "flag": 1,
            },
        ]
    }
    sheet = spatial_sheet(
        regions=[
            {"type": "view", "id": "empty", "bbox": [0, 0, 10, 10]},
            {"type": "view", "id": "actual", "bbox": [65, 65, 85, 85]},
        ]
    )
    sheet["source_locator"] = {"source_ref": {"handle": 23}}
    result = build_json_artifacts(drawing, [sheet])
    assert result["asset_chunks"][sheet["sheet_id"]]["empty"] == {"paths": [], "complete": False}
    assert result["asset_chunks"][sheet["sheet_id"]]["actual"]["complete"]


def test_frame_exclusion_uses_placement_path_not_the_reused_definition_handle():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            {"object": "BLOCK_HEADER", "handle": [0, 0, 20], "name": "Rectangle"},
            {
                "entity": "LWPOLYLINE",
                "handle": [0, 0, 21],
                "ownerhandle": [0, 0, 20],
                "points": [[0, 0], [100, 0], [100, 100], [0, 100]],
                "flag": 1,
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 22],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [0, 0],
            },
            {
                "entity": "INSERT",
                "handle": [0, 0, 23],
                "ownerhandle": [0, 0, 1],
                "block_header": [0, 0, 20],
                "ins_pt": [20, 20],
                "scale": [0.1, 0.1, 1],
            },
        ]
    }
    sheet = spatial_sheet(
        regions=[
            {"type": "view", "id": "small-rectangle", "bbox": [20, 20, 30, 30]},
            {"type": "view", "id": "empty", "bbox": [50, 50, 60, 60]},
        ]
    )
    sheet["source_locator"] = {"source_ref": {"handle": 21, "instance_path": [22]}}
    result = build_json_artifacts(drawing, [sheet], max_chars=500, max_tokens=180)
    assets = result["asset_chunks"][sheet["sheet_id"]]
    assert assets["empty"] == {"paths": [], "complete": False}
    assert assets["small-rectangle"]["complete"]
    matched = read_json_records(
        result["files"][path] for path in assets["small-rectangle"]["paths"]
    )
    assert any(
        record["kind"] == "instance"
        and record["object_index"] == 2
        and record["instance_path"] == [23]
        for record in matched
    )


def test_cross_sheet_record_is_stored_once_and_unknown_mapping_is_partial():
    drawing = {
        "OBJECTS": [
            {"object": "BLOCK_HEADER", "handle": [0, 0, 1], "name": "*Model_Space"},
            {
                "entity": "LINE",
                "handle": [0, 0, 2],
                "ownerhandle": [0, 0, 1],
                "start": [20, 50],
                "end": [120, 50],
            },
            {"entity": "ACAD_PROXY_ENTITY", "handle": [0, 0, 3], "ownerhandle": [0, 0, 1]},
            {"object": "BLOCK_HEADER", "handle": [0, 0, 30], "name": "Dormant"},
            {
                "entity": "TEXT",
                "handle": [0, 0, 31],
                "ownerhandle": [0, 0, 30],
                "ins_pt": [0, 0],
                "text_value": "未放置定义仍保留",
            },
        ]
    }
    sheets = [
        spatial_sheet(
            bounds=[0, 0, 90, 100], regions=[{"type": "view", "id": "a", "bbox": [0, 0, 90, 100]}]
        ),
        spatial_sheet(
            sheet_id="sheet-b",
            bounds=[100, 0, 200, 100],
            regions=[{"type": "view", "id": "b", "bbox": [100, 0, 200, 100]}],
        ),
    ]
    result = build_json_artifacts(drawing, sheets)
    root = read_json_records(
        content for path, content in result["files"].items() if path.startswith("chunks/")
    )
    assert {record["object_index"] for record in root if record["kind"] == "object"} == {1, 2}
    assert (
        sum(
            record["kind"] == "instance" and record["object_index"] == 1
            for record in records(result)
        )
        == 1
    )
    assert result["parse_status"] == "partial"
    for assets in result["asset_chunks"].values():
        assert all(not asset["complete"] and asset["paths"] for asset in assets.values())
    restored = sorted(
        (record for record in records(result) if record["kind"] == "object"),
        key=lambda record: record["object_index"],
    )
    assert [record["value"] for record in restored] == drawing["OBJECTS"]


def test_dwg_budget_config_roundtrip():
    config = OpenVikingConfig.from_dict(
        {"parsers": {"dwg": {"json_max_tokens": 1024, "json_max_chars": 3000}}}
    )
    assert config.dwg.to_dict() == {"json_max_tokens": 1024, "json_max_chars": 3000}
    with pytest.raises(ValueError):
        OpenVikingConfig.from_dict({"dwg": {"json_max_tokens": 0}})


@pytest.mark.asyncio
async def test_production_parser_writes_all_described_chunks_with_embedding_headroom(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from openviking.parse.parsers import dwg
    from openviking.parse.parsers.base_parser import BaseParser
    from tests.parse.dwg_test_helpers import _FakeVikingFS

    source = tmp_path / "source.dwg"
    source.write_bytes(b"source snapshot")
    drawing = drawing_fixture()
    fs = _FakeVikingFS()
    parser = dwg.DWGParser()
    monkeypatch.setattr(
        dwg,
        "get_openviking_config",
        lambda: SimpleNamespace(
            markdown=SimpleNamespace(max_section_size=2048, max_section_chars=6000),
            dwg=SimpleNamespace(json_max_tokens=2048, json_max_chars=1000),
            embedding=SimpleNamespace(max_input_tokens=400),
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
    assert result.meta["parse_status"] == "complete"
    for path in json_paths:
        content = fs.files[f"{root}/{path}"].decode("utf-8")
        assert len(content) <= 1000
        assert estimate_embedding_input_tokens(content) <= 320
    assert fs.files[f"{root}/source.dwg"] == source.read_bytes()
    assert not any(path.endswith("sheet.dwg") for path in fs.files)
