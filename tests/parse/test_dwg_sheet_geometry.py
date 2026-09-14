from openviking.parse.parsers.dwg_sheet_geometry import (
    detect_marker_groups,
    detect_table_grids,
)


def _line(index, a, b):
    return {
        "a": a,
        "b": b,
        "record": {"index": index, "handle": [0, 1, index]},
        "source_ref": {"object_index": index, "handle": index},
    }


def _text(index, value, point, height=1):
    return {
        "uid": f"text-{index}",
        "text": value,
        "point": point,
        "height": height,
        "record": {"index": index, "handle": [0, 1, index]},
        "instance_path": [900],
    }


def _square(index, x, y, size=2):
    return {
        "bbox": (x, y, x + size, y + size),
        "source_kind": "closed_polyline",
        "source_ref": {"object_index": index, "handle": index, "instance_path": [900]},
    }


def test_detect_table_grid_restores_multilevel_header_and_sources():
    # The middle divider starts below the top header, so that header spans two columns.
    lines = [
        _line(1, (0, 0), (0, 30)),
        _line(2, (20, 0), (20, 30)),
        _line(3, (10, 0), (10, 20)),
        _line(4, (0, 0), (20, 0)),
        _line(5, (0, 10), (20, 10)),
        _line(6, (0, 20), (20, 20)),
        _line(7, (0, 30), (20, 30)),
    ]
    texts = [
        _text(101, "截面尺寸", (2, 25)),
        _text(102, "宽", (2, 15)),
        _text(103, "高", (12, 15)),
        _text(104, "100", (2, 5)),
        _text(105, "200", (12, 5)),
    ]

    tables = detect_table_grids(lines, texts, sheet_bbox=(0, 0, 100, 100))

    assert len(tables) == 1
    table = tables[0]
    assert table["rows"] == [["截面尺寸", ""], ["宽", "高"], ["100", "200"]]
    assert table["header_rows"] == 2
    assert table["spans"] == [{"row": 0, "column": 0, "row_span": 1, "column_span": 2}]
    assert table["cells"][0]["source_refs"]
    assert {ref["handle"] for ref in table["source_refs"]} >= {1, 2, 4, 5, 6, 7, 101}
    assert table["text_uids"] == ["text-101", "text-102", "text-103", "text-104", "text-105"]


def test_detect_table_grid_accepts_text_baselines_just_outside_edge_cells():
    lines = [
        _line(1, (0, 0), (0, 4)),
        _line(2, (3, 0), (3, 4)),
        _line(3, (8, 0), (8, 4)),
        _line(4, (13, 0), (13, 4)),
        *[_line(10 + row, (0, row), (13, row)) for row in range(5)],
    ]
    texts = [
        _text(101, "环境类别", (-0.4, 3.2)),
        _text(102, "板、墙、壳", (4, 3.2)),
        _text(103, "梁、", (9, 3.2)),
        _text(113, "柱、杆", (10, 3.21)),
        _text(104, "一", (-0.2, 2.2)),
        _text(105, "15", (4, 2.2)),
        _text(106, "20", (9, 2.2)),
        _text(107, "二a", (-0.2, 1.2)),
        _text(108, "20", (4, 1.2)),
        _text(109, "25", (9, 1.2)),
        _text(110, "二b", (-0.2, -0.1)),
        _text(111, "25", (4, 0.2)),
        _text(112, "35", (9, 0.2)),
    ]

    tables = detect_table_grids(lines, texts, sheet_bbox=(0, 0, 100, 100))

    assert len(tables) == 1
    assert tables[0]["rows"] == [
        ["环境类别", "板、墙、壳", "梁、柱、杆"],
        ["一", "15", "20"],
        ["二a", "20", "25"],
        ["二b", "25", "35"],
    ]


def test_detect_table_grid_keeps_a_left_offset_caption_out_of_header_cells():
    lines = [
        *[_line(1 + column, (column * 10, 0), (column * 10, 30)) for column in range(3)],
        *[_line(10 + row, (0, row * 15), (20, row * 15)) for row in range(3)],
    ]
    texts = [
        _text(100, "3.2 结构设计等级：", (-0.6, 30.4)),
        _text(101, "安全等级", (2, 25)),
        _text(102, "抗震类别", (12, 25)),
        _text(103, "二级", (2, 5)),
        _text(104, "丙类", (12, 5)),
    ]

    table = detect_table_grids(lines, texts, sheet_bbox=(0, 0, 100, 100))[0]

    assert table["title"] == "3.2 结构设计等级："
    assert table["rows"] == [["安全等级", "抗震类别"], ["二级", "丙类"]]
    assert table["anchor"] == (-0.6, 30.4)


def test_detect_table_grid_merges_a_raised_unit_power_into_its_value():
    lines = [
        *[_line(1 + column, (column * 10, 0), (column * 10, 20)) for column in range(3)],
        *[_line(10 + row, (0, row * 10), (20, row * 10)) for row in range(3)],
    ]
    texts = [
        _text(101, "设计值", (2, 15)),
        _text(102, "等级", (12, 15)),
        _text(103, "fy=270N/mm", (2, 5)),
        _text(104, "2", (8, 5.5), height=0.6),
        _text(105, "C30", (12, 5)),
    ]

    table = detect_table_grids(lines, texts, sheet_bbox=(0, 0, 100, 100))[0]

    assert table["rows"] == [["设计值", "等级"], ["fy=270N/mm²", "C30"]]
    assert "text-104" in table["text_uids"]


def test_detect_table_grid_rejects_giant_sparse_sheet_alignment():
    lines = [
        _line(1, (0, 0), (0, 100)),
        _line(2, (50, 0), (50, 100)),
        _line(3, (100, 0), (100, 100)),
        _line(4, (0, 0), (100, 0)),
        _line(5, (0, 50), (100, 50)),
        _line(6, (0, 100), (100, 100)),
    ]
    texts = [_text(101, "一层平面图", (10, 75)), _text(102, "设备间", (60, 25))]

    assert detect_table_grids(lines, texts, sheet_bbox=(0, 0, 100, 100)) == []


def test_detect_table_grid_rejects_notes_panel_with_giant_text_cell():
    lines = [
        _line(1, (0, 0), (0, 30)),
        _line(2, (10, 0), (10, 30)),
        _line(3, (20, 0), (20, 30)),
        _line(4, (0, 0), (20, 0)),
        _line(5, (0, 10), (20, 10)),
        _line(6, (0, 20), (20, 20)),
        _line(7, (0, 30), (20, 30)),
    ]
    texts = [
        _text(101, "结构设计总说明" * 200, (2, 25)),
        _text(102, "普通说明", (12, 15)),
    ]

    assert detect_table_grids(lines, texts, sheet_bbox=(0, 0, 100, 100)) == []


def test_detect_marker_group_distinguishes_tick_cross_and_empty():
    frames = [_square(1, 0, 20), _square(2, 0, 10), _square(3, 0, 0)]
    lines = [
        _line(11, (0.3, 21.0), (0.8, 20.4)),
        _line(12, (0.8, 20.4), (1.7, 21.7)),
        _line(13, (0.3, 10.3), (1.7, 11.7)),
        _line(14, (0.3, 11.7), (1.7, 10.3)),
        # A same-source edge and a remote diagonal must not become selection marks.
        {**_line(1, (0.1, 20.1), (1.9, 21.9)), "source_ref": frames[0]["source_ref"]},
        _line(15, (1000, 1000), (1002, 1002)),
    ]
    texts = [
        _text(101, "现浇", (3, 21), height=1),
        _text(102, "预制", (3, 11), height=1),
        _text(103, "其他", (3, 1), height=1),
    ]

    groups = detect_marker_groups(frames, lines, texts)

    assert len(groups) == 1
    group = groups[0]
    assert group["coverage_complete"] is True
    assert [(marker["mark"], marker["state"]) for marker in group["markers"]] == [
        ("check", "selected"),
        ("cross", "selected"),
        ("none", "unselected"),
    ]
    assert [marker["label"] for marker in group["markers"]] == ["现浇", "预制", "其他"]
    assert all(marker["source_refs"] for marker in group["markers"])


def test_empty_marker_stays_unknown_when_group_coverage_is_incomplete():
    frames = [_square(1, 0, 10), _square(2, 0, 0)]
    lines = [
        _line(11, (0.3, 11.0), (0.8, 10.4)),
        _line(12, (0.8, 10.4), (1.7, 11.7)),
    ]
    texts = [_text(101, "选项 A", (3, 11), height=1)]

    groups = detect_marker_groups(frames, lines, texts)

    assert len(groups) == 1
    assert groups[0]["coverage_complete"] is False
    assert [marker["state"] for marker in groups[0]["markers"]] == ["selected", "unknown"]


def test_circle_frames_are_preserved_as_radio_markers():
    frames = [
        {
            "center": (1, 11),
            "radius": 1,
            "source_kind": "circle",
            "source_ref": {"object_index": 1, "handle": 1},
            "filled": True,
        },
        {
            "center": (1, 1),
            "radius": 1,
            "source_kind": "circle",
            "source_ref": {"object_index": 2, "handle": 2},
        },
    ]
    texts = [_text(101, "是", (3, 11)), _text(102, "否", (3, 1))]

    groups = detect_marker_groups(frames, [], texts)

    assert groups[0]["marker_type"] == "radio"
    assert [
        (marker["shape"], marker["mark"], marker["state"]) for marker in groups[0]["markers"]
    ] == [
        ("circle", "fill", "selected"),
        ("circle", "none", "unselected"),
    ]


def test_all_empty_explicit_group_remains_unknown_without_positive_mark():
    frames = [
        {**_square(1, 0, 10), "marker_group": "choice"},
        {**_square(2, 0, 0), "marker_group": "choice"},
    ]
    texts = [_text(101, "是", (3, 11)), _text(102, "否", (3, 1))]

    groups = detect_marker_groups(frames, [], texts)

    assert groups[0]["coverage_complete"] is False
    assert [marker["state"] for marker in groups[0]["markers"]] == ["unknown", "unknown"]


def test_concentric_frames_are_not_misread_as_duplicate_checked_options():
    frames = [_square(1, 0, 0, 2), _square(2, -0.25, -0.25, 2.5)]
    lines = [
        _line(11, (0.3, 1.0), (0.8, 0.4)),
        _line(12, (0.8, 0.4), (1.7, 1.7)),
    ]

    assert detect_marker_groups(frames, lines, [_text(101, "构造符号", (3, 1))]) == []
