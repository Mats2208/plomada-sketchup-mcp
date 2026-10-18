"""The 0.5 tools in-process against the fake extension: write_plan_dxf, set_slab, add_slab and build_plan."""

from __future__ import annotations

from conftest import FakeExtension
from plan05 import full_plan
from plomada_bridge.dxf import read_plan
from plomada_bridge.models import Plan
from test_tools import _session, _text

SQUARE = [[0, 0], [4000, 0, 0.5], [4000, 3000], [0, 3000]]


def _plan_input():
    raw = full_plan()
    raw.pop("storey")  # the tools take the storey as their own arguments
    return raw


def test_write_plan_dxf_writes_a_file_that_reads_back_exactly(run, tmp_path):
    fake = FakeExtension()
    path = tmp_path / "planos" / "N01.dxf"

    async def body(client):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        result = await client.call_tool(
            "write_plan_dxf",
            {"plan": _plan_input(), "path": str(path), "storey": "N01", "storey_height": 3000, "elevation": 3100},
        )
        return tools["write_plan_dxf"], result

    tool, result = run(_session(fake, tmp_path, body))
    assert not result.is_error, _text(result)
    assert tool.annotations.read_only_hint is False and tool.annotations.open_world_hint is False
    assert tool.annotations.destructive_hint is True and tool.annotations.idempotent_hint is False  # open=true
    out = result.structured_content
    assert out["path"] == str(path) and out["records"] == 30 and "PLOMADA-LOSA" in out["layers"]
    assert out["entities"] > out["records"]
    assert read_plan(path).plan.to_wire() == Plan.model_validate(full_plan()).to_wire()
    assert [m for m in fake.methods() if m != "hello"] == []  # SketchUp is never asked


def test_write_plan_dxf_refuses_a_bad_plan_before_writing(run, tmp_path):
    raw = _plan_input()
    raw["pools"][0]["on"] = "L9"
    path = tmp_path / "bad.dxf"

    async def body(client):
        return await client.call_tool("write_plan_dxf", {"plan": raw, "path": str(path)})

    result = run(_session(FakeExtension(), tmp_path, body))
    assert result.is_error and not path.exists()
    assert _text(result) == (
        'write_plan_dxf refused its input (-32004): plan.pools[0].on names slab "L9", which is not in the plan; '
        "slabs are L1, L2. Fix it and call again."
    )


def test_set_slab_sends_only_the_given_fields(run, tmp_path):
    fake = FakeExtension(handlers={"set_slab": lambda p: {"slab": p["id"]}})

    async def body(client):
        first = await client.call_tool(
            "set_slab", {"id": "L1", "outline": SQUARE, "top_offset": -40, "edge_band": {"height": 450, "depth": 150}}
        )
        second = await client.call_tool("set_slab", {"id": "L1", "holes": [], "edge_band": False, "storey": "N01"})
        return first, second

    first, second = run(_session(fake, tmp_path, body))
    assert not first.is_error and not second.is_error, (_text(first), _text(second))
    sent = [p for m, p in fake.seen if m == "set_slab"]
    assert sent[0] == {
        "id": "L1",
        "outline": [[0.0, 0.0], [4000.0, 0.0, 0.5], [4000.0, 3000.0], [0.0, 3000.0]],
        "top_offset": -40.0,
        "edge_band": {"height": 450.0, "depth": 150.0},
    }
    assert sent[1] == {"id": "L1", "holes": [], "edge_band": None, "storey": "N01"}


def test_add_slab_keeps_the_0_4_wire_and_adds_the_new_keys_when_given(run, tmp_path):
    fake = FakeExtension()

    async def body(client):
        await client.call_tool("add_slab", {"outline": [[0, 0], [1000, 0], [1000, 1000]]})
        await client.call_tool("add_slab", {"id": "L2", "outline": SQUARE, "material": "MAT_madera", "storey": "N01"})

    run(_session(fake, tmp_path, body))
    old, new = [p for m, p in fake.seen if m == "add_slab"]
    assert old == {"thickness": 150.0, "outline": [[0.0, 0.0], [1000.0, 0.0], [1000.0, 1000.0]]}
    assert new["id"] == "L2" and new["outline"][1] == [4000.0, 0.0, 0.5] and new["material"] == "MAT_madera"
    assert set(new) == {"thickness", "outline", "id", "material", "storey"}


def test_build_plan_sends_the_slabs_and_ground_level(run, tmp_path):
    fake = FakeExtension(handlers={"build_plan": lambda p: {"walls": len(p["plan"]["walls"]), "warnings": []}})

    async def body(client):
        built = await client.call_tool(
            "build_plan", {"plan": _plan_input(), "storey": "N01", "elevation": 3100, "ground_level_mm": -450}
        )
        plain = await client.call_tool("build_plan", {"plan": {"walls": full_plan()["walls"]}})
        return built, plain

    built, plain = run(_session(fake, tmp_path, body))
    assert not built.is_error and not plain.is_error, (_text(built), _text(plain))
    sent, sent_plain = [p for m, p in fake.seen if m == "build_plan"]
    assert [s["id"] for s in sent["plan"]["slabs"]] == ["L1", "L2"] and sent["plan"]["site_on_ground"] is True
    assert sent["plan"]["ramps"][0]["z_end"] == -1620.0
    assert sent["options"]["ground_level_mm"] == -450.0 and sent["options"]["elevation"] == 3100.0
    assert "ground_level_mm" not in sent_plain["options"] and "slabs" not in sent_plain["plan"]
