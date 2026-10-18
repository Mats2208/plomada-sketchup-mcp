"""The 0.6 layer standard: what any CAD draws on the PLOMADA-* layers, read back exactly with no XDATA;
units, chaining, opening hosts, refusals, and DWG input through a converter."""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import ezdxf
import pytest
from ezdxf import units
from ezdxf.enums import TextEntityAlignment

from conftest import DXF, FIXTURES
from plomada_bridge import cadfile
from plomada_bridge.dxf import PLOMADA_APP, DxfPlanError, chain, read_plan

QUARTER = math.tan(math.radians(90) / 4)
HALF = 1.0


def _doc(unit: int = units.MM):
    doc = ezdxf.new("R2013")
    doc.units = unit
    return doc, doc.modelspace()


def _save(doc, tmp_path, name="capas.dxf"):
    path = tmp_path / name
    doc.saveas(path)
    return path


def _box(msp, layer="PLOMADA-MURO-200", w=6000.0, h=4000.0):
    return msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True, dxfattribs={"layer": layer})


def _tag(entity, payload):
    text = json.dumps({"v": 1, **payload})
    entity.set_xdata(PLOMADA_APP, [(1000, text[i : i + 255]) for i in range(0, len(text), 255)])
    return entity


# --- walls ----------------------------------------------------------------------------------


def test_lines_chain_into_one_closed_wall_in_the_direction_of_the_first(tmp_path):
    doc, msp = _doc()
    first = msp.add_line((0, 0), (6000, 0), dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_line((0, 4000), (6000, 4000), dxfattribs={"layer": "plomada-wall-200"})  # drawn backwards
    msp.add_line((0, 0), (0, 4000.4), dxfattribs={"layer": "PLOMADA-MURO-200"})  # 0.4 mm short of the corner
    msp.add_line((6000, 0), (6000, 4000), dxfattribs={"layer": "PLOMADA-MURO-200"})
    (w,) = read_plan(_save(doc, tmp_path)).plan.to_wire()["walls"]
    assert (w["id"], w["closed"], w["thickness"], w["justification"]) == (first.dxf.handle, True, 200.0, "center")
    assert w["axis"] == [[0.0, 0.0], [6000.0, 0.0], [6000.0, 4000.0], [0.0, 4000.0]]


def test_a_node_of_three_ends_and_another_thickness_stop_a_chain(tmp_path):
    doc, msp = _doc()
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_line((5000, 0), (9000, 0), dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_line((5000, 0), (5000, 3000), dxfattribs={"layer": "PLOMADA-MURO-200"})  # a T: three ends at 5000
    msp.add_line((9000, 0), (9000, 3000), dxfattribs={"layer": "PLOMADA-MURO-120"})  # thinner: its own wall
    walls = read_plan(_save(doc, tmp_path)).plan.to_wire()["walls"]
    assert [(len(w["axis"]), w["thickness"], w["closed"]) for w in walls] == [
        (2, 200.0, False),
        (2, 200.0, False),
        (2, 200.0, False),
        (2, 120.0, False),
    ]


def test_arcs_keep_their_bulge_and_chain_with_lines(tmp_path):
    doc, msp = _doc()
    msp.add_line((0, 0), (4000, 0), dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_arc((4000, 2000), 2000, -90, 90, dxfattribs={"layer": "PLOMADA-MURO-200"})  # a half circle CCW
    msp.add_line((4000, 4000), (0, 4000), dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_line((0, 4000), (0, 0), dxfattribs={"layer": "PLOMADA-MURO-200"})
    (w,) = read_plan(_save(doc, tmp_path)).plan.to_wire()["walls"]
    assert w["closed"] is True
    assert w["axis"] == [[0.0, 0.0], [4000.0, 0.0, pytest.approx(HALF)], [4000.0, 4000.0], [0.0, 4000.0]]


def test_an_arc_over_180_degrees_is_split_and_a_mirrored_arc_turns_clockwise(tmp_path):
    doc, msp = _doc()
    msp.add_arc((0, 0), 3000, 0, 270, dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_arc((20000, 0), 3000, 0, 90, dxfattribs={"layer": "PLOMADA-MURO-150", "extrusion": (0, 0, -1)})
    big, mirrored = read_plan(_save(doc, tmp_path)).plan.to_wire()["walls"]
    third = math.tan(math.radians(135) / 4)
    assert big["axis"] == [
        [3000.0, 0.0, pytest.approx(third)],
        [pytest.approx(-2121.320344), pytest.approx(2121.320344), pytest.approx(third)],
        [0.0, -3000.0],
    ]
    # OCS angles 0..90 seen from below run clockwise from (-23000, 0) to (-20000, 3000) about (-20000, 0)
    assert mirrored["axis"] == [[-23000.0, 0.0, pytest.approx(-QUARTER)], [-20000.0, 3000.0]]


def test_circles_and_old_polylines_are_walls(tmp_path):
    doc, msp = _doc()
    msp.add_circle((10000, 0), 2500, dxfattribs={"layer": "PLOMADA-MURO-200"})
    pl = msp.add_polyline2d([(0, 0), (4000, 0), (4000, 3000)], format="xy", dxfattribs={"layer": "PLOMADA-MURO-150"})
    pl.vertices[1].dxf.bulge = 0.25
    ring, old = read_plan(_save(doc, tmp_path)).plan.to_wire()["walls"]
    assert ring["closed"] is True and ring["axis"] == [[12500.0, 0.0, 1.0], [7500.0, 0.0, 1.0]]
    assert old["closed"] is False and old["axis"] == [[0.0, 0.0], [4000.0, 0.0, 0.25], [4000.0, 3000.0]]


def test_chain_joins_through_degree_two_nodes_only():
    # B is drawn backwards and 0.5 mm off A's end; C continues it; D would make the node at 3000 a T
    pieces = [("A", [[0, 0], [1000, 0]]), ("B", [[2000, 0], [1000, 0.5]]), ("C", [[2000, 0], [3000, 0]])]
    assert chain(pieces) == [("A", [[0, 0], [1000, 0], [2000, 0], [3000, 0]], False)]
    pieces += [("D", [[3000, 0], [3000, 1000]]), ("E", [[3000, 0], [4000, 0]])]
    assert [h for h, _, _ in chain(pieces)] == ["A", "D", "E"]
    # two half circles, the second drawn the same way as the first: a closed ring, the second reversed
    ((handle, verts, closed),) = chain([("A", [[0, 0, 1.0], [1000, 0]]), ("B", [[0, 0, 1.0], [1000, 0]])])
    assert (handle, closed) == ("A", True) and verts == [[0, 0, 1.0], [1000, 0, -1.0]]


# --- openings -------------------------------------------------------------------------------


def test_doors_and_windows_take_offset_width_sill_and_height_from_the_drawing(tmp_path):
    doc, msp = _doc()
    wall = _box(msp)
    door = msp.add_line((2400, 30), (1500, -20), dxfattribs={"layer": "PLOMADA-PUERTA"})  # far jamb first
    msp.add_line((6000, 1000), (6000, 2500), dxfattribs={"layer": "plomada-ventana-900-1200"})
    msp.add_lwpolyline([(4000, 4000), (5000, 4000)], dxfattribs={"layer": "PLOMADA-DOOR-2400"})
    msp.add_line((0, 3500), (0, 1500), dxfattribs={"layer": "PLOMADA-WINDOW-0-2400"})
    parsed = read_plan(_save(doc, tmp_path))
    d1, w1, d2, w2 = parsed.plan.to_wire()["openings"]
    assert d1 == {
        "v": 1,
        "kind": "opening",
        "id": door.dxf.handle,
        "wall": wall.dxf.handle,
        "opening_kind": "door",
        "offset": pytest.approx(1500.0),
        "width": pytest.approx(900.0, abs=1e-3),
        "sill": None,
        "height": None,
        "swing": "in",
        "hand": "left",
        "tag": None,
    }
    assert (w1["opening_kind"], w1["offset"], w1["width"], w1["sill"], w1["height"]) == (
        "window",
        7000.0,
        1500.0,
        900.0,
        1200.0,
    )
    assert (d2["offset"], d2["width"], d2["height"]) == (11000.0, 1000.0, 2400.0)  # 6000 + 4000 + (6000 - 5000)
    assert (w2["offset"], w2["width"], w2["sill"], w2["height"]) == (16500.0, 2000.0, 0.0, 2400.0)
    assert parsed.records == 5 and parsed.warnings == []


def test_an_opening_on_an_arc_measures_along_the_arc(tmp_path):
    doc, msp = _doc()
    arc = [(0, 0, 0, 0, HALF), (8000, 0, 0, 0, 0)]
    msp.add_lwpolyline(arc, format="xyseb", dxfattribs={"layer": "PLOMADA-MURO-200"})
    # the half circle runs counter-clockwise from (0, 0) under its centre (4000, 0) to (8000, 0)
    msp.add_arc((4000, 0), 4000, 225, 270, dxfattribs={"layer": "PLOMADA-VENTANA"})
    (o,) = read_plan(_save(doc, tmp_path)).plan.to_wire()["openings"]
    assert o["offset"] == pytest.approx(4000 * math.pi / 4, abs=1e-5)
    assert o["width"] == pytest.approx(4000 * math.pi / 4, abs=1e-5)
    assert (o["sill"], o["height"]) == (None, None)


def test_openings_on_a_closed_wall_touching_its_first_vertex(tmp_path):
    doc, msp = _doc()
    _box(msp)
    msp.add_line((0, 1000), (0, 0), dxfattribs={"layer": "PLOMADA-VENTANA"})  # the last run, ending at vertex 0
    msp.add_line((0, 0), (800, 0), dxfattribs={"layer": "PLOMADA-PUERTA"})  # the first run, from vertex 0
    last, first = read_plan(_save(doc, tmp_path)).plan.to_wire()["openings"]
    assert (last["offset"], last["width"]) == (19000.0, 1000.0)
    assert (first["offset"], first["width"]) == (0.0, 800.0)


def test_refusals_name_the_entity(tmp_path):
    doc, msp = _doc()
    _box(msp)
    stray = msp.add_line((3000, 2000), (3900, 2000), dxfattribs={"layer": "PLOMADA-PUERTA"})
    with pytest.raises(DxfPlanError) as err:
        read_plan(_save(doc, tmp_path))
    assert str(err.value) == (
        f"door {stray.dxf.handle} on PLOMADA-PUERTA from (3000, 2000) to (3900, 2000) is on no wall: no wall axis "
        "lies within half the wall's thickness of both jambs; draw it from jamb to jamb on its wall's axis"
    )

    doc, msp = _doc()
    a = _box(msp)
    b = msp.add_line((1000, 0), (5000, 0), dxfattribs={"layer": "PLOMADA-MURO-120"})  # doubles the bottom run
    door = msp.add_line((2000, 0), (2900, 0), dxfattribs={"layer": "PLOMADA-PUERTA"})
    with pytest.raises(DxfPlanError) as err:
        read_plan(_save(doc, tmp_path, "doble.dxf"))
    assert str(err.value) == (
        f"door {door.dxf.handle} on PLOMADA-PUERTA lies on walls {a.dxf.handle}, {b.dxf.handle} at once; "
        "draw it on one wall's axis"
    )

    doc, msp = _doc()
    a = _box(msp)
    seam = msp.add_lwpolyline([(0, 500), (0, 0), (500, 0)], dxfattribs={"layer": "PLOMADA-VENTANA"})
    with pytest.raises(DxfPlanError, match=f"window {seam.dxf.handle} on PLOMADA-VENTANA is a LWPOLYLINE of 3 points"):
        read_plan(_save(doc, tmp_path, "tres.dxf"))
    seam = msp.query("LWPOLYLINE[layer=='PLOMADA-VENTANA']")[0]
    msp.delete_entity(seam)
    corner = msp.add_line((0, 500), (500, 0), dxfattribs={"layer": "PLOMADA-VENTANA"})
    with pytest.raises(DxfPlanError) as err:
        read_plan(_save(doc, tmp_path, "esquina.dxf"))
    assert str(err.value) == (
        f"window {corner.dxf.handle} on PLOMADA-VENTANA straddles the first vertex of closed wall {a.dxf.handle}; "
        "an opening sits inside one run of its wall"
    )


def test_openings_find_hosts_among_autocad_mcp_pro_walls_and_take_a_plomada_payload(tmp_path):
    doc = ezdxf.readfile(DXF)  # EXT runs (100, 100) -> (11900, 100) -> ... closed, 200 thick
    msp = doc.modelspace()
    doc.appids.new(PLOMADA_APP)
    door = msp.add_line((1100, 100), (2000, 100), dxfattribs={"layer": "PLOMADA-PUERTA-2200"})
    _tag(door, {"kind": "opening", "id": "D9", "swing": "out", "hand": "right", "tag": "P9"})
    parsed = read_plan(_save(doc, tmp_path, "mixta.dxf"))
    d9 = next(o for o in parsed.plan.to_wire()["openings"] if o["id"] == "D9")
    assert (d9["wall"], d9["offset"], d9["width"], d9["height"]) == ("EXT", 1000.0, 900.0, 2200.0)
    assert (d9["swing"], d9["hand"], d9["tag"]) == ("out", "right", "P9")
    assert parsed.records == 19 + 1


# --- rooms, stairs, furniture ---------------------------------------------------------------


def test_room_labels_give_name_point_and_area(tmp_path):
    doc, msp = _doc()
    _box(msp)
    left = msp.add_text("DORMITORIO", dxfattribs={"layer": "PLOMADA-AMBIENTE", "insert": (1000, 1000)})
    centred = msp.add_text("BAÑO", dxfattribs={"layer": "PLOMADA-ROOM"})
    centred.set_placement((4000, 3000), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_mtext(r"{\fArial|b1;SALA}\P12,5 m²", dxfattribs={"layer": "PLOMADA-AMBIENTE", "insert": (3000, 2000)})
    msp.add_mtext(r"COCINA\PA: 9.75 m2", dxfattribs={"layer": "PLOMADA-AMBIENTE", "insert": (500, 3500)})
    msp.add_text("   ", dxfattribs={"layer": "PLOMADA-AMBIENTE"})
    parsed = read_plan(_save(doc, tmp_path))
    rooms = parsed.plan.to_wire()["rooms"]
    assert [(r["name"], r["at"], r["area"]) for r in rooms] == [
        ("DORMITORIO", [1000.0, 1000.0], 0.0),
        ("BAÑO", [4000.0, 3000.0], 0.0),
        ("SALA", [3000.0, 2000.0], 12_500_000.0),
        ("COCINA", [500.0, 3500.0], 9_750_000.0),
    ]
    assert rooms[0]["id"] == left.dxf.handle and rooms[0]["number"] is None
    assert len(parsed.warnings) == 1 and parsed.warnings[0].endswith("on PLOMADA-AMBIENTE skipped: no room name")


def test_a_stair_line_gets_risers_from_floor_to_floor(tmp_path):
    doc, msp = _doc()
    _box(msp, w=9000)
    line = msp.add_line((1000, 1000), (1000, 4200), dxfattribs={"layer": "PLOMADA-ESCALERA-1000"})
    (s,) = read_plan(_save(doc, tmp_path), storey_height=2800.0).plan.to_wire()["stairs"]
    # 2800 storey + 150 slab = 2950 mm: 17 risers of 173.53, 16 goings of 3200 / 16
    assert s == {
        "v": 1,
        "kind": "stair",
        "id": line.dxf.handle,
        "start": [1000.0, 1000.0],
        "direction_deg": 90.0,
        "width": 1000.0,
        "risers": 17,
        "riser_height": 173.529412,
        "going": 200.0,
        "stair_kind": "straight",
        "turn": "left",
    }


def test_a_stair_on_an_explicit_storey_and_an_l_stair_from_its_payload(tmp_path):
    doc, msp = _doc()
    doc.appids.new(PLOMADA_APP)
    _box(msp, w=9000)
    _tag(msp.add_point((0, 0)), {"kind": "storey", "name": "N01", "height": 3000, "elevation": 3000})
    msp.add_line((1000, 1000), (5480, 1000), dxfattribs={"layer": "PLOMADA-STAIR-1100"})
    l_stair = msp.add_line((7000, 500), (7000, 1500), dxfattribs={"layer": "PLOMADA-STAIR-900"})
    _tag(l_stair, {"kind": "stair", "id": "S2", "risers": 18, "riser_height": 166.666667, "going": 270,
                   "stair_kind": "l", "turn": "right"})  # fmt: skip
    straight, l_ = read_plan(_save(doc, tmp_path)).plan.to_wire()["stairs"]
    assert (straight["risers"], straight["riser_height"], straight["going"]) == (17, 176.470588, 280.0)
    assert (l_["id"], l_["stair_kind"], l_["turn"], l_["risers"], l_["width"]) == ("S2", "l", "right", 18, 900.0)


def test_inserts_of_any_block_on_the_furniture_layer(tmp_path):
    doc, msp = _doc()
    _box(msp)
    for name in ("SOFA", "Cama Doble", "ARCH_WC", "ROBLE"):
        doc.blocks.new(name).add_line((0, 0), (100, 0))
    msp.add_blockref("SOFA", (1000, 500), dxfattribs={"layer": "PLOMADA-MUEBLE", "rotation": 90})
    msp.add_blockref("Cama Doble", (2000, 500), dxfattribs={"layer": "PLOMADA-FURNITURE"}).add_attrib(
        "ITEM", "double_bed"
    )
    msp.add_blockref("ARCH_WC", (3000, 500), dxfattribs={"layer": "PLOMADA-MUEBLE"})  # counted once
    msp.add_blockref("ROBLE", (9000, 500), dxfattribs={"layer": "ARBOLES"})  # not furniture
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "PLOMADA-MUEBLE"})
    parsed = read_plan(_save(doc, tmp_path))
    assert [(f["item"], f["at"], f["rotation"]) for f in parsed.plan.to_wire()["furniture"]] == [
        ("sofa", [1000.0, 500.0], 90.0),
        ("double_bed", [2000.0, 500.0], 0.0),
        ("wc", [3000.0, 500.0], 0.0),
    ]
    (warning,) = parsed.warnings
    assert warning.endswith("on PLOMADA-MUEBLE skipped: a furniture layer takes INSERT")


def test_english_aliases_for_the_0_5_layers(tmp_path):
    doc, msp = _doc()
    _box(msp, layer="PLOMADA-WALL-200")
    msp.add_lwpolyline([(0, 0), (6000, 0), (6000, 4000)], close=True, dxfattribs={"layer": "PLOMADA-SLAB"})
    msp.add_line((0, 0), (3000, 0), dxfattribs={"layer": "PLOMADA-RAILING"})
    msp.add_line((3000, 0), (3000, 2000), dxfattribs={"layer": "PLOMADA-RAILING"})
    msp.add_circle((1000, 1000), 150, dxfattribs={"layer": "PLOMADA-COLUMN"})
    msp.add_lwpolyline([(0, 0), (2000, 0), (2000, 1000), (0, 1000)], close=True, dxfattribs={"layer": "PLOMADA-POOL"})
    msp.add_arc((0, 0), 5000, 0, 90, dxfattribs={"layer": "PLOMADA-CURTAIN-WALL"})
    msp.add_lwpolyline([(0, 0), (900, 0), (900, 400)], close=True, dxfattribs={"layer": "PLOMADA-PLANTER"})
    wire = read_plan(_save(doc, tmp_path)).plan.to_wire()
    kinds = ("walls", "slabs", "railings", "columns", "pools", "curtain_walls", "planters")
    assert [len(wire[k]) for k in kinds] == [1] * len(kinds)
    assert wire["railings"][0]["path"] == [[0.0, 0.0], [3000.0, 0.0], [3000.0, 2000.0]]
    assert wire["curtain_walls"][0]["path"] == [[5000.0, 0.0, pytest.approx(QUARTER)], [0.0, 5000.0]]


def test_unknown_layers_and_wrong_types_are_skipped_with_a_warning(tmp_path):
    doc, msp = _doc()
    _box(msp)
    text = msp.add_text("M1", dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "PLOMADA-MUROS-200"})
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "PLOMADA-MUROS-200"})
    msp.add_line((0, 0), (1, 0), dxfattribs={"layer": "PLOMADA-VENTANA-900"})
    parsed = read_plan(_save(doc, tmp_path))
    assert parsed.warnings == [
        f"TEXT {text.dxf.handle} on PLOMADA-MURO-200 skipped: a wall layer takes LINE, ARC, LWPOLYLINE, POLYLINE, "
        "SPLINE, ELLIPSE, CIRCLE",
        "layer PLOMADA-MUROS-200 is not part of the drawing standard (docs/drawing-standard.md): 2 entities skipped",
        "layer PLOMADA-VENTANA-900 is not part of the drawing standard (docs/drawing-standard.md): 1 entity skipped",
    ]


def test_a_drawing_with_nothing_readable_names_the_three_routes(tmp_path):
    doc, msp = _doc()
    msp.add_line((0, 0), (5000, 0), dxfattribs={"layer": "MUROS"})
    with pytest.raises(DxfPlanError) as err:
        read_plan(_save(doc, tmp_path, "nada.dxf"))
    assert str(err.value) == (
        "nada.dxf holds no ACADMCP_ARCH records, no PLOMADA records and nothing on a PLOMADA-* layer: draw it with "
        "AutoCAD MCP Pro's arch_* tools, write it with write_plan_dxf, or draw the PLOMADA-* layer standard with "
        "any CAD (docs/drawing-standard.md)"
    )


# --- units ----------------------------------------------------------------------------------


def test_insunits_scales_coordinates_but_never_the_numbers_in_layer_names(tmp_path):
    doc, msp = _doc(units.CM)
    _box(msp, w=600, h=400)
    msp.add_line((150, 0), (240, 0), dxfattribs={"layer": "PLOMADA-PUERTA-2100"})
    msp.add_mtext(r"SALA\P24 m2", dxfattribs={"layer": "PLOMADA-AMBIENTE", "insert": (300, 200)})
    msp.add_circle((100, 100), 15, dxfattribs={"layer": "PLOMADA-COLUMNA"})
    parsed = read_plan(_save(doc, tmp_path))
    wire = parsed.plan.to_wire()
    assert parsed.units == "cm" and parsed.units_note is None
    assert wire["walls"][0]["axis"][2] == [6000.0, 4000.0] and wire["walls"][0]["thickness"] == 200.0
    o = wire["openings"][0]
    assert (o["offset"], o["width"], o["height"]) == (1500.0, 900.0, 2100.0)
    assert wire["rooms"][0]["at"] == [3000.0, 2000.0] and wire["rooms"][0]["area"] == 24_000_000.0
    assert wire["columns"][0]["at"] == [1000.0, 1000.0] and wire["columns"][0]["diameter"] == 300.0


def test_metres_that_would_span_kilometres_are_read_as_millimetres(tmp_path):
    doc, msp = _doc(units.M)  # ezdxf's default: a drawing made in mm keeps $INSUNITS 6
    _box(msp)
    parsed = read_plan(_save(doc, tmp_path))
    assert parsed.units == "mm" and parsed.plan.walls[0].axis[1] == (6000.0, 0.0)
    assert parsed.units_note == (
        "$INSUNITS 6 (metres) would make the walls span 6000 m; read as millimetres (pass units to force a unit)"
    )
    doc, msp = _doc(units.M)
    _box(msp, w=6, h=4)
    parsed = read_plan(_save(doc, tmp_path, "metros.dxf"))
    assert (parsed.units, parsed.units_note, parsed.plan.walls[0].axis[1]) == ("m", None, (6000.0, 0.0))


def test_inches_must_be_confirmed_and_units_overrides_everything(tmp_path):
    doc, msp = _doc(units.IN)
    _box(msp, w=240, h=120)
    path = _save(doc, tmp_path, "pulgadas.dxf")
    with pytest.raises(DxfPlanError) as err:
        read_plan(path)
    assert str(err.value) == (
        "pulgadas.dxf declares $INSUNITS 1 (inches), the default of AutoCAD's imperial template that drawings "
        'made in millimetres often keep; pass units="in" if it is in inches, or units="mm"'
    )
    parsed = read_plan(path, units="in")
    assert parsed.units == "in" and parsed.plan.walls[0].axis[1] == (6096.0, 0.0)
    assert read_plan(path, units="ft").plan.walls[0].axis[1] == pytest.approx((73152.0, 0.0))
    with pytest.raises(DxfPlanError, match="units must be one of auto, mm, cm, m, in, ft; got 'yd'"):
        read_plan(path, units="yd")


def test_records_are_millimetres_whatever_insunits_says_unless_units_is_given():
    path = FIXTURES / "casa_completa_N00.dxf"  # AutoCAD MCP Pro wrote $INSUNITS 6 over millimetre records
    auto = read_plan(path)
    assert auto.units == "mm"
    assert auto.units_note == "$INSUNITS 6 ignored: ACADMCP_ARCH and PLOMADA records are millimetres"
    cm = read_plan(path, units="cm")
    a, b = auto.plan.to_wire(), cm.plan.to_wire()
    assert b["walls"][0]["axis"][1] == [10 * v for v in a["walls"][0]["axis"][1]]
    assert b["walls"][0]["thickness"] == 10 * a["walls"][0]["thickness"]
    assert b["openings"][0]["offset"] == 10 * a["openings"][0]["offset"]
    assert b["rooms"][0]["area"] == pytest.approx(100 * a["rooms"][0]["area"])
    assert b["furniture"][0]["at"] == [10 * v for v in a["furniture"][0]["at"]]
    assert b["site"]["objects"][0]["at"] == [10 * v for v in a["site"]["objects"][0]["at"]]


# --- DWG ------------------------------------------------------------------------------------


def _fake_converter(calls, copy_from):
    """A subprocess.run that records its arguments and writes the converted file where each tool would."""

    def run(args, **kwargs):
        calls.append(args)
        if args[0].endswith("ODAFileConverter.exe"):
            out_dir, kind, name = args[2], args[4], args[7]
            out = f"{out_dir}/{name.rsplit('.', 1)[0]}.{kind.lower()}"
        else:
            out = args[3]
        with open(out, "wb") as fh:
            fh.write(copy_from.read_bytes())
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_a_dwg_is_read_through_the_oda_file_converter(tmp_path, monkeypatch):
    exe = tmp_path / "ODA" / "ODAFileConverter.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    dwg = tmp_path / "casa.dwg"
    dwg.write_bytes(b"AC1032 fake")
    calls: list[list[str]] = []
    monkeypatch.setenv("PLOMADA_ODA", str(exe.parent))
    monkeypatch.setattr(subprocess, "run", _fake_converter(calls, DXF))
    parsed = read_plan(dwg)
    assert len(parsed.plan.walls) == 4 and parsed.path == dwg
    ((oda, src_dir, out_dir, version, kind, recurse, audit, name),) = calls
    assert (oda, src_dir, version, kind, recurse, audit, name) == (
        str(exe),
        str(tmp_path),
        "ACAD2018",
        "DXF",
        "0",
        "1",
        "casa.dwg",
    )
    assert Path(out_dir).name.startswith(cadfile.TEMP_PREFIX) and not Path(out_dir).exists()  # removed after reading


def test_a_dwg_falls_back_to_libredwg_and_else_names_both_converters(tmp_path, monkeypatch):
    dwg = tmp_path / "casa.dwg"
    dwg.write_bytes(b"AC1032 fake")
    monkeypatch.delenv("PLOMADA_ODA", raising=False)
    monkeypatch.setattr(cadfile, "ODA_GLOB", str(tmp_path / "nada" / "*.exe"))
    monkeypatch.setattr(cadfile.shutil, "which", lambda name: f"C:/libredwg/{name}.exe")
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_converter(calls, DXF))
    assert len(read_plan(dwg).plan.openings) == 11
    assert calls[0][:3] == ["C:/libredwg/dwg2dxf.exe", "-y", "-o"] and calls[0][4] == str(dwg)
    monkeypatch.setattr(cadfile.shutil, "which", lambda name: None)
    with pytest.raises(DxfPlanError) as err:
        read_plan(dwg)
    assert str(err.value).startswith("casa.dwg is a DWG and no converter was found; install the free ODA File")
    assert "LibreDWG's dwg2dxf and dxf2dwg on PATH" in str(err.value)


def test_a_failing_converter_is_reported(tmp_path, monkeypatch):
    dwg = tmp_path / "rota.dwg"
    dwg.write_bytes(b"x")
    monkeypatch.delenv("PLOMADA_ODA", raising=False)
    monkeypatch.setattr(cadfile, "ODA_GLOB", str(tmp_path / "nada" / "*.exe"))
    monkeypatch.setattr(cadfile.shutil, "which", lambda name: "dwg2dxf")
    monkeypatch.setattr(
        subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 2, "", "ERROR: bad version\n")
    )
    with pytest.raises(DxfPlanError, match=r"converting rota.dwg to DXF failed: dwg2dxf exited with 2 \(ERROR: bad"):
        read_plan(dwg)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: subprocess.CompletedProcess(args, 0, "", ""))
    with pytest.raises(DxfPlanError, match=r"converting rota.dwg to DXF wrote nothing: rota.dxf is missing"):
        read_plan(dwg)
    with pytest.raises(DxfPlanError, match=r"plano\.pdf is not a \.dxf or \.dwg file"):
        (tmp_path / "plano.pdf").write_bytes(b"%PDF")
        read_plan(tmp_path / "plano.pdf")
