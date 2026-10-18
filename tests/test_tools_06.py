"""The 0.6 tools in-process against the fake extension: build_from_autocad on the layer standard with units,
write_plan_dxf styles, DWG and open, and preview_plan without SketchUp."""

from __future__ import annotations

import base64
import subprocess

import ezdxf
from ezdxf import units

from conftest import DXF, FIXTURES, FakeExtension, broken_dxfs
from plan05 import full_plan
from plomada_bridge import cadfile, server
from plomada_bridge.dxf import read_plan
from plomada_bridge.models import Plan
from test_tools import _session, _text
from test_write_06 import STANDARD


def _plan_input(raw=None):
    raw = dict(raw or full_plan())
    raw.pop("storey", None)
    return raw


def test_preview_plan_is_read_only_and_write_plan_dxf_takes_style_format_and_open(run, tmp_path):
    async def body(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    tools = run(_session(FakeExtension(), tmp_path, body))
    assert tools["preview_plan"].annotations.read_only_hint is True
    props = tools["write_plan_dxf"].input_schema["properties"]
    assert props["style"]["default"] == "drawing" and props["style"]["enum"] == ["drawing", "records", "layers"]
    assert props["format"]["enum"] == ["dxf", "dwg"] and props["open"]["default"] is False
    build = tools["build_from_autocad"].input_schema["properties"]
    assert (
        build["units"]["enum"] == ["auto", "mm", "cm", "m", "in", "ft"] and ".dwg" in build["dxf_path"]["description"]
    )


def test_build_from_autocad_reads_the_layer_standard_in_centimetres(run, tmp_path):
    doc = ezdxf.new("R2013")
    doc.units = units.CM
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True, dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_line((150, 0), (240, 0), dxfattribs={"layer": "PLOMADA-PUERTA"})
    path = tmp_path / "cm.dxf"
    doc.saveas(path)
    fake = FakeExtension(handlers={"build_plan": lambda p: {"walls": 1, "openings": 1, "warnings": []}})

    async def body(client):
        return await client.call_tool("build_from_autocad", {"dxf_path": str(path), "roof": "none"})

    result = run(_session(fake, tmp_path, body))
    assert not result.is_error, _text(result)
    plan = next(p for m, p in fake.seen if m == "build_plan")["plan"]
    assert plan["walls"][0]["axis"][2] == [6000.0, 4000.0] and plan["openings"][0]["offset"] == 1500.0
    out = result.structured_content
    assert (out["units"], out["records"], "units_note" in out) == ("cm", 2, False)


def test_build_from_autocad_says_why_it_ignored_insunits(run, tmp_path):
    fake = FakeExtension(handlers={"build_plan": lambda p: {"warnings": []}})

    async def body(client):
        return await client.call_tool("build_from_autocad", {"dxf_path": str(FIXTURES / "casa_completa_N00.dxf")})

    out = run(_session(fake, tmp_path, body)).structured_content
    assert out["units"] == "mm"
    assert out["units_note"] == "$INSUNITS 6 ignored: ACADMCP_ARCH and PLOMADA records are millimetres"


def test_write_plan_dxf_draws_by_default_and_reads_back_exactly(run, tmp_path):
    path = tmp_path / "N01.dxf"

    async def body(client):
        return await client.call_tool(
            "write_plan_dxf",
            {"plan": _plan_input(), "path": str(path), "storey": "N01", "storey_height": 3000, "elevation": 3100},
        )

    result = run(_session(FakeExtension(), tmp_path, body))
    assert not result.is_error, _text(result)
    out = result.structured_content
    assert (out["style"], out["format"], out["records"], "opened" in out) == ("drawing", "dxf", 30, False)
    assert "A-WALL-PATT" in out["layers"]
    assert read_plan(path).plan.to_wire() == Plan.model_validate(full_plan()).to_wire()


def test_write_plan_dxf_in_the_layer_standard_reports_what_is_lost(run, tmp_path):
    async def body(client):
        exact = await client.call_tool(
            "write_plan_dxf", {"plan": _plan_input(STANDARD), "path": str(tmp_path / "a.dxf"), "style": "layers"}
        )
        lossy = await client.call_tool(
            "write_plan_dxf", {"plan": _plan_input(), "path": str(tmp_path / "b.dxf"), "style": "layers"}
        )
        return exact, lossy

    exact, lossy = run(_session(FakeExtension(), tmp_path, body))
    assert not exact.is_error, _text(exact)
    # the tool always writes its storey, which the standard leaves to build_from_autocad's arguments
    assert exact.structured_content["lost"] == [
        "storey N00 left out: the standard has no storey; build_from_autocad takes storey, storey_height and elevation"
    ]
    assert len(lossy.structured_content["lost"]) > 10


def test_write_plan_dxf_writes_a_dwg_through_the_converter_and_opens_it(run, tmp_path, monkeypatch):
    exe = tmp_path / "ODAFileConverter.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("PLOMADA_ODA", str(exe))
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        out_dir, kind, name = args[2], args[4], args[7]
        with open(f"{out_dir}/{name.rsplit('.', 1)[0]}.{kind.lower()}", "wb") as fh:
            fh.write(b"AC1032 converted")
        return subprocess.CompletedProcess(args, 0, "", "")

    opened: list[str] = []
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(server, "open_file", lambda p: opened.append(str(p)))
    path = tmp_path / "planos" / "casa.dwg"

    async def body(client):
        good = await client.call_tool(
            "write_plan_dxf", {"plan": _plan_input(), "path": str(path), "format": "dwg", "open": True}
        )
        mismatch = await client.call_tool("write_plan_dxf", {"plan": _plan_input(), "path": str(path)})
        return good, mismatch

    good, mismatch = run(_session(FakeExtension(), tmp_path, body))
    assert not good.is_error, _text(good)
    out = good.structured_content
    assert (out["path"], out["format"], out["opened"]) == (str(path), "dwg", True)
    assert path.read_bytes() == b"AC1032 converted" and opened == [str(path)]
    ((_, _, _, version, kind, _, _, name),) = calls
    assert (version, kind, name) == ("ACAD2018", "DWG", "casa.dxf")
    assert mismatch.is_error and _text(mismatch) == (
        "write_plan_dxf refused its input (-32004): path ends in .dwg but format is dxf. Fix it and call again."
    )


def test_write_plan_dxf_to_dwg_without_a_converter_names_both(run, tmp_path, monkeypatch):
    monkeypatch.delenv("PLOMADA_ODA", raising=False)
    monkeypatch.setattr(cadfile, "ODA_GLOB", str(tmp_path / "nada" / "*.exe"))
    monkeypatch.setattr(cadfile.shutil, "which", lambda name: None)
    path = tmp_path / "casa.dwg"

    async def body(client):
        return await client.call_tool("write_plan_dxf", {"plan": _plan_input(), "path": str(path), "format": "dwg"})

    result = run(_session(FakeExtension(), tmp_path, body))
    assert result.is_error and not path.exists()
    assert _text(result).startswith(f"write_plan_dxf could not write {path}: casa.dxf is a DXF and no converter")


def test_preview_plan_draws_a_plan_or_a_drawing_without_sketchup(run, tmp_path):
    fake = FakeExtension()

    async def body(client):
        plan = await client.call_tool("preview_plan", {"plan": _plan_input(STANDARD), "width": 800})
        drawing = await client.call_tool("preview_plan", {"dxf_path": str(DXF), "width": 640})
        neither = await client.call_tool("preview_plan", {})
        missing = await client.call_tool("preview_plan", {"dxf_path": str(tmp_path / "no.dxf")})
        broken = [await client.call_tool("preview_plan", {"dxf_path": str(p)}) for p in broken_dxfs(tmp_path)]
        return plan, drawing, neither, missing, broken

    plan, drawing, neither, missing, broken = run(_session(fake, tmp_path, body))
    for result in (plan, drawing):
        assert not result.is_error, _text(result)
        image = next(c for c in result.content if c.type == "image")
        data = base64.b64decode(image.data)
        assert image.mime_type == "image/png" and data[:4] == b"\x89PNG" and len(data) <= 350_000
    assert '"bytes"' in _text(plan) and "plomada_preview_casa_minimalista.png" in _text(drawing)
    assert _text(neither) == "preview_plan refused its input (-32004): give plan or dxf_path, one of them. Fix it."
    assert "is not a file" in _text(missing)
    for result, name in zip(broken, ("cortado.dxf", "ilegible.dxf"), strict=True):
        assert result.is_error and _text(result).startswith(
            f"preview_plan refused its input (-32004): cannot read {name} as DXF: "
        )
    assert [m for m in fake.methods() if m != "hello"] == []  # SketchUp is never asked
