# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""DWG parsing: preserve source data and generate one lightweight navigation view."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import struct
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image

from openviking.parse.base import NodeType, ParseResult, ResourceNode
from openviking.parse.parsers.base_parser import BaseParser
from openviking_cli.utils.config import get_openviking_config
from openviking_cli.utils.llm import parse_json_from_response
from openviking_cli.utils.uri import VikingURI

_PROMPT_LABEL_BYTE_LIMIT = 24 * 1024
_MAX_POINTERS_PER_ANCHOR = 32
_NUMBER_ONLY_RE = re.compile(
    r"^[+-]?(?:\d+(?:[.,]\d+)?|\.\d+)(?:\s*(?:mm|cm|m|㎡|m²|%|°|f))?$",
    re.IGNORECASE,
)
_DIMENSION_ONLY_RE = re.compile(r"^\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)+$", re.IGNORECASE)

# These are labels, not a domain schema.  They only decide which non-numeric
# strings are worth showing to the single VLM request.
_LABEL_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "title",
        (
            "图纸",
            "图号",
            "图名",
            "编号",
            "项目",
            "工程",
            "建设",
            "设计",
            "日期",
            "比例",
            "审定",
            "审核",
            "校对",
            "制图",
            "专业",
            "drawing",
            "project",
            "scale",
            "date",
            "client",
            "design",
            "sheet",
            "title",
        ),
    ),
    (
        "view",
        (
            "建筑",
            "建施",
            "总平",
            "平面",
            "立面",
            "剖面",
            "详图",
            "节点",
            "plan",
            "elevation",
            "section",
            "detail",
        ),
    ),
    (
        "business",
        (
            "面积",
            "户型",
            "楼栋",
            "楼层",
            "单元",
            "层数",
            "房间",
            "area",
            "floor",
            "unit",
            "building",
        ),
    ),
)


class DWGParser(BaseParser):
    """Keep the DWG and complete LibreDWG JSON; L0/L1 only point into them."""

    @property
    def supported_extensions(self) -> List[str]:
        return [".dwg"]

    async def parse(
        self, source: Union[str, Path], instruction: str = "", **kwargs: Any
    ) -> ParseResult:
        del instruction, kwargs
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"DWG file not found: {source_path}")

        started_at = time.perf_counter()
        source_bytes, source_sha256, source_mtime = await asyncio.to_thread(
            _read_source_snapshot, source_path
        )
        drawing, json_bytes, conversion_warnings = await self._export_dwg_json(source_path)

        catalog, text_object_count = _build_label_catalog(drawing)
        preview_bytes, preview_warning = _extract_embedded_preview(drawing)
        prompt, prompt_catalog = _build_vlm_prompt(catalog)
        navigation = await self._generate_navigation(prompt, prompt_catalog, preview_bytes)

        warnings = [*conversion_warnings, *_collect_warnings(drawing)]
        if preview_warning:
            warnings.append(preview_warning)

        source_filename = VikingURI.sanitize_segment(source_path.name) or "drawing.dwg"
        root_name = VikingURI.sanitize_segment(f"{source_path.stem}_dwg") or "drawing_dwg"
        abstract = _render_abstract(
            source_filename=source_filename,
            source_sha256=source_sha256,
            navigation=navigation,
            has_preview=preview_bytes is not None,
        )
        overview = _render_overview(
            source_filename=source_filename,
            source_sha256=source_sha256,
            navigation=navigation,
            catalog=catalog,
            warnings=warnings,
        )

        viking_fs = self._get_viking_fs()
        temp_uri = viking_fs.create_temp_uri()
        root_uri = f"{temp_uri}/{root_name}"
        try:
            await viking_fs.mkdir(root_uri, exist_ok=True)
            await viking_fs.write_file_bytes(f"{root_uri}/{source_filename}", source_bytes)
            await viking_fs.write_file_bytes(f"{root_uri}/libredwg.json", json_bytes)
            await viking_fs.write_file(f"{root_uri}/.abstract.md", abstract)
            await viking_fs.write_file(f"{root_uri}/.overview.md", overview)
            if preview_bytes is not None:
                await viking_fs.write_file_bytes(f"{root_uri}/preview.png", preview_bytes)
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
                "original_filename": source_path.name,
                "source_filename": source_filename,
                "source_sha256": source_sha256,
                "source_size_bytes": len(source_bytes),
                "source_modified_at": source_mtime,
                "libredwg_json": "libredwg.json",
                "preview": "preview.png" if preview_bytes is not None else None,
                "text_object_count": text_object_count,
                "semantic_label_count": len(catalog),
                "prompt_label_count": len(prompt_catalog),
                "summary_info": _nonempty_mapping(summary_info),
                "warnings": warnings,
            },
        )
        return ParseResult(
            root=root_node,
            source_path=str(source_path),
            temp_dir_path=temp_uri,
            source_format="dwg",
            parser_name="DWGParser",
            parser_version="1.0",
            parse_time=time.perf_counter() - started_at,
            parse_timestamp=datetime.now(timezone.utc),
            meta=root_node.meta,
            warnings=warnings,
            precomputed_semantics=True,
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

    async def _generate_navigation(
        self,
        prompt: str,
        catalog: Dict[str, Dict[str, Any]],
        preview_bytes: Optional[bytes],
    ) -> Dict[str, Any]:
        """Use exactly one configured VLM request; it never sees the full DWG JSON."""
        vlm = get_openviking_config().vlm
        if preview_bytes is None:
            response = await vlm.get_completion_async(prompt)
        else:
            response = await vlm.get_vision_completion_async(prompt, images=[preview_bytes])
        if not isinstance(response, str):
            response = getattr(response, "content", None) or getattr(response, "text", None)
        payload = parse_json_from_response(response) if isinstance(response, str) else None
        if not isinstance(payload, dict):
            raise RuntimeError("DWG navigation VLM returned invalid JSON")
        return _validate_navigation(payload, catalog)


def _read_source_snapshot(path: Path) -> Tuple[bytes, str, str]:
    payload = path.read_bytes()
    modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return payload, hashlib.sha256(payload).hexdigest(), modified_at


def _normalize_libredwg_json(
    raw: bytes, source_name: str
) -> Tuple[Dict[str, Any], bytes, List[str]]:
    """Turn LibreDWG's occasionally non-strict JSON into UTF-8 JSON for grep.

    LibreDWG can put raw proxy bytes in JSON strings and emits lowercase ``nan``.
    The original DWG remains stored unchanged; this representation preserves every
    parseable field while making source text usable by downstream tools.
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
            f"{invalid_byte_count} invalid UTF-8 bytes in LibreDWG strings were escaped in libredwg.json"
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


def _handle_number(value: Any) -> Optional[int]:
    if isinstance(value, list) and value:
        value = value[-1]
    if isinstance(value, int):
        return value
    return None


def _format_handle(value: Any) -> Optional[str]:
    number = _handle_number(value)
    return f"0x{number:X}" if number is not None else None


def _normalize_text(value: str) -> str:
    return " ".join(value.replace("\\P", " ").split())


def _label_priority(label: str) -> Optional[int]:
    folded = label.casefold()
    for priority, (_, keywords) in enumerate(_LABEL_GROUPS):
        if any(keyword.casefold() in folded for keyword in keywords):
            return priority
    return None


def _semantic_label(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    label = _normalize_text(value)
    if not label:
        return None
    for delimiter in ("：", ":"):
        if delimiter in label:
            head = label.split(delimiter, 1)[0].strip()
            if _label_priority(head) is not None:
                label = head
            break
    compact = label.replace(" ", "")
    if (
        len(label) > 48
        or _NUMBER_ONLY_RE.fullmatch(compact)
        or _DIMENSION_ONLY_RE.fullmatch(compact)
    ):
        return None
    return label if _label_priority(label) is not None else None


def _build_label_catalog(drawing: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    objects = drawing.get("OBJECTS")
    if not isinstance(objects, list):
        return {}, 0

    block_names: Dict[int, str] = {}
    layout_names: Dict[int, str] = {}
    for record in objects:
        if not isinstance(record, dict):
            continue
        handle = _handle_number(record.get("handle"))
        kind = _object_kind(record)
        name = record.get("name")
        if kind == "BLOCK_HEADER" and handle is not None and isinstance(name, str) and name:
            block_names[handle] = _normalize_text(name)
        if kind == "LAYOUT":
            block_handle = _handle_number(record.get("block_header"))
            layout_name = record.get("layout_name") or name
            if block_handle is not None and isinstance(layout_name, str) and layout_name:
                layout_names[block_handle] = _normalize_text(layout_name)

    entries_by_label: Dict[str, Dict[str, Any]] = {}
    text_object_count = 0
    for object_index, record in enumerate(objects):
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("text_value"), str):
            text_object_count += 1
        label = _semantic_label(record.get("text_value"))
        if label is None:
            continue
        key = label.casefold()
        owner_handle = _handle_number(record.get("ownerhandle"))
        occurrence: Dict[str, Any] = {
            "object_index": object_index,
            "handle": _format_handle(record.get("handle")),
            "layout": layout_names.get(owner_handle),
            "container": block_names.get(owner_handle),
            "position": record.get("ins_pt"),
            "bbox": record.get("bbox"),
        }
        entry = entries_by_label.get(key)
        if entry is None:
            entry = {
                "label": label,
                "priority": _label_priority(label),
                "first_object_index": object_index,
                "occurrences": [],
            }
            entries_by_label[key] = entry
        entry["occurrences"].append(occurrence)

    ordered = sorted(
        entries_by_label.values(),
        key=lambda item: (item["priority"], item["first_object_index"], item["label"]),
    )
    catalog: Dict[str, Dict[str, Any]] = {}
    for index, entry in enumerate(ordered, start=1):
        entry["id"] = f"t{index:04d}"
        catalog[entry["id"]] = entry
    return catalog, text_object_count


def _build_vlm_prompt(catalog: Dict[str, Dict[str, Any]]) -> Tuple[str, Dict[str, Dict[str, Any]]]:
    prefix = """You create navigation for one DWG drawing. The labels below are non-numeric source labels only.\nDo not infer, calculate, or return any numbers or measurements. Select only supplied ids.\nReturn JSON only with this schema:\n{"drawing_kind":"short drawing classification","aliases":["retrieval term"],"anchors":[{"id":"t0001","role":"title_block|area_table|plan_view|other"}]}\nKeep aliases short, grounded in the supplied labels or preview, and return at most 12 anchors.\nLabels:\n"""
    chosen: Dict[str, Dict[str, Any]] = {}
    lines: List[str] = []
    used = len(prefix.encode("utf-8"))
    for candidate_id, entry in catalog.items():
        line = f"{candidate_id}\t{entry['label']}\n"
        line_size = len(line.encode("utf-8"))
        if used + line_size > _PROMPT_LABEL_BYTE_LIMIT:
            break
        chosen[candidate_id] = entry
        lines.append(line)
        used += line_size
    # ponytail: one 24 KiB prompt; add a second pass only if real drawings prove this drops needed labels.
    return prefix + ("".join(lines) if lines else "<no semantic labels extracted>\n"), chosen


def _clean_vlm_text(value: Any, *, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    cleaned = _normalize_text(value)
    if not cleaned or len(cleaned) > limit or _NUMBER_ONLY_RE.fullmatch(cleaned.replace(" ", "")):
        return None
    return cleaned


def _validate_navigation(
    payload: Dict[str, Any], catalog: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    drawing_kind = _clean_vlm_text(payload.get("drawing_kind"), limit=80)
    if drawing_kind is None:
        raise RuntimeError("DWG navigation VLM omitted a valid drawing_kind")

    aliases: List[str] = []
    raw_aliases = payload.get("aliases", [])
    if not isinstance(raw_aliases, list):
        raise RuntimeError("DWG navigation VLM returned invalid aliases")
    for value in raw_aliases:
        alias = _clean_vlm_text(value, limit=80)
        if alias and alias.casefold() not in {item.casefold() for item in aliases}:
            aliases.append(alias)
        if len(aliases) == 12:
            break

    raw_anchors = payload.get("anchors", [])
    if not isinstance(raw_anchors, list):
        raise RuntimeError("DWG navigation VLM returned invalid anchors")
    anchors: List[Dict[str, str]] = []
    seen_ids = set()
    for raw_anchor in raw_anchors:
        if not isinstance(raw_anchor, dict):
            raise RuntimeError("DWG navigation VLM returned a malformed anchor")
        candidate_id = raw_anchor.get("id")
        if candidate_id not in catalog:
            raise RuntimeError(f"DWG navigation VLM selected unknown label id: {candidate_id}")
        if candidate_id in seen_ids:
            continue
        role = _clean_vlm_text(raw_anchor.get("role"), limit=48) or "other"
        anchors.append({"id": candidate_id, "role": role})
        seen_ids.add(candidate_id)
        if len(anchors) == 12:
            break
    return {"drawing_kind": drawing_kind, "aliases": aliases, "anchors": anchors}


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


def _extract_embedded_preview(drawing: Dict[str, Any]) -> Tuple[Optional[bytes], Optional[str]]:
    thumbnail = drawing.get("THUMBNAILIMAGE")
    chain = thumbnail.get("chain") if isinstance(thumbnail, dict) else None
    if not chain:
        return None, "No usable embedded DWG preview"
    try:
        if isinstance(chain, str):
            raw = bytes.fromhex("".join(chain.split()))
        elif isinstance(chain, list):
            raw = bytes(chain)
        else:
            return None, "No usable embedded DWG preview"
        for signature in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"):
            start = raw.find(signature)
            if start >= 0:
                preview = _image_as_png(raw[start:])
                if preview:
                    return preview, None
        start = raw.find(b"\x28\x00\x00\x00")
        while start >= 0:
            preview = _image_as_png(_dib_as_bmp(raw[start:]))
            if preview:
                return preview, None
            start = raw.find(b"\x28\x00\x00\x00", start + 1)
    except Exception:
        pass
    return None, "No usable embedded DWG preview"


def _dib_as_bmp(dib: bytes) -> bytes:
    if len(dib) < 40:
        return b""
    header_size = struct.unpack_from("<I", dib, 0)[0]
    if header_size < 12 or header_size > len(dib):
        return b""
    if header_size == 12:
        bit_count = struct.unpack_from("<H", dib, 10)[0]
        palette_size = (1 << bit_count) * 3 if bit_count <= 8 else 0
    else:
        bit_count = struct.unpack_from("<H", dib, 14)[0]
        colors_used = struct.unpack_from("<I", dib, 32)[0] if len(dib) >= 36 else 0
        palette_size = (colors_used or (1 << bit_count)) * 4 if bit_count <= 8 else 0
    pixel_offset = 14 + header_size + palette_size
    if pixel_offset > 14 + len(dib):
        return b""
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, pixel_offset) + dib


def _image_as_png(raw: bytes) -> Optional[bytes]:
    if not raw:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue()
    except Exception:
        return None


def _render_abstract(
    *,
    source_filename: str,
    source_sha256: str,
    navigation: Dict[str, Any],
    has_preview: bool,
) -> str:
    aliases = ", ".join(navigation["aliases"]) or "none"
    preview = "preview.png" if has_preview else "unavailable"
    return "\n".join(
        [
            f"# {source_filename}",
            "",
            f"DWG navigation for: {navigation['drawing_kind']}",
            f"Retrieval terms: {aliases}",
            "Authoritative source data: libredwg.json (complete LibreDWG export) and the original DWG.",
            f"Embedded preview: {preview}",
            f"Source SHA-256: {source_sha256}",
            "Use this only to navigate. Read libredwg.json for numerical or geometric questions.",
            "",
        ]
    )


def _render_overview(
    *,
    source_filename: str,
    source_sha256: str,
    navigation: Dict[str, Any],
    catalog: Dict[str, Dict[str, Any]],
    warnings: List[str],
) -> str:
    lines = [
        f"# DWG navigation: {source_filename}",
        "",
        f"- Drawing type: {navigation['drawing_kind']}",
        f"- Source SHA-256: {source_sha256}",
        "- Full source: `libredwg.json`; original DWG is stored beside it.",
        "- Numerical/geometric answers must be read or calculated from `libredwg.json`.",
        "",
        "## VLM-selected anchors",
    ]
    if not navigation["anchors"]:
        lines.append("- No anchor labels selected. Search `libredwg.json` directly.")
    for anchor in navigation["anchors"]:
        entry = catalog[anchor["id"]]
        lines.append(f"- `{anchor['id']}` ({anchor['role']}): {entry['label']}")
        # ponytail: keep only the first 32 repeated pointers; the full JSON remains authoritative.
        for occurrence in entry["occurrences"][:_MAX_POINTERS_PER_ANCHOR]:
            lines.append(f"  - {_render_pointer(occurrence)}")
        extra = len(entry["occurrences"]) - _MAX_POINTERS_PER_ANCHOR
        if extra > 0:
            lines.append(f"  - {extra} more occurrences; grep `libredwg.json` for this label.")
    if warnings:
        lines.extend(["", "## Parse warnings"])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.append("")
    return "\n".join(lines)


def _render_pointer(occurrence: Dict[str, Any]) -> str:
    parts = [f"`libredwg.json` → `OBJECTS[{occurrence['object_index']}]`"]
    if occurrence.get("handle"):
        parts.append(f"handle `{occurrence['handle']}`")
    if occurrence.get("layout"):
        parts.append(f"layout `{occurrence['layout']}`")
    elif occurrence.get("container"):
        parts.append(f"container `{occurrence['container']}`")
    if occurrence.get("position") is not None:
        parts.append(f"insertion point `{occurrence['position']}`")
    bbox = occurrence.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        parts.append(f"bbox `{list(bbox)}`")
    return ", ".join(parts)
