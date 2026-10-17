"""The 0.5 DXF adapter: PLOMADA XDATA records, PLOMADA-* layer fallbacks, flattened curves,
0.4 wire identity, and write_plan's exact round trip."""

from __future__ import annotations

import json
import math

import ezdxf
import pytest

from conftest import FIXTURES
from plan05 import QUARTER, full_plan
from plomada_bridge.dxf import PLOMADA_APP, DxfPlanError, read_plan
from plomada_bridge.dxfwrite import write_plan
from plomada_bridge.models import Plan

SNAPSHOT = FIXTURES / "wire_0.4_snapshot.json"  # to_wire() of the 0.4 fixtures, taken before 0.5


def _tag(entity, payload):
    text = json.dumps({"v": 1, **payload})
    entity.set_xdata(PLOMADA_APP, [(1000, text[i : i + 255]) for i in range(0, len(text), 255)])
    return entity


def _new_doc():
    doc = ezdxf.new("R2013")
    doc.appids.new(PLOMADA_APP)
    return doc, doc.modelspace()


def test_the_0_4_fixtures_serialise_byte_identically():
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert len(snapshot) == 5
    for name, text in snapshot.items():
        wire = read_plan(FIXTURES / name, storey_height=2800.0).plan.to_wire()
        assert json.dumps(wire, ensure_ascii=False) == text, name


def test_plomada_xdata_records_take_their_geometry_from_the_entity(tmp_path):
    doc, msp = _new_doc()
    wall = msp.add_lwpolyline([(0, 0, 0, 0, QUARTER), (4000, 4000, 0, 0, 0)], format="xyseb")
    _tag(wall, {"kind": "wall", "id": "CURVA", "thickness": 200, "material": "concrete"})
    hole = msp.add_lwpolyline([(1000, 1000), (2000, 1000), (2000, 2000)], close=True)
    slab = msp.add_lwpolyline([(0, 0), (6000, 0, 0, 0, 0.5), (6000, 4000), (0, 4000)], format="xyseb", close=True)
    _tag(slab, {"kind": "slab", "id": "L1", "holes": [hole.dxf.handle], "edge_band": {"height": 400, "depth": 120}})
    _tag(msp.add_circle((3000, 2000), 300), {"kind": "column", "id": "C1", "height": 2700})
    path = tmp_path / "plomada.dxf"
    doc.saveas(path)
    parsed = read_plan(path)
    wire = parsed.plan.to_wire()
    assert parsed.records == 3 and wire["walls"][0]["axis"] == [[0.0, 0.0, QUARTER], [4000.0, 4000.0]]
    assert wire["walls"][0]["material"] == "concrete" and wire["walls"][0]["closed"] is False
    (l1,) = wire["slabs"]
    assert l1["outline"][1] == [6000.0, 0.0, 0.5] and l1["holes"] == [
        [[1000.0, 1000.0], [2000.0, 1000.0], [2000.0, 2000.0]]
    ]
    assert l1["edge_band"] == {"height": 400.0, "depth": 120.0}
    assert wire["columns"] == [
        {
            "v": 1,
            "kind": "column",
            "id": "C1",
            "at": [3000.0, 2000.0],
            "diameter": 600.0,
            "height": 2700.0,
            "material": "MAT_hormigon",
        }
    ]


def test_plomada_layers_without_xdata_give_records_with_defaults(tmp_path):
    doc, msp = _new_doc()
    wall = msp.add_lwpolyline(
        [(0, 0), (5000, 0), (5000, 3000), (0, 3000)], close=True, dxfattribs={"layer": "PLOMADA-MURO-150"}
    )
    slab = msp.add_lwpolyline([(0, 0), (5000, 0), (5000, 3000)], close=True, dxfattribs={"layer": "PLOMADA-LOSA"})
    msp.add_lwpolyline(
        [(0, 0), (2000, 0, 0, 0, 0.3), (3000, 1000)], format="xyseb", dxfattribs={"layer": "PLOMADA-BARANDA"}
    )
    col = msp.add_circle((1000, 1000), 200, dxfattribs={"layer": "PLOMADA-COLUMNA"})
    ramp = msp.add_lwpolyline([(0, 0), (6000, 0)], dxfattribs={"layer": "PLOMADA-RAMPA"})
    path = tmp_path / "capas.dxf"
    doc.saveas(path)
    parsed = read_plan(path)
    wire = parsed.plan.to_wire()
    (w,) = wire["walls"]
    assert (w["id"], w["thickness"], w["justification"], w["closed"]) == (wall.dxf.handle, 150.0, "center", True)
    assert wire["slabs"][0]["id"] == slab.dxf.handle and wire["slabs"][0]["thickness"] == 150.0
    assert wire["railings"][0]["path"][1] == [2000.0, 0.0, 0.3] and wire["railings"][0]["style"] == "glass"
    assert wire["columns"][0]["id"] == col.dxf.handle and wire["columns"][0]["diameter"] == 400.0
    assert "ramps" not in wire
    assert parsed.warnings == [f"ramp {ramp.dxf.handle} on PLOMADA-RAMPA skipped: its z_end travels only in XDATA"]


def test_splines_and_ellipses_are_flattened_within_5_mm(tmp_path):
    doc, msp = _new_doc()
    msp.add_lwpolyline([(0, 0), (9000, 0), (9000, 9000)], close=True, dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_ellipse((0, 0), major_axis=(2000, 0), ratio=1.0, dxfattribs={"layer": "PLOMADA-LOSA"})
    msp.add_spline([(0, 0), (1000, 800), (2000, 0), (3000, 800)], dxfattribs={"layer": "PLOMADA-BARANDA"})
    path = tmp_path / "curvas.dxf"
    doc.saveas(path)
    wire = read_plan(path).plan.to_wire()
    ring = wire["slabs"][0]["outline"]
    assert len(ring) > 40 and all(len(v) == 2 for v in ring)
    assert all(math.hypot(*v) == pytest.approx(2000.0) for v in ring)
    # every chord stays within 5 mm of the circle it replaces (and the ring is not closed twice)
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(ring, ring[1:] + ring[:1], strict=True)]
    assert min(math.hypot(*m) for m in mids) >= 2000.0 - 5.0
    rail = wire["railings"][0]
    assert rail["closed"] is False and len(rail["path"]) > 10
    assert rail["path"][0] == pytest.approx([0.0, 0.0]) and rail["path"][-1] == pytest.approx([3000.0, 800.0])


def test_site_polylines_keep_their_bulges(tmp_path):
    doc, msp = _new_doc()
    msp.add_lwpolyline([(0, 0), (9000, 0), (9000, 9000)], close=True, dxfattribs={"layer": "PLOMADA-MURO-200"})
    msp.add_lwpolyline(
        [(0, 0, 0, 0, 0), (4000, 0, 0, 0, 0.5), (4000, 3000, 0, 0, 0), (0, 3000, 0, 0, 0)],
        format="xyseb",
        close=True,
        dxfattribs={"layer": "SITIO-PILETA"},
    )
    path = tmp_path / "sitio.dxf"
    doc.saveas(path)
    (pool,) = read_plan(path).plan.to_wire()["site"]["pools"]
    assert pool["points"] == [[0.0, 0.0], [4000.0, 0.0, 0.5], [4000.0, 3000.0], [0.0, 3000.0]]


def test_a_slab_naming_a_missing_hole_is_refused(tmp_path):
    doc, msp = _new_doc()
    msp.add_lwpolyline([(0, 0), (9000, 0), (9000, 9000)], close=True, dxfattribs={"layer": "PLOMADA-MURO-200"})
    _tag(
        msp.add_lwpolyline([(0, 0), (5000, 0), (5000, 3000)], close=True),
        {"kind": "slab", "id": "L1", "holes": ["FFFF"]},
    )
    path = tmp_path / "hueco.dxf"
    doc.saveas(path)
    with pytest.raises(DxfPlanError, match="slab L1 names hole 'FFFF', which is not a polyline in the drawing"):
        read_plan(path)


def test_write_plan_round_trip_is_exact(tmp_path):
    plan = Plan.model_validate(full_plan())
    path = tmp_path / "salida" / "completo.dxf"
    out = write_plan(plan, path)
    assert out["path"] == str(path) and out["records"] == 30
    assert {"PLOMADA-MURO-200", "PLOMADA-MURO-150", "A-WALL-E-N", "PLOMADA-LOSA-HUECO", "SITIO-CERCO"} <= set(
        out["layers"]
    )
    parsed = read_plan(path)
    assert parsed.warnings == [] and parsed.records == 21
    assert parsed.plan.to_wire() == plan.to_wire()


def test_written_dxf_is_millimetres_with_faces_and_visible_rooms(tmp_path):
    plan = Plan.model_validate(full_plan())
    path = tmp_path / "doc.dxf"
    write_plan(plan, path)
    doc = ezdxf.readfile(path)
    assert doc.header["$INSUNITS"] == 4
    msp = doc.modelspace()
    (room,) = msp.query('TEXT[layer=="A-ANNO-ROOM-N"]')
    assert room.dxf.text == "LIVING"
    faces = msp.query('LWPOLYLINE[layer=="A-WALL-E-N"]')
    assert len(faces) == 2 * (4 + 1 + 2)  # two faces per segment: EXT 4, C1 1, RING 2
    # C1 is a left-justified quarter arc (radius 4000 / sqrt 2): its faces are the axis itself and the
    # concentric arc 150 mm toward the centre, with the same bulge
    c1 = [f for f in faces if f.get_points("xyb")[0][2] == pytest.approx(QUARTER)]
    radii = sorted(math.dist(f.get_points("xy")[0], f.get_points("xy")[1]) / math.sqrt(2) for f in c1)
    assert radii == pytest.approx([4000 / math.sqrt(2) - 150, 4000 / math.sqrt(2)])
