#!/usr/bin/env python3
"""Run the DWG Sheet/Markdown pipeline without OpenViking ingestion."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dwg_sheet_smoke import _read_dwg, _sha256

from openviking.parse.parsers.dwg import _region_classifier
from openviking.parse.parsers.dwg_render import sheet_renderer
from openviking.parse.parsers.dwg_sheet import build_sheet_artifacts
from openviking_cli.utils.config import get_openviking_config


def _append(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def _successes(events: Path, output: Path) -> tuple[set[str], dict[str, dict]]:
    completed = set()
    by_sha = {}
    if not events.exists():
        return completed, by_sha
    for line in events.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("status") == "success" and all(
            (output / path).is_file() for path in event.get("files", [])
        ):
            completed.add(event["source"])
            digest = event.get("sha256") or _sha256(Path(event["source"]))
            by_sha.setdefault(digest, event)
    return completed, by_sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    files = sorted(path for path in args.dataset.rglob("*") if path.suffix.casefold() == ".dwg")
    if args.limit is not None:
        files = files[: args.limit]
    completed, by_sha = _successes(args.events, args.output)
    markdown = get_openviking_config().markdown
    classifier = _region_classifier()

    args.output.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(files, 1):
        source_key = str(source.resolve())
        if source_key in completed:
            print(f"[{index:02d}/{len(files)}] skip {source.name}", flush=True)
            continue
        relative = source.relative_to(args.dataset)
        destination = args.output / relative
        digest = _sha256(source)
        started = time.monotonic()
        if digest in by_sha:
            previous = by_sha[digest]
            previous_output = args.output / previous["relative_source"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(previous_output, destination)
            output_files = [
                (relative / path.relative_to(destination)).as_posix()
                for path in sorted(destination.rglob("*.md"))
            ]
            event = {
                "at": datetime.now(timezone.utc).isoformat(),
                "source": source_key,
                "relative_source": relative.as_posix(),
                "sha256": digest,
                "status": "success",
                "sheet_count": previous["sheet_count"],
                "parse_status": previous.get("parse_status", "unknown"),
                "files": output_files,
                "reused_from": previous["source"],
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            _append(args.events, event)
            print(
                f"[{index:02d}/{len(files)}] reuse {event['sheet_count']} sheets "
                f"from {Path(previous['source']).name}",
                flush=True,
            )
            continue
        print(f"[{index:02d}/{len(files)}] parse {relative}", flush=True)
        try:
            if destination.exists():
                raise RuntimeError(f"output already exists without a success event: {destination}")
            drawing, warnings = _read_dwg(source)
            artifacts = build_sheet_artifacts(
                drawing,
                source_name=source.name,
                source_sha256=digest,
                source_size_bytes=source.stat().st_size,
                max_section_tokens=markdown.max_section_size,
                max_section_chars=markdown.max_section_chars,
                region_classifier=classifier,
                sheet_renderer=sheet_renderer(source),
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=f".{destination.name}-", dir=destination.parent
            ) as temp_dir:
                stage = Path(temp_dir) / destination.name
                written = []
                for sheet in artifacts["sheets"]:
                    sheet_dir = stage / sheet["directory"]
                    sheet_dir.mkdir(parents=True, exist_ok=True)
                    for filename, content in sheet["content_files"]:
                        if not filename.startswith("context") or not filename.endswith(".md"):
                            raise RuntimeError(f"unexpected Markdown filename: {filename}")
                        target = sheet_dir / filename
                        target.write_text(content, encoding="utf-8")
                        written.append(target.relative_to(stage).as_posix())
                os.replace(stage, destination)
            output_files = [(relative / path).as_posix() for path in written]
            event = {
                "at": datetime.now(timezone.utc).isoformat(),
                "source": source_key,
                "relative_source": relative.as_posix(),
                "sha256": digest,
                "status": "success",
                "sheet_count": len(artifacts["sheets"]),
                "parse_status": "partial"
                if any(sheet["status"] == "partial" for sheet in artifacts["sheets"])
                else "complete",
                "files": output_files,
                "warnings": warnings,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            _append(args.events, event)
            by_sha.setdefault(digest, event)
            print(
                f"[{index:02d}/{len(files)}] success "
                f"{len(artifacts['sheets'])} sheets, {len(written)} Markdown, "
                f"{time.monotonic() - started:.1f}s",
                flush=True,
            )
        except Exception as exc:
            _append(
                args.events,
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "source": source_key,
                    "relative_source": relative.as_posix(),
                    "sha256": digest,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
            )
            print(f"[{index:02d}/{len(files)}] failed {type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
