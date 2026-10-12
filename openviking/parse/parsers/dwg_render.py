# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Transient Model Space images for VLM region selection; never export Sheet DWGs."""

from __future__ import annotations

import io
import math
import os
import shutil
import sys
import threading
from pathlib import Path

_RENDER_LOCK = threading.Lock()  # matplotlib and ezdxf font caches are process-global


def _configure_oda_file_converter() -> None:
    import ezdxf
    from ezdxf.addons import odafc

    candidates = [
        os.environ.get("OPENVIKING_ODA_FILE_CONVERTER", "").strip(),
        shutil.which("ODAFileConverter") or "",
    ]
    if sys.platform == "darwin":
        candidates.append("/Applications/ODAFileConverter.app/Contents/MacOS/ODAFileConverter")
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if executable:
        ezdxf.options.set("odafc-addon", "unix_exec_path", executable)
    if not odafc.is_installed():
        raise RuntimeError(
            "DWG rendering requires ODA File Converter; install it or set OPENVIKING_ODA_FILE_CONVERTER"
        )


def _decode_render_text(document) -> None:
    """Decode ODA's MIF notation only in the temporary document used for display."""
    from ezdxf.lldxf.encoding import decode_mif_to_unicode, has_mif_encoding

    for entity in document.entitydb.values():
        kind = entity.dxftype()
        if kind == "MTEXT":
            if has_mif_encoding(entity.text):
                entity.text = decode_mif_to_unicode(entity.text)
        elif kind in {"TEXT", "ATTRIB", "ATTDEF"}:
            # Standalone ATTDEF renders its tag; ATTRIB renders its text value.
            for field in ("text", "tag") if kind == "ATTDEF" else ("text",):
                text = entity.dxf.get(field, "")
                if has_mif_encoding(text):
                    entity.dxf.set(field, decode_mif_to_unicode(text))


def sheet_renderer(source: Path):
    """Lazily convert/render once, then crop recordings in original model coordinates."""
    player = None
    failure = None

    def render(box):
        nonlocal player, failure
        with _RENDER_LOCK:
            if failure is not None:
                raise RuntimeError(failure)
            try:
                if player is None:
                    from ezdxf.addons import odafc
                    from ezdxf.addons.drawing import Frontend, RenderContext
                    from ezdxf.addons.drawing.config import (
                        BackgroundPolicy,
                        Configuration,
                        ImagePolicy,
                    )
                    from ezdxf.addons.drawing.recorder import Recorder

                    _configure_oda_file_converter()
                    document = odafc.readfile(source, version="R2018", audit=True)
                    _decode_render_text(document)
                    recorder = Recorder()
                    config = Configuration(
                        background_policy=BackgroundPolicy.BLACK, image_policy=ImagePolicy.RECT
                    )
                    Frontend(RenderContext(document), recorder, config=config).draw_layout(
                        document.modelspace()
                    )
                    player = recorder.player()
                return render_crop(player, box)
            except Exception as exc:
                failure = f"DWG region render unavailable: {exc}"
                raise RuntimeError(failure) from exc

    return render


def render_crop(player, box, max_pixels: int = 2048) -> bytes:
    """Map bbox corners to PNG corners, with CAD Y increasing upward.

    For the returned width W / height H: px=(x-x0)*W/(x1-x0),
    py=(y1-y)*H/(y1-y0). No automatic crop, margin or viewport transform.
    """
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        raise ValueError("Invalid Sheet render bounds")
    x0, y0, x1, y1 = box
    sx, sy = x1 - x0, y1 - y0
    if sx <= 0 or sy <= 0:
        raise ValueError("Empty Sheet render bounds")
    width = max(1, round(max_pixels * sx / max(sx, sy)))
    height = max(1, round(max_pixels * sy / max(sx, sy)))
    figure = Figure(figsize=(width / 100, height / 100), dpi=100)
    canvas = FigureCanvasAgg(figure)
    axes = figure.add_axes((0, 0, 1, 1))
    cropped = player.copy()
    cropped.crop_rect((x0, y0), (x1, y1), distance=max(sx, sy) / max_pixels / 4)
    cropped.replay(MatplotlibBackend(axes, adjust_figure=False))
    # replay.finalize() enables autoscaling, so fix the window afterwards.
    axes.set_xlim(x0, x1)
    axes.set_ylim(y0, y1)
    axes.set_aspect("auto")
    axes.set_autoscale_on(False)
    output = io.BytesIO()
    canvas.print_png(output)
    figure.clear()
    return output.getvalue()
