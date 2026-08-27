import pytest

from openviking.parse.parsers.dwg_sheet import (
    _clean_text,
    _document_flow,
    _exploded_form_glyphs,
    _restore_form_slots,
    _roman_i_glyph,
)


def _event(uid, text, raw, x, *, layer="PUB_TEXT", y=100, height=100, width=0.7):
    return {
        "kind": "text",
        "uid": uid,
        "text": text,
        "point": (x, y),
        "height": height,
        "layer": layer,
        "record": {
            "entity": "TEXT",
            "text_value": raw,
            "height": height,
            "width_factor": width,
        },
        "form_fill_eligible": True,
        "source_refs": [{"handle": uid}],
    }


def _line(index, a, b, layer="11"):
    return {
        "a": a,
        "b": b,
        "layer": layer,
        "instance_path": [],
        "source_ref": {"object_index": index, "handle": index},
    }


def _i_lines(index, x, bottom=0, height=370, cap=114):
    top = bottom + height
    half = cap / 2
    return [
        _line(index, (x, bottom), (x, top)),
        _line(index + 1, (x - half, top), (x + half, top)),
        _line(index + 2, (x - half, bottom), (x + half, bottom)),
    ]


def test_restore_form_slots_reinserts_three_values_in_source_order():
    template = _event(
        "template",
        "主要使用功能为: 地下 层，地上 层。",
        "主要使用功能为:              地下    层，地上     层。",
        0,
    )
    group = [
        template,
        _event("room", "配电房", "配电房", 560, layer="11", y=98),
        _event("below", "0", "0", 1221, layer="11", y=102),
        _event("above", "2", "2", 1659, layer="11", y=102),
    ]

    assert _restore_form_slots(group) == "主要使用功能为: 配电房 地下0层，地上2层。"


def test_restore_form_slots_decodes_plus_minus_and_accepts_small_baseline_offset():
    group = [
        _event(
            "template",
            "1.4 本工程设计标高%%P0.00相当于绝对标高 m。",
            "1.4 本工程设计标高%%P0.00相当于绝对标高        m。",
            0,
            height=80.75,
            width=0.7,
        ),
        _event("value", "16.750", "16.750", 1562, layer="11", y=87, height=100),
    ]

    assert _restore_form_slots(group) == "1.4 本工程设计标高±0.00相当于绝对标高 16.750 m。"


def test_restore_form_slots_handles_multiple_template_fragments_on_one_line():
    group = [
        _event("first", "荷载 kN", "荷载      kN", 0),
        _event("load", "0.40", "0.40", 180, layer="VALUE"),
        _event("second", "（ 年），类别 类。", "（     年），类别    类。", 700),
        _event("year", "50", "50", 850, layer="VALUE"),
        _event("class", "B", "B", 1250, layer="VALUE"),
    ]

    assert _restore_form_slots(group) == "荷载 0.40 kN （ 50 年），类别B类。"


def test_restore_form_slots_keeps_unfilled_slots_while_binding_unique_values():
    group = [
        _event(
            "template",
            "主要使用功能为: 地下 层，地上 层。",
            "主要使用功能为:              地下    层，地上     层。",
            0,
        ),
        _event("room", "配电房", "配电房", 560, layer="11"),
        _event("below", "0", "0", 1221, layer="11"),
    ]

    assert _restore_form_slots(group) == "主要使用功能为: 配电房 地下0层，地上 层。"


def test_restore_form_slots_accepts_same_layer_value_inside_slot_core():
    group = [
        _event("template", "高度 米。", "高度      米。", 0),
        _event("value", "13.45", "13.45", 180),
    ]

    assert _restore_form_slots(group) == "高度13.45米。"


def test_restore_form_slots_treats_two_ascii_spaces_as_one_cjk_value_slot():
    group = [
        _event("template", "钢筋具 腐蚀性。", "钢筋具  腐蚀性。", 0),
        _event("value", "微", "微", 210, layer="11", y=99),
    ]

    assert _restore_form_slots(group) == "钢筋具微腐蚀性。"


def test_clean_text_decodes_verified_autocad_display_codes():
    assert _clean_text("总伸长率不应小于9%%%") == "总伸长率不应小于9%"
    assert _clean_text("弯钩180%%D") == "弯钩180°"
    assert _clean_text("标高%%P0.00") == "标高±0.00"
    assert _clean_text("19kN/m%%1403%%141") == "19kN/m³"


def test_restore_form_slots_does_not_swallow_an_other_layer_note_on_a_slot_boundary():
    group = [
        _event("template", "A B C", "A      B      C", 0, width=0.7),
        _event("value", "0.40", "0.40", 100, layer="VALUE"),
        _event("note", "异层批注", "异层批注", 273, layer="VALUE"),
    ]

    assert _restore_form_slots(group) == "A 0.40 B C 异层批注"


def test_document_flow_keeps_all_source_refs_without_persisting_form_evidence():
    events = [
        _event(
            "template",
            "主要使用功能为: 地下 层，地上 层。",
            "主要使用功能为:              地下    层，地上     层。",
            0,
        ),
        _event("room", "配电房", "配电房", 560, layer="11", y=98),
        _event("below", "0", "0", 1221, layer="11", y=102),
        _event("above", "2", "2", 1659, layer="11", y=102),
    ]
    records = [
        {
            "uid": event["uid"],
            "text": event["text"],
            "point": event["point"],
            "height": event["height"],
            "layer": event["layer"],
            "record": {
                **event["record"],
                "index": index,
                "handle": [0, 0, index],
            },
            "instance_path": [],
        }
        for index, event in enumerate(events, 1)
    ]

    blocks, flow = _document_flow(records, [], set(), [0])

    assert blocks == ["主要使用功能为: 配电房 地下0层，地上2层。"]
    assert len(flow[0]["source_refs"]) == 4
    assert not {"record", "raw_text", "form_fill"} & flow[0].keys()


@pytest.mark.parametrize(("count", "expected"), ((1, "Ⅰ"), (2, "Ⅱ"), (3, "Ⅲ")))
def test_roman_i_glyph_recognizes_one_to_three_exploded_letters(count, expected):
    lines = [line for index in range(count) for line in _i_lines(index * 3, index * 130)]

    assert _roman_i_glyph(lines, 500) == expected


def test_roman_i_glyph_rejects_an_incomplete_shape():
    assert _roman_i_glyph(_i_lines(1, 0)[:-1], 500) is None


def test_document_flow_restores_linework_roman_category_and_text_values():
    template = {
        "uid": "template",
        "text": "2）建筑场地类别为 类， 特征周期 s。",
        "point": (4781077.806, 483166.561),
        "height": 500.0,
        "layer": "PUB_TEXT",
        "record": {
            "entity": "TEXT",
            "index": 1,
            "handle": [0, 0, 1],
            "text_value": "2）建筑场地类别为     类，    特征周期       s。",
            "height": 500.0,
            "width_factor": 0.7,
        },
        "instance_path": [],
    }
    records = [
        template,
        {
            **template,
            "uid": "period",
            "text": "0.35",
            "point": (4788012.810, 483154.651),
            "layer": "11",
            "record": {**template["record"], "index": 2, "handle": [0, 0, 2], "text_value": "0.35"},
        },
        {
            **template,
            "uid": "liquefaction-template",
            "text": "地面下勘探深度范围内 液化土层。",
            "point": (4789909.308, 483167.360),
            "record": {
                **template["record"],
                "index": 3,
                "handle": [0, 0, 3],
                "text_value": "地面下勘探深度范围内  液化土层。",
            },
        },
        {
            **template,
            "uid": "liquefaction",
            "text": "无",
            "point": (4793384.249, 483161.181),
            "layer": "11",
            "record": {**template["record"], "index": 4, "handle": [0, 0, 4], "text_value": "无"},
        },
    ]
    lines = [
        _line(10, (4784532.153, 483197.489), (4784532.153, 483568.298)),
        _line(11, (4784479.022, 483568.298), (4784585.283, 483568.298)),
        _line(12, (4784486.476, 483198.400), (4784592.737, 483198.400)),
        _line(13, (4784660.150, 483197.489), (4784660.150, 483568.298)),
        _line(14, (4784607.020, 483568.298), (4784713.281, 483568.298)),
        _line(15, (4784614.474, 483198.400), (4784720.735, 483198.400)),
    ]

    glyphs = _exploded_form_glyphs(records, lines)
    blocks, flow = _document_flow(records, [], set(), [4781077.806], lines=lines)

    assert glyphs[0]["text"] == "Ⅱ"
    assert len(glyphs[0]["source_refs"]) == 6
    assert blocks == [
        "2）建筑场地类别为Ⅱ类， 特征周期 0.35 s。 "
        "地面下勘探深度范围内无液化土层。"
    ]
    assert len(flow[0]["source_refs"]) == 10


def test_document_flow_merges_a_small_raised_unit_exponent():
    records = [
        {
            "uid": "line",
            "text": "基本风压0.40kN/m",
            "point": (0, 100),
            "height": 100,
            "layer": "PUB_TEXT",
            "record": {
                "entity": "TEXT",
                "index": 1,
                "handle": [0, 0, 1],
                "text_value": "基本风压0.40kN/m",
                "height": 100,
                "width_factor": 0.7,
            },
            "instance_path": [],
        },
        {
            "uid": "exponent",
            "text": "2",
            "point": (500, 150),
            "height": 60,
            "layer": "PUB_TEXT",
            "record": {
                "entity": "TEXT",
                "index": 2,
                "handle": [0, 0, 2],
                "text_value": "2",
                "height": 60,
                "width_factor": 0.7,
            },
            "instance_path": [],
        },
    ]

    blocks, _ = _document_flow(records, [], set(), [0])

    assert blocks == ["基本风压0.40kN/m²"]
