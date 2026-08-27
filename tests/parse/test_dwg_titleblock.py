from openviking.parse.parsers.dwg_sheet import _extract_meta, _title
from openviking.parse.parsers.dwg_titleblock import extract_titleblock_meta


def _text(uid, value, x, y, height=5):
    return {"uid": uid, "text": value, "point": (x, y), "height": height}


def test_extract_titleblock_meta_prefers_values_over_english_placeholders():
    records = [
        _text("body", "结构设计总说明", 100, 900, 10),
        _text("notice", "不得量取图纸尺寸施工。", 910, 980, 4),
        _text("notes", "NOTES:", 970, 930),
        _text("designer", "江苏东方建筑设计有限公司", 915, 760, 8),
        _text("cert", "A132003462", 970, 735),
        _text("client-label", "建设单位", 915, 600),
        _text("client-placeholder", "CLIENT", 980, 600),
        _text("client", "华天科技（南京）有限公司", 930, 575),
        _text("project-label", "项目名称", 915, 540),
        _text("project-placeholder", "PROJECT TITLE", 970, 540),
        _text("project", "华天科技（南京）有限公司110KV变电站工程设计", 930, 515),
        _text("title-label", "图 名", 915, 480),
        _text("title-placeholder", "DWG TITLE", 975, 480),
        _text("title", "钢筋混凝土结构说明（一）", 940, 455),
        _text("project-no-label", "工程编号", 915, 420),
        _text("project-no-placeholder", "PROJECT NO.", 915, 410, 4),
        _text("project-no", "2019-T021", 950, 416),
        _text("date-label", "出图日期", 915, 390),
        _text("date-placeholder", "DATE", 925, 380, 4),
        _text("revision-label", "修改版本", 950, 390),
        _text("revision-placeholder", "REV.", 952, 380, 4),
        _text("drawing-no-label", "图纸编号", 975, 390),
        _text("drawing-no-placeholder", "DWG NO.", 980, 380, 4),
        _text("date", "2019年11月", 920, 365),
        _text("revision", "0", 952, 365, 9),
        _text("drawing-no", "T0202-001", 978, 365),
    ]

    meta, used = extract_titleblock_meta(records, (0, 0, 1000, 1000))

    assert meta["图名"] == "钢筋混凝土结构说明（一）"
    assert meta["图号"] == "T0202-001"
    assert meta["项目名称"] == "华天科技（南京）有限公司110KV变电站工程设计"
    assert meta["工程编号"] == "2019-T021"
    assert meta["建设单位"] == "华天科技（南京）有限公司"
    assert meta["出图日期"] == "2019年11月"
    assert meta["版本"] == "0"
    assert meta["设计单位"] == "江苏东方建筑设计有限公司"
    assert meta["设计资质编号"] == "A132003462"
    assert "DWG TITLE" not in meta.values()
    assert "PROJECT TITLE" not in meta.values()
    assert {"notice", "drawing-no", "drawing-no-placeholder"} <= used


def test_extract_titleblock_meta_requires_a_titleblock_anchor_cluster():
    records = [
        _text("one", "图名", 900, 500),
        _text("value", "局部详图", 930, 450),
    ]

    assert extract_titleblock_meta(records, (0, 0, 1000, 1000)) == ({}, set())


def test_sheet_title_compacts_cjk_display_spacing_without_using_header_labels():
    classified = {
        "meta": {},
        "labels": [
            {"text": "图 纸 目 录", "role": "label", "count": 1},
            {"text": "图 纸 名 称", "role": "label", "count": 1},
        ],
        "unassigned": [],
    }

    assert _title(classified, "Sheet 001") == "图纸目录"


def test_sheet_meta_does_not_pair_adjacent_directory_headers_as_values():
    records = [
        _text("drawing-no", "图号", 10, 20),
        _text("revision", "版本", 20, 20),
        _text("drawing-title", "图 纸 名 称", 30, 20),
        _text("size", "图幅", 40, 20),
    ]

    meta, _ = _extract_meta(records, {}, (0, 0, 100, 100))

    assert "图号" not in meta
    assert "版本" not in meta
