"""The 0.6 writer: the drawing style (documentation over the exact data), the layers style (the plan in the
PLOMADA-* layer standard with no XDATA, read back to the same building), drafting geometry and the preview."""

from __future__ import annotations

import math
from typing import Any

import ezdxf
import pytest

from plan05 import QUARTER, full_plan
from plomada_bridge import preview
from plomada_bridge.drafting import wall_pieces
from plomada_bridge.dxf import APP_ID, PLOMADA_APP, read_plan
from plomada_bridge.dxfwrite import build_doc, lost, write_plan
from plomada_bridge.models import Plan, arc_radius

BOX = [[0, 0], [6000, 0], [6000, 4000], [0, 4000]]

# Everything the layer standard carries, with every kind it reads: an arc wall, a ring, a partition, doors
# and windows with and without sizes (one on the arc), rooms with and without area, a straight stair, a
# furniture piece, the 0.5 kinds with their defaults and the site without payloads.
STANDARD: dict[str, Any] = {
    "walls": [
        {"id": "EXT", "axis": [[0, 0], [10000, 0, 0.3], [10000, 7000], [0, 7000]], "thickness": 200, "closed": True},
        {"id": "P1", "axis": [[6000, 0], [6000, 7000]], "thickness": 120},
        {"id": "RING", "axis": [[20000, 0, 1.0], [24000, 0, 1.0]], "thickness": 200, "closed": True},
    ],
    "openings": [
        {"id": "D1", "wall": "EXT", "opening_kind": "door", "offset": 1500, "width": 900},
        {"id": "D2", "wall": "P1", "opening_kind": "door", "offset": 1000, "width": 800, "height": 2400},
        {"id": "W1", "wall": "EXT", "opening_kind": "window", "offset": 30000, "width": 1500, "sill": 900,
         "height": 1500},
        {"id": "W2", "wall": "EXT", "opening_kind": "window", "offset": 12500, "width": 1600},
        {"id": "W3", "wall": "RING", "opening_kind": "window", "offset": 1000, "width": 1200, "sill": 0,
         "height": 2400},
    ],
    "rooms": [
        {"id": "R1", "name": "LIVING", "at": [3000, 1800], "area": 20160000},
        {"id": "R2", "name": "Dormitorio {principal}", "at": [3000, 5200]},
    ],
    "stairs": [
        {"id": "S1", "start": [7000, 1000], "direction_deg": 90, "width": 1000, "risers": 17,
         "riser_height": 2950 / 17, "going": 280}
    ],
    "furniture": [{"id": "F1", "item": "sofa_3_seat", "at": [1000, 5000], "rotation": 270}],
    "slabs": [{"id": "L1", "outline": [[0, 0], [10000, 0, 0.3], [10000, 7000], [0, 7000]]}],
    "columns": [
        {"id": "C1", "at": [8500, 5500], "diameter": 400},
        {"id": "C2", "outline": [[0, 0], [300, 0], [300, 300]]},
    ],
    "railings": [{"id": "B1", "path": [[0, 8000], [4000, 8000, QUARTER], [8000, 12000]]}],
    "curtain_walls": [{"id": "M1", "path": [[12000, 8000], [16000, 8000]]}],
    "planters": [{"id": "J1", "outline": [[0, 0], [1000, 0], [1000, 500], [0, 500]]}],
    "pools": [{"id": "P1", "outline": [[0, -5000], [4000, -5000], [4000, -3000], [0, -3000]]}],
    "site": {
        "objects": [{"id": "T1", "item": "ARBOL", "at": [15000, 2000], "rotation": 30}],
        "paving": [{"id": "PV1", "points": [[12000, 0], [18000, 0, -0.3], [18000, 5000], [12000, 5000]]}],
        "fences": [{"id": "CE1", "points": [[-2000, -2000], [10000, -2000, 0.2]]}],
    },
}  # fmt: skip


def _shape(value: Any, walls: dict[str, int] | None = None) -> Any:
    """The wire plan without ids, every opening naming its host by index: what must survive any route."""
    if isinstance(value, dict):
        out = {k: _shape(v, walls) for k, v in value.items() if k != "id"}
        if walls is not None and value.get("kind") == "opening":
            out["wall"] = walls[value["wall"]]
        return out
    if isinstance(value, list):
        return [_shape(v, walls) for v in value]
    return value


def _approx(a: Any, b: Any) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_approx(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_approx(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float | int) and isinstance(b, float | int) and not isinstance(a, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)
    return a == b


def _without_ids(plan: Plan) -> Any:
    wire = plan.to_wire()
    return _shape(wire, {w["id"]: i for i, w in enumerate(wire["walls"])})


def test_the_drawing_style_reads_back_exactly_with_the_same_records(tmp_path):
    plan = Plan.model_validate(full_plan())
    out = write_plan(plan, tmp_path / "plano.dxf", style="drawing")
    assert (out["records"], out["style"]) == (30, "drawing")
    assert {"A-WALL", "A-WALL-PATT", "A-DOOR", "A-GLAZ", "A-AREA-IDEN", "A-FLOR-STRS", "A-COLS-PATT"} <= set(
        out["layers"]
    )
    assert "A-WALL-E-N" not in out["layers"]
    parsed = read_plan(tmp_path / "plano.dxf")
    assert parsed.warnings == [] and parsed.records == 21
    assert parsed.plan.to_wire() == plan.to_wire()


def test_data_layers_are_grey_and_do_not_plot_documentation_layers_do(tmp_path):
    write_plan(Plan.model_validate(full_plan()), tmp_path / "plano.dxf", style="drawing")
    doc = ezdxf.readfile(tmp_path / "plano.dxf")
    layers = {layer.dxf.name: layer for layer in doc.layers}
    for name in ("PLOMADA-MURO-200", "PLOMADA-LOSA", "A-DOOR-E-N", "A-ANNO-ROOM-N", "A-STAIR-E-N", "SITIO"):
        assert (layers[name].dxf.plot, layers[name].color) == (0, 8), name
    assert layers["PLOMADA-MURO-200"].dxf.linetype == "CENTER"
    for name in ("A-WALL", "A-DOOR", "A-AREA-IDEN", "SITIO-CERCO"):
        assert layers[name].dxf.plot == 1, name
    assert (layers["A-WALL"].color, layers["A-WALL"].dxf.lineweight) == (7, 50)
    assert doc.styles.get("Standard").dxf.font == "arial.ttf"


def test_the_layer_standard_alone_carries_the_whole_building(tmp_path):
    plan = Plan.model_validate(STANDARD)
    path = tmp_path / "capas.dxf"
    out = write_plan(plan, path, style="layers")
    assert out["lost"] == [] and out["style"] == "layers"
    doc = ezdxf.readfile(path)
    assert not any(
        e.has_xdata(APP_ID) or e.has_xdata(PLOMADA_APP) for e in doc.entitydb.values() if hasattr(e, "has_xdata")
    )
    assert {"PLOMADA-MURO-200", "PLOMADA-MURO-120", "PLOMADA-PUERTA", "PLOMADA-PUERTA-2400", "PLOMADA-VENTANA",
            "PLOMADA-VENTANA-900-1500", "PLOMADA-VENTANA-0-2400", "PLOMADA-AMBIENTE", "PLOMADA-ESCALERA-1000",
            "PLOMADA-MUEBLE"} <= set(out["layers"])  # fmt: skip
    back = read_plan(path)
    assert back.warnings == []
    assert _approx(_without_ids(back.plan), _without_ids(plan))
    assert back.plan.rooms[1].name == "Dormitorio {principal}" and back.plan.rooms[0].area == 20_160_000.0


def test_the_layer_standard_names_what_it_cannot_carry(tmp_path):
    out = write_plan(Plan.model_validate(full_plan()), tmp_path / "capas.dxf", style="layers")
    assert out["lost"][:5] == [
        "stairs[0] S1 left out: an l stair travels only in XDATA",
        "ramps[0] R1 left out: a ramp's z_end travels only in XDATA",
        "ramps[1] R2 left out: a ramp's z_end travels only in XDATA",
        "storey N01 left out: the standard has no storey; build_from_autocad takes storey, storey_height and elevation",
        "site_on_ground left out: it travels only in XDATA",
    ]
    assert (
        'walls[1] C1: height 2600.0 reads back as nil, justification "left" reads back as "center", '
        'material "gypsum_board" reads back as "brick"' in out["lost"]
    )
    assert 'openings[2] W2: panel_width 600.0 reads back as nil, tag "V2" reads back as nil' in out["lost"]
    assert not any(line.startswith(("walls[0]", "walls[2]", "openings[0]")) for line in out["lost"])


def test_lost_compares_hosts_through_the_walls_and_sizes_through_their_defaults():
    plan = Plan.model_validate(
        {
            "walls": [{"id": "A", "axis": BOX, "thickness": 200, "closed": True}],
            "openings": [{"id": "W", "wall": "A", "opening_kind": "window", "offset": 100, "width": 900, "sill": 900}],
        }
    )
    back = Plan.model_validate(
        {
            "walls": [{"id": "1F", "axis": BOX, "thickness": 200, "closed": True}],
            "openings": [
                {
                    "id": "2A",
                    "wall": "1F",
                    "opening_kind": "window",
                    "offset": 100.0000001,
                    "width": 900,
                    "height": 1200,
                }
            ],
        }
    )
    assert lost(plan, back) == []
    back.openings[0].hand = "right"
    assert lost(plan, back) == ['openings[0] W: hand "left" reads back as "right"']


def test_records_style_faces_are_concentric_for_centred_arc_walls(tmp_path):
    """0.5 wrote both faces of a centred arc wall at r - t / 2; the outer one is r + t / 2."""
    write_plan(Plan.model_validate(full_plan()), tmp_path / "doc.dxf")
    faces = ezdxf.readfile(tmp_path / "doc.dxf").modelspace().query('LWPOLYLINE[layer=="A-WALL-E-N"]')
    arc = [f for f in faces if f.get_points("xyb")[0][2] == pytest.approx(0.25)]
    r = arc_radius(8000.0, 0.25)
    radii = sorted(arc_radius(math.dist(*f.get_points("xy")), 0.25) for f in arc)
    assert radii == pytest.approx([r - 100, r + 100])


# --- drafting -------------------------------------------------------------------------------


def test_wall_faces_are_cut_at_openings_mitred_at_corners_and_trimmed_at_tees():
    plan = Plan.model_validate(
        {
            "walls": [
                {"id": "EXT", "axis": BOX, "thickness": 200, "closed": True},
                {"id": "P", "axis": [[3000, 0], [3000, 4000]], "thickness": 100},
            ],
            "openings": [{"id": "D", "wall": "EXT", "opening_kind": "door", "offset": 1500, "width": 900}],
        }
    )
    ((left, right, closed, cap_a, cap_b),) = wall_pieces(plan.walls[0], plan.openings, plan.walls)
    assert (closed, cap_a, cap_b) == (False, True, True)
    # from the door's far jamb round the box to its near jamb, the inner face mitred at (5900, 100)...
    assert _approx(left, [[2400, 100], [5900, 100], [5900, 3900], [100, 3900], [100, 100], [1500, 100]])
    assert right[0] == pytest.approx([2400, -100]) and right[1] == pytest.approx([6100, -100])
    ((pl, _, _, pa, pb),) = wall_pieces(plan.walls[1], plan.openings, plan.walls)
    # ...and the partition stops at both faces of the wall it tees into, without caps
    assert _approx(pl, [[2950, 100], [2950, 3900]]) and (pa, pb) == (False, False)
    ((ring_l, ring_r, ring_closed, _, _),) = wall_pieces(plan.walls[0], [], plan.walls)
    assert ring_closed and len(ring_l) == len(ring_r) == 4


def _doors(tmp_path, swing, hand):
    plan = Plan.model_validate(
        {
            "walls": [{"id": "EXT", "axis": BOX, "thickness": 200, "closed": True}],
            "openings": [
                {"id": "D", "wall": "EXT", "opening_kind": "door", "offset": 1500, "width": 900, "swing": swing,
                 "hand": hand}
            ],
        }
    )  # fmt: skip
    doc, *_ = build_doc(plan, "drawing")
    msp = doc.modelspace()
    (leaf,) = msp.query('LINE[layer=="A-DOOR"]')
    (swing_arc,) = msp.query('ARC[layer=="A-DOOR"]')
    return leaf, swing_arc


@pytest.mark.parametrize(
    ("swing", "hand", "hinge", "tip"),
    [
        ("in", "left", (2400, 100), (2400, 1000)),
        ("in", "right", (1500, 100), (1500, 1000)),
        ("out", "left", (1500, -100), (1500, -1000)),
        ("out", "right", (2400, -100), (2400, -1000)),
    ],
)
def test_doors_hinge_and_swing_as_the_extension_builds_them(tmp_path, swing, hand, hinge, tip):
    leaf, arc = _doors(tmp_path, swing, hand)
    assert tuple(leaf.dxf.start)[:2] == pytest.approx(hinge) and tuple(leaf.dxf.end)[:2] == pytest.approx(tip)
    assert tuple(arc.dxf.center)[:2] == pytest.approx(hinge) and arc.dxf.radius == pytest.approx(900)
    assert (arc.dxf.end_angle - arc.dxf.start_angle) % 360 == pytest.approx(90)


def test_windows_rooms_stairs_and_columns_are_drawn(tmp_path):
    plan = Plan.model_validate(STANDARD)
    msp = build_doc(plan, "drawing")[0].modelspace()
    texts = sorted(t.dxf.text for t in msp.query('TEXT[layer=="A-AREA-IDEN"]'))
    assert texts == ["20.16 m²", "Dormitorio {principal}", "LIVING"]
    assert len(msp.query('LINE[layer=="A-FLOR-STRS"]')) == 17 - 2  # a nosing between every two risers
    assert any(t.dxf.text == "SUBE" for t in msp.query('TEXT[layer=="A-FLOR-STRS"]'))
    assert len(msp.query('HATCH[layer=="A-COLS-PATT"]')) == 2 and len(msp.query('CIRCLE[layer=="A-COLS"]')) == 1
    glaz = msp.query('LWPOLYLINE[layer=="A-GLAZ"]')
    assert len(glaz) == 3 * 3  # three lines per window
    ring = [g.get_points("xyb") for g in glaz if g.get_points("xy")[0][0] > 19000]
    assert len(ring) == 3 and all(p[0][2] > 0 for p in ring)  # on the ring they are arcs


# --- preview --------------------------------------------------------------------------------


def test_preview_is_a_png_under_350_kb_and_shrinks_when_it_must(monkeypatch):
    doc = build_doc(Plan.model_validate(STANDARD), "drawing")[0]
    png, w, h = preview.render(doc, 1200)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) <= preview.MAX_BYTES
    assert 1190 <= w <= 1210 and h < w
    monkeypatch.setattr(preview, "MAX_BYTES", 20_000)
    small, w2, _ = preview.render(doc, 1200)
    assert len(small) < len(png) and w2 < w


def test_preview_leaves_out_what_does_not_plot():
    doc = ezdxf.new()
    doc.layers.add("DATOS").dxf.plot = 0
    doc.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "DATOS"})
    with pytest.raises(preview.PreviewError, match="the drawing has nothing to plot"):
        preview.render(doc)
