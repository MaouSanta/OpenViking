# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

import json
import subprocess
from pathlib import Path

from scripts import dwg_sheet_smoke


def test_dwg_sheet_smoke_writes_only_inspection_artifacts(tmp_path, monkeypatch, capsys):
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"fake-dwg")
    output = tmp_path / "inspection"
    drawing = {
        "OBJECTS": [
            {
                "object": "BLOCK_HEADER",
                "handle": [0, 0, 10],
                "name": "*Model_Space",
            },
            {
                "entity": "TEXT",
                "handle": [0, 0, 20],
                "ownerhandle": [0, 0, 10],
                "ins_pt": [1, 2],
                "text_value": "图纸名称：建筑平面图",
            },
        ]
    }

    def fake_dwgread(command, **kwargs):
        assert command[:4] == ["dwgread", "-O", "JSON", "-o"]
        Path(command[4]).write_text(json.dumps(drawing, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(dwg_sheet_smoke.subprocess, "run", fake_dwgread)
    monkeypatch.setattr(
        dwg_sheet_smoke,
        "export_flat_sheet_dwgs",
        lambda _source, sheets: (
            {
                sheet["sheet_id"]: {
                    "bytes": b"AC1027sheet",
                    "entity_count": 1,
                    "entity_types": {"TEXT": 1},
                    "audit_fixes": 0,
                    "bbox": [0, 0, 1, 1],
                }
                for sheet in sheets
            },
            [],
        ),
    )

    assert dwg_sheet_smoke.main([str(source), str(output), "--json"]) == 0
    summary = json.loads(capsys.readouterr().out)
    files = [path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()]

    assert summary["sheet_count"] == 1
    assert set(summary["written"]) == set(files)
    assert "document.json" in files
    assert any(path.endswith("/content.md") for path in files)
    assert any(path.endswith("/structure.json") for path in files)
    assert any(path.endswith("/sheet.dwg") for path in files)
    assert not any("preview" in path for path in files)
