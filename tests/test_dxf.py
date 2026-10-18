"""DXF parsing of the AutoCAD MCP Pro fixture with ezdxf."""

from __future__ import annotations

import json
import shutil

import ezdxf
import pytest

from conftest import DXF, FIXTURES, broken_dxfs
from plomada_bridge.dxf import APP_ID, DxfPlanError, read_plan, records


def test_fixture_has_exactly_4_walls_11_openings_4_rooms():
    parsed = read_plan(DXF)
    plan = parsed.plan
    assert len(plan.walls) == 4
    assert len(plan.openings) == 11
    assert len(plan.rooms) == 4
    assert parsed.records == 19
    assert sorted(w.id for w in plan.walls) == ["B", "EXT", "P1", "P2"]
    assert sum(o.opening_kind == "door" for o in plan.openings) == 4
    assert sum(o.opening_kind == "window" for o in plan.openings) == 7
    assert parsed.warnings == []


def test_records_match_the_spec_examples():
    plan = read_plan(DXF).plan
    ext = next(w for w in plan.walls if w.id == "EXT")
    assert ext.axis == [(100.0, 100.0), (11900.0, 100.0), (11900.0, 8900.0), (100.0, 8900.0)]
    assert (ext.closed, ext.thickness, ext.justification, ext.material) == (True, 200.0, "center", "brick")
    p1 = next(w for w in plan.walls if w.id == "P1")
    assert p1.axis == [(7060.0, 3560.0), (11900.0, 3560.0)] and p1.thickness == 120.0
    assert p1.material == "gypsum_board"
    d1 = next(o for o in plan.openings if o.id == "D1")
    assert (d1.wall, d1.opening_kind, d1.offset, d1.width, d1.height, d1.sill, d1.swing, d1.hand) == (
        "EXT",
        "door",
        5500.0,
        1000.0,
        2200.0,
        None,
        "in",
        "left",
    )
    r1 = next(r for r in plan.rooms if r.id == "R1")
    assert (r1.name, r1.number, r1.at, r1.area) == ("LIVING", "01", (3000.0, 2500.0), 58480000.0)
    assert next(r for r in plan.rooms if r.id == "R3").name == "BAÑO"
    offsets = sorted(o.offset for o in plan.openings if o.wall == "EXT")
    assert offsets == [700, 5500, 8500, 15800, 21900, 26500, 29300, 35300]


def test_dxf_agrees_with_the_decoded_records_dump():
    dump = json.loads((FIXTURES / "casa_arch_records.json").read_text(encoding="cp1252"))
    doc = ezdxf.readfile(DXF)
    assert sorted(json.dumps(r, sort_keys=True) for r in records(doc)) == sorted(
        json.dumps(r, sort_keys=True) for r in dump
    )


def test_wire_plan_carries_versions_and_kinds():
    wire = read_plan(DXF, storey_height=3000.0).plan.to_wire()
    assert {w["kind"] for w in wire["walls"]} == {"wall"}
    assert all(r["v"] == 1 for r in wire["walls"] + wire["openings"] + wire["rooms"])
    assert wire["storey"] == {"name": "N00", "height": 3000.0}


def _edit_first(path, kind, mutate):
    doc = ezdxf.readfile(path)
    for e in doc.entitydb.values():
        if hasattr(e, "has_xdata") and e.has_xdata(APP_ID):
            payload = json.loads("".join(t.value for t in e.get_xdata(APP_ID) if t.code == 1000))
            if payload["kind"] == kind:
                mutate(payload)
                text = json.dumps(payload)
                e.set_xdata(APP_ID, [(1000, text[i : i + 255]) for i in range(0, len(text), 255)])
                break
    doc.saveas(path)


def test_record_version_other_than_1_is_refused_by_name(tmp_path):
    path = tmp_path / "v2.dxf"
    shutil.copy(DXF, path)
    _edit_first(path, "opening", lambda p: p.update(v=2))
    with pytest.raises(DxfPlanError, match=r"has version 2; Plomada reads version 1 only"):
        read_plan(path)


def test_invalid_record_names_the_field(tmp_path):
    path = tmp_path / "bad.dxf"
    shutil.copy(DXF, path)
    _edit_first(path, "wall", lambda p: p.update(thickness=-200))
    with pytest.raises(DxfPlanError, match=r"walls\[0\]\.thickness must be greater than 0, got -200"):
        read_plan(path)


def test_a_dxf_without_records_and_a_missing_file(tmp_path):
    empty = tmp_path / "empty.dxf"
    ezdxf.new().saveas(empty)
    with pytest.raises(DxfPlanError, match="holds no ACADMCP_ARCH records"):
        read_plan(empty)
    with pytest.raises(DxfPlanError, match="is not a file"):
        read_plan(tmp_path / "nope.dxf")


def test_a_damaged_dxf_is_refused_by_name(tmp_path):
    for path in broken_dxfs(tmp_path):
        with pytest.raises(DxfPlanError, match=rf"^cannot read {path.name} as DXF: "):
            read_plan(path)


def test_unseen_four_building_plan_parses_completely():
    """Four buildings drawn after 0.1.0 (L-shape, X crossing, six rooms, reference house)."""
    parsed = read_plan(FIXTURES / "casos_prueba.dxf")
    plan = parsed.plan
    assert (len(plan.walls), len(plan.openings), len(plan.rooms)) == (15, 44, 12)
    assert parsed.records == 71
    assert sum(o.opening_kind == "door" for o in plan.openings) == 18
    assert sum(o.opening_kind == "window" for o in plan.openings) == 26
    assert {"L_EXT", "L_T1", "X_A", "X_B", "S_C"} <= {w.id for w in plan.walls}


def _add_record(path, payload):
    doc = ezdxf.readfile(path)
    if APP_ID not in doc.appids:
        doc.appids.new(APP_ID)
    line = doc.modelspace().add_line((0, 0), (1, 0))
    text = json.dumps(payload)
    line.set_xdata(APP_ID, [(1000, text[i : i + 255]) for i in range(0, len(text), 255)])
    doc.saveas(path)


STAIR = {
    "v": 1,
    "kind": "stair",
    "id": "S1",
    "start": [1000.0, 1000.0],
    "direction_deg": 0.0,
    "width": 1000.0,
    "risers": 17,
    "riser_height": 173.5,
    "going": 280.0,
    "stair_kind": "l",
    "turn": "left",
}


def test_stair_records_reach_the_plan_with_their_stair_kind(tmp_path):
    path = tmp_path / "stair.dxf"
    shutil.copy(DXF, path)
    _add_record(path, STAIR)
    parsed = read_plan(path)
    assert not any("stair" in w for w in parsed.warnings)
    (stair,) = parsed.plan.to_wire()["stairs"]
    assert stair["kind"] == "stair" and stair["stair_kind"] == "l"
    assert stair["start"] == [1000.0, 1000.0] and stair["risers"] == 17


def test_a_stair_with_a_fractional_riser_count_is_refused_by_name(tmp_path):
    path = tmp_path / "bad_stair.dxf"
    shutil.copy(DXF, path)
    _add_record(path, {**STAIR, "risers": 16.5})
    with pytest.raises(DxfPlanError, match=r"stairs\[0\]\.risers"):
        read_plan(path)


def test_catalogue_blocks_become_furniture_records():
    parsed = read_plan(FIXTURES / "casa_2_plantas_N00.dxf")
    pieces = parsed.plan.to_wire()["furniture"]
    assert sorted(p["item"] for p in pieces) == [
        "armchair",
        "coffee_table",
        "dining_table_4",
        "fridge",
        "kitchen_counter",
        "sofa_3_seat",
    ]
    sofa = next(p for p in pieces if p["item"] == "sofa_3_seat")
    assert sofa["at"] == [100.0, 5000.0] and sofa["rotation"] == 270.0 and sofa["kind"] == "furniture"
    assert len(parsed.plan.stairs) == 1 and not parsed.warnings


def test_site_blocks_and_layers_become_site_records(tmp_path):
    path = tmp_path / "sitio.dxf"
    shutil.copy(DXF, path)
    doc = ezdxf.readfile(path)
    blk = doc.blocks.new("SITIO_ARBOL")
    blk.add_circle((0, 0), 2500)
    msp = doc.modelspace()
    msp.add_blockref("SITIO_ARBOL", (15000, 2000), dxfattribs={"rotation": 30})
    doc.layers.add("SITIO-PAVIMENTO")
    doc.layers.add("SITIO-CERCO")
    msp.add_lwpolyline(
        [(12000, 0), (18000, 0), (18000, 5000), (12000, 5000)], close=True, dxfattribs={"layer": "SITIO-PAVIMENTO"}
    )
    msp.add_lwpolyline([(-2000, -2000), (20000, -2000)], dxfattribs={"layer": "SITIO-CERCO"})
    doc.saveas(path)
    wire = read_plan(path).plan.to_wire()["site"]
    (tree,) = wire["objects"]
    assert tree["item"] == "ARBOL" and tree["at"] == [15000.0, 2000.0] and tree["rotation"] == 30.0
    assert len(wire["paving"]) == 1 and len(wire["paving"][0]["points"]) == 4
    assert wire["fences"][0]["closed"] is False and wire["fences"][0]["points"][1] == [20000.0, -2000.0]


def test_the_complete_house_fixture_carries_furniture_and_site():
    wire = read_plan(FIXTURES / "casa_completa_N00.dxf").plan.to_wire()
    assert (len(wire["walls"]), len(wire["openings"]), len(wire["rooms"]), len(wire["furniture"])) == (6, 13, 5, 17)
    site = wire["site"]
    assert {k: len(v) for k, v in site.items()} == {"objects": 21, "paving": 2, "decks": 1, "pools": 1, "fences": 1}
    assert {o["item"] for o in site["objects"]} >= {"AUTO", "SUV", "ARBOL", "PALMERA", "REPOSERA", "PERSONA"}
    garage = next(o for o in wire["openings"] if o["id"] == "GD")
    assert garage["width"] == 2700.0
