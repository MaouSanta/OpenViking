#!/usr/bin/env python3
# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Write deterministic DWG Sheet artifacts to a local inspection directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from openviking.parse.parsers.dwg import _normalize_libredwg_json
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_dwg(source: Path) -> tuple[dict, list[str]]:
    with tempfile.TemporaryDirectory(prefix="openviking-dwg-smoke-") as temp_dir:
        json_path = Path(temp_dir) / "libredwg.json"
        command = ["dwgread", "-O", "JSON", "-o", str(json_path), str(source)]
        try:
            process = subprocess.run(command, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise RuntimeError("LibreDWG executable 'dwgread' is not available") from exc
        if process.returncode:
            details = (process.stderr or process.stdout).decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"LibreDWG conversion failed for {source.name} "
                f"(exit {process.returncode}): {details[-2000:]}"
            )
        try:
            raw = json_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"LibreDWG produced no JSON for {source.name}") from exc
    drawing, _normalized, warnings = _normalize_libredwg_json(raw, source.name)
    return drawing, warnings


def _write_artifacts(output: Path, artifacts: dict) -> list[str]:
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise RuntimeError(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    written = ["document.json"]
    (output / "document.json").write_text(
        json.dumps(artifacts["document"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for sheet in artifacts["sheets"]:
        sheet_dir = output / sheet["directory"]
        sheet_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in sheet["content_files"]:
            path = sheet_dir / filename
            path.write_text(content, encoding="utf-8")
            written.append(path.relative_to(output).as_posix())
        structure_path = output / sheet["structure_path"]
        structure_path.write_text(
            json.dumps(sheet["structure"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(structure_path.relative_to(output).as_posix())
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dwg", type=Path, help="input DWG file")
    parser.add_argument("output", type=Path, help="new or empty inspection directory")
    parser.add_argument("--json", action="store_true", help="print a JSON result summary")
    args = parser.parse_args(argv)

    try:
        source = args.dwg.expanduser().resolve()
        output = args.output.expanduser().resolve()
        if not source.is_file():
            raise RuntimeError(f"DWG file not found: {source}")
        if source.suffix.casefold() != ".dwg":
            raise RuntimeError(f"input must be a .dwg file: {source}")
        if output == source.parent or source.is_relative_to(output):
            raise RuntimeError("output must not contain the source DWG")

        drawing, warnings = _read_dwg(source)
        artifacts = build_sheet_artifacts(
            drawing,
            source_name=source.name,
            source_sha256=_sha256(source),
            source_size_bytes=source.stat().st_size,
        )
        written = _write_artifacts(output, artifacts)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")

    summary = {
        "document_id": artifacts["document"]["document_id"],
        "sheet_count": artifacts["document"]["sheet_count"],
        "output": str(output),
        "written": written,
        "warnings": warnings,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Wrote {summary['sheet_count']} Sheet bundle(s) to {output}")
        for path in written:
            print(f"  {path}")
        for warning in warnings:
            print(f"warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
