# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""DWG parsing: preserve the source and derive compact searchable artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from openviking.parse.base import NodeType, ParseResult, ResourceNode
from openviking.parse.parsers.base_parser import BaseParser
from openviking.parse.parsers.dwg_extract import build_beam_topology
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts
from openviking.parse.parsers.dwg_sheet_export import export_flat_sheet_dwgs
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.llm import parse_json_from_response
from openviking_cli.utils.uri import VikingURI


class DWGParser(BaseParser):
    """Keep the DWG and derive deterministic topology and text artifacts."""

    @property
    def supported_extensions(self) -> List[str]:
        return [".dwg"]

    async def parse(
        self, source: Union[str, Path], instruction: str = "", **kwargs: Any
    ) -> ParseResult:
        del instruction
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"DWG file not found: {source_path}")

        started_at = time.perf_counter()
        source_bytes, source_sha256, source_mtime = await asyncio.to_thread(
            _read_source_snapshot, source_path
        )
        drawing, _json_bytes, conversion_warnings = await self._export_dwg_json(source_path)
        warnings = [*conversion_warnings, *_collect_warnings(drawing)]

        requested_name = kwargs.get("source_name")
        original_filename = Path(requested_name).name if requested_name else source_path.name
        source_filename = VikingURI.sanitize_segment(original_filename) or "drawing.dwg"
        root_name = VikingURI.sanitize_segment(f"{Path(source_filename).stem}_dwg") or "drawing_dwg"
        topology = build_beam_topology(
            drawing,
            source_name=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=len(source_bytes),
        )
        markdown_config = get_openviking_config().markdown
        sheet_mode_classifier = kwargs.get("sheet_mode_classifier") or _sheet_mode_classifier()
        sheet_artifacts = await asyncio.to_thread(
            build_sheet_artifacts,
            drawing,
            source_name=source_filename,
            source_sha256=source_sha256,
            source_size_bytes=len(source_bytes),
            max_section_tokens=markdown_config.max_section_size,
            max_section_chars=markdown_config.max_section_chars,
            sheet_mode_classifier=sheet_mode_classifier,
        )
        sheet_dwg_exporter = kwargs.get("sheet_dwg_exporter") or export_flat_sheet_dwgs
        sheet_dwgs, sheet_dwg_warnings = await asyncio.to_thread(
            sheet_dwg_exporter, source_path, sheet_artifacts["sheets"]
        )
        warnings.extend(sheet_dwg_warnings)
        document_sheets = {
            sheet["sheet_id"]: sheet for sheet in sheet_artifacts["document"]["sheets"]
        }
        for sheet in sheet_artifacts["sheets"]:
            exported = sheet_dwgs.get(sheet["sheet_id"])
            if not exported:
                continue
            dwg_path = f"{sheet['directory']}/sheet.dwg"
            export_meta = {key: value for key, value in exported.items() if key != "bytes"}
            sheet["dwg_path"] = dwg_path
            sheet["dwg_bytes"] = exported["bytes"]
            sheet["structure"]["sheet"]["dwg_path"] = "sheet.dwg"
            sheet["structure"]["sheet"]["dwg_export"] = {
                "mode": "flattened_bbox_intersection",
                **export_meta,
            }
            document_sheets[sheet["sheet_id"]]["dwg_path"] = dwg_path
            document_sheets[sheet["sheet_id"]]["dwg_export"] = export_meta
        topology_bytes = json.dumps(topology, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        document_bytes = json.dumps(
            sheet_artifacts["document"], ensure_ascii=False, indent=2
        ).encode("utf-8")

        viking_fs = self._get_viking_fs()
        temp_uri = viking_fs.create_temp_uri()
        root_uri = f"{temp_uri}/{root_name}"
        try:
            await viking_fs.mkdir(root_uri, exist_ok=True)
            await viking_fs.mkdir(f"{root_uri}/sheets", exist_ok=True)
            await viking_fs.write_file_bytes(f"{root_uri}/{source_filename}", source_bytes)
            await viking_fs.write_file_bytes(f"{root_uri}/beam_topology.json", topology_bytes)
            await viking_fs.write_file_bytes(f"{root_uri}/document.json", document_bytes)
            for sheet in sheet_artifacts["sheets"]:
                await viking_fs.mkdir(f"{root_uri}/{sheet['directory']}", exist_ok=True)
                for filename, content in sheet["content_files"]:
                    await viking_fs.write_file_bytes(
                        f"{root_uri}/{sheet['directory']}/{filename}", content.encode("utf-8")
                    )
                await viking_fs.write_file_bytes(
                    f"{root_uri}/{sheet['structure_path']}",
                    json.dumps(sheet["structure"], ensure_ascii=False, indent=2).encode("utf-8"),
                )
                if sheet.get("dwg_path"):
                    await viking_fs.write_file_bytes(
                        f"{root_uri}/{sheet['dwg_path']}", sheet["dwg_bytes"]
                    )
        except BaseException:
            try:
                await viking_fs.delete_temp(temp_uri)
            except Exception:
                pass
            raise

        summary_info = drawing.get("SummaryInfo")
        root_node = ResourceNode(
            type=NodeType.ROOT,
            title=source_path.stem,
            meta={
                "content_type": "drawing",
                "original_filename": original_filename,
                "source_filename": source_filename,
                "source_sha256": source_sha256,
                "source_size_bytes": len(source_bytes),
                "source_modified_at": source_mtime,
                "beam_topology": "beam_topology.json",
                "document_manifest": "document.json",
                "sheet_bundles": [
                    {
                        key: sheet[key]
                        for key in (
                            "sheet_id",
                            "directory",
                            "title",
                            "content_paths",
                            "structure_path",
                            "dwg_path",
                            "kind",
                        )
                        if key in sheet
                    }
                    for sheet in sheet_artifacts["sheets"]
                ],
                "beam_label_count": topology["stats"]["beam_labels"],
                "sheet_count": len(sheet_artifacts["sheets"]),
                "summary_info": _nonempty_mapping(summary_info),
                "temp_root_uri": root_uri,
                "warnings": warnings,
            },
        )
        return ParseResult(
            root=root_node,
            source_path=str(source_path),
            temp_dir_path=temp_uri,
            source_format="dwg",
            parser_name="DWGParser",
            parser_version="3.1",
            parse_time=time.perf_counter() - started_at,
            parse_timestamp=datetime.now(timezone.utc),
            meta=root_node.meta,
            warnings=warnings,
            precomputed_semantics=False,
        )

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, instruction: str = "", **kwargs: Any
    ) -> ParseResult:
        del content, source_path, instruction, kwargs
        raise NotImplementedError("DWGParser requires a local .dwg file")

    async def _export_dwg_json(self, source_path: Path) -> Tuple[Dict[str, Any], bytes, List[str]]:
        """Run LibreDWG once and reject invalid JSON before writing any resource files."""
        with tempfile.TemporaryDirectory(prefix="openviking-dwg-") as temp_dir:
            output_path = Path(temp_dir) / "libredwg.json"
            try:
                process = await asyncio.create_subprocess_exec(
                    "dwgread",
                    "-O",
                    "JSON",
                    "-o",
                    str(output_path),
                    str(source_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("LibreDWG executable 'dwgread' is not available") from exc

            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                details = (stderr or stdout).decode("utf-8", errors="replace").strip()[-2000:]
                raise RuntimeError(
                    f"LibreDWG conversion failed for {source_path.name} (exit {process.returncode}): {details}"
                )
            try:
                json_bytes = await asyncio.to_thread(output_path.read_bytes)
            except OSError as exc:
                raise RuntimeError(f"LibreDWG produced no JSON for {source_path.name}") from exc

        return _normalize_libredwg_json(json_bytes, source_path.name)


def _read_source_snapshot(path: Path) -> Tuple[bytes, str, str]:
    payload = path.read_bytes()
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return payload, hashlib.sha256(payload).hexdigest(), modified_at


def _sheet_mode_classifier():
    """Use one transient layout image to distinguish document sheets from drawings."""
    vlm = get_openviking_config().vlm
    if not vlm.is_available():
        return None

    def classify(image: bytes, evidence: Dict[str, Any]) -> Any:
        prompt = f"""Classify this complete DWG sheet by layout only.
Return JSON: {{"mode":"document|drawing","confidence":0.0,"reason":"short reason"}}.
document means the sheet is primarily a multi-column manual/specification whose numbered text and inline tables must be restored in reading order. drawing means a plan/detail whose text is spatial annotation. Do not transcribe or summarize text.
Native evidence: {json.dumps(evidence, ensure_ascii=False)}"""
        return parse_json_from_response(
            vlm.get_vision_completion(prompt=prompt, images=[image], thinking=False)
        )

    return classify


def _normalize_libredwg_json(
    raw: bytes, source_name: str
) -> Tuple[Dict[str, Any], bytes, List[str]]:
    """Normalize and validate LibreDWG's transient, occasionally non-strict JSON.

    LibreDWG can put raw proxy bytes in JSON strings and emits lowercase ``nan``.
    The normalized object feeds the deterministic topology and text extractors but
    is not persisted in the resource bundle.
    """
    text = raw.decode("utf-8-sig", errors="surrogateescape")
    invalid_byte_count = sum(0xDC80 <= ord(char) <= 0xDCFF for char in text)
    text, nonfinite_count = _replace_nonfinite_json_tokens(text)
    try:
        drawing = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LibreDWG produced invalid JSON for {source_name}: {exc}") from exc
    if not isinstance(drawing, dict):
        raise RuntimeError(f"LibreDWG JSON root is not an object for {source_name}")

    normalized = _replace_surrogate_bytes(drawing)
    try:
        normalized_json = json.dumps(
            normalized, ensure_ascii=False, indent=2, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(f"Cannot normalize LibreDWG JSON for {source_name}: {exc}") from exc

    warnings: List[str] = []
    if invalid_byte_count:
        warnings.append(
            f"{invalid_byte_count} invalid UTF-8 bytes in LibreDWG strings were escaped"
        )
    if nonfinite_count:
        warnings.append(
            f"{nonfinite_count} non-finite LibreDWG numeric tokens were normalized to null"
        )
    return normalized, normalized_json, warnings


def _replace_nonfinite_json_tokens(text: str) -> Tuple[str, int]:
    """Replace bare non-standard numeric tokens without touching quoted drawing text."""
    replacements = {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }
    output: List[str] = []
    index = 0
    in_string = False
    replaced = 0
    while index < len(text):
        char = text[index]
        if in_string and char == "\\":
            output.append(text[index : index + 2])
            index += 2
            continue
        if char == '"':
            in_string = not in_string
            output.append(char)
            index += 1
            continue
        if not in_string and (char.isalpha() or char in "+-"):
            end = index
            while end < len(text) and (text[end].isalpha() or text[end] in "+-"):
                end += 1
            token = text[index:end]
            if token.casefold() in replacements:
                output.append("null")
                replaced += 1
                index = end
                continue
        output.append(char)
        index += 1
    return "".join(output), replaced


def _replace_surrogate_bytes(value: Any) -> Any:
    if isinstance(value, str):
        return "".join(
            f"\\x{ord(char) - 0xDC00:02x}" if 0xDC80 <= ord(char) <= 0xDCFF else char
            for char in value
        )
    if isinstance(value, list):
        return [_replace_surrogate_bytes(item) for item in value]
    if isinstance(value, dict):
        return {
            _replace_surrogate_bytes(key): _replace_surrogate_bytes(item)
            for key, item in value.items()
        }
    return value


def _nonempty_mapping(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if item not in (None, "", [], {})}


def _object_kind(record: Dict[str, Any]) -> str:
    return str(record.get("object") or record.get("entity") or "").upper()


def _collect_warnings(drawing: Dict[str, Any]) -> List[str]:
    objects = drawing.get("OBJECTS")
    if not isinstance(objects, list):
        return ["LibreDWG JSON has no OBJECTS list"]
    proxy_count = sum(
        1
        for record in objects
        if isinstance(record, dict) and _object_kind(record) == "PROXY_OBJECT"
    )
    xref_count = 0
    for record in objects:
        if not isinstance(record, dict):
            continue
        if record.get("xrefdep") or record.get("xref_dependent"):
            xref_count += 1
        elif _object_kind(record) == "BLOCK_HEADER" and "|" in str(record.get("name", "")):
            xref_count += 1
    warnings: List[str] = []
    if proxy_count:
        warnings.append(f"{proxy_count} proxy objects may not be fully represented by LibreDWG")
    if xref_count:
        warnings.append(f"{xref_count} possible XREF references detected")
    return warnings
