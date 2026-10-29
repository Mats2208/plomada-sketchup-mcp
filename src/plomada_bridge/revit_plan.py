"""Plan -> Horizun Revit MCP batches: a pure compiler, no Revit and no SketchUp involved.

plan_to_revit turns one or more validated storey plans into ordered stages of Horizun Revit MCP
calls (tool name + arguments, schemas of Horizun 3.0.6): document, discovery of the template's
types, levels, wall/floor types, walls, opening types, openings, floors, roofs, rooms, stairs,
railings / columns / curtain walls, verification and exports. Every length is millimetres and
every call sends units "mm"; XYZ is Revit's internal origin with z the absolute elevation.

Revit ids do not exist until a call has run, so a value that comes from an earlier call is a
placeholder ``{"$ref": "<name>"}``; each call's ``binds`` names the refs its reply defines and
where (``rows[3]`` = the id of reply row 3, rows in request order). ``{"$input": "<name>"}`` is a
value the caller must supply (a file path) and ``inputs`` lists them.

Mapping decisions (see skill/reference/revit.md):
- A Plomada wall polyline becomes one Revit wall per segment; an arc segment becomes one curved
  wall declared as Horizun wants it (centre, radius, clockwise), the circle found through three
  points: both ends and the arc's midpoint from the bulge.
- Horizun's wall row has no location-line field (Wall.Create builds on the wall centreline), so a
  left / right justified axis is moved to the centreline: +-thickness / 2 along the axis normal,
  with mitred joints between consecutive segments.
- Floor and roof profiles take points only, so their arc edges are tessellated (sagitta <= 5 mm).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import PureWindowsPath
from typing import Any, Literal

from .config import CONFIG
from .models import (
    DOOR_HEIGHT_MM,
    WINDOW_HEIGHT_MM,
    WINDOW_SILL_MM,
    Opening,
    Plan,
    Stair,
    Wall,
    bulge_of,
    fmt,
    ring_area,
)

__all__ = ["FORMAT", "HORIZUN_SCHEMA_VERSION", "RevitStorey", "plan_to_revit"]

FORMAT = "plomada.revit_batches/1"
HORIZUN_SCHEMA_VERSION = "3.0.6"  # the Horizun contract these calls were written against
DEFAULT_TEMPLATE = "Default_M_ESP.rte"
TEMPLATE_DIR = r"C:\ProgramData\Autodesk\RVT {year}\Templates"
ROOF_LEVEL = "TECHO"
ARC_MAX_STEP_DEG = 10.0  # tessellation of floor / roof / railing arcs: at most this sweep per chord
ARC_MAX_SAGITTA_MM = 5.0  # ... and at most this gap between chord and arc
RAILING_MAX_POINTS = 200  # horizun_create_railing path maxItems
PROBE_MM = 50.0  # how far past a wall face the inside / outside probe looks
EPS = 1e-6

RoofKind = Literal["flat", "gable", "hip", "none"]
Pt = tuple[float, float]


# --- geometry -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Seg:
    """A straight segment, or an arc on (centre, radius) running counter-clockwise when ccw."""

    a: Pt
    b: Pt
    centre: Pt | None = None
    radius: float = 0.0
    ccw: bool = True

    @property
    def arc(self) -> bool:
        return self.centre is not None


def _sub(p: Pt, q: Pt) -> Pt:
    return (p[0] - q[0], p[1] - q[1])


def _add(p: Pt, v: Pt, k: float = 1.0) -> Pt:
    return (p[0] + v[0] * k, p[1] + v[1] * k)


def _unit(v: Pt) -> Pt:
    n = math.hypot(*v)
    return (v[0] / n, v[1] / n)


def _cross(u: Pt, v: Pt) -> float:
    return u[0] * v[1] - u[1] * v[0]


def arc_mid(a: Pt, b: Pt, bulge: float) -> Pt:
    """The midpoint of the arc a bulge draws from a to b (sagitta bulge * chord / 2, to the right
    of the chord for a counter-clockwise arc)."""
    d = math.dist(a, b)
    ux, uy = _unit(_sub(b, a))
    m = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    return (m[0] + uy * bulge * d / 2.0, m[1] - ux * bulge * d / 2.0)


def circle_through(p: Pt, q: Pt, r: Pt) -> tuple[Pt, float]:
    """Centre and radius of the circle through three points."""
    d = 2.0 * (p[0] * (q[1] - r[1]) + q[0] * (r[1] - p[1]) + r[0] * (p[1] - q[1]))
    if abs(d) < 1e-12:
        raise ValueError("three collinear points describe no arc")
    pp, qq, rr = p[0] ** 2 + p[1] ** 2, q[0] ** 2 + q[1] ** 2, r[0] ** 2 + r[1] ** 2
    cx = (pp * (q[1] - r[1]) + qq * (r[1] - p[1]) + rr * (p[1] - q[1])) / d
    cy = (pp * (r[0] - q[0]) + qq * (p[0] - r[0]) + rr * (q[0] - p[0])) / d
    return (cx, cy), math.dist((cx, cy), p)


def seg_from_bulge(a: Sequence[float], b: Sequence[float], bulge: float) -> Seg:
    pa, pb = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    if abs(bulge) <= 1e-12:
        return Seg(pa, pb)
    mid = arc_mid(pa, pb, bulge)
    centre, radius = circle_through(pa, mid, pb)
    return Seg(pa, pb, centre, radius, _cross(_sub(mid, pa), _sub(pb, mid)) > 0)


def segments_of(vertices: Sequence[Sequence[float]], closed: bool) -> list[Seg]:
    n = len(vertices)
    count = n if closed else n - 1
    return [seg_from_bulge(vertices[i], vertices[(i + 1) % n], bulge_of(vertices[i])) for i in range(count)]


def _angle(c: Pt, p: Pt) -> float:
    return math.atan2(p[1] - c[1], p[0] - c[0])


def sweep(s: Seg) -> float:
    """The arc's sweep in radians, always positive."""
    a0, a1 = _angle(s.centre, s.a), _angle(s.centre, s.b)  # type: ignore[arg-type]
    d = (a1 - a0) if s.ccw else (a0 - a1)
    while d <= 1e-12:
        d += 2.0 * math.pi
    return d


def seg_length(s: Seg) -> float:
    return s.radius * sweep(s) if s.arc else math.dist(s.a, s.b)


def point_at(s: Seg, t: float) -> Pt:
    """The point t mm along the segment from its start."""
    if not s.arc:
        return _add(s.a, _unit(_sub(s.b, s.a)), t)
    ang = _angle(s.centre, s.a) + (t / s.radius) * (1.0 if s.ccw else -1.0)  # type: ignore[arg-type]
    return (s.centre[0] + s.radius * math.cos(ang), s.centre[1] + s.radius * math.sin(ang))  # type: ignore[index]


def arc_midpoint(s: Seg) -> Pt:
    return point_at(s, seg_length(s) / 2.0)


def left_normal(s: Seg, p: Pt) -> Pt:
    """The unit normal on the left of the direction of travel at p."""
    if not s.arc:
        ux, uy = _unit(_sub(s.b, s.a))
        return (-uy, ux)
    radial = _unit(_sub(p, s.centre))  # type: ignore[arg-type]
    return (-radial[0], -radial[1]) if s.ccw else radial


def offset_seg(s: Seg, d: float) -> Seg:
    """The segment moved d mm to its left (negative: to its right)."""
    if not s.arc:
        n = left_normal(s, s.a)
        return Seg(_add(s.a, n, d), _add(s.b, n, d))
    r = s.radius - d if s.ccw else s.radius + d
    if r <= 1.0:
        raise ValueError(f"an arc of radius {fmt(round(s.radius, 1))} mm cannot be offset {fmt(round(d, 1))} mm")
    k = r / s.radius
    c = s.centre
    return Seg(_add(c, _sub(s.a, c), k), _add(c, _sub(s.b, c), k), c, r, s.ccw)  # type: ignore[arg-type]


def _line_line(p: Pt, u: Pt, q: Pt, v: Pt) -> list[Pt]:
    den = _cross(u, v)
    if abs(den) < 1e-9:
        return []
    t = _cross(_sub(q, p), v) / den
    return [_add(p, u, t)]


def _line_circle(p: Pt, u: Pt, c: Pt, r: float) -> list[Pt]:
    f = _sub(p, c)
    b = f[0] * u[0] + f[1] * u[1]
    disc = b * b - (f[0] ** 2 + f[1] ** 2 - r * r)
    if disc < 0:
        return []
    root = math.sqrt(disc)
    return [_add(p, u, -b - root), _add(p, u, -b + root)]


def _circle_circle(c0: Pt, r0: float, c1: Pt, r1: float) -> list[Pt]:
    d = math.dist(c0, c1)
    if d < 1e-9 or d > r0 + r1 or d < abs(r0 - r1):
        return []
    a = (r0 * r0 - r1 * r1 + d * d) / (2.0 * d)
    h = math.sqrt(max(r0 * r0 - a * a, 0.0))
    u = _unit(_sub(c1, c0))
    m = _add(c0, u, a)
    return [(m[0] - u[1] * h, m[1] + u[0] * h), (m[0] + u[1] * h, m[1] - u[0] * h)]


def _meet(s: Seg, t: Seg, near: Pt) -> Pt | None:
    """Where the (extended) segments s and t cross, nearest to near."""
    if not s.arc and not t.arc:
        cands = _line_line(s.a, _unit(_sub(s.b, s.a)), t.a, _unit(_sub(t.b, t.a)))
    elif s.arc and t.arc:
        cands = _circle_circle(s.centre, s.radius, t.centre, t.radius)  # type: ignore[arg-type]
    else:
        line, circ = (t, s) if s.arc else (s, t)
        cands = _line_circle(line.a, _unit(_sub(line.b, line.a)), circ.centre, circ.radius)  # type: ignore[arg-type]
    return min(cands, key=lambda c: math.dist(c, near)) if cands else None


def _on(s: Seg, p: Pt) -> Pt:
    """p moved onto the segment's circle (an arc end must stay on its circle)."""
    if not s.arc:
        return p
    return _add(s.centre, _unit(_sub(p, s.centre)), s.radius)  # type: ignore[arg-type]


def offset_chain(segs: list[Seg], d: float, closed: bool) -> list[Seg]:
    """Every segment moved d mm to its left, consecutive ones mitred where they meet again."""
    if abs(d) < EPS:
        return list(segs)
    out = [offset_seg(s, d) for s in segs]
    pairs = [(i, i + 1) for i in range(len(out) - 1)]
    if closed and len(out) > 1:
        pairs.append((len(out) - 1, 0))
    for i, j in pairs:
        s, t = out[i], out[j]
        if math.dist(s.b, t.a) <= EPS:
            continue
        near = ((s.b[0] + t.a[0]) / 2.0, (s.b[1] + t.a[1]) / 2.0)
        p = _meet(s, t, near)
        if p is None or math.dist(p, near) > 20.0 * abs(d) + 1.0:
            p = near
        out[i] = replace(s, b=_on(s, p))
        out[j] = replace(t, a=_on(t, p))
    return out


def tessellate(segs: Sequence[Seg], closed: bool) -> list[Pt]:
    """The chain as points; arcs split into chords at most ARC_MAX_STEP_DEG and ARC_MAX_SAGITTA_MM."""
    pts: list[Pt] = []
    for s in segs:
        pts.append(s.a)
        if s.arc:
            sw = sweep(s)
            step = min(math.radians(ARC_MAX_STEP_DEG), 2.0 * math.acos(max(-1.0, 1.0 - ARC_MAX_SAGITTA_MM / s.radius)))
            n = max(2, math.ceil(sw / max(step, 1e-6)))
            length = seg_length(s)
            pts.extend(point_at(s, length * k / n) for k in range(1, n))
    if not closed and segs:
        pts.append(segs[-1].b)
    return pts


def signed_area(ring: Sequence[Pt]) -> float:
    return sum(_cross(ring[i], ring[(i + 1) % len(ring)]) for i in range(len(ring))) / 2.0


def point_in_ring(p: Pt, ring: Sequence[Pt]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        (x0, y0), (x1, y1) = ring[i], ring[(i + 1) % n]
        if (y0 > p[1]) != (y1 > p[1]) and p[0] < (x1 - x0) * (p[1] - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def _deg(v: Pt) -> float:
    return math.degrees(math.atan2(v[1], v[0])) % 360.0


def _r(v: float) -> float:
    x = round(float(v), 3)
    return 0.0 if x == 0 else x


def _p3(p: Pt, z: float) -> list[float]:
    return [_r(p[0]), _r(p[1]), _r(z)]


def _ref(name: str) -> dict[str, str]:
    return {"$ref": name}


# --- storeys --------------------------------------------------------------------------------


@dataclass
class RevitStorey:
    """One storey to compile: a plan plus its name, height and floor elevation (mm). Missing values
    come from the plan's own storey record, else N00, N01...; the config height; stacked elevation."""

    plan: Plan
    name: str | None = None
    height: float | None = None
    elevation: float | None = None


@dataclass
class _Storey:
    plan: Plan
    name: str
    height: float
    elevation: float
    walls: list[_RWall]
    envelopes: list[Wall]


@dataclass
class _RWall:
    """A Plomada wall as Revit walls: the axis segments, the centreline segments and each one's ref."""

    wall: Wall
    axis: list[Seg]
    centre: list[Seg]
    refs: list[str]
    shift: float  # mm the centreline lies to the left of the axis
    exterior_left: list[bool]


def _as_storey(item: Any) -> RevitStorey:
    if isinstance(item, RevitStorey):
        return item
    if isinstance(item, Plan):
        return RevitStorey(item)
    if isinstance(item, Mapping):
        if "plan" in item and "walls" not in item:
            raw = item["plan"]
            plan = raw if isinstance(raw, Plan) else Plan.model_validate(raw)
            return RevitStorey(plan, item.get("name"), item.get("height"), item.get("elevation"))
        return RevitStorey(Plan.model_validate(item))
    raise ValueError(f"a storey must be a Plan, a plan dict or a RevitStorey, got {type(item).__name__}")


def _storeys(plan: Any) -> list[RevitStorey]:
    if isinstance(plan, (Plan, RevitStorey, Mapping)):
        return [_as_storey(plan)]
    if isinstance(plan, Sequence) and not isinstance(plan, (str, bytes)) and plan:
        return [_as_storey(p) for p in plan]
    raise ValueError("plan must be a plan or a non-empty list of storeys")


def _resolve(specs: list[RevitStorey]) -> list[tuple[Plan, str, float, float]]:
    out: list[tuple[Plan, str, float, float]] = []
    seen: set[str] = set()
    prev: tuple[float, float] | None = None
    for i, spec in enumerate(specs):
        rec = spec.plan.storey
        # A storey record's own name counts, except a defaulted N00 on a later storey of a list.
        own = rec.name if rec is not None and (i == 0 or rec.name != "N00") else None
        name = spec.name or own or f"N{i:02d}"
        if name in seen:
            raise ValueError(f"storey {name} appears twice; give each storey its own name")
        seen.add(name)
        height = spec.height or (rec.height if rec is not None else None) or CONFIG.storey_height_mm
        if spec.elevation is not None:
            elevation = float(spec.elevation)
        elif rec is not None and rec.elevation_mode == "explicit" and rec.elevation is not None:
            elevation = float(rec.elevation)
        else:
            elevation = 0.0 if prev is None else prev[0] + prev[1]
        prev = (elevation, float(height))
        out.append((spec.plan, name, float(height), elevation))
    return out


# --- the compiler ---------------------------------------------------------------------------


class _Compiler:
    def __init__(
        self,
        storeys: list[tuple[Plan, str, float, float]],
        *,
        template: str,
        horizun_version: str,
        revit_year: int,
        roof: RoofKind,
        overhang: float,
        pitch: float,
        roof_thickness: float,
        slab: bool,
        slab_thickness: float,
        rvt_path: str | None,
        export_dir: str | None,
        exports: Sequence[str],
        stair_railings: bool,
        family_paths: Mapping[str, str],
    ) -> None:
        self.template = template
        self.horizun_version = horizun_version
        self.revit_year = revit_year
        self.roof = roof
        self.overhang = overhang
        self.pitch = pitch
        self.roof_thickness = roof_thickness
        self.slab = slab
        self.slab_thickness = slab_thickness
        self.exports = list(exports)
        self.stair_railings = stair_railings
        self.family_paths = dict(family_paths)
        self.inputs: dict[str, dict[str, Any]] = {}
        self.doc: Any = (
            rvt_path if rvt_path else self._input("rvt_path", "absolute path of the new .rvt (must not exist)")
        )
        self.export_dir = export_dir
        self.stages: list[dict[str, Any]] = []
        self.not_mapped: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.sources: dict[str, str] = {}  # discovery refs the plan needs -> how to pick them
        self.expected: dict[str, int] = {}
        self.storeys = [self._prepare(*s) for s in storeys]

    # --- small helpers ---

    def _input(self, name: str, what: str, optional: bool = False) -> dict[str, str]:
        entry = self.inputs.setdefault(name, {"what": what})
        if optional:
            entry["optional"] = True
        return {"$input": name}

    def _count(self, key: str, n: int = 1) -> None:
        self.expected[key] = self.expected.get(key, 0) + n

    def _source(self, name: str, how: str) -> dict[str, str]:
        self.sources.setdefault(name, how)
        return _ref(f"source:{name}")

    def _skip(self, storey: str, record: str, ids: list[str], why: str, suggest: str) -> None:
        if ids:
            self.not_mapped.append({"storey": storey, "record": record, "ids": ids, "why": why, "suggest": suggest})

    @staticmethod
    def _call(
        tool: str,
        args: dict[str, Any],
        *,
        write: bool = True,
        binds: dict[str, str] | None = None,
        note: str | None = None,
        when: str | None = None,
    ) -> dict[str, Any]:
        call: dict[str, Any] = {"tool": tool, "write": write, "args": args}
        if binds:
            call["binds"] = binds
        if when:
            call["when"] = when
        if note:
            call["note"] = note
        return call

    def _stage(self, sid: str, title: str, calls: list[dict[str, Any]]) -> None:
        if calls:
            self.stages.append({"id": sid, "title": title, "calls": calls})

    def _create(
        self, rows: list[tuple[str | None, dict[str, Any]]], name: str, note: str | None = None
    ) -> dict[str, Any]:
        """One atomic horizun_create_elements batch; rows are (ref or None, element)."""
        binds = {ref: f"rows[{i}]" for i, (ref, _) in enumerate(rows) if ref}
        args = {
            "target_document": self.doc,
            "units": "mm",
            "elements": [e for _, e in rows],
            "transaction_name": f"Plomada: {name}",
        }
        return self._call("horizun_create_elements", args, binds=binds, note=note)

    # --- storey preparation: walls as Revit segments ---

    def _prepare(self, plan: Plan, name: str, height: float, elevation: float) -> _Storey:
        closed = [w for w in plan.walls if w.closed]
        rings = {w.id: tessellate(segments_of(w.axis, True), True) for w in closed}
        envelopes = [
            w
            for w in closed
            if not any(o is not w and point_in_ring(tuple(w.axis[0][:2]), rings[o.id]) for o in closed)  # type: ignore[arg-type]
        ]
        env_rings = [rings[w.id] for w in envelopes]
        walls: list[_RWall] = []
        for w in plan.walls:
            axis = segments_of(w.axis, w.closed)
            shift = {"center": 0.0, "left": w.thickness / 2.0, "right": -w.thickness / 2.0}[w.justification]
            try:
                centre = offset_chain(axis, shift, w.closed)
            except ValueError as exc:
                raise ValueError(f"storey {name} wall {w.id}: {exc}") from None
            refs = (
                [f"wall:{name}:{w.id}"] if len(centre) == 1 else [f"wall:{name}:{w.id}#{i}" for i in range(len(centre))]
            )
            ext: list[bool] = []
            for s in centre:
                if w.closed:
                    ext.append(ring_area(w.axis) < 0)  # counter-clockwise ring: the outside is on the right
                    continue
                m = arc_midpoint(s)
                n = left_normal(s, m)
                probe = w.thickness / 2.0 + PROBE_MM
                in_l = any(point_in_ring(_add(m, n, probe), r) for r in env_rings)
                in_r = any(point_in_ring(_add(m, n, -probe), r) for r in env_rings)
                ext.append(not (in_l and not in_r))
            walls.append(_RWall(w, axis, centre, refs, shift, ext))
        return _Storey(plan, name, height, elevation, walls, envelopes)

    def _rwall(self, st: _Storey, wall_id: str) -> _RWall:
        return next(r for r in st.walls if r.wall.id == wall_id)

    def _outline(self, st: _Storey, env: Wall, extra: float) -> list[Pt]:
        """The outer face of a closed wall, pushed extra mm further out, as a point ring."""
        rw = self._rwall(st, env.id)
        out_left = rw.exterior_left[0]
        d = (rw.wall.thickness / 2.0 + extra) * (1.0 if out_left else -1.0)
        return tessellate(offset_chain(rw.centre, d, True), True)

    # --- stages ---

    def compile(self) -> dict[str, Any]:
        self._document()
        levels = self._levels()
        types = self._host_types()
        self._walls()
        self._opening_types()
        self._openings()
        self._floors()
        self._roofs()
        self._rooms()
        self._stairs(levels)
        self._others()
        self._discover()  # built last, inserted second: only the source types the plan uses
        self._verify()
        self._export(levels)
        self._unmapped_globals()
        return {
            "format": FORMAT,
            "horizun_version": self.horizun_version,
            "revit_year": self.revit_year,
            "template": self.template,
            "units": "mm",
            "storeys": [
                {"name": s.name, "elevation": _r(s.elevation), "height": _r(s.height), "level_ref": f"level:{s.name}"}
                for s in self.storeys
            ],
            "protocol": PROTOCOL,
            "inputs": self.inputs,
            "types": types,
            "stages": self.stages,
            "expected": self.expected,
            "not_mapped": self.not_mapped,
            "assumptions": ASSUMPTIONS,
            "warnings": self.warnings,
        }

    def _document(self) -> None:
        calls = [
            self._call("horizun_health", {}, write=False, note="First, always: which Revit and document is active."),
            self._call(
                "horizun_document_session",
                {"operation": "new_project", "template_path": self.template, "save_as_path": self.doc},
                note="Rehearses by default; apply with the token. The .rvt must not exist.",
            ),
            self._call(
                "horizun_document_session",
                {
                    "operation": "open",
                    "file_path": self.doc,
                    "expected_version": str(self.revit_year),
                    "dry_run": False,
                },
                write=False,
                when="new_project replied that the new document was NOT activated",
                note="Every later write requires target_document to be the ACTIVE document.",
            ),
        ]
        self._stage("document", "New project from the template", calls)

    def _discover(self) -> None:
        if not self.sources:
            return
        cats = sorted({CATEGORY[k.split(":")[0]] for k in self.sources})
        call = self._call(
            "horizun_query_model",
            {
                "target_document": self.doc,
                "categories": cats,
                "include_types": True,
                "response_mode": "compact",
                "max_rows": 500,
            },
            write=False,
            binds={f"source:{k}": v for k, v in sorted(self.sources.items())},
            note="Pick each source type from the reply. A missing door, window, column or stair family is loaded "
            "first with horizun_create_family source_path (see the optional calls in the types stages).",
        )
        self.stages.insert(1, {"id": "discover", "title": "Source types in the template", "calls": [call]})

    def _levels(self) -> dict[str, float]:
        levels = {s.name: s.elevation for s in self.storeys}
        top = max(self.storeys, key=lambda s: s.elevation)
        roof_elev = top.elevation + top.height
        needs_roof_level = self.roof != "none" or any(top.plan.stairs)
        if needs_roof_level and all(abs(e - roof_elev) > 1.0 for e in levels.values()):
            levels[ROOF_LEVEL] = roof_elev
        rows = [(f"level:{n}", {"kind": "level", "elevation": _r(e), "name": n}) for n, e in levels.items()]
        self._count("levels", len(rows))
        self._stage(
            "levels",
            "Levels",
            [
                self._create(
                    rows,
                    "levels",
                    note="The template already has its own levels (Default_M_ESP: Nivel 1, Nivel 2); they stay. "
                    "Delete or rename them after checking no view depends on them.",
                )
            ],
        )
        return levels

    def _host_types(self) -> dict[str, Any]:
        actions: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()

        def add(ref: str, action: dict[str, Any]) -> None:
            if ref not in seen:
                seen.add(ref)
                actions.append((ref, action))

        def layer(width: float, function: str = "Structure") -> dict[str, Any]:
            return {"layers": [{"function": function, "width": _r(width)}], "structural_layer_index": 0}

        for st in self.storeys:
            for rw in st.walls:
                w = rw.wall
                add(
                    self._wall_type(w),
                    {
                        "source_type_id": self._source("wall", PICK["wall"]),
                        "new_name": f"Plomada muro {fmt(w.thickness)} {w.material}",
                        "compound_structure": layer(w.thickness),
                    },
                )
            for cw in st.plan.curtain_walls:
                add(
                    f"type:curtain:{fmt(cw.mullion_spacing)}",
                    {
                        "source_type_id": self._source("curtain_wall", PICK["curtain_wall"]),
                        "new_name": f"Plomada muro cortina {fmt(cw.mullion_spacing)}",
                        "values": {"SPACING_LAYOUT_VERT": 1, "SPACING_LENGTH_VERT": f"{fmt(cw.mullion_spacing)} mm"},
                    },
                )
            for t in self._slab_thicknesses(st):
                add(
                    f"type:floor:{fmt(t)}",
                    {
                        "source_type_id": self._source("floor", PICK["floor"]),
                        "new_name": f"Plomada losa {fmt(t)}",
                        "compound_structure": layer(t),
                    },
                )
        if self.roof != "none" and any(self._roof_outlines()):
            add(
                "type:roof",
                {
                    "source_type_id": self._source("roof", PICK["roof"]),
                    "new_name": f"Plomada techo {fmt(self.roof_thickness)}",
                    "compound_structure": layer(self.roof_thickness),
                },
            )
        if actions:
            self._stage(
                "types",
                "Wall, curtain wall, floor and roof types",
                [
                    self._call(
                        "horizun_manage_system_types",
                        {
                            "target_document": self.doc,
                            "units": "mm",
                            "actions": [a for _, a in actions],
                            "transaction_name": "Plomada: host types",
                        },
                        binds={ref: f"rows[{i}]" for i, (ref, _) in enumerate(actions)},
                        note="Each type is a duplicate of a template type with ONE structure layer of the plan's "
                        "thickness; materials are not mapped (material_id -1 keeps the layer's default).",
                    )
                ],
            )
        return {ref: a["new_name"] for ref, a in actions}

    @staticmethod
    def _wall_type(w: Wall) -> str:
        return f"type:wall:{fmt(w.thickness)}:{w.material}"

    def _slab_thicknesses(self, st: _Storey) -> list[float]:
        if st.plan.slabs:
            return sorted({s.thickness for s in st.plan.slabs})
        return [self.slab_thickness] if self.slab and st.envelopes else []

    def _walls(self) -> None:
        calls = []
        for st in self.storeys:
            rows: list[tuple[str | None, dict[str, Any]]] = []
            for rw in st.walls:
                w = rw.wall
                for i, (s, ref) in enumerate(zip(rw.centre, rw.refs, strict=True)):
                    row: dict[str, Any] = {
                        "kind": "wall",
                        "start": _p3(s.a, st.elevation),
                        "end": _p3(s.b, st.elevation),
                        "level_id": _ref(f"level:{st.name}"),
                        "type_id": _ref(self._wall_type(w)),
                        "height": _r(w.height or st.height),
                    }
                    if s.arc:
                        row["arc"] = {
                            "centre": _p3(s.centre, st.elevation),
                            "radius": _r(s.radius),  # type: ignore[arg-type]
                            "clockwise": not s.ccw,
                        }
                    if not rw.exterior_left[i]:
                        row["flip"] = True
                    rows.append((ref, row))
            self._count("walls", len(rows))
            if rows:
                calls.append(self._create(rows, f"walls {st.name}"))
        self._stage("walls", "Walls (one Revit wall per segment, on the centreline)", calls)

    def _opening_type(self, o: Opening) -> tuple[str, float]:
        door = o.opening_kind == "door"
        height = o.height or (DOOR_HEIGHT_MM if door else WINDOW_HEIGHT_MM)
        return f"type:{o.opening_kind}:{fmt(o.width)}x{fmt(height)}", height

    def _opening_types(self) -> None:
        actions: list[tuple[str, dict[str, Any]]] = []
        seen: set[str] = set()
        for st in self.storeys:
            for o in st.plan.openings:
                ref, height = self._opening_type(o)
                if ref in seen:
                    continue
                seen.add(ref)
                door = o.opening_kind == "door"
                prefix = "DOOR" if door else "WINDOW"
                actions.append(
                    (
                        ref,
                        {
                            "source_type_id": self._source(o.opening_kind, PICK[o.opening_kind]),
                            "new_name": f"Plomada {'puerta' if door else 'ventana'} {fmt(o.width)}x{fmt(height)}",
                            "values": {
                                f"{prefix}_WIDTH": f"{fmt(o.width)} mm",
                                f"{prefix}_HEIGHT": f"{fmt(height)} mm",
                            },
                        },
                    )
                )
        if not actions:
            return
        kinds = sorted({ref.split(":")[1] for ref, _ in actions})
        calls = [self._family_load(k) for k in kinds]
        calls.append(
            self._call(
                "horizun_manage_system_types",
                {
                    "target_document": self.doc,
                    "units": "mm",
                    "actions": [a for _, a in actions],
                    "transaction_name": "Plomada: door and window types",
                },
                binds={ref: f"rows[{i}]" for i, (ref, _) in enumerate(actions)},
            )
        )
        self._stage("opening_types", "Door and window types", calls)

    def _family_load(self, kind: str) -> dict[str, Any]:
        return self._call(
            "horizun_create_family",
            {
                "target_document": self.doc,
                "source_path": self.family_paths.get(kind)
                or self._input(f"{kind}_rfa", f"an existing {kind} .rfa, if the template has none", optional=True),
                "load_into_project": True,
            },
            binds={f"source:{kind}": "the loaded family's first type"},
            when=f"the discover stage found no {kind} family in the template",
        )

    def _openings(self) -> None:
        calls = []
        for st in self.storeys:
            rows: list[tuple[str | None, dict[str, Any]]] = []
            for o in st.plan.openings:
                row = self._opening_row(st, o)
                if row is not None:
                    rows.append((f"opening:{st.name}:{o.id}", row))
                    self._count("doors" if o.opening_kind == "door" else "windows")
            self._skip(
                st.name,
                "openings.head",
                [o.id for o in st.plan.openings if o.head == "arch"],
                "Revit has no arched head on a rectangular door or window type; placed with a flat head at "
                "the full height",
                "load an arched family (or a wall_opening plus an in-place sweep) and swap the type",
            )
            if rows:
                calls.append(self._create(rows, f"openings {st.name}"))
        self._stage("openings", "Doors and windows hosted in their walls", calls)

    def _opening_row(self, st: _Storey, o: Opening) -> dict[str, Any] | None:
        rw = self._rwall(st, o.wall)
        lengths = [seg_length(s) for s in rw.axis]
        s_mid = o.offset + o.width / 2.0
        i, acc = 0, 0.0
        while i < len(lengths) and s_mid > acc + lengths[i] + EPS:
            acc += lengths[i]
            i += 1
        if i == len(lengths):
            self.warnings.append(
                f"storey {st.name} opening {o.id}: its centre ({fmt(round(s_mid, 1))} mm) is past the end of wall "
                f"{o.wall} ({fmt(round(sum(lengths), 1))} mm); not placed"
            )
            return None
        if o.offset < acc - EPS or o.offset + o.width > acc + lengths[i] + EPS:
            self.warnings.append(
                f"storey {st.name} opening {o.id} straddles a vertex of wall {o.wall}; Revit hosts it on segment {i} "
                "only, check it"
            )
        seg = rw.axis[i]
        p = point_at(seg, s_mid - acc)
        n = left_normal(seg, p)
        on_line = _add(p, n, rw.shift)
        door = o.opening_kind == "door"
        sill = o.sill if o.sill is not None else (0.0 if door else WINDOW_SILL_MM)
        if door:
            swing = n if o.swing == "in" else (-n[0], -n[1])
            facing = (-swing[0], -swing[1])  # a Revit door swings away from its facing (assumption)
        else:
            facing = n if rw.exterior_left[i] else (-n[0], -n[1])
        ref, _ = self._opening_type(o)
        row: dict[str, Any] = {
            "kind": "family_instance",
            "point": [_r(on_line[0]), _r(on_line[1]), _r(sill)],
            "coordinate_mode": "level_offset",
            "level_id": _ref(f"level:{st.name}"),
            "host_id": _ref(rw.refs[i]),
            "type_id": _ref(ref),
            "facing_degrees": _r(_deg(facing)),
        }
        if door and o.hand == "right":
            row["flip"] = True
        params: dict[str, Any] = {}
        if not door or sill > 0:
            params["INSTANCE_SILL_HEIGHT_PARAM"] = f"{fmt(sill)} mm"
        if o.tag:
            params["ALL_MODEL_MARK"] = o.tag
        if params:
            row["parameters"] = params
        if seg.arc and o.panel_width is not None:
            self.warnings.append(
                f"storey {st.name} opening {o.id}: panel_width is not mapped; Revit places one flat unit on the arc"
            )
        return row

    def _stair_wells(self, st: _Storey) -> list[list[Pt]]:
        idx = self.storeys.index(st)
        if idx == 0:
            return []
        below = self.storeys[idx - 1]
        return [stair_footprint(s) for s in below.plan.stairs]

    def _floors(self) -> None:
        calls = []
        for st in self.storeys:
            rows: list[tuple[str | None, dict[str, Any]]] = []
            for s in st.plan.slabs:
                z = st.elevation + s.top_offset
                profile = [[_p3(p, z) for p in tessellate(segments_of(s.outline, True), True)]]
                profile += [[_p3(p, z) for p in tessellate(segments_of(h, True), True)] for h in s.holes]
                rows.append(
                    (
                        f"floor:{st.name}:{s.id}",
                        {
                            "kind": "floor",
                            "profile": profile,
                            "level_id": _ref(f"level:{st.name}"),
                            "type_id": _ref(f"type:floor:{fmt(s.thickness)}"),
                        },
                    )
                )
            self._skip(
                st.name,
                "slabs.edge_band",
                [s.id for s in st.plan.slabs if s.edge_band is not None],
                "Horizun has no slab-edge (downstand) element",
                "a Revit Slab Edge on the floor's edge (horizun_execute_python) or a beam under it",
            )
            if not st.plan.slabs and self.slab:
                wells = self._stair_wells(st)
                for k, env in enumerate(st.envelopes):
                    ring = self._outline(st, env, 0.0)
                    holes = [h for h in wells if all(point_in_ring(p, ring) for p in h)]
                    if len(holes) < len(wells) and len(st.envelopes) == 1:
                        self.warnings.append(
                            f"storey {st.name}: a stair well from the storey below is not inside the slab outline; "
                            "cut it afterwards with a slab_opening or a shaft"
                        )
                    profile = [[_p3(p, st.elevation) for p in ring]]
                    profile += [[_p3(p, st.elevation) for p in h] for h in holes]
                    sid = "losa" if k == 0 else f"losa{k + 1}"
                    rows.append(
                        (
                            f"floor:{st.name}:{sid}",
                            {
                                "kind": "floor",
                                "profile": profile,
                                "level_id": _ref(f"level:{st.name}"),
                                "type_id": _ref(f"type:floor:{fmt(self.slab_thickness)}"),
                            },
                        )
                    )
            self._count("floors", len(rows))
            if rows:
                calls.append(self._create(rows, f"floors {st.name}"))
        self._stage("floors", "Floors (arc edges tessellated)", calls)

    def _roof_outlines(self) -> list[list[Pt]]:
        top = max(self.storeys, key=lambda s: s.elevation)
        return [self._outline(top, env, self.overhang) for env in top.envelopes]

    def _roofs(self) -> None:
        if self.roof == "none":
            return
        top = max(self.storeys, key=lambda s: s.elevation)
        rings = self._roof_outlines()
        if not rings:
            self.warnings.append(f"storey {top.name} has no closed exterior wall: no roof")
            return
        z = top.elevation + top.height
        level = (
            ROOF_LEVEL
            if all(abs(s.elevation - z) > 1.0 for s in self.storeys)
            else next(s.name for s in self.storeys if abs(s.elevation - z) <= 1.0)
        )
        rows: list[tuple[str | None, dict[str, Any]]] = []
        for k, ring in enumerate(rings):
            row: dict[str, Any] = {
                "kind": "roof",
                "profile": [[_p3(p, z) for p in ring]],
                "level_id": _ref(f"level:{level}"),
                "type_id": _ref("type:roof"),
            }
            kind = self.roof
            if kind == "gable" and len(ring) != 4:
                self.warnings.append("gable needs a rectangular footprint; this roof is built as hip")
                kind = "hip"
            if kind == "hip":
                row["slope_degrees"] = _r(self.pitch)
            elif kind == "gable":
                edges = [math.dist(ring[i], ring[(i + 1) % 4]) for i in range(4)]
                long_first = edges[0] + edges[2] >= edges[1] + edges[3]
                row["edge_slopes"] = [
                    {"defines_slope": True, "slope_degrees": _r(self.pitch)}
                    if (i % 2 == 0) == long_first
                    else {"defines_slope": False}
                    for i in range(4)
                ]
            rows.append((f"roof:{top.name}:{k}", row))
        self._count("roofs", len(rows))
        self._stage("roofs", f"Roof ({self.roof}) on the top storey", [self._create(rows, "roof")])

    def _rooms(self) -> None:
        calls = []
        for st in self.storeys:
            rows: list[tuple[str | None, dict[str, Any]]] = []
            for r in st.plan.rooms:
                row: dict[str, Any] = {
                    "kind": "room",
                    "point": [_r(r.at[0]), _r(r.at[1])],
                    "level_id": _ref(f"level:{st.name}"),
                    "name": r.name,
                }
                if r.number:
                    row["number"] = r.number
                rows.append((f"room:{st.name}:{r.id}", row))
            self._count("rooms", len(rows))
            if rows:
                calls.append(self._create(rows, f"rooms {st.name}"))
        self._stage("rooms", "Rooms (bounded by the walls just built)", calls)

    def _stairs(self, levels: dict[str, float]) -> None:
        calls = []
        ordered = sorted(levels.items(), key=lambda kv: kv[1])
        for st in self.storeys:
            for s in st.plan.stairs:
                above = [(n, e) for n, e in ordered if e > st.elevation + 1.0]
                if not above:
                    self.warnings.append(f"storey {st.name} stair {s.id}: no level above it; not built")
                    continue
                top_name, top_elev = above[0]
                rise = s.risers * s.riser_height
                if abs(rise - (top_elev - st.elevation)) > 1.0:
                    self.warnings.append(
                        f"storey {st.name} stair {s.id}: {s.risers} x {fmt(s.riser_height)} = {fmt(round(rise, 1))} "
                        f"mm but level {top_name} is {fmt(round(top_elev - st.elevation, 1))} mm up; Revit divides the "
                        "level height by the risers"
                    )
                runs, landings = stair_runs(s, st.elevation)
                if any(r["expected_risers"] < 2 for r in runs):
                    self.warnings.append(f"storey {st.name} stair {s.id}: a flight of one riser has no run; check it")
                row: dict[str, Any] = {
                    "kind": "stairs",
                    "level_id": _ref(f"level:{st.name}"),
                    "top_level_id": _ref(f"level:{top_name}"),
                    "type_id": self._source("stairs", PICK["stairs"]),
                    "desired_risers": s.risers,
                    "tread_depth": _r(s.going),
                    "runs": runs,
                }
                if landings:
                    row["landings"] = landings
                ref = f"stair:{st.name}:{s.id}"
                calls.append(
                    self._create(
                        [(ref, row)], f"stair {st.name} {s.id}", note="A stair must be the only element of its batch."
                    )
                )
                self._count("stairs")
                if self.stair_railings:
                    calls.append(
                        self._call(
                            "horizun_create_railing",
                            {
                                "target_document": self.doc,
                                "units": "mm",
                                "type_id": self._source("railing", PICK["railing"]),
                                "host_id": _ref(ref),
                                "placement": "treads",
                            },
                            binds={f"railing:{ref}": "created_ids"},
                            note="Revit places one railing per side of the stair.",
                        )
                    )
        self._stage("stairs", "Stairs (one batch each) and their railings", calls)

    def _others(self) -> None:
        calls: list[dict[str, Any]] = []
        col_types: list[tuple[str, dict[str, Any]]] = []
        col_calls: list[dict[str, Any]] = []
        for st in self.storeys:
            slab_top = {s.id: s.top_offset for s in st.plan.slabs}
            for rl in st.plan.railings:
                pts = tessellate(segments_of(rl.path, rl.closed), rl.closed)
                if rl.closed:
                    pts.append(pts[0])  # Horizun takes an open path: a closed railing returns to its start
                if len(pts) > RAILING_MAX_POINTS:
                    self.warnings.append(f"storey {st.name} railing {rl.id}: {len(pts)} points, Horizun takes 200")
                args: dict[str, Any] = {
                    "target_document": self.doc,
                    "units": "mm",
                    "type_id": self._source("railing", PICK["railing"]),
                    "path": [_p3(p, st.elevation) for p in pts[:RAILING_MAX_POINTS]],
                    "level_id": _ref(f"level:{st.name}"),
                }
                if rl.on is not None and slab_top.get(rl.on):
                    args["base_offset"] = _r(slab_top[rl.on])
                calls.append(
                    self._call("horizun_create_railing", args, binds={f"railing:{st.name}:{rl.id}": "created_ids"})
                )
                self._count("railings")
            if st.plan.railings:
                self._skip(
                    st.name,
                    "railings.style/height/inset",
                    [r.id for r in st.plan.railings],
                    "a Revit railing takes its look and height from its type; the path is used as drawn (no inset)",
                    "duplicate a railing type per style and height (glass panel, bars, solid) and swap it",
                )
            rows: list[tuple[str | None, dict[str, Any]]] = []
            bad: list[str] = []
            for c in st.plan.columns:
                spec = column_spec(c)
                if spec is None:
                    bad.append(c.id)
                    continue
                ref_t, (cx, cy), rot, values, kind = spec
                if ref_t not in {r for r, _ in col_types}:
                    col_types.append(
                        (
                            ref_t,
                            {
                                "source_type_id": self._source(f"column_{kind}", PICK[f"column_{kind}"]),
                                "new_name": "Plomada pilar " + ref_t.split(":", 2)[2],
                                "values": values,
                            },
                        )
                    )
                row: dict[str, Any] = {
                    "kind": "structural_column",
                    "point": [_r(cx), _r(cy), 0.0],
                    "coordinate_mode": "level_offset",
                    "level_id": _ref(f"level:{st.name}"),
                    "type_id": _ref(ref_t),
                    "height": _r(c.height or st.height),
                }
                if abs(rot) > 1e-9:
                    row["rotation_degrees"] = _r(rot)
                rows.append((f"column:{st.name}:{c.id}", row))
            self._skip(
                st.name,
                "columns",
                bad,
                "only round and rectangular columns map to a column family",
                "model it as a floor-like extrusion (in-place family) or a profiled wall_profile",
            )
            self._count("columns", len(rows))
            if rows:
                col_calls.append(self._create(rows, f"columns {st.name}"))
            cw_rows: list[tuple[str | None, dict[str, Any]]] = []
            grid: list[dict[str, Any]] = []
            for cw in st.plan.curtain_walls:
                head = cw.head or st.height
                segs = segments_of(cw.path, cw.closed)
                refs = (
                    [f"curtain:{st.name}:{cw.id}"]
                    if len(segs) == 1
                    else [f"curtain:{st.name}:{cw.id}#{i}" for i in range(len(segs))]
                )
                for s, ref in zip(segs, refs, strict=True):
                    z = st.elevation + cw.sill
                    row = {
                        "kind": "wall",
                        "start": _p3(s.a, z),
                        "end": _p3(s.b, z),
                        "level_id": _ref(f"level:{st.name}"),
                        "type_id": _ref(f"type:curtain:{fmt(cw.mullion_spacing)}"),
                        "height": _r(head - cw.sill),
                        "offset": _r(cw.sill),
                    }
                    if s.arc:
                        row["arc"] = {"centre": _p3(s.centre, z), "radius": _r(s.radius), "clockwise": not s.ccw}  # type: ignore[arg-type]
                    cw_rows.append((ref, row))
                    for t in cw.transoms:
                        if cw.sill < t < head:
                            grid.append(
                                self._call(
                                    "horizun_manage_curtain",
                                    {
                                        "target_document": self.doc,
                                        "units": "mm",
                                        "operation": "add_grid_line",
                                        "element_id": _ref(ref),
                                        "grid_index": 0,
                                        "direction": "u",
                                        "offset": _r(t - cw.sill),
                                    },
                                )
                            )
            if st.plan.curtain_walls:
                self._skip(
                    st.name,
                    "curtain_walls.profile/glass",
                    [c.id for c in st.plan.curtain_walls],
                    "mullion section and glass thickness belong to the curtain wall type",
                    "set the type's mullion and panel types (horizun_manage_curtain set_mullions / set_panel_type)",
                )
            self._count("curtain_walls", len(cw_rows))
            if cw_rows:
                calls.append(self._create(cw_rows, f"curtain walls {st.name}"))
                calls.extend(grid)
        if col_types:
            kinds = sorted({a["source_type_id"]["$ref"].split(":")[1] for _, a in col_types})
            pre = [self._family_load(k) for k in kinds]
            pre.append(
                self._call(
                    "horizun_manage_system_types",
                    {
                        "target_document": self.doc,
                        "units": "mm",
                        "actions": [a for _, a in col_types],
                        "transaction_name": "Plomada: column types",
                    },
                    binds={ref: f"rows[{i}]" for i, (ref, _) in enumerate(col_types)},
                    note="b / h are the dimension parameters of the metric concrete column families; check the names "
                    "of the family actually loaded.",
                )
            )
            col_calls = pre + col_calls
        self._stage("railings_columns_curtain", "Railings, columns and curtain walls", calls + col_calls)

    def _verify(self) -> None:
        cats = [CATEGORY_OF_COUNT[k] for k in self.expected if k in CATEGORY_OF_COUNT]
        calls = [
            self._call(
                "horizun_verify_changes",
                {"target_document": self.doc, "scope": "session", "capture": True, "max_findings": 200},
                write=False,
                note="Look at the image; do not report the model as done while errors remain.",
            ),
            self._call(
                "horizun_query_model",
                {"target_document": self.doc, "categories": sorted(set(cats)), "group_by": ["category", "level"]},
                write=False,
                note="Compare the counts with expected.",
            ),
            self._call(
                "horizun_document_session",
                {"operation": "save", "target_document": self.doc},
                write=False,
                note="save does not rehearse by default.",
            ),
        ]
        self._stage("verify", "Verify and save", calls)

    def _export(self, levels: dict[str, float]) -> None:
        if not self.exports:
            return
        folder = self.export_dir or self._input("export_dir", "absolute folder for the DWG, IFC and FBX files")

        def out(ext: str) -> Any:
            if isinstance(folder, str):
                return str(PureWindowsPath(folder) / f"plomada.{ext}")
            return {"$input": "export_dir", "file": f"plomada.{ext}"}

        actions = [
            {
                "operation": "create_floor_plan",
                "key": f"plan_{s.name}",
                "level_id": _ref(f"level:{s.name}"),
                "name": f"Plomada {s.name}",
            }
            for s in self.storeys
        ]
        actions.append({"operation": "create_3d", "key": "3d", "name": "Plomada 3D"})
        binds = {f"view:plan:{s.name}": f"key plan_{s.name}" for s in self.storeys}
        binds["view:3d"] = "key 3d"
        calls = [
            self._call(
                "horizun_manage_views", {"target_document": self.doc, "units": "mm", "actions": actions}, binds=binds
            )
        ]
        if "dwg" in self.exports:
            calls.append(
                self._call(
                    "horizun_export",
                    {
                        "target_document": self.doc,
                        "format": "dwg",
                        "output_path": out("dwg"),
                        "view_ids": [_ref(f"view:plan:{s.name}") for s in self.storeys],
                        "file_naming": "view_name",
                    },
                    note="One DWG per plan view.",
                )
            )
        if "ifc" in self.exports:
            calls.append(
                self._call(
                    "horizun_export",
                    {
                        "target_document": self.doc,
                        "format": "ifc",
                        "output_path": out("ifc"),
                        "ifc_version": "IFC4",
                    },
                )
            )
        if "fbx" in self.exports:
            calls.append(
                self._call(
                    "horizun_export",
                    {
                        "target_document": self.doc,
                        "format": "fbx",
                        "output_path": out("fbx"),
                        "view_ids": [_ref("view:3d")],
                    },
                )
            )
        self._stage("exports", "Views and exports", calls)

    def _unmapped_globals(self) -> None:
        for st in self.storeys:
            p = st.plan
            self._skip(
                st.name,
                "furniture",
                [f.id for f in p.furniture],
                "catalogue blocks have no Revit family behind them",
                "load matching .rfa families (horizun_create_family source_path) and place them as "
                "family_instance rows; at is the back-left corner, rotation degrees CCW",
            )
            site = p.site
            self._skip(
                st.name,
                "site.objects",
                [o.id for o in site.objects],
                "trees, cars and people are planting / entourage families",
                "place Planting / Entourage families as family_instance rows",
            )
            self._skip(
                st.name,
                "site.paving/decks",
                [x.id for x in site.paving + site.decks],
                "site surfaces are not part of the building",
                "floors on a site level, or toposolid sub-regions",
            )
            self._skip(
                st.name,
                "site.pools",
                [x.id for x in site.pools],
                "a pool is a void in the ground",
                "a toposolid (create_elements kind toposolid) cut by a floor with a pool family, or link the "
                "Plomada model exported from SketchUp",
            )
            self._skip(
                st.name,
                "site.fences",
                [x.id for x in site.fences],
                "fences are site walls",
                "walls of a thin type on a site level, or railings with a fence type",
            )
            self._skip(
                st.name,
                "pools",
                [x.id for x in p.pools],
                "a recessed or raised basin has no typed element",
                "a floor with a slab_opening plus thin walls, or a pool family",
            )
            self._skip(
                st.name,
                "planters",
                [x.id for x in p.planters],
                "curb, soil and plants have no typed element",
                "thin walls for the curb plus a floor for the soil, then Planting families",
            )
            self._skip(
                st.name,
                "ramps",
                [x.id for x in p.ramps],
                "Horizun has no typed ramp",
                "a sloped floor (horizun_slab_shape modify_subelement) or a Revit Ramp through horizun_execute_python",
            )
        mats = sorted({rw.wall.material for st in self.storeys for rw in st.walls})
        if mats:
            self.not_mapped.append(
                {
                    "storey": None,
                    "record": "materials",
                    "ids": mats,
                    "why": "types are duplicated with the template's default layer material",
                    "suggest": "create or pick Revit materials (horizun_manage_materials) and set each layer's "
                    "material_id in the types stage",
                }
            )


# --- stairs and columns ------------------------------------------------------------------


def _stair_frame(s: Stair) -> Any:
    a = math.radians(s.direction_deg)
    sx, sy = s.start

    def world(x: float, y: float) -> Pt:
        y = -y if s.turn == "right" else y
        return (sx + x * math.cos(a) - y * math.sin(a), sy + x * math.sin(a) + y * math.cos(a))

    return world


def stair_footprint(s: Stair) -> list[Pt]:
    """The plan outline of a stair (flights and landing), world coordinates, counter-clockwise, as the
    extension cuts it out of the slab above (geometry/stairs.rb)."""
    w, g, n = s.width, s.going, s.risers
    h = w / 2.0
    if s.stair_kind == "straight":
        local = [(0.0, -h), ((n - 1) * g, -h), ((n - 1) * g, h), (0.0, h)]
    else:
        first = (n + 1) // 2
        second = n - first
        landing = (first - 1) * g
        if s.stair_kind == "l":
            top = h + (second - 1) * g
            local = [(0.0, -h), (landing + w, -h), (landing + w, top), (landing, top), (landing, h), (0.0, h)]
        else:
            x0 = min(0.0, landing - (second - 1) * g)
            local = [(x0, -h), (landing + w, -h), (landing + w, 3 * h), (x0, 3 * h)]
    world = _stair_frame(s)
    ring = [world(x, y) for x, y in local]
    return ring if signed_area(ring) > 0 else ring[::-1]


def stair_runs(s: Stair, elevation: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Horizun stairs runs (centre-justified paths, absolute XYZ at each run's base) and landings."""
    w, g, n, r = s.width, s.going, s.risers, s.riser_height
    h = w / 2.0
    world = _stair_frame(s)

    def run(a: Pt, b: Pt, risers: int, z: float) -> dict[str, Any]:
        return {"start": _p3(world(*a), z), "end": _p3(world(*b), z), "width": _r(w), "expected_risers": risers}

    if s.stair_kind == "straight":
        return [run((0.0, 0.0), ((n - 1) * g, 0.0), n, elevation)], []
    first = (n + 1) // 2
    second = n - first
    landing = (first - 1) * g
    z_land = elevation + first * r
    runs = [run((0.0, 0.0), (landing, 0.0), first, elevation)]
    if s.stair_kind == "l":
        rect = [(landing, -h), (landing + w, -h), (landing + w, h), (landing, h)]
        runs.append(run((landing + h, h), (landing + h, h + (second - 1) * g), second, z_land))
    else:
        rect = [(landing, -h), (landing + w, -h), (landing + w, 3 * h), (landing, 3 * h)]
        runs.append(run((landing, 2 * h), (landing - (second - 1) * g, 2 * h), second, z_land))
    ring = [world(*p) for p in rect]
    if signed_area(ring) < 0:
        ring.reverse()
    return runs, [{"profile": [[_p3(p, z_land) for p in ring]]}]


def column_spec(c: Any) -> tuple[str, Pt, float, dict[str, str], str] | None:
    """(type ref, centre, rotation degrees, type values, round|rect) for a round or rectangular column."""
    if c.outline is None:
        d = fmt(c.diameter)
        return f"type:column:D{d}", (c.at[0], c.at[1]), 0.0, {"b": f"{d} mm"}, "round"
    pts = [tuple(v[:2]) for v in c.outline]
    if len(pts) != 4 or any(len(v) > 2 for v in c.outline):
        return None
    edges = [_sub(pts[(i + 1) % 4], pts[i]) for i in range(4)]
    for i in range(4):
        e, f = _unit(edges[i]), _unit(edges[(i + 1) % 4])
        if abs(e[0] * f[0] + e[1] * f[1]) > 1e-6:
            return None
    b, hh = math.hypot(*edges[0]), math.hypot(*edges[1])
    cx = sum(p[0] for p in pts) / 4.0
    cy = sum(p[1] for p in pts) / 4.0
    rot = math.degrees(math.atan2(edges[0][1], edges[0][0]))
    bs, hs = fmt(round(b, 1)), fmt(round(hh, 1))
    return f"type:column:{bs}x{hs}", (cx, cy), rot, {"b": f"{bs} mm", "h": f"{hs} mm"}, "rect"


# --- tables ---------------------------------------------------------------------------------

CATEGORY = {
    "wall": "OST_Walls",
    "curtain_wall": "OST_Walls",
    "floor": "OST_Floors",
    "roof": "OST_Roofs",
    "door": "OST_Doors",
    "window": "OST_Windows",
    "stairs": "OST_Stairs",
    "railing": "OST_StairsRailing",
    "column_round": "OST_StructuralColumns",
    "column_rect": "OST_StructuralColumns",
}

CATEGORY_OF_COUNT = {
    "levels": "OST_Levels",
    "walls": "OST_Walls",
    "curtain_walls": "OST_Walls",
    "doors": "OST_Doors",
    "windows": "OST_Windows",
    "floors": "OST_Floors",
    "roofs": "OST_Roofs",
    "rooms": "OST_Rooms",
    "stairs": "OST_Stairs",
    "railings": "OST_StairsRailing",
    "columns": "OST_StructuralColumns",
}

PICK = {
    "wall": "a Basic WallType (Kind Basic), e.g. a generic one",
    "curtain_wall": "a Curtain WallType (Kind Curtain)",
    "floor": "an architectural FloorType",
    "roof": "a basic RoofType",
    "door": "a single-leaf door FamilySymbol (OST_Doors)",
    "window": "a fixed or casement window FamilySymbol (OST_Windows)",
    "stairs": "an assembled or monolithic StairsType",
    "railing": "a RailingType",
    "column_round": "a round concrete structural column FamilySymbol",
    "column_rect": "a rectangular concrete structural column FamilySymbol",
}

PROTOCOL = [
    "Run the stages in order and the calls of a stage in order; a call with 'when' only when its condition holds.",
    "Before sending a call, replace every {'$ref': name} with the integer id an earlier call bound to that name, "
    "and every {'$input': name} with the caller's value (see inputs); {'$input': 'export_dir', 'file': f} is "
    "that folder joined with f.",
    "write=true: send the args as given (Horizun rehearses: dry_run defaults to true), read confirmation_token, "
    "then send the same args plus dry_run=false and confirmation_token. write=false: send once.",
    "binds maps ref -> where its id is in the reply: rows[i] is reply row i (rows in request order); "
    "created_ids is the railing ids; 'key k' is the view created by the action with key k; anything else says "
    "what to pick from the reply.",
    "A refusal names the failing row and nothing was written: fix that row (or drop it and tell the user) and "
    "rehearse again. Never fall back to horizun_execute_python for a row a typed call refused for bad arguments.",
]

ASSUMPTIONS = [
    "Wall.Create puts a wall's exterior on the LEFT of start->end; flip=true marks walls whose exterior is on the "
    "right.",
    "A door swings away from its FacingOrientation (facing_degrees = opposite of the swing side), and flip=true "
    "(flipHand) moves the hinge to the other jamb for hand=right.",
    "A hosted window takes its sill from INSTANCE_SILL_HEIGHT_PARAM and from the point's level offset alike.",
    "DOOR_WIDTH / DOOR_HEIGHT and WINDOW_WIDTH / WINDOW_HEIGHT are the type parameters of the template's door and "
    "window families.",
    "SPACING_LAYOUT_VERT = 1 (fixed distance) and SPACING_LENGTH_VERT space a curtain wall type's vertical grid.",
    "b (diameter / width) and h are the column families' type dimensions.",
    "Level elevations equal internal-origin Z (the template's project base point sits on the internal origin).",
    "Revit auto-joins the walls' corners within Horizun's join allowance (one wall thickness) without failing the "
    "location postcondition; otherwise send join_rule none and accept unjoined corners.",
]


# --- entry point ----------------------------------------------------------------------------


def _template_path(template: str, year: int) -> str:
    p = PureWindowsPath(template)
    return template if p.is_absolute() else str(PureWindowsPath(TEMPLATE_DIR.format(year=year)) / template)


def plan_to_revit(
    plan: Any,
    *,
    template: str = DEFAULT_TEMPLATE,
    horizun_version: str = HORIZUN_SCHEMA_VERSION,
    revit_year: int = 2027,
    roof: RoofKind = "flat",
    overhang: float = CONFIG.overhang_mm,
    pitch: float = CONFIG.gable_pitch_deg,
    roof_thickness: float = CONFIG.roof_thickness_mm,
    slab: bool = True,
    slab_thickness: float = CONFIG.slab_thickness_mm,
    rvt_path: str | None = None,
    export_dir: str | None = None,
    exports: Sequence[str] = ("dwg", "ifc", "fbx"),
    stair_railings: bool = True,
    family_paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compiles a plan (a Plan, a plan dict, a RevitStorey or a list of them, one per storey, in build
    order) into ordered stages of Horizun Revit MCP calls. family_paths maps door, window, column_round and
    column_rect to an .rfa loaded when the template lacks that family. Pure: reads no file and calls nothing.
    Raises ValueError for a plan that cannot be compiled."""
    if roof not in ("flat", "gable", "hip", "none"):
        raise ValueError(f"roof must be flat, gable, hip or none, got {roof!r}")
    unknown = sorted(set(exports) - {"dwg", "ifc", "fbx"})
    if unknown:
        raise ValueError(f"exports takes dwg, ifc and fbx, got {', '.join(unknown)}")
    storeys = _resolve(_storeys(plan))
    compiler = _Compiler(
        storeys,
        template=_template_path(template, revit_year),
        horizun_version=horizun_version,
        revit_year=revit_year,
        roof=roof,
        overhang=overhang,
        pitch=pitch,
        roof_thickness=roof_thickness,
        slab=slab,
        slab_thickness=slab_thickness,
        rvt_path=rvt_path,
        export_dir=export_dir,
        exports=exports,
        stair_railings=stair_railings,
        family_paths=family_paths or {},
    )
    out = compiler.compile()
    if horizun_version != HORIZUN_SCHEMA_VERSION:
        out["warnings"].insert(
            0,
            f"these calls follow the Horizun {HORIZUN_SCHEMA_VERSION} schemas; check them against "
            f"horizun://contract/tools/<tool> on {horizun_version}",
        )
    return out
