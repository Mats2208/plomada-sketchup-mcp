"""plan_from_dxf's inferrer: architect-style drawings of known plans read back, the AutoCAD MCP Pro
fixtures read without their records, and the report's honesty about what it could not tell."""

from __future__ import annotations

import json
import math
import random
import shutil
import subprocess
from pathlib import Path

import ezdxf
import pytest

import plans_infer as plans
from conftest import FIXTURES
from drafter import Drafter, Seg, Style, draft
from plomada_bridge.dxf import read_plan
from plomada_bridge.infer import InferError, InferOptions, LayerRoles, infer_plan
from plomada_bridge.models import Plan, parse_plan

ROOT = Path(__file__).resolve().parents[1]
RUBY = shutil.which("ruby")
FIXTURE_NAMES = ("casa_minimalista", "casa_2_plantas_N00", "casos_prueba", "casa_completa_N00")


# --- comparing two plans in world coordinates --------------------------------------------------


def _segs(plan: Plan) -> list[Seg]:
    return [s for segs in Drafter(plan, Style()).segs.values() for s in segs]


def _distance(p: tuple[float, float], s: Seg) -> float:
    x = s.local(p)
    if -1e-6 <= x <= s.length + 1e-6:
        return abs(s.lateral(p))
    return min(math.dist(p, s.a), math.dist(p, s.b))


def _coverage(src: Plan, dst: Plan, tol: float = 2.0, step: float = 25.0) -> tuple[float, float]:
    """Share of src's axis length lying within tol of a dst axis of the same thickness, and the worst
    distance found among the covered samples."""
    targets = _segs(dst)
    hit = total = 0
    worst = 0.0
    for s in _segs(src):
        n = max(2, math.ceil(s.length / step))
        for k in range(n + 1):
            p = s.point(s.length * k / n)
            near = [_distance(p, t) for t in targets if abs(t.t - s.t) <= 0.5]
            total += 1
            d = min(near, default=math.inf)
            if d <= tol:
                hit += 1
                worst = max(worst, d)
    return hit / total, worst


def _opening(plan: Plan, op) -> dict:
    segs = Drafter(plan, Style()).segs[op.wall]
    seg = next(s for s in segs if op.offset < s.cum + s.length - 1e-6)
    s0 = op.offset - seg.cum
    s1 = s0 + op.width
    sigma = 1 if op.swing == "in" else -1
    far = (op.hand == "left") == (op.swing == "in")
    return {
        "kind": op.opening_kind,
        "centre": seg.point((s0 + s1) / 2.0),
        "width": op.width,
        "hinge": seg.point(s1 if far else s0),
        "side": seg.point((s0 + s1) / 2.0, sigma * seg.t),
    }


def _openings_match(gt: Plan, got: Plan, tol: float = 5.0) -> tuple[float, float, list[str]]:
    """Recall and precision of openings (centre and width within tol, same kind), and the mismatches
    in kind, hinge or swing side of the matched doors."""
    a = [_opening(gt, o) | {"id": o.id} for o in gt.openings]
    b = [_opening(got, o) | {"id": o.id} for o in got.openings]
    used: set[int] = set()
    wrong: list[str] = []
    for o in a:
        k = next(
            (
                i
                for i, x in enumerate(b)
                if i not in used and math.dist(x["centre"], o["centre"]) <= tol and abs(x["width"] - o["width"]) <= tol
            ),
            None,
        )
        if k is None:
            continue
        used.add(k)
        x = b[k]
        if x["kind"] != o["kind"]:
            wrong.append(f"{o['id']} read as {x['kind']}")
        elif o["kind"] == "door" and (math.dist(x["hinge"], o["hinge"]) > tol or math.dist(x["side"], o["side"]) > tol):
            wrong.append(f"{o['id']} hinge or swing side differs")
    recall = len(used) / len(a) if a else 1.0
    precision = len(used) / len(b) if b else 1.0
    return recall, precision, wrong


def _assert_same(gt: Plan, got: Plan) -> None:
    recall, worst = _coverage(gt, got)
    precision, worst2 = _coverage(got, gt)
    assert recall == 1.0 and precision == 1.0, (recall, precision)
    assert max(worst, worst2) <= 2.0
    assert {w.thickness for w in gt.walls} == {w.thickness for w in got.walls}  # exact, coverage matched them
    r, p, wrong = _openings_match(gt, got)
    assert (r, p, wrong) == (1.0, 1.0, [])


def _assert_rooms(gt: Plan, got: Plan) -> None:
    want = sorted((r.name, (round(r.at[0], 1), round(r.at[1], 1)), round(r.area / 1e4)) for r in gt.rooms)
    have = sorted((r.name, (round(r.at[0], 1), round(r.at[1], 1)), round(r.area / 1e4)) for r in got.rooms)
    assert have == want


def _solver(plan: Plan, tmp_path: Path) -> dict:
    """The extension's wall solver (pure Ruby, no SketchUp) on the plan: {"ok", "manifold"} or the refusal."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan.to_wire(), ensure_ascii=False), encoding="utf-8")
    out = subprocess.run([RUBY, str(ROOT / "tests" / "solve_walls.rb"), str(path)], capture_output=True, text=True)
    return json.loads(out.stdout)


def _shifted(plan: Plan, dx: float, dy: float) -> Plan:
    wire = plan.to_wire()
    for w in wire["walls"]:
        w["axis"] = [[v[0] + dx, v[1] + dy, *v[2:]] for v in w["axis"]]
    for r in wire["rooms"]:
        r["at"] = [r["at"][0] + dx, r["at"][1] + dy]
    return parse_plan(wire)


needs_ruby = pytest.mark.skipif(RUBY is None, reason="ruby is not installed")


# --- architect-style drawings of known plans ------------------------------------------------------


def test_a_house_with_tees_reads_back_exactly(tmp_path):
    gt = plans.house()
    res = infer_plan(draft(gt, tmp_path / "house.dxf", door_blocks=True))
    _assert_same(gt, res.plan)
    _assert_rooms(gt, res.plan)
    rep = res.report
    assert rep["verdict"].endswith("nothing flagged, still a draft"), rep["verdict"]
    assert rep["layers"]["walls"] == ["MUROS"] and rep["layers"]["doors"] == ["PUERTAS"]
    assert {"EJES", "COTAS"} <= set(rep["layers"]["ignored"])
    assert rep["unpaired"]["faces"] == 0 and not rep["dropped"] and not rep["unclassified_gaps"]
    assert all(w["paired_pct"] == 100.0 for w in rep["walls"])
    assert rep["ignored"]["entity_types"]["DIMENSION"] == 2
    assert rep["ignored"]["blocks"] == {"CAMA_2P": 2, "MESA_4": 1}
    assert [w.id for w in res.plan.walls] == ["W1", "W2", "W3"]
    assert [o.id for o in res.plan.openings] == ["D1", "V1", "V2", "V3", "V4", "D2", "D3", "D4"]
    doors = [o for o in res.report["openings"] if o["kind"] == "door"]
    assert all("swing arc" in o["evidence"] for o in doors)


def test_an_l_house_with_window_blocks_and_polyline_faces(tmp_path):
    gt = plans.l_house()
    res = infer_plan(draft(gt, tmp_path / "l.dxf", window_blocks=True, faces="polylines"))
    _assert_same(gt, res.plan)
    _assert_rooms(gt, res.plan)
    (ext,) = [w for w in res.plan.walls if w.closed]
    assert len(ext.axis) == 6 and ext.axis[0] == (0.0, 0.0)
    assert {o["evidence"] for o in res.report["openings"] if o["kind"] == "window"} == {
        "window linework on VENTANA_1500x200",
        "window linework on VENTANA_2000x200",
        "window linework on VENTANA_1800x200",
    }


def test_a_curved_wall_from_concentric_arcs(tmp_path):
    gt = plans.curved()
    res = infer_plan(draft(gt, tmp_path / "curved.dxf"))
    _assert_same(gt, res.plan)
    (ext,) = [w for w in res.plan.walls if w.closed]
    assert ext.axis[0] == (0.0, 0.0, -0.4)
    windows = [o for o in res.plan.openings if o.opening_kind == "window"]
    assert sorted((o.offset, o.width) for o in windows) == [(1500.0, 1500.0), (7500.0, 1500.0)]


def test_a_drawing_in_metres_reads_as_millimetres(tmp_path):
    gt = plans.house()
    res = infer_plan(draft(gt, tmp_path / "m.dxf", scale=0.001, insunits=6))
    _assert_same(gt, res.plan)
    _assert_rooms(gt, res.plan)
    assert res.report["units"]["unit"] == "m" and res.report["units"]["source"] == "$INSUNITS"
    forced = infer_plan(tmp_path / "m.dxf", InferOptions(units="m"))
    assert forced.plan.to_wire() == res.plan.to_wire() and forced.report["units"]["source"] == "units parameter"


def test_insunits_that_lie_are_overruled_by_the_walls(tmp_path):
    gt = plans.house()
    path = draft(gt, tmp_path / "lie.dxf", insunits=6)  # millimetre drawing, header says metres
    res = infer_plan(path)
    _assert_same(gt, res.plan)
    assert res.report["units"]["unit"] == "mm" and res.report["units"]["source"] == "wall evidence"
    assert any("$INSUNITS says m" in a for a in res.report["assumptions"])


def test_walls_inside_a_block(tmp_path):
    gt = plans.house()
    res = infer_plan(draft(gt, tmp_path / "blk.dxf", in_block=(5000.0, 3000.0), noise=False))
    moved = _shifted(gt, 5000.0, 3000.0)
    _assert_same(moved, res.plan)
    _assert_rooms(moved, res.plan)


def test_walls_drawn_as_closed_outlines(tmp_path):
    gt = plans.box()
    res = infer_plan(draft(gt, tmp_path / "box.dxf", faces="outlines"))
    _assert_same(gt, res.plan)
    assert res.report["unpaired"]["faces"] == 0


def test_walls_drawn_only_as_solid_hatches(tmp_path):
    gt = plans.box()
    path = draft(gt, tmp_path / "hatch.dxf", faces="outlines")
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    for pl in list(msp.query("LWPOLYLINE[layer=='MUROS']")):
        hatch = msp.add_hatch(color=8, dxfattribs={"layer": "MUROS"})
        hatch.paths.add_polyline_path([p[:2] for p in pl.get_points("xy")], is_closed=True)
        msp.delete_entity(pl)
    doc.saveas(path)
    res = infer_plan(path)
    _assert_same(gt, res.plan)
    assert res.report["unpaired"]["faces"] == 0  # hatch edges are evidence, never unpaired


def test_openings_against_junctions_double_and_sliding_doors(tmp_path):
    gt = plans.corners()
    path = draft(gt, tmp_path / "corners.dxf", double_doors=("D1",), sliding_doors=("D3",))
    res = infer_plan(path)
    assert _coverage(gt, res.plan)[0] == 1.0 and _coverage(res.plan, gt)[0] == 1.0
    have = [(_opening(res.plan, o), o, e) for o, e in zip(res.plan.openings, res.report["openings"], strict=True)]
    assert len(have) == len(gt.openings)
    for o in gt.openings:
        w = _opening(gt, o)
        h, rec, entry = next(x for x in have if math.dist(x[0]["centre"], w["centre"]) <= 5.0)
        assert h["kind"] == w["kind"] and abs(h["width"] - w["width"]) <= 5.0, o.id
        if o.id == "D1":
            assert entry["assumed"] == ["double door drawn with two leaves; built as one leaf"]
        elif o.id == "D2":  # hard against P's face: found from Q's free end
            assert math.dist(h["hinge"], w["hinge"]) <= 5.0 and math.dist(h["side"], w["side"]) <= 5.0
            assert rec.offset >= 60.0 and rec.offset + rec.width <= 860.0
        elif o.id == "D3":
            assert entry["evidence"] == "door linework on PUERTAS" and entry["assumed"]
    if RUBY:
        assert _solver(res.plan, tmp_path)["ok"]


def test_an_empty_gap_is_reported_not_guessed(tmp_path):
    gt = plans.house()
    path = draft(gt, tmp_path / "gap.dxf", skip_openings=("V2",))
    res = infer_plan(path)
    (gap,) = res.report["unclassified_gaps"]
    assert gap["at"] == [12000.0, 2250.0] and gap["width"] == 1500.0 and gap["emitted_as"] == "skip"
    assert len(res.plan.openings) == len(gt.openings) - 1
    assert "1 unclassified gaps" in res.report["verdict"]
    assert not any(w.closed for w in res.plan.walls)  # the loop is open at the full-height gap
    assert res.report["counts"]["walls"] == 3
    as_window = infer_plan(path, InferOptions(default_opening="window"))
    _assert_same(gt, as_window.plan)
    (gap,) = as_window.report["unclassified_gaps"]
    assert gap["emitted_as"] == "window" and gap["id"] in {o.id for o in as_window.plan.openings}
    entry = next(o for o in as_window.report["openings"] if o["id"] == gap["id"])
    assert entry["evidence"] == "unclassified gap" and entry["assumed"]


def test_layer_mapping_and_geometric_fallback(tmp_path):
    gt = plans.house()
    path = draft(gt, tmp_path / "odd.dxf", walls="L1", doors="L2", windows="L3", rooms="L4")
    mapped = infer_plan(
        path, InferOptions(layers=LayerRoles(walls=["l1"], doors=["L2"], windows=["L*3"], rooms=["L4"]))
    )
    _assert_same(gt, mapped.plan)
    _assert_rooms(gt, mapped.plan)
    assert mapped.report["layers"]["how"]["wall"] == "mapping" and mapped.report["layers"]["walls"] == ["L1"]
    guessed = infer_plan(path)
    _assert_same(gt, guessed.plan)
    _assert_rooms(gt, guessed.plan)
    assert guessed.report["layers"]["how"]["wall"] == "geometry"
    assert any("walls read from the layers whose lines pair" in a for a in guessed.report["assumptions"])
    assert guessed.report["layers"]["rooms_from"].startswith("texts inside the walls")
    wrong = infer_plan(path, InferOptions(layers=LayerRoles(walls=["L1"], ignore=["L2", "L3"])))
    assert len(wrong.plan.openings) == 0 and len(wrong.report["unclassified_gaps"]) == len(gt.openings)


def test_furniture_and_stray_parallels_do_not_become_walls(tmp_path):
    gt = plans.house()
    path = draft(gt, tmp_path / "amb.dxf")
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    # a wardrobe 600 deep standing free in the living room, on the wall layer
    msp.add_lwpolyline(
        [(1000, 5500), (2800, 5500), (2800, 6100), (1000, 6100)], close=True, dxfattribs={"layer": "MUROS"}
    )
    # a counter edge 550 off P1's face, along it, on the wall layer
    msp.add_line((5375, 4800), (5375, 7600), dxfattribs={"layer": "MUROS"})
    # a wardrobe block in the bedroom corner, inserted on the wall layer
    blk = doc.blocks.new("ROPERO_180")
    blk.add_lwpolyline([(0, 0), (1800, 0), (1800, 600), (0, 600)], close=True)
    msp.add_blockref("ROPERO_180", (6075, 3350), dxfattribs={"layer": "MUROS"})
    doc.saveas(path)
    res = infer_plan(path)
    _assert_same(gt, res.plan)
    dropped = [d for d in res.report["dropped"] if d["what"] == "wall run"]
    assert any(d["thickness"] == 600.0 for d in dropped)


@pytest.mark.parametrize("deg", [7.5, 30.0, 135.0])
@pytest.mark.parametrize("name", ["house", "l_house", "curved"])
def test_a_rotated_and_shifted_plan(tmp_path, name, deg):
    gt = plans.moved(getattr(plans, name)(), deg, 1234.5, -678.9)
    res = infer_plan(draft(gt, tmp_path / "rot.dxf"))
    _assert_same(gt, res.plan)
    _assert_rooms(gt, res.plan)


def test_endpoints_off_by_half_a_millimetre(tmp_path):
    """Imprecise linework: every line and arc centre moved up to 0.5 mm at random."""
    gt = plans.house()
    path = draft(gt, tmp_path / "jitter.dxf")
    doc = ezdxf.readfile(path)
    rnd = random.Random(7)

    def jitter(v):
        return (v.x + rnd.uniform(-0.5, 0.5), v.y + rnd.uniform(-0.5, 0.5))

    for e in doc.modelspace():
        if e.dxftype() == "LINE":
            e.dxf.start, e.dxf.end = jitter(e.dxf.start), jitter(e.dxf.end)
        elif e.dxftype() == "ARC":
            e.dxf.center = jitter(e.dxf.center)
    doc.saveas(path)
    res = infer_plan(path)
    _assert_same(gt, res.plan)
    assert res.report["verdict"].endswith("nothing flagged, still a draft")


def test_the_same_drawing_gives_the_same_plan(tmp_path):
    path = draft(plans.l_house(), tmp_path / "again.dxf")
    one, two = infer_plan(path), infer_plan(path)
    assert one.wire() == two.wire()


@needs_ruby
@pytest.mark.parametrize("name", ["house", "l_house", "curved", "box"])
def test_the_wall_solver_accepts_every_inferred_plan(tmp_path, name):
    style = {"faces": "outlines"} if name == "box" else {}
    res = infer_plan(draft(getattr(plans, name)(), tmp_path / f"{name}.dxf", **style))
    out = _solver(res.plan, tmp_path)
    assert out["ok"] and out["manifold"], out
    assert out["openings"] >= len(res.plan.openings)  # an opening on an arc is one frame per flat panel


# --- the AutoCAD MCP Pro fixtures, records ignored ---------------------------------------------


def _one_layer(path: Path, tmp_path: Path) -> Path:
    """The fixture with walls, doors and windows (and the wall hatches) all on layer 0."""
    doc = ezdxf.readfile(path)
    for e in doc.modelspace():
        if e.dxf.layer.startswith(("A-WALL", "A-DOOR", "A-WIND")):
            e.dxf.layer = "0"
    out = tmp_path / f"{path.stem}_one_layer.dxf"
    doc.saveas(out)
    return out


@pytest.mark.parametrize("one_layer", [False, True], ids=["layers", "one-layer"])
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_autocad_fixtures_without_their_records(tmp_path, monkeypatch, name, one_layer):
    """The linework AutoCAD MCP Pro's arch tools drew, read with every XDATA accessor disabled, against the
    records read_plan finds in the same file."""
    truth = read_plan(FIXTURES / f"{name}.dxf").plan
    path = _one_layer(FIXTURES / f"{name}.dxf", tmp_path) if one_layer else FIXTURES / f"{name}.dxf"

    def no_xdata(*_a, **_k):
        raise AssertionError("the inferrer read XDATA")

    monkeypatch.setattr(ezdxf.entities.DXFEntity, "get_xdata", no_xdata)
    monkeypatch.setattr(ezdxf.entities.DXFEntity, "has_xdata", no_xdata)
    res = infer_plan(path)
    monkeypatch.undo()
    w_recall, w_dev = _coverage(truth, res.plan)
    w_precision, _ = _coverage(res.plan, truth)
    o_recall, o_precision, wrong = _openings_match(truth, res.plan)
    rooms = {r.name for r in truth.rooms} == {r.name for r in res.plan.rooms}
    print(
        f"\n{name} ({'one layer' if one_layer else 'named layers'}): walls recall {w_recall:.3f} precision "
        f"{w_precision:.3f} (worst {w_dev:.2f} mm); openings recall {o_recall:.3f} precision {o_precision:.3f}, "
        f"{len(wrong)} wrong; rooms {'all' if rooms else 'differ'}"
    )
    assert w_recall >= 0.99 and w_precision >= 0.99
    assert o_recall >= 0.99 and o_precision >= 0.99 and wrong == []
    assert rooms
    assert res.report["units"]["unit"] == "mm"


@needs_ruby
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_the_wall_solver_accepts_the_inferred_fixtures(tmp_path, name):
    res = infer_plan(FIXTURES / f"{name}.dxf")
    out = _solver(res.plan, tmp_path)
    assert out["ok"] and out["manifold"], out
    assert out["openings"] >= len(res.plan.openings)  # an opening on an arc is one frame per flat panel


# --- refusals ---------------------------------------------------------------------------------------


def test_refusals_name_the_problem(tmp_path):
    with pytest.raises(InferError, match="is a DWG; plan_from_dxf reads DXF only"):
        infer_plan(tmp_path / "plano.dwg")
    with pytest.raises(InferError, match="is not a file"):
        infer_plan(tmp_path / "missing.dxf")
    (tmp_path / "junk.dxf").write_text("not a dxf", encoding="ascii")
    with pytest.raises(InferError, match=r"cannot read junk\.dxf as DXF"):
        infer_plan(tmp_path / "junk.dxf")
    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (5000, 0), dxfattribs={"layer": "MUROS"})
    doc.saveas(tmp_path / "one_line.dxf")
    with pytest.raises(InferError, match="the wall layers MUROS hold no pair of parallel faces 60-600 mm apart"):
        infer_plan(tmp_path / "one_line.dxf")
    doc = ezdxf.new()
    doc.modelspace().add_text("hola", dxfattribs={"layer": "NOTAS"})
    doc.saveas(tmp_path / "text.dxf")
    with pytest.raises(InferError, match="no layer is named or mapped as walls"):
        infer_plan(tmp_path / "text.dxf")
