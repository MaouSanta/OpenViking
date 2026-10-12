# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import copy
import html
import posixpath
import re
from urllib.parse import unquote

import pytest

from openviking.parse.parsers.dwg_assets import build_asset_catalogs
from openviking.utils.embedding_input import estimate_embedding_input_tokens


def sheet_fixture():
    return {
        "sheet_id": "S001",
        "directory": "sheets/S001",
        "title": "结构构造详图",
        "content_paths": ["sheets/S001/context.md"],
        "regions": [
            {
                "id": "R001",
                "type": "view",
                "number": "图五",
                "title": "斜梁、折梁配筋构造",
                "description": "一组立面及平面配筋示意。",
                "bbox": [0, 0, 100, 100],
            }
        ],
    }


def build(sheet, paths=(), *, complete=True, max_tokens=2048, max_chars=6000):
    build_asset_catalogs(
        [sheet],
        {sheet["sheet_id"]: {"R001": {"paths": list(paths), "complete": complete}}},
        max_tokens=max_tokens,
        max_chars=max_chars,
    )
    return "\n".join(content for _, content in sheet["asset_files"])


def linked_paths(sheet):
    return [
        posixpath.normpath(posixpath.join(sheet["directory"], unquote(path)))
        for _, content in sheet["asset_files"]
        for path in re.findall(r"\]\(([^)]+)\)", content)
    ]


def test_real_disjoint_ranges_and_dependencies_preserve_all_links():
    sheet = sheet_fixture()
    paths = [f"sheets/S001/chunks/chunk_{n:04d}.json" for n in [12, 13, 14, 31, 32]]
    paths += ["chunks/chunk_0001.json", "metadata_0001.json"]
    text = build(sheet, paths)
    assert "chunk\\_0012\\.json–chunk\\_0014\\.json" in text
    assert "chunk\\_0031\\.json–chunk\\_0032\\.json" in text
    assert "chunk\\_0012\\.json–chunk\\_0032\\.json" not in text
    assert "../../metadata_0001.json" in text
    assert set(linked_paths(sheet)) == set(paths)
    assert sheet["asset_paths"] == ["sheets/S001/assets.md"]
    assert sheet["content_paths"] == ["sheets/S001/context.md"]


def test_assets_may_share_a_chunk_without_changing_regions_or_body_paths():
    sheet = sheet_fixture()
    sheet["regions"].append({**sheet["regions"][0], "id": "R002", "number": "图六"})
    before = copy.deepcopy(sheet)
    path = "sheets/S001/chunks/chunk_0001.json"
    build_asset_catalogs(
        [sheet],
        {"S001": {key: {"paths": [path], "complete": True} for key in ["R001", "R002"]}},
        max_tokens=2048,
        max_chars=6000,
    )
    assert linked_paths(sheet) == [path, path]
    assert sheet["regions"] == before["regions"]
    assert sheet["content_paths"] == before["content_paths"]
    assert "共用" in sheet["asset_files"][0][1]


def test_long_asset_pages_keep_every_part_link_and_repeat_identity():
    sheet = sheet_fixture()
    paths = [f"sheets/S001/chunks/chunk_{n:04d}.json" for n in range(1, 121)]
    paths += [f"metadata_{n:04d}.json" for n in range(1, 8)]
    build(sheet, paths, max_tokens=240, max_chars=750)
    assert len(sheet["asset_files"]) > 5
    assert set(linked_paths(sheet)) == set(paths)
    assert len(linked_paths(sheet)) == len(paths)
    for index, (name, text) in enumerate(sheet["asset_files"], 1):
        assert name == f"assets_{index:03d}.md"
        assert len(text) <= 750
        assert estimate_embedding_input_tokens(text) <= 240
        assert "S001" in text and "R001" in text and "图五" in text
        assert "斜梁、折梁配筋构造" in text
    assert "（续）" in sheet["asset_files"][1][1]


def test_large_description_pages_preserve_escaped_text_and_links():
    sheet = sheet_fixture()
    description = "剖面<&[节点]>*配筋。" * 500
    sheet["regions"][0]["description"] = description
    path = "sheets/S001/chunks/chunk_0001.json"
    build(sheet, [path], max_tokens=210, max_chars=650)
    pieces = []
    for _, text in sheet["asset_files"]:
        assert len(text) <= 650
        assert estimate_embedding_input_tokens(text) <= 210
        for value in re.findall(r"图像概述(?:（续）)?：([^\n]*)", text):
            pieces.append(html.unescape(re.sub(r"\\(.)", r"\1", value)))
    assert "".join(pieces) == description
    assert linked_paths(sheet) == [path]


def test_model_text_and_local_urls_cannot_inject_markdown():
    sheet = sheet_fixture()
    sheet["title"] = "[sheet](https://bad.example)"
    sheet["regions"][0].update(
        number="图五\n# injected",
        title="![evil](https://bad.example) <script>alert(1)</script>",
        description="[run](javascript:evil) `code` & text",
    )
    paths = ["sheets/S001/chunks/a](https://bad.example).json"]
    text = build(sheet, paths)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "\\!\\[evil\\]\\(https://bad\\.example\\)" in text
    assert "\n# injected" not in text
    assert len(re.findall(r"(?<!\\)\]\(", text)) == 1
    assert linked_paths(sheet) == [posixpath.normpath(path) for path in paths]
    assert "%5D%28https%3A" in text


@pytest.mark.parametrize("regions", [[], [{"type": "document"}], [{"type": "table"}]])
def test_no_graphical_asset_produces_no_catalog(regions):
    sheet = {**sheet_fixture(), "regions": regions}
    build(sheet)
    assert sheet["asset_files"] == []
    assert sheet["asset_paths"] == []


@pytest.mark.parametrize("mapping", [{}, {"S001": {"R001": {"paths": [], "complete": True}}}])
def test_missing_mapping_stays_partial_without_fabricated_numbers(mapping):
    sheet = sheet_fixture()
    build_asset_catalogs([sheet], mapping, max_tokens=2048, max_chars=6000)
    text = sheet["asset_files"][0][1]
    assert "不完整（partial）" in text
    assert "尚未绑定" in text
    assert "chunk_" not in text


def test_missing_title_and_partial_mapping_stay_explicit():
    sheet = sheet_fixture()
    sheet["regions"][0]["title"] = ""
    text = build(sheet, ["sheets/S001/chunks/chunk_0001.json"], complete=False)
    assert "原题名：未读清" in text
    assert "不完整（partial）" in text


@pytest.mark.parametrize("path", ["../../outside.json", "/absolute.json"])
def test_paths_cannot_escape_document(path):
    with pytest.raises(ValueError, match="inside the DWG"):
        build(sheet_fixture(), [path])


@pytest.mark.parametrize("budget", [(1, 6000), (2048, 30), (110, 400)])
def test_unusable_budget_raises_instead_of_truncating_a_link(budget):
    with pytest.raises(ValueError, match="budget"):
        build(
            sheet_fixture(),
            ["sheets/S001/chunks/" + "very_long_path_" * 80 + ".json"],
            max_tokens=budget[0],
            max_chars=budget[1],
        )
