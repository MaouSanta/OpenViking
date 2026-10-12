# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Build searchable asset navigation from image labels and actual chunk paths."""

from __future__ import annotations

import posixpath
import re
from urllib.parse import quote

from openviking.utils.embedding_input import estimate_embedding_input_tokens

_CHUNK = re.compile(r"chunk_(\d+)\.json$")
_MARKDOWN = frozenset("\\`*_{}[]()#+.!|>-")


def _text_units(value: object) -> list[str]:
    # Escape before pagination, keeping each escape/entity intact across pages.
    return [
        {"&": "&amp;", "<": "&lt;", ">": "&gt;"}.get(
            char, "\\" + char if char in _MARKDOWN else char
        )
        for char in " ".join(str(value or "").split())
    ]


def _text(value: object) -> str:
    return "".join(_text_units(value))


def _path_groups(paths: list[str]) -> list[list[str]]:
    normalized = set()
    for path in paths:
        value = posixpath.normpath(path)
        if path.startswith("/") or value == ".." or value.startswith("../") or "\0" in value:
            raise ValueError("Asset paths must stay inside the DWG document")
        normalized.add(value)

    groups: list[list[str]] = []
    for path in sorted(normalized):
        match = _CHUNK.fullmatch(posixpath.basename(path))
        previous = groups[-1][-1] if groups else ""
        last = _CHUNK.fullmatch(posixpath.basename(previous))
        if (
            match
            and last
            and posixpath.dirname(path) == posixpath.dirname(previous)
            and len(match[1]) == len(last[1])
            and int(match[1]) == int(last[1]) + 1
        ):
            groups[-1].append(path)
        else:
            groups.append([path])
    return groups


def _path_line(paths: list[str], directory: str) -> str:
    relative = [posixpath.relpath(path, directory) for path in paths]
    label = "相关分片" if relative[0].startswith("chunks/") else "依赖"
    if len(relative) > 1:
        span = _text(relative[0]) + "–" + _text(posixpath.basename(relative[-1]))
        links = "、".join(
            f"[{_CHUNK.fullmatch(posixpath.basename(path))[1]}]({quote(path, safe='/-._~')})"
            for path in relative
        )
        return f"- {label} {span}：{links}\n"
    path = relative[0]
    return f"- {label}：[{_text(path)}]({quote(path, safe='/-._~')})\n"


def _asset_parts(
    asset_id: str, asset: dict, mapping: dict, directory: str, page_header: str, fits
) -> list[str]:
    groups = _path_groups(mapping.get("paths", []))
    title = _text(asset.get("title"))
    complete = mapping.get("complete") is True and bool(groups) and bool(title)

    def heading(continued: bool) -> str:
        return (
            f"## {_text(asset_id)}{'（续）' if continued else ''}\n\n"
            f"原图号：{_text(asset.get('number')) or '未读清或无独立图号'}\n\n"
            f"原题名：{title or '未读清'}\n\n"
            f"定位：{'完整' if complete else '不完整（partial）；标题或分片绑定有缺项'}\n\n"
        )

    description = _text_units(asset.get("description") or "未提供可读图像概述")
    path_lines = [_path_line(group, directory) for group in groups] or [
        "相关分片：尚未绑定，不提供猜测编号。\n"
    ]
    whole = heading(False) + "图像概述：" + "".join(description) + "\n\n" + "".join(path_lines)
    if fits(page_header + whole):
        return [whole]

    # Only an oversized asset needs subdivision. Repeated identity and
    # every complete link count toward the same budget as the prose.
    parts: list[str] = []
    part = heading(False)
    has_body = False

    def flush() -> None:
        nonlocal part, has_body
        if not has_body:
            raise ValueError("DWG asset budget cannot hold its identity and a complete item")
        parts.append(part)
        part, has_body = heading(True), False

    def fit_count(items: list[str], render) -> int:
        low, high = 0, len(items)
        while low < high:
            mid = (low + high + 1) // 2
            if fits(page_header + part + render(items[:mid])):
                low = mid
            else:
                high = mid - 1
        return low

    offset = 0
    while offset < len(description):
        label = "图像概述（续）：" if offset else "图像概述："

        def render(values: list[str], label: str = label) -> str:
            return label + "".join(values) + "\n\n"

        count = fit_count(description[offset:], render)
        if not count:
            flush()
            continue
        part += render(description[offset : offset + count])
        has_body = True
        offset += count
        if offset < len(description):
            flush()

    for group in groups:
        remaining = group
        while remaining:
            count = fit_count(remaining, lambda values: _path_line(values, directory))
            if not count:
                flush()
                continue
            part += _path_line(remaining[:count], directory)
            has_body = True
            remaining = remaining[count:]
            if remaining:
                flush()
    if not groups:
        if not fits(page_header + part + path_lines[0]):
            flush()
        if not fits(page_header + part + path_lines[0]):
            raise ValueError("DWG asset budget cannot hold its identity and mapping status")
        part += path_lines[0]
        has_body = True
    if has_body:
        parts.append(part)
    return parts


def build_asset_catalogs(
    sheets: list[dict],
    asset_chunks: dict,
    *,
    max_tokens: int,
    max_chars: int,
) -> None:
    """Attach bounded Markdown files without reading or changing JSON records.

    A range label always includes individual links to all its actual files.
    Missing mappings stay explicit; no chunk number is supplied by the model.
    """

    def fits(value: str) -> bool:
        return len(value) <= max_chars and estimate_embedding_input_tokens(value) <= max_tokens

    for sheet in sheets:
        sheet["asset_files"], sheet["asset_paths"] = [], []
        assets = [region for region in sheet.get("regions", []) if region.get("type") == "view"]
        if not assets:
            continue
        directory = sheet["directory"]
        page_header = (
            f"# 图形资产目录 · {_text(sheet['sheet_id'])}\n\n"
            f"Sheet：{_text(sheet.get('title', ''))}\n\n"
            "分片可被多个 asset 共用；图像概述仅供导航。\n\n"
        )
        parts: list[str] = []
        for index, asset in enumerate(assets, 1):
            asset_id = asset.get("id") or f"A{index:03d}"
            mapping = asset_chunks.get(sheet["sheet_id"], {}).get(asset_id, {})
            parts.extend(_asset_parts(asset_id, asset, mapping, directory, page_header, fits))

        pages: list[str] = []
        current = ""
        for part in parts:
            candidate = current + ("\n" if current else "") + part
            if not fits(page_header + candidate):
                if current:
                    pages.append(page_header + current)
                current = part
            else:
                current = candidate
            if not fits(page_header + current):
                raise ValueError("DWG asset budget cannot hold its identity and a complete item")
        if current:
            pages.append(page_header + current)
        names = (
            ["assets.md"]
            if len(pages) == 1
            else [f"assets_{index:03d}.md" for index in range(1, len(pages) + 1)]
        )
        sheet["asset_files"] = list(zip(names, pages, strict=True))
        sheet["asset_paths"] = [f"{directory}/{name}" for name in names]
