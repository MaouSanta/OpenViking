# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Extract ordinary right-side DWG title-block fields from normalized text atoms."""

from __future__ import annotations

import re
import statistics
from typing import Any


def _normalized(value: Any) -> str:
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff]+", "", str(value or "").upper())


_LABELS = {
    "建设单位": "建设单位",
    "项目名称": "项目名称",
    "图名": "图名",
    "工程编号": "工程编号",
    "出图日期": "出图日期",
    "修改版本": "版本",
    "图纸编号": "图号",
    "公司地址": "设计单位地址",
    "办公室电话": "设计单位电话",
    "制图": "制图",
    "设计": "设计",
    "专业负责人": "专业负责人",
    "项目负责人": "项目负责人",
    "复核": "复核",
    "审核": "审核",
    "批准": "批准",
    "建筑": "会签-建筑",
    "结构": "会签-结构",
    "给排水": "会签-给排水",
    "电气": "会签-电气",
    "暖通": "会签-暖通",
    "景观": "会签-景观",
    "智能": "会签-智能",
}
_CORE_LABELS = {"建设单位", "项目名称", "图名", "工程编号", "出图日期", "修改版本", "图纸编号"}
_PLACEHOLDERS = {
    "职责",
    "实名",
    "签名",
    "图纸会签",
    "专业",
    "CLIENT",
    "PROJECTTITLE",
    "DWGTITLE",
    "PROJECTNO",
    "DATE",
    "REV",
    "DWGNO",
    "COMPANYADD",
    "OFFICETEL",
    "DRAWN",
    "DESIGNED",
    "DISCIPLINECHARGE",
    "PROJECTMANAGER",
    "CHECKED",
    "APPROVED",
    "AUDITED",
    "ARCHITECTURE",
    "STRUCTURE",
    "WATERWASTEWATER",
    "ELECTRIC",
    "HVAC",
    "LANDSCAPE",
    "INTELLIGENCE",
    "RESPONSIBILITY",
    "NAMETYPED",
    "SIGNATURE",
    "DISCIPLINE",
    "DISCIPLINEJOINTLYSIGNEDBY",
    "NOTES",
}
_DESIGN_UNIT_RE = re.compile(r"(?:设计院|设计公司|建筑设计|工程设计).*(?:公司|院)?$")
_QUALIFICATION_RE = re.compile(r"^[A-Z]{1,3}\d{5,}$")


def _point(record: dict[str, Any]) -> tuple[float, float] | None:
    value = record.get("point")
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None


def _uid(record: dict[str, Any]) -> str:
    return str(record.get("uid") or id(record))


def _join(records: list[dict[str, Any]]) -> str:
    values: list[str] = []
    for record in sorted(records, key=lambda item: (-_point(item)[1], _point(item)[0])):
        value = str(record.get("text") or "").strip()
        if value and value not in values:
            values.append(value)
    return "\n".join(values)


def extract_titleblock_meta(
    records: list[dict[str, Any]],
    sheet_bbox: tuple[float, float, float, float] | None = None,
) -> tuple[dict[str, str], set[str]]:
    """Return title-block fields and every text UID belonging to that block.

    ``records`` are the normalized text atoms produced by ``dwg_sheet._walk_block``.
    The detector intentionally targets the conventional right-side title block; when
    three identifying labels are not present there, it returns no result.
    """
    positioned = [record for record in records if _point(record) and record.get("text")]
    if not positioned:
        return {}, set()
    points = [_point(record) for record in positioned]
    if sheet_bbox:
        left, bottom, right, top = sheet_bbox
    else:
        xs, ys = [point[0] for point in points], [point[1] for point in points]
        left, bottom, right, top = min(xs), min(ys), max(xs), max(ys)
    width, height = max(right - left, 1.0), max(top - bottom, 1.0)
    right_side = [record for record in positioned if _point(record)[0] >= left + width * 0.7]
    core = [
        record for record in right_side if _normalized(record.get("text")) in _CORE_LABELS
    ]
    if len({_normalized(record.get("text")) for record in core}) < 3:
        return {}, set()

    text_heights = [
        float(record.get("height") or 0.0)
        for record in core
        if float(record.get("height") or 0.0) > 0
    ]
    median_height = statistics.median(text_heights) if text_heights else height * 0.005
    block_left = min(_point(record)[0] for record in core) - max(median_height * 3, width * 0.003)
    block_records = [
        record
        for record in positioned
        if block_left <= _point(record)[0] <= right and bottom <= _point(record)[1] <= top
    ]
    labels = [
        record for record in block_records if _normalized(record.get("text")) in _LABELS
    ]
    if not labels:
        return {}, set()

    row_tolerance = max(median_height * 1.7, height * 0.001)
    rows: list[list[dict[str, Any]]] = []
    for label in sorted(labels, key=lambda item: -_point(item)[1]):
        if rows and abs(_point(label)[1] - statistics.mean(_point(item)[1] for item in rows[-1])) <= row_tolerance:
            rows[-1].append(label)
        else:
            rows.append([label])

    excluded = set(_LABELS) | _PLACEHOLDERS
    fields: dict[str, str] = {}
    for index, row in enumerate(rows):
        row_y = statistics.mean(_point(item)[1] for item in row)
        next_y = (
            statistics.mean(_point(item)[1] for item in rows[index + 1])
            if index + 1 < len(rows)
            else bottom - median_height
        )
        ordered = sorted(row, key=lambda item: _point(item)[0])
        for column, label in enumerate(ordered):
            x = _point(label)[0]
            x_min = (
                (x + _point(ordered[column - 1])[0]) / 2 if column else block_left
            )
            x_max = (
                (x + _point(ordered[column + 1])[0]) / 2
                if column + 1 < len(ordered)
                else right
            )
            candidates = [
                record
                for record in block_records
                if next_y + median_height * 0.18 < _point(record)[1] <= row_y + median_height * 0.65
                and x_min <= _point(record)[0] <= x_max
                and _normalized(record.get("text")) not in excluded
            ]
            value = _join(candidates)
            if value:
                fields[_LABELS[_normalized(label.get("text"))]] = value

    client_y = max(
        (_point(record)[1] for record in labels if _normalized(record.get("text")) == "建设单位"),
        default=bottom,
    )
    design_units = [
        record
        for record in block_records
        if _point(record)[1] > client_y
        and _DESIGN_UNIT_RE.search(str(record.get("text") or ""))
        and _normalized(record.get("text")) not in excluded
    ]
    if design_units:
        fields["设计单位"] = max(design_units, key=lambda item: _point(item)[1])["text"]

    qualifications = [
        record
        for record in block_records
        if _point(record)[1] > client_y
        and _QUALIFICATION_RE.fullmatch(_normalized(record.get("text")))
    ]
    if qualifications:
        fields["设计资质编号"] = max(qualifications, key=lambda item: _point(item)[1])["text"]

    note_labels = [
        record for record in block_records if _normalized(record.get("text")) == "NOTES"
    ]
    if note_labels:
        note_y = max(_point(record)[1] for record in note_labels)
        statements = [record for record in block_records if _point(record)[1] > note_y]
        value = _join(statements)
        if value:
            fields["题签声明"] = value

    return fields, {_uid(record) for record in block_records}
