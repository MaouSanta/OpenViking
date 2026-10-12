# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Exercise transient CAD crops without ODA, model calls, or output files."""

import builtins
import io

import pytest

from openviking.parse.parsers.dwg_render import _decode_render_text, render_crop, sheet_renderer

ezdxf = pytest.importorskip("ezdxf")
pytest.importorskip("matplotlib")
Image = pytest.importorskip("PIL.Image")


def _record(document):
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, Configuration
    from ezdxf.addons.drawing.recorder import Recorder

    recorder = Recorder()
    Frontend(
        RenderContext(document),
        recorder,
        config=Configuration(background_policy=BackgroundPolicy.BLACK),
    ).draw_layout(document.modelspace())
    return recorder.player()


def _image(player, box, pixels=400):
    content = render_crop(player, box, max_pixels=pixels)
    assert content.startswith(b"\x89PNG\r\n\x1a\n")
    return Image.open(io.BytesIO(content)).convert("RGB")


def _solid(layout, points, color):
    hatch = layout.add_hatch(dxfattribs={"true_color": color})
    hatch.paths.add_polyline_path(points, is_closed=True)


def test_cad_curves_hatch_and_text_each_survive_rendering():
    document = ezdxf.new()
    model = document.modelspace()
    model.add_circle((0, 0), 10, dxfattribs={"color": 7})
    model.add_arc((40, 0), 10, 30, 250, dxfattribs={"color": 7})
    model.add_ellipse((80, 0), major_axis=(10, 0), ratio=0.5, dxfattribs={"color": 7})
    _solid(model, [(100, 0), (110, 0), (110, 10), (100, 10)], 0xFFFFFF)
    model.add_text("CAD notes 123", dxfattribs={"insert": (0, 30), "height": 8, "color": 7})
    player = _record(document)
    for box in [
        (-15, -15, 15, 15),
        (25, -15, 55, 15),
        (65, -15, 95, 15),
        (98, -2, 112, 12),
        (-2, 28, 90, 45),
    ]:
        image = _image(player, box, pixels=240)
        histogram = image.convert("L").histogram()
        assert sum(histogram[101:]) > 20
        assert sum(histogram[:20]) > 20


def test_bbox_maps_to_png_without_margins_with_upward_cad_y():
    document = ezdxf.new()
    model = document.modelspace()
    _solid(model, [(0, 70), (20, 70), (20, 90), (0, 90)], 0xFF0000)
    _solid(model, [(140, 30), (160, 30), (160, 50), (140, 50)], 0x0000FF)
    player = _record(document)
    image = _image(player, (-20, 10, 180, 110))
    assert image.size == (400, 200)
    assert image.getpixel((60, 60)) == (255, 0, 0)
    assert image.getpixel((340, 140)) == (0, 0, 255)
    assert image.getpixel((60, 140)) == (0, 0, 0)
    assert image.getpixel((340, 60)) == (0, 0, 0)
    red_pixels = [
        (x, y)
        for y in range(image.height)
        for x in range(image.width)
        if image.getpixel((x, y)) == (255, 0, 0)
    ]
    # Coordinates map to x=40..80, y=40..80; allow one antialiased edge pixel.
    assert abs(min(x for x, _ in red_pixels) - 40) <= 1
    assert abs(max(x for x, _ in red_pixels) - 80) <= 1
    assert abs(min(y for _, y in red_pixels) - 40) <= 1
    assert abs(max(y for _, y in red_pixels) - 80) <= 1
    detail = _image(player, (0, 70, 20, 90))
    assert detail.size == (400, 400)
    assert detail.getpixel((200, 200)) == (255, 0, 0)


def test_repeated_crops_leave_original_recording_unchanged():
    document = ezdxf.new()
    model = document.modelspace()
    model.add_circle((0, 0), 10)
    model.add_line((-30, -20), (30, 20))
    model.add_text("UNCHANGED", dxfattribs={"insert": (0, 15), "height": 3})
    player = _record(document)
    bounds = (tuple(player.bbox().extmin), tuple(player.bbox().extmax))
    count = len(player.records)
    full_box = (-35, -25, 40, 25)
    before = _image(player, full_box).tobytes()
    for box in [(-5, -5, 5, 5), (0, 10, 30, 20), (100, 100, 110, 110)]:
        _image(player, box)
    assert _image(player, full_box).tobytes() == before
    assert (tuple(player.bbox().extmin), tuple(player.bbox().extmax)) == bounds
    assert len(player.records) == count


def test_crop_returns_bytes_without_opening_output_files(monkeypatch):
    document = ezdxf.new()
    document.modelspace().add_circle((0, 0), 10)
    player = _record(document)
    original_open = builtins.open
    original_io_open = io.open

    def read_only(original):
        def open_file(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                pytest.fail(f"render_crop opened a file for output: {file}")
            return original(file, mode, *args, **kwargs)

        return open_file

    monkeypatch.setattr(builtins, "open", read_only(original_open))
    monkeypatch.setattr(io, "open", read_only(original_io_open))
    assert _image(player, (-15, -15, 15, 15)).size == (400, 400)


@pytest.mark.parametrize("box", [(0, 0, 0, 10), (0, 0, 10, -1), (0, 0, float("nan"), 10)])
def test_invalid_crop_bounds_are_rejected(box):
    with pytest.raises(ValueError):
        render_crop(_record(ezdxf.new()), box)


def test_mif_display_decoding_preserves_unicode_and_mtext_formatting():
    document = ezdxf.new()
    model = document.modelspace()
    encoded = r"\M+5B9E2\M+5B7FC"
    text = model.add_text(encoded)
    mtext = model.add_mtext(r"{\C1;" + encoded + r"}\P正常 Unicode")
    block = document.blocks.new("example")
    definition = block.add_attdef(encoded, text=encoded, dxfattribs={"prompt": encoded})
    attribute = model.add_blockref("example", (0, 0)).add_attrib("name", encoded)
    unicode_text = model.add_text("已是中文 Unicode ± Ø 123")
    malformed = model.add_text(r"keep \M+6FFFF literal")

    _decode_render_text(document)

    assert text.dxf.text == attribute.dxf.text == definition.dxf.text == "光伏"
    assert definition.dxf.tag == "光伏"
    assert definition.dxf.prompt == encoded  # The prompt is not rendered.
    assert mtext.text == r"{\C1;光伏}\P正常 Unicode"
    assert unicode_text.dxf.text == "已是中文 Unicode ± Ø 123"
    assert malformed.dxf.text == r"keep \M+6FFFF literal"


def test_sheet_renderer_decodes_temporary_document_without_writing_source(tmp_path, monkeypatch):
    from ezdxf.addons import odafc

    source = tmp_path / "source.dwg"
    source.write_bytes(b"immutable DWG snapshot")
    temporary_dxf = tmp_path / "oda.dxf"
    document = ezdxf.new()
    text = document.modelspace().add_text(r"\M+5B9E2\M+5B7FC", dxfattribs={"height": 3})
    document.saveas(temporary_dxf)
    before = {path: path.read_bytes() for path in (source, temporary_dxf)}
    loaded = ezdxf.readfile(temporary_dxf)
    calls = []

    def read_oda(path, **kwargs):
        calls.append((path, kwargs))
        return loaded

    def no_save(*args, **kwargs):
        pytest.fail("Rendering attempted to save its decoded document")

    monkeypatch.setattr(odafc, "readfile", read_oda)
    monkeypatch.setattr(
        "openviking.parse.parsers.dwg_render._configure_oda_file_converter", lambda: None
    )
    monkeypatch.setattr(loaded, "save", no_save)
    monkeypatch.setattr(loaded, "saveas", no_save)
    render = sheet_renderer(source)
    assert render((-2, -2, 20, 8)).startswith(b"\x89PNG\r\n\x1a\n")
    assert render((0, 0, 10, 6)).startswith(b"\x89PNG\r\n\x1a\n")
    assert loaded.entitydb[text.dxf.handle].dxf.text == "光伏"
    assert calls == [(source, {"version": "R2018", "audit": True})]
    assert {path: path.read_bytes() for path in before} == before
