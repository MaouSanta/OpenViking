"""Focused checks for the deterministic STEP parser contract."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.step import (
    STEP_EVIDENCE_SCHEMA,
    VIEW_DPI,
    VIEW_SIZE,
    VIEWS,
    STEPParser,
)
from openviking.parse.registry import ParserRegistry


class _FakeVikingFS:
    def __init__(self):
        self.files = {}
        self.dirs = []

    def create_temp_uri(self):
        return "viking://temp/step"

    async def mkdir(self, uri, exist_ok=False):
        self.dirs.append(uri)

    async def write_file_bytes(self, uri, content):
        self.files[uri] = content

    async def delete_temp(self, uri):
        self.files = {key: value for key, value in self.files.items() if not key.startswith(uri)}


@pytest.mark.asyncio
async def test_step_parser_writes_only_source_evidence_and_fixed_views(tmp_path: Path, monkeypatch):
    source = tmp_path / "multi-body.step"
    source.write_text("ISO-10303-21;", encoding="utf-8")
    fake_fs = _FakeVikingFS()
    evidence = {
        "schema": STEP_EVIDENCE_SCHEMA,
        "source": {"file": source.name, "step_header": {}},
        "dimensions": {},
        "shape_metrics": {},
        "topology": {"solids": 2},
        "mass_properties": {},
        "geometry_facts": {},
        "surfaces": {},
        "edges": {},
        "evidence_limits": [],
    }
    pngs = {name: f"png:{name}".encode() for name in VIEWS}
    monkeypatch.setattr(
        "openviking.parse.parsers.step._build_step_artifacts",
        lambda _path, _name: (evidence, pngs),
    )

    with patch.object(BaseParser, "_get_viking_fs", return_value=fake_fs):
        result = await STEPParser().parse(source)

    assert result.source_format == "step"
    assert {Path(uri).name for uri in fake_fs.files} == {
        source.name,
        "evidence.json",
        *(f"{name}.png" for name in VIEWS),
    }
    saved = json.loads(
        next(value for uri, value in fake_fs.files.items() if uri.endswith("evidence.json"))
    )
    assert saved["schema"] == STEP_EVIDENCE_SCHEMA
    assert "feature_history" not in saved
    assert "evidence_availability" not in saved
    assert VIEW_SIZE == 768
    assert VIEW_DPI == (72, 72)


def test_registry_routes_both_step_extensions_to_the_dedicated_parser():
    registry = ParserRegistry(register_optional=False)
    assert isinstance(registry.get_parser_for_file(Path("part.stp")), STEPParser)
    assert isinstance(registry.get_parser_for_file(Path("part.step")), STEPParser)
