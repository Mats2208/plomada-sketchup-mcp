"""plan_to_revit and the revit_batches tool: a plan compiled into Horizun Revit MCP calls, without Revit."""

from __future__ import annotations

import itertools
import json
import math
from typing import Any

import pytest

from conftest import FIXTURES, FakeExtension
from plan05 import QUARTER, full_plan
from plomada_bridge.dxf import read_plan
from plomada_bridge.models import Plan
from plomada_bridge.revit_plan import (
    ARC_MAX_SAGITTA_MM,
    RevitStorey,
    arc_mid,
    plan_to_revit,
    segments_of,
    stair_footprint,
    stair_runs,
)
from test_tools import _session, _text

# The fields Horizun 3.0.6 accepts per create_elements kind (Horizun.Contracts ToolInputRules.CreationFields) and
# the ones it requires (AddCreationVariants); any other field is refused as "not applicable".
ALLOWED = {
    "level": {"elevation", "name"},
    "wall": {
        "start",
        "end",
        "level_id",
        "type_id",
        "height",
        "offset",
        "base_offset",
        "top_level_id",
        "top_offset",
        "flip",
        "structural",
        "arc",
        "join_rule",
    },
    "floor": {"profile", "level_id", "type_id", "offset", "structural"},
    "roof": {"profile", "level_id", "type_id", "offset", "slope_degrees", "slope_ratio", "edge_slopes"},
    "room": {"point", "level_id", "name", "number", "placement", "phase_id", "min_area_m2"},
    "family_instance": {
        "point",
        "type_id",
        "level_id",
        "coordinate_mode",
        "structural_type",
        "host_id",
        "rotation_degrees",
        "flip",
        "face_allowance_mm",
        "facing_degrees",
        "side_dead_band_mm",
        "host_face",
        "top_level_id",
        "top_offset",
        "height",
    },
    "structural_column": {
        "point",
        "type_id",
        "level_id",
        "coordinate_mode",
        "rotation_degrees",
        "top_level_id",
        "top_offset",
        "height",
    },
    "stairs": {"level_id", "top_level_id", "type_id", "desired_risers", "tread_depth", "runs", "landings"},
}
REQUIRED = {
    "level": {"elevation"},
    "wall": {"start", "end", "level_id", "height"},
    "floor": {"profile", "level_id"},
    "roof": {"profile", "level_id"},
    "room": {"level_id"},
    "family_instance": {"point", "type_id", "coordinate_mode"},
    "structural_column": {"point", "type_id", "level_id", "coordinate_mode"},
    "stairs": {"level_id", "top_level_id", "type_id", "desired_risers", "tread_depth", "runs"},
}
STAGES = [
    "document",
    "discover",
    "levels",
    "types",
    "walls",
    "opening_types",
    "openings",
    "floors",
    "roofs",
    "rooms",
    "stairs",
    "railings_columns_curtain",
    "verify",
    "exports",
]


def two_storeys() -> list[Plan]:
    return [read_plan(FIXTURES / f"casa_2_plantas_{n}.dxf").plan for n in ("N00", "N01")]


def stage(out: dict[str, Any], sid: str) -> dict[str, Any]:
    return next(s for s in out["stages"] if s["id"] == sid)


def rows(out: dict[str, Any], sid: str) -> list[dict[str, Any]]:
    return [
        e for c in stage(out, sid)["calls"] if c["tool"] == "horizun_create_elements" for e in c["args"]["elements"]
    ]


def bound(out: dict[str, Any], sid: str) -> dict[str, dict[str, Any]]:
    """ref -> the element row that binds it, over the create_elements calls of a stage."""
    found = {}
    for c in stage(out, sid)["calls"]:
        if c["tool"] == "horizun_create_elements":
            for ref, where in c.get("binds", {}).items():
                found[ref] = c["args"]["elements"][int(where[5:-1])]
    return found


def refs_in(value: Any) -> list[str]:
    if isinstance(value, dict):
        if "$ref" in value:
            return [value["$ref"]]
        return [r for v in value.values() for r in refs_in(v)]
    if isinstance(value, list):
        return [r for v in value for r in refs_in(v)]
    return []


def check_contract(out: dict[str, Any]) -> None:
    """Every ref is bound by an earlier call, every create_elements row fits its kind, a stair rides alone."""
    known: set[str] = set()
    for st in out["stages"]:
        for call in st["calls"]:
            for ref in refs_in(call["args"]):
                assert ref in known, f"{st['id']} {call['tool']} uses {ref} before any call binds it"
            if call["tool"] == "horizun_create_elements":
                assert call["args"]["units"] == "mm"
                elements = call["args"]["elements"]
                for e in elements:
                    kind = e["kind"]
                    assert set(e) - {"kind", "parameters"} <= ALLOWED[kind], (kind, set(e) - ALLOWED[kind])
                    assert REQUIRED[kind] <= set(e), (kind, REQUIRED[kind] - set(e))
                    if kind == "stairs":
                        assert len(elements) == 1
                assert sorted(int(w[5:-1]) for w in call.get("binds", {}).values()) == sorted(
                    set(int(w[5:-1]) for w in call.get("binds", {}).values())
                )
            known.update(call.get("binds", {}))
    json.dumps(out)


# --- two storeys from the DXF fixtures -------------------------------------------------------


def test_two_storeys_compile_in_stage_order_with_levels_and_refs():
    out = plan_to_revit(two_storeys(), rvt_path=r"C:\obra\casa.rvt", export_dir=r"C:\obra\salida")
    check_contract(out)
    ids = [s["id"] for s in out["stages"]]
    assert ids == [s for s in STAGES if s in ids]
    assert ids[:5] == ["document", "discover", "levels", "types", "walls"] and ids[-2:] == ["verify", "exports"]
    assert out["horizun_version"] == "3.0.6" and out["units"] == "mm"
    assert out["template"] == r"C:\ProgramData\Autodesk\RVT 2027\Templates\Default_M_ESP.rte"
    doc = stage(out, "document")["calls"][1]["args"]
    assert doc == {"operation": "new_project", "template_path": out["template"], "save_as_path": r"C:\obra\casa.rvt"}
    levels = {e["name"]: e["elevation"] for e in rows(out, "levels")}
    assert levels == {"N00": 0.0, "N01": 2800.0, "TECHO": 5600.0}
    assert [s["name"] for s in out["storeys"]] == ["N00", "N01"]
    walls = bound(out, "walls")
    assert walls["wall:N00:EXT#0"]["level_id"] == {"$ref": "level:N00"}
    assert walls["wall:N01:EXT#0"]["start"][2] == 2800.0
    assert out["expected"]["walls"] == len(rows(out, "walls"))
    # rvt_path and export_dir given: only the optional family files are left to the caller
    assert set(out["inputs"]) == {"door_rfa", "window_rfa"} and all(i["optional"] for i in out["inputs"].values())
    loads = [c for c in stage(out, "opening_types")["calls"] if c["tool"] == "horizun_create_family"]
    assert [c["when"] for c in loads] == [
        f"the discover stage found no {k} family in the template" for k in ("door", "window")
    ]
    given = plan_to_revit(two_storeys(), family_paths={"door": r"C:\fam\puerta.rfa"})
    assert stage(given, "opening_types")["calls"][0]["args"]["source_path"] == r"C:\fam\puerta.rfa"
    dwg = next(c for c in stage(out, "exports")["calls"] if c["args"].get("format") == "dwg")
    assert dwg["args"]["output_path"] == r"C:\obra\salida\plomada.dwg"
    assert dwg["args"]["view_ids"] == [{"$ref": "view:plan:N00"}, {"$ref": "view:plan:N01"}]


def test_the_upper_floor_gets_the_stair_well_and_the_stair_rides_alone():
    out = plan_to_revit(two_storeys())
    floors = bound(out, "floors")
    ground, upper = floors["floor:N00:losa"], floors["floor:N01:losa"]
    assert len(ground["profile"]) == 1 and len(upper["profile"]) == 2  # perimeter + the stair well
    assert {p[2] for p in upper["profile"][1]} == {2800.0}
    assert ground["profile"][0][0] == [-100.0, -100.0, 0.0]  # the outer face of the 200 mm exterior wall
    (stair_call,) = [c for c in stage(out, "stairs")["calls"] if c["tool"] == "horizun_create_elements"]
    (stair,) = stair_call["args"]["elements"]
    assert stair["top_level_id"] == {"$ref": "level:N01"} and stair["desired_risers"] == 17
    assert [r["expected_risers"] for r in stair["runs"]] == [9, 8] and len(stair["landings"]) == 1
    assert any("2949.5" in w for w in out["warnings"])  # 17 x 173.5 against a 2800 mm storey
    railing = next(c for c in stage(out, "stairs")["calls"] if c["tool"] == "horizun_create_railing")
    assert railing["args"]["host_id"] == {"$ref": "stair:N00:S1"}
    assert {"rvt_path", "export_dir"} <= set(out["inputs"])
    assert stage(out, "document")["calls"][1]["args"]["save_as_path"] == {"$input": "rvt_path"}


def test_storeys_take_explicit_names_heights_and_elevations():
    a, b = two_storeys()
    out = plan_to_revit([RevitStorey(a, "PB", 3000.0), {"plan": b.model_dump(), "name": "P1", "elevation": 3200.0}])
    assert [(s["name"], s["elevation"], s["height"]) for s in out["storeys"]] == [
        ("PB", 0.0, 3000.0),
        ("P1", 3200.0, 2800.0),
    ]
    assert {e["name"] for e in rows(out, "levels")} == {"PB", "P1", "TECHO"}
    with pytest.raises(ValueError, match="appears twice"):
        plan_to_revit([RevitStorey(a, "N00"), RevitStorey(b, "N00")])


# --- curves, justification and openings ------------------------------------------------------


def _full() -> Plan:
    return Plan.model_validate(full_plan())


def test_an_arc_becomes_one_curved_wall_through_the_bulge_midpoint():
    out = plan_to_revit(_full(), roof="hip")
    check_contract(out)
    walls = bound(out, "walls")
    arc = walls["wall:N01:EXT#1"]  # [12000, 0, 0.25] -> [12000, 8000]
    (cx, cy, cz), r = arc["arc"]["centre"], arc["arc"]["radius"]
    assert arc["arc"]["clockwise"] is False and cz == arc["start"][2]
    mid = arc_mid((12000.0, 0.0), (12000.0, 8000.0), 0.25)
    for p in (arc["start"][:2], arc["end"][:2], mid):
        assert math.dist(p, (cx, cy)) == pytest.approx(r, abs=0.01)
    ring = [walls[f"wall:N01:RING#{i}"] for i in (0, 1)]  # two semicircles
    assert [w["arc"]["radius"] for w in ring] == [2000.0, 2000.0] and ring[0]["end"] == ring[1]["start"]


def test_a_left_justified_arc_wall_moves_its_axis_to_the_centreline():
    out = plan_to_revit(_full())
    c1 = bound(out, "walls")["wall:N01:C1"]  # quarter circle, 150 mm, justification left
    axis = segments_of([[2000, 2000, QUARTER], [6000, 2000]], False)[0]
    assert c1["arc"]["centre"][:2] == pytest.approx(axis.centre)
    assert c1["arc"]["radius"] == pytest.approx(axis.radius - 75.0, abs=0.001)  # left of a CCW arc: inwards
    assert "flip" not in c1  # an interior wall keeps Revit's default side


def test_a_justified_open_wall_is_mitred_at_its_corner():
    plan = Plan.model_validate(
        {
            "walls": [
                {"id": "L", "axis": [[0, 0], [4000, 0], [4000, 3000]], "thickness": 200, "justification": "right"},
            ]
        }
    )
    w = bound(plan_to_revit(plan, roof="none"), "walls")
    first, second = w["wall:N00:L#0"], w["wall:N00:L#1"]
    assert first["start"][:2] == [0.0, -100.0] and second["end"][:2] == [4100.0, 3000.0]
    assert first["end"] == second["start"] == [4100.0, -100.0, 0.0]


def test_openings_are_hosted_on_their_segment_facing_out_with_sill_and_mark():
    plan = Plan.model_validate(
        {
            "walls": [
                {"id": "EXT", "axis": [[0, 0], [6000, 0], [6000, 4000], [0, 4000]], "thickness": 200, "closed": True},
            ],
            "openings": [
                {
                    "id": "D1",
                    "wall": "EXT",
                    "opening_kind": "door",
                    "offset": 1000,
                    "width": 900,
                    "hand": "right",
                    "tag": "P1",
                },
                {"id": "W1", "wall": "EXT", "opening_kind": "window", "offset": 7000, "width": 1200, "sill": 1000},
                {"id": "D2", "wall": "EXT", "opening_kind": "door", "offset": 3000, "width": 800, "swing": "out"},
            ],
        }
    )
    out = plan_to_revit(plan)
    check_contract(out)
    o = bound(out, "openings")
    door, window, out_door = o["opening:N00:D1"], o["opening:N00:W1"], o["opening:N00:D2"]
    assert door["host_id"] == {"$ref": "wall:N00:EXT#0"} and door["point"] == [1450.0, 0.0, 0.0]
    assert door["facing_degrees"] == 270.0  # swings in (to +y, inside the CCW ring): faces -y
    assert door["flip"] is True and door["parameters"] == {"ALL_MODEL_MARK": "P1"}
    assert out_door["facing_degrees"] == 90.0 and "flip" not in out_door
    assert window["host_id"] == {"$ref": "wall:N00:EXT#1"} and window["point"] == [6000.0, 1600.0, 1000.0]
    assert window["facing_degrees"] == 0.0  # the outside of the east wall
    assert window["parameters"] == {"INSTANCE_SILL_HEIGHT_PARAM": "1000 mm"}
    assert window["coordinate_mode"] == "level_offset" and window["type_id"] == {"$ref": "type:window:1200x1200"}
    types = stage(out, "opening_types")["calls"][-1]["args"]["actions"]
    assert {a["new_name"] for a in types} == {
        "Plomada puerta 900x2100",
        "Plomada puerta 800x2100",
        "Plomada ventana 1200x1200",
    }
    assert {"DOOR_WIDTH": "900 mm", "DOOR_HEIGHT": "2100 mm"} in [a["values"] for a in types]
    assert all(w.get("flip") is True for w in rows(out, "walls"))  # CCW ring: exterior on the right


def test_an_opening_past_its_wall_is_dropped_with_a_warning():
    plan = Plan.model_validate(
        {
            "walls": [{"id": "A", "axis": [[0, 0], [3000, 0]], "thickness": 150}],
            "openings": [{"id": "W9", "wall": "A", "opening_kind": "window", "offset": 2800, "width": 1000}],
        }
    )
    out = plan_to_revit(plan, roof="none")
    assert "openings" not in [s["id"] for s in out["stages"]]
    assert any("W9" in w and "not placed" in w for w in out["warnings"])


def test_slab_and_roof_arcs_are_tessellated_within_the_sagitta():
    out = plan_to_revit(_full(), roof="hip", overhang=500)
    roofs = rows(out, "roofs")  # one per building: the house and the round pavilion
    assert len(roofs) == 2
    roof = roofs[0]
    ring = roof["profile"][0]
    assert len(ring) > 8 and roof["slope_degrees"] == 30.0 and {p[2] for p in ring} == {6100.0}
    # the arc edge bulges 0.25 past x 12000: its outer face plus overhang reaches past x 12600
    assert max(p[0] for p in ring) > 12600.0
    slab = bound(out, "floors")["floor:N01:L1"]
    assert len(slab["profile"]) == 3 and {p[2] for p in slab["profile"][0]} == {3050.0}  # top_offset -50
    assert slab["type_id"] == {"$ref": "type:floor:180"}
    pts = [tuple(p[:2]) for p in slab["profile"][0]]
    assert len(pts) > 4
    # every chord of the quarter arc (8000, 0) -> (12000, 4000), centre (8000, 4000), stays within the sagitta
    on_arc = [p for p in pts if p[0] >= 8000.0 and p[1] <= 4000.0]
    for p, q in itertools.pairwise(on_arc):
        mid = ((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0)
        assert 4000.0 - math.dist(mid, (8000.0, 4000.0)) <= ARC_MAX_SAGITTA_MM + 0.01


def test_gable_slopes_the_two_long_edges_and_falls_back_to_hip_off_a_rectangle():
    rect = Plan.model_validate(
        {
            "walls": [
                {"id": "E", "axis": [[0, 0], [10000, 0], [10000, 6000], [0, 6000]], "thickness": 200, "closed": True}
            ]
        }
    )
    (roof,) = rows(plan_to_revit(rect, roof="gable", pitch=25), "roofs")
    slopes = roof["edge_slopes"]
    assert [e["defines_slope"] for e in slopes] == [True, False, True, False]
    assert slopes[0]["slope_degrees"] == 25.0 and roof["level_id"] == {"$ref": "level:TECHO"}
    out = plan_to_revit(_full(), roof="gable")
    assert all("slope_degrees" in r for r in rows(out, "roofs")) and any("gable" in w for w in out["warnings"])
    assert "roofs" not in [s["id"] for s in plan_to_revit(rect, roof="none")["stages"]]


# --- stairs, columns, curtain walls, railings ------------------------------------------------


def test_stair_runs_follow_the_extension_layout():
    straight = (
        Plan.model_validate(full_plan())
        .stairs[0]
        .model_copy(update={"stair_kind": "straight", "turn": "left", "direction_deg": 0, "start": (0, 0)})
    )
    (run,), landings = stair_runs(straight, 100.0)
    assert run["start"] == [0.0, 0.0, 100.0] and run["end"] == [16 * 280.0, 0.0, 100.0] and landings == []
    u = straight.model_copy(update={"stair_kind": "u"})
    runs, (landing,) = stair_runs(u, 0.0)
    assert [r["expected_risers"] for r in runs] == [9, 8]
    assert runs[1]["start"] == [2240.0, 1000.0, 1561.5] and runs[1]["end"] == [280.0, 1000.0, 1561.5]
    assert {p[2] for p in landing["profile"][0]} == {1561.5}
    ring = stair_footprint(u)
    assert len(ring) == 4 and min(p[0] for p in ring) == 0.0 and max(p[1] for p in ring) == 1500.0


def test_columns_curtain_walls_and_railings_map_to_their_calls():
    out = plan_to_revit(_full())
    check_contract(out)
    calls = stage(out, "railings_columns_curtain")["calls"]
    cols = bound(out, "railings_columns_curtain")
    round_col = next(v for k, v in cols.items() if k.startswith("column:"))
    assert round_col["kind"] == "structural_column" and round_col["coordinate_mode"] == "level_offset"
    types = next(c for c in calls if c["tool"] == "horizun_manage_system_types")["args"]["actions"]
    assert any(a["values"].get("b") for a in types)
    curtain = next(v for k, v in cols.items() if k.startswith("curtain:"))
    assert curtain["kind"] == "wall" and curtain["type_id"]["$ref"].startswith("type:curtain:")
    assert curtain["start"][2] == 3100.0 + curtain["offset"]
    railing = next(c for c in calls if c["tool"] == "horizun_create_railing")
    assert all(len(p) == 3 for p in railing["args"]["path"]) and railing["args"]["level_id"] == {"$ref": "level:N01"}
    host_types = stage(out, "types")["calls"][0]["args"]["actions"]
    curtain_type = next(a for a in host_types if "cortina" in a["new_name"])
    assert curtain_type["values"]["SPACING_LAYOUT_VERT"] == 1


def test_what_has_no_native_element_is_listed_with_a_suggestion():
    out = plan_to_revit(_full())
    records = {n["record"] for n in out["not_mapped"]}
    assert {
        "furniture",
        "site.objects",
        "site.pools",
        "site.fences",
        "pools",
        "planters",
        "ramps",
        "slabs.edge_band",
        "openings.head",
        "materials",
    } <= records
    assert all(n["suggest"] and n["why"] and n["ids"] for n in out["not_mapped"])
    assert out["assumptions"] and isinstance(out["assumptions"][0], str)


def test_refusals_name_the_problem():
    with pytest.raises(ValueError, match="roof must be"):
        plan_to_revit(_full(), roof="dome")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exports"):
        plan_to_revit(_full(), exports=["pdf"])
    with pytest.raises(ValueError, match="non-empty"):
        plan_to_revit([])
    out = plan_to_revit(_full(), horizun_version="3.1.0", exports=[])
    assert "3.0.6" in out["warnings"][0] and "exports" not in [s["id"] for s in out["stages"]]


# --- the MCP tool ----------------------------------------------------------------------------


def test_revit_batches_tool_reads_dxfs_without_sketchup(run, tmp_path):
    storeys = [{"dxf_path": str(FIXTURES / f"casa_2_plantas_{n}.dxf"), "name": n} for n in ("N00", "N01")]

    async def body(client):
        tools = {t.name: t for t in (await client.list_tools()).tools}
        args = {"storeys": storeys, "roof": "hip", "exports": ["ifc"], "family_paths": {"window": r"C:\v.rfa"}}
        ok = await client.call_tool("revit_batches", args)
        single = await client.call_tool(
            "revit_batches",
            {"plan": {"walls": [{"id": "A", "axis": [[0, 0], [3000, 0]], "thickness": 150}]}, "storey": "PB"},
        )
        wall = {"id": "A", "axis": [[0, 0], [3000, 0]], "thickness": 150}
        both = await client.call_tool("revit_batches", {"dxf_path": "x.dxf", "plan": {"walls": [wall]}})
        bad = await client.call_tool(
            "revit_batches",
            {
                "plan": {
                    "walls": [wall],
                    "openings": [{"id": "W", "wall": "B", "opening_kind": "window", "offset": 0, "width": 900}],
                }
            },
        )
        return tools, ok, single, both, bad

    fake = FakeExtension()
    tools, ok, single, both, bad = run(_session(fake, tmp_path, body))
    assert tools["revit_batches"].annotations.read_only_hint is True
    assert not ok.is_error, _text(ok)
    out = ok.structured_content
    assert [s["name"] for s in out["storeys"]] == ["N00", "N01"]
    assert [c["args"]["format"] for c in stage(out, "exports")["calls"][1:]] == ["ifc"]
    assert rows(out, "roofs")[0]["slope_degrees"] == 30.0
    assert "window_rfa" not in out["inputs"] and "door_rfa" in out["inputs"]
    assert not single.is_error and single.structured_content["storeys"][0]["name"] == "PB"
    assert both.is_error and "one of them" in _text(both)
    assert bad.is_error and _text(bad).startswith("revit_batches refused its input (-32004): plan.openings[0].wall")
    assert fake.seen == []  # SketchUp was never asked anything
