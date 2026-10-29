"""The MCP tools in-process against the fake extension: what they send and what the model reads."""

from __future__ import annotations

import base64
import json

from mcp.client import Client

from conftest import DXF, FakeExtension, Reject, make_config
from plomada_bridge.errors import NOT_RESPONDING
from plomada_bridge.server import Bridge, create_server

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100 + b"\xff\xd9"


async def _session(fake: FakeExtension, tmp_path, body, **cfg):
    await fake.start()
    bridge = Bridge(make_config(tmp_path, fake.port, **cfg))
    try:
        async with Client(create_server(bridge)) as client:
            return await body(client)
    finally:
        await bridge.client.close()
        await fake.stop()


def _text(result) -> str:
    return " ".join(getattr(c, "text", "") for c in result.content)


def test_tool_list_and_annotations(run, tmp_path):
    async def body(client):
        return (await client.list_tools()).tools

    tools = {t.name: t for t in run(_session(FakeExtension(), tmp_path, body))}
    assert len(tools) == 34
    for name in (
        "status",
        "model_info",
        "list_entities",
        "list_tags",
        "list_materials",
        "get_plan",
        "capture_view",
        "job_status",
        "plan_from_dxf",
        "preview_plan",
        "revit_batches",
    ):
        assert tools[name].annotations.read_only_hint is True, name
        assert tools[name].annotations.destructive_hint is False
    for name in ("build_from_autocad", "reset_plomada", "undo", "execute_ruby"):
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is True
    # axonometric_scenes adds scenes and section planes but erases nothing it did not make
    assert tools["axonometric_scenes"].annotations.read_only_hint is False
    assert tools["axonometric_scenes"].annotations.destructive_hint is False
    assert tools["axonometric_scenes"].annotations.idempotent_hint is True
    assert tools["set_slab"].annotations.read_only_hint is False  # replace a slab: the same call, the same result
    assert tools["set_slab"].annotations.idempotent_hint is True
    assert tools["write_plan_dxf"].annotations.read_only_hint is False  # open=true launches the app on every call
    assert tools["write_plan_dxf"].annotations.idempotent_hint is False
    # library_prepare writes .skp caches next to the sources but leaves the model as it was
    assert tools["library_prepare"].annotations.read_only_hint is False
    assert tools["library_prepare"].annotations.destructive_hint is False
    assert tools["library_prepare"].annotations.idempotent_hint is True
    props = tools["build_from_autocad"].input_schema["properties"]
    assert props["storey_height"]["default"] == 2800.0 and "mm" in props["storey_height"]["description"]
    assert props["roof"]["enum"] == ["flat", "gable", "hip", "none"]


def test_build_from_autocad_sends_the_parsed_plan(run, tmp_path):
    fake = FakeExtension(
        progress={"build_plan": 2},
        handlers={"build_plan": lambda p: {"walls": 4, "openings": 11, "steps": 2, "warnings": []}},
    )

    async def body(client):
        return await client.call_tool("build_from_autocad", {"dxf_path": str(DXF), "roof": "gable"})

    result = run(_session(fake, tmp_path, body))
    assert not result.is_error, _text(result)
    sent = next(p for m, p in fake.seen if m == "build_plan")
    plan = sent["plan"]
    assert (len(plan["walls"]), len(plan["openings"]), len(plan["rooms"])) == (4, 11, 4)
    assert sent["options"] == {
        "storey_height": 2800.0,
        "slab": True,
        "roof": "gable",
        "overhang": 400.0,
        "replace": True,
        "storey": "N00",
        "furniture": "library",
    }
    assert result.structured_content["records"] == 19


def test_export_drawings_sends_views_scale_and_sections(run, tmp_path):
    fake = FakeExtension(handlers={"export_drawings": lambda p: {"dir": p["dir"], "images": []}})

    async def body(client):
        return await client.call_tool(
            "export_drawings",
            {
                "dir": str(tmp_path),
                "views": ["elevations", "sections"],
                "styles": ["line"],
                "px_per_m": 90,
                "sections": [{"name": "A-A", "axis": "x", "at": 12500, "look": "west"}],
            },
        )

    result = run(_session(fake, tmp_path, body))
    assert not result.is_error, _text(result)
    sent = next(p for m, p in fake.seen if m == "export_drawings")
    assert sent["views"] == ["elevations", "sections"] and sent["styles"] == ["line"] and sent["px_per_m"] == 90
    assert sent["sections"] == [{"name": "A-A", "axis": "x", "at": 12500.0, "look": "west"}]
    assert sent["cut_height"] == 1200.0


def test_the_component_library_travels_with_the_build(run, tmp_path):
    lib = tmp_path / "biblioteca"
    lib.mkdir()
    (lib / "mapa_componentes.json").write_text("{}", encoding="utf-8")
    fake = FakeExtension(handlers={"build_plan": lambda p: {"walls": 4, "warnings": []}})

    async def body(client):
        return await client.call_tool("build_from_autocad", {"dxf_path": str(DXF), "furniture": "massing"})

    result = run(_session(fake, tmp_path, body, library_dir=lib))
    assert not result.is_error, _text(result)
    options = next(p for m, p in fake.seen if m == "build_plan")["options"]
    assert options["library"] == str(lib) and options["furniture"] == "massing"


def test_upper_storey_and_edits_carry_the_storey(run, tmp_path):
    fake = FakeExtension(
        handlers={
            "build_plan": lambda p: {"walls": 4, "warnings": []},
            "move_opening": lambda p: {"opening": p["id"]},
        },
    )

    async def body(client):
        built = await client.call_tool(
            "build_from_autocad", {"dxf_path": str(DXF), "storey": "N01", "elevation": 2950.0, "roof": "none"}
        )
        moved = await client.call_tool("move_opening", {"id": "W1", "offset": 500.0, "storey": "N01"})
        bad = await client.call_tool("move_opening", {"id": "W1", "offset": 500.0, "storey": "piso 1"})
        return built, moved, bad

    built, moved, bad = run(_session(fake, tmp_path, body))
    assert not built.is_error and not moved.is_error, (_text(built), _text(moved))
    options = next(p for m, p in fake.seen if m == "build_plan")["options"]
    assert (options["storey"], options["elevation"], options["roof"]) == ("N01", 2950.0, "none")
    assert next(p for m, p in fake.seen if m == "move_opening") == {"id": "W1", "offset": 500.0, "storey": "N01"}
    assert bad.is_error and "storey" in _text(bad)


def test_bad_dxf_path_is_refused_before_sketchup(run, tmp_path):
    fake = FakeExtension()

    async def body(client):
        return await client.call_tool("build_from_autocad", {"dxf_path": str(tmp_path / "missing.dxf")})

    result = run(_session(fake, tmp_path, body))
    assert result.is_error and "(-32004)" in _text(result) and "is not a file" in _text(result)
    assert "build_plan" not in fake.methods()


def test_status_when_sketchup_is_blocked(run, tmp_path):
    async def body(client):
        return await client.call_tool("status", {})

    result = run(_session(FakeExtension(answer_hello=False), tmp_path, body))
    assert result.is_error
    assert _text(result) == NOT_RESPONDING


def test_capture_view_returns_an_image(run, tmp_path):
    fake = FakeExtension(
        handlers={
            "capture_view": lambda p: {
                "path": "C:/Temp/plomada_capture_1.jpg",
                "width": p["width"],
                "height": p["height"],
                "bytes": len(JPEG),
                "mime": "image/jpeg",
                "style": p["style"],
                "data_base64": base64.b64encode(JPEG).decode(),
            }
        }
    )

    async def body(client):
        return await client.call_tool("capture_view", {"style": "lines_only"})

    result = run(_session(fake, tmp_path, body))
    image = next(c for c in result.content if c.type == "image")
    assert image.mime_type == "image/jpeg" and base64.b64decode(image.data) == JPEG
    meta = json.loads(next(c.text for c in result.content if c.type == "text"))
    assert meta["path"].endswith(".jpg") and meta["width"] == 1280 and meta["style"] == "lines_only"


def test_extension_errors_become_tool_errors(run, tmp_path):
    def refuse(_p):
        raise Reject(-32010, "execute_ruby is disabled")

    async def body(client):
        return await client.call_tool("execute_ruby", {"code": "1+1"})

    result = run(_session(FakeExtension(handlers={"execute_ruby": refuse}), tmp_path, body))
    assert result.is_error and "(-32010)" in _text(result) and "Extensions > Plomada > Settings" in _text(result)


def test_argument_bounds_are_enforced_by_the_schema(run, tmp_path):
    async def body(client):
        return await client.call_tool("undo", {"steps": 11})

    fake = FakeExtension()
    result = run(_session(fake, tmp_path, body))
    assert result.is_error
    assert "undo" not in fake.methods()


def test_library_prepare_sends_the_library_keys_and_force(run, tmp_path):
    lib = tmp_path / "biblioteca"
    lib.mkdir()
    (lib / "mapa_componentes.json").write_text('{"sitio": {"tree": {"src": "a/tree.glb"}}}', encoding="utf-8")
    reply = {"library": str(lib), "entries": [], "converted": 0, "cached": 0, "failed": 0}
    fake = FakeExtension(handlers={"library_prepare": lambda p: reply})

    async def body(client):
        every = await client.call_tool("library_prepare", {})
        some = await client.call_tool("library_prepare", {"keys": ["tree"], "force": True})
        return every, some

    every, some = run(_session(fake, tmp_path, body, library_dir=lib))
    assert not every.is_error and not some.is_error, _text(every) + _text(some)
    sent = [p for m, p in fake.seen if m == "library_prepare"]
    assert sent[0] == {"library": str(lib), "force": False}
    assert sent[1] == {"library": str(lib), "force": True, "keys": ["tree"]}


def test_library_prepare_refuses_a_folder_without_a_map(run, tmp_path):
    async def body(client):
        return await client.call_tool("library_prepare", {"library": str(tmp_path)})

    result = run(_session(FakeExtension(), tmp_path, body))
    assert result.is_error and "no mapa_componentes.json in" in _text(result)
