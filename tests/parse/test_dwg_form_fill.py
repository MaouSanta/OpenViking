from openviking.parse.parsers.dwg_sheet import _document_flow, _restore_form_slots


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

    assert _restore_form_slots(group) == "主要使用功能为: 配电房 地下 0 层，地上 2 层。"


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

    assert _restore_form_slots(group) == "荷载 0.40 kN （ 50 年），类别 B 类。"


def test_restore_form_slots_fails_closed_when_slot_and_value_counts_differ():
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

    assert _restore_form_slots(group) == "主要使用功能为: 地下 层，地上 层。 配电房 0"


def test_restore_form_slots_does_not_treat_same_layer_text_as_form_values():
    group = [
        _event("template", "高度 米。", "高度      米。", 0),
        _event("value", "13.45", "13.45", 180),
    ]

    assert _restore_form_slots(group) == "高度 米。 13.45"


def test_restore_form_slots_rejects_an_other_layer_note_on_a_slot_boundary():
    group = [
        _event("template", "A B C", "A      B      C", 0, width=0.7),
        _event("value", "0.40", "0.40", 100, layer="VALUE"),
        _event("note", "异层批注", "异层批注", 273, layer="VALUE"),
    ]

    assert _restore_form_slots(group) == "A B C 0.40 异层批注"


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

    assert blocks == ["主要使用功能为: 配电房 地下 0 层，地上 2 层。"]
    assert len(flow[0]["source_refs"]) == 4
    assert not {"record", "raw_text", "form_fill"} & flow[0].keys()
