"""0.5 records in pydantic: bulged vertices, the new kinds, their defaults and refusals in the extension's words."""

from __future__ import annotations

import math

import pytest

from plan05 import QUARTER, full_plan
from plomada_bridge.models import parse_plan, polyline_length, ring_area

WALL = {"id": "W", "axis": [[0, 0], [4000, 0]], "thickness": 200}


def refuse(raw) -> str:
    with pytest.raises(ValueError) as info:
        parse_plan(raw)
    return str(info.value)


def with_walls(**lists):
    return {"walls": [dict(WALL)], **lists}


def test_bulged_vertices_are_kept_and_zero_bulges_dropped():
    wire = parse_plan(
        {"walls": [{"id": "A", "axis": [[0, 0, 0], [1000, 0, 0.5], [2000, 0, 0.3]], "thickness": 200}]}
    ).to_wire()
    # [x, y, 0] becomes [x, y]; the last vertex of an open wall has no segment, so its bulge goes too
    assert wire["walls"][0]["axis"] == [[0.0, 0.0], [1000.0, 0.0, 0.5], [2000.0, 0.0]]


def test_every_new_kind_validates_with_the_extension_defaults():
    wire = parse_plan(full_plan()).to_wire()
    assert [k for k in wire if k not in ("walls", "openings", "rooms", "stairs", "furniture", "site")] == [
        "slabs",
        "columns",
        "curtain_walls",
        "railings",
        "planters",
        "pools",
        "ramps",
        "site_on_ground",
        "storey",
    ]
    l1, l2 = wire["slabs"]
    assert l1["kind"] == "slab" and l1["v"] == 1 and l1["outline"][1] == [8000.0, 0.0, QUARTER]
    assert l1["holes"][1] == [[3000.0, 1000.0, 1.0], [4000.0, 1000.0, 1.0]] and l1["edge_band"] == {
        "height": 450.0,
        "depth": 150.0,
    }
    assert (l2["thickness"], l2["top_offset"], l2["material"], l2["holes"]) == (150.0, 0.0, "MAT_piso_porcelanato", [])
    b2 = wire["railings"][1]
    assert (b2["style"], b2["inset"], b2["closed"], b2["rail"]) == ("glass", 50.0, True, True)
    j1, j2 = wire["planters"]
    assert (j1["soil_level"], j1["plants"]) == (400.0, {"keys": ["hedge", "ARBUSTO"], "spacing": 500.0, "seed": 7})
    assert j2["soil_level"] == 300.0 and "plants" not in j2  # min(400, height)
    c1, c2 = wire["columns"]
    assert (c1["at"], c1["diameter"], c1["material"]) == ([4000.0, 2500.0], 600.0, "MAT_hormigon")
    assert "at" not in c2 and c2["material"] == "MAT_acero"
    (m1,) = wire["curtain_walls"]
    assert m1["path"] == [[0.0, 0.0], [6000.0, 0.0]] and m1["transoms"] == [1100.0, 2100.0]
    p2 = wire["pools"][1]
    assert (p2["depth"], p2["coping"], p2["raised"]) == (1400.0, 0.0, "auto")
    assert wire["ramps"][0]["side_walls"] is True and wire["ramps"][1]["wall_height"] == 900.0
    assert wire["storey"] == {"name": "N01", "height": 3000.0, "elevation_mode": "explicit", "elevation": 3100.0}
    d1, w1, w2 = wire["openings"]
    assert "head" not in d1 and "panel_width" not in d1 and d1["sill"] is None
    assert (w1["head"], w1["arch_rise"], w2["panel_width"]) == ("arch", 400.0, 600.0)


def test_a_bulge_above_a_semicircle_is_refused_naming_the_segment():
    b200 = math.tan(math.radians(200) / 4)
    assert refuse({"walls": [{**WALL, "axis": [[0, 0], [4000, 0, b200], [4000, 3000]]}]}) == (
        "segment 1 of wall W sweeps 200 degrees; split arcs above 180 degrees"
    )
    raw = full_plan()
    raw["slabs"][0]["holes"][1][0][2] = 1.5
    assert refuse(raw) == "segment 0 of slab L1 hole 1 sweeps 225.24 degrees; split arcs above 180 degrees"
    raw = full_plan()
    raw["site"]["paving"][0]["points"][1][2] = -2.0
    assert refuse(raw) == "segment 1 of site.paving[0] PV1 sweeps 253.74 degrees; split arcs above 180 degrees"
    # an open polyline's last vertex starts no segment: its bulge is ignored, not refused
    parse_plan({"walls": [{**WALL, "axis": [[0, 0], [4000, 0, 5.0]]}]})


def test_a_ramp_of_21_percent_is_refused_and_18_percent_accepted():
    steep = {"id": "R9", "axis": [[0, 0], [10000, 0]], "width": 1200, "z_end": 2100}
    assert refuse(with_walls(ramps=[steep])) == (
        "ramp R9 climbs 2100 mm over 10000 mm, a slope of 21 percent; the most is 20 percent"
    )
    assert parse_plan(with_walls(ramps=[{**steep, "z_end": -1800}])).ramps[0].z_end == -1800.0
    # the run of an arc is its length, r |theta|: a semicircle over a 2000 mm chord runs 3141.6 mm
    arc = {"id": "R8", "axis": [[0, 0, 1.0], [2000, 0]], "width": 1200, "z_end": 600}
    assert parse_plan(with_walls(ramps=[arc])).ramps[0].axis[0] == (0.0, 0.0, 1.0)
    assert polyline_length([[0, 0, 1.0], [2000, 0]]) == pytest.approx(1000 * math.pi)


def test_a_closed_wall_of_two_arcs_is_a_ring():
    ring = parse_plan({"walls": [{**WALL, "axis": [[0, 0, 1.0], [4000, 0, 1.0]], "closed": True}]})
    assert ring_area(ring.walls[0].axis) == pytest.approx(math.pi * 2000**2)
    assert refuse({"walls": [{**WALL, "closed": True}]}) == (
        "walls[0].axis must hold at least 3 points for a closed wall, got 2"
    )


def test_new_record_refusals_name_the_field_like_the_extension():
    raw = full_plan()
    raw["openings"][1]["arch_rise"] = 800
    assert refuse(raw) == "openings[1].arch_rise must be at most half the width (750), got 800"
    raw = full_plan()
    raw["openings"][1].update(width=3000, arch_rise=1300)
    assert refuse(raw) == "openings[1].arch_rise 1300 is taller than the opening (1200)"
    raw = full_plan()
    raw["railings"][0]["on"] = "L9"
    assert refuse(raw) == 'railings[0].on names slab "L9", which is not in the plan; slabs are L1, L2'
    raw = full_plan()
    raw["planters"][1]["soil_level"] = 350
    assert refuse(raw) == "planters[1].soil_level 350 is above the curb height 300"
    raw = full_plan()
    raw["planters"][0]["plants"]["keys"] = []
    assert refuse(raw) == "planters[0].plants.keys must be a list of at least one library key, got []"
    raw = full_plan()
    raw["columns"][1]["diameter"] = 300
    assert refuse(raw) == "columns[1] takes either at and diameter (round) or outline, not both"
    raw = full_plan()
    raw["pools"][0]["raised"] = "maybe"
    assert refuse(raw) == 'pools[0].raised must be auto, true or false, got "maybe"'
    raw = full_plan()
    raw["slabs"][1]["top_offset"] = 2500
    assert refuse(raw) == "slabs[1].top_offset must be within +-2000 mm, got 2500"
    raw = full_plan()
    raw["slabs"][1]["outline"] = [[0, 0], [1000, 0], [2000, 0]]
    assert refuse(raw) == "slabs[1].outline encloses no area"
    raw = full_plan()
    raw["curtain_walls"][0]["head"] = 50
    assert refuse(raw) == "curtain_walls[0].head 50 must be above the sill 100"


def test_storey_elevation_is_explicit_and_bounded():
    assert refuse(with_walls(storey={"elevation_mode": "explicit"})) == (
        "storey.elevation is required when storey.elevation_mode is explicit"
    )
    assert refuse(with_walls(storey={"elevation": 70000})) == (
        "storey.elevation must be between -10000 and 60000 mm, got 70000"
    )
    basement = parse_plan(with_walls(storey={"name": "SS", "elevation": -3000})).to_wire()["storey"]
    assert basement == {"name": "SS", "height": 2800.0, "elevation_mode": "explicit", "elevation": -3000.0}
    assert parse_plan(with_walls(storey={"name": "N01"})).to_wire()["storey"] == {"name": "N01", "height": 2800.0}
