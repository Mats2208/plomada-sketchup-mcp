"""Infers a draft plan from an ordinary architectural DXF: linework only, no records.

Most drawings carry no ACADMCP_ARCH or PLOMADA data: walls are two parallel lines (or a closed
outline, or a hatch boundary), a door is a leaf and a swing arc or a block, a window is a few
lines across a gap or a block, a room is a TEXT. This module reads that linework and proposes a
``Plan`` plus a report of everything it was not sure about. It never reads XDATA, and it never
invents silently: what it assumes is listed, what it could not classify is listed, what it threw
away is listed with the reason.

The method, in order:

1. Load. Model space is read with every INSERT exploded virtually (nested, with its transform);
   an entity on layer 0 inside a block takes the insert's layer, and the outermost block name
   travels with it, so a door or window block is recognised by name whatever layer its lines are
   on. LWPOLYLINE and POLYLINE become lines and arcs, HATCH and SOLID boundaries too (marked as
   hatch, used as evidence but never counted as unpaired). Units come from ``$INSUNITS`` unless
   ``units`` is given; when the header says one unit but the walls only pair in another (an
   ezdxf file left at its metre default, say), the unit the walls pair in wins and the report
   says so.
2. Layer roles. An explicit mapping names the layers of each role (case-insensitive, ``*``
   wildcards); a role it leaves out is found by layer-name tokens (MURO, PARED, TABIQUE, WALL;
   PUERTA, DOOR; VENTANA, WINDOW, WIND, GLAZ; TEXTO, AMBIENTE, ROOM, AREA; ignored: COTA, DIM,
   EJE, GRID, AXIS, SYMB, ANNO...). With no wall layer by name, the layers whose lines pair as
   wall faces are the walls.
3. Faces and pairs. Collinear wall lines that touch are fused into face lines. Two face lines
   pair when they are parallel within 1 degree, their gap is within the thickness range and
   changes by at most 2 mm over their overlap, they overlap by at least ``min_overlap`` and at
   least their gap (a wall is longer than it is thick, so the two jamb lines of a short pier do
   not pair), and each is the other's nearest face on that side over that stretch. The midline is
   an axis piece and the gap its thickness, snapped to 5 mm when within 1 mm of a multiple.
   Concentric arcs pair the same way into curved pieces.
4. Runs and gaps. Pieces on one axis line (or circle) of one thickness merge into runs. A gap
   between two pieces closes when a face line runs across it (a T stem interrupts only the near
   face), when a crossing wall fills it, or when it is shorter than ``gap_min``. A gap from
   ``gap_min`` to ``max_opening`` is an opening when something in it says so: a swing arc hinged
   at a jamb with a radius within 20 % of the gap (a door: swing and hand from the arc), a door
   or window block or layer, or a glass line inside the wall (a window). A gap up to ``gap_max``
   with no evidence is unclassified and handled by ``default_opening``; wider ones split the run.
   A window drawn inside a continuous wall (window lines between unbroken faces) is an opening
   too. Free run ends facing another wall across a gap get the same treatment.
5. Junctions. Run ends are extended or trimmed to the axis of the wall they meet: two ends whose
   axes cross within half the other's thickness (plus 20 mm) form an L at the crossing; an end
   within reach of another wall's axis is a T stem ending on that axis; a curve continuing a run
   tangentially is joined in line. Runs of one thickness that meet in an L are chained into one
   polyline, closed when the chain returns. Where three or more ends meet, two of one thickness
   are chained and the others tee into the vertex; with no such pair the extras are pulled back
   and reported. A run that touches no other wall (a wardrobe drawn on the wall layer) is dropped
   and reported. Openings that would run into a junction are dropped and reported.
6. Rooms. TEXT, MTEXT and block attributes on room layers (any text inside the walls when no
   room layer exists) that hold letters become rooms at their insertion point; a number like
   ``01`` and an area like ``12.5 m2`` close below it become its number and area (mm2).
7. Output. Ids are W1.., D1.. (doors), V1.. (windows), R1.., ordered by position (walls by
   their first vertex, bottom-left first; openings by wall then offset; rooms bottom-left
   first), so the same drawing always gives the same plan. The plan passes ``parse_plan``: a wall
   that does not is dropped and reported.
"""

from __future__ import annotations

import bisect
import fnmatch
import itertools
import math
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import ezdxf
from ezdxf.document import Drawing
from pydantic import BaseModel, ConfigDict, ValidationError

from .cadfile import as_dxf
from .dxf import DxfPlanError
from .models import Opening, Plan, Room, Wall, describe_error, parse_plan, polyline_length, ring_area

Vec = tuple[float, float]

ANGLE_TOL = math.radians(1.0)  # face lines this close in direction are parallel
DRIFT_MM = 3.0  # paired faces may diverge this much over their overlap
FUSE_ANGLE = math.radians(0.1)  # collinear wall lines that fuse into one face
FUSE_MM = 0.5
CENTRE_TOL_MM = 2.0  # concentric arcs
AXIS_ANGLE = math.radians(1.0)  # pieces on one axis line
AXIS_TOL_MM = 5.0
THICK_TOL_MM = 3.0  # pieces of one run differ at most this in thickness
TOUCH_MM = 1.0  # pieces this close along an axis touch
SNAP_STEP_MM = 5.0
SNAP_WITHIN_MM = 1.0
JUNCTION_TOL_MM = 20.0  # an end may stop this far short of (or run past) a face and still join
MIN_JUNCTION_DEG = 5.0  # the solver's minimum junction angle
SMOOTH_MM = 5.0  # a tangent continuation leaves at most this between the two ends
SMOOTH_DEG = 3.0
FACE_TOL_MM = 2.0  # a line this close to a face line lies on it
JAMB_REACH_MM = 60.0  # a hinge this far off its jamb or face still belongs to the door
DOOR_RADIUS = (0.8, 1.2)  # swing radius over gap width
OPENING_GUARD_MM = 0.5  # openings stay this far from a junction
LIST_MAX = 20  # longest lists in the report

UNIT_MM: dict[str, float] = {"mm": 1.0, "cm": 10.0, "dm": 100.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
INSUNITS = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m", 14: "dm"}
SCALES = (1.0, 10.0, 1000.0, 25.4, 304.8, 100.0)

ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "ignore": ("COTA", "DIM", "EJE", "GRID", "AXIS", "DEFPOINTS", "ANNO", "SYMB", "NOTA", "TITLE", "ROTULO", "VPORT"),
    "door": ("PUERTA", "DOOR", "PORTON"),
    "window": ("VENTANA", "WINDOW", "WIND", "GLAZ", "VIDRIO", "CRISTAL"),
    "wall": ("MURO", "PARED", "TABIQ", "WALL", "MAMPOST"),
    "room": ("TEXTO", "TEXT", "AMBIENTE", "ROOM", "AREA", "LOCAL", "ESPACIO", "RECINTO"),
    "other": ("MOBIL", "MUEBLE", "FURN", "EQUIP", "SANR", "SANIT", "STAIR", "ESCAL", "SITIO", "PAISAJ", "PLANT"),
}
FURNITURE_BLOCK = re.compile(
    r"^(ARCH_|SITIO_)|MUEBLE|MOBIL|FURN|CAMA|BED|SOFA|MESA|TABLE|SILLA|CHAIR|INODORO|WC|LAVA|DUCHA|BANERA|BATH|"
    r"ROPERO|ARMARIO|CLOSET|WARDROBE|PLACARD|ESTANTE|SHELF|MESADA|COUNTER|ARBOL|TREE|AUTO|CAR\b",
    re.IGNORECASE,
)
AREA_TEXT = re.compile(
    r"^(?:(?:AREA|ÁREA|SUP(?:ERFICIE)?|S)\s*[:=]?\s*)?(\d+(?:[.,]\d+)?)\s*(?:m2|m²|m\^2|mt2|mts2|m\s*2)\.?$",
    re.IGNORECASE,
)
NUMBER_TEXT = re.compile(r"^[A-Z]?\d{1,3}[A-Z]?$", re.IGNORECASE)
DIMENSION_TEXT = re.compile(r"^[\d\s.,x\u00d7/+\-=%:()]+$", re.IGNORECASE)


class InferError(ValueError):
    """The DXF cannot be read, or holds nothing that pairs as walls; the message says why."""


class LayerRoles(BaseModel):
    """Layers of each role, case-insensitive, with * and ? wildcards. A role left out (null) is found by
    layer-name tokens; a layer named here takes no other role."""

    model_config = ConfigDict(extra="forbid")
    walls: list[str] | None = None
    doors: list[str] | None = None
    windows: list[str] | None = None
    rooms: list[str] | None = None
    ignore: list[str] | None = None


UnitName = Literal["auto", "mm", "cm", "dm", "m", "in", "ft"]
OpeningPolicy = Literal["door", "window", "skip"]


@dataclass(frozen=True)
class InferOptions:
    """Every tolerance the reader exposes; lengths in mm."""

    units: UnitName = "auto"
    layers: LayerRoles | None = None
    thickness_min: float = 60.0
    thickness_max: float = 600.0
    min_overlap: float = 150.0
    gap_min: float = 500.0
    gap_max: float = 4000.0
    max_opening: float = 8000.0
    default_opening: OpeningPolicy = "skip"
    door_height: float = 2100.0
    window_sill: float = 900.0
    window_height: float = 1200.0


@dataclass
class InferResult:
    plan: Plan
    report: dict[str, Any]

    def wire(self) -> dict[str, Any]:
        """{"plan": the JSON build_plan takes, "report": ...}."""
        return {"plan": self.plan.to_wire(), "report": self.report}


# --- vectors ----------------------------------------------------------------------------


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def _mul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: Vec, b: Vec) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _dir(angle: float) -> Vec:
    return (math.cos(angle), math.sin(angle))


def _wrap(angle: float) -> float:
    """The angle in (-pi, pi]."""
    a = math.fmod(angle + math.pi, math.tau)
    if a <= 0:
        a += math.tau
    return a - math.pi


def _canon(d: Vec) -> float:
    """Direction angle of a line in [-ANGLE_TOL, pi - ANGLE_TOL): one value for both senses."""
    phi = math.atan2(d[1], d[0]) % math.pi
    return phi - math.pi if phi >= math.pi - ANGLE_TOL else phi


def _angle_diff(a: float, b: float) -> float:
    """Difference of two line directions, mod pi."""
    d = abs(a - b) % math.pi
    return min(d, math.pi - d)


def _r(value: float) -> float:
    return round(value, 1) + 0.0


def _pt(p: Vec) -> list[float]:
    return [_r(p[0]), _r(p[1])]


def _union_length(intervals: list[tuple[float, float]]) -> float:
    total, end = 0.0, -math.inf
    for lo, hi in sorted(intervals):
        if hi <= end:
            continue
        total += hi - max(lo, end)
        end = hi
    return total


class _Grid:
    """Items by bounding box on a uniform grid; query returns those sharing a cell with a box, in the order
    they were added (so every scan stays deterministic). A box over 4096 cells goes to a list every query
    returns."""

    def __init__(self, cell: float = 2000.0) -> None:
        self.cell = cell
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        self.big: list[int] = []
        self.items: list[Any] = []

    def _span(self, box: tuple[float, float, float, float]) -> tuple[range, range]:
        c = self.cell
        return range(math.floor(box[0] / c), math.floor(box[2] / c) + 1), range(
            math.floor(box[1] / c), math.floor(box[3] / c) + 1
        )

    def add(self, box: tuple[float, float, float, float], item: Any) -> None:
        k = len(self.items)
        self.items.append(item)
        xs, ys = self._span(box)
        if len(xs) * len(ys) > 4096:
            self.big.append(k)
            return
        for i in xs:
            for j in ys:
                self.cells[(i, j)].append(k)

    def query(self, box: tuple[float, float, float, float]) -> list[Any]:
        xs, ys = self._span(box)
        if len(xs) * len(ys) > 4096:
            return list(self.items)
        found = set(self.big)
        for i in xs:
            for j in ys:
                found.update(self.cells.get((i, j), ()))
        return [self.items[k] for k in sorted(found)]


def _box(points: list[Vec], grow: float = 0.0) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow


# --- primitives -------------------------------------------------------------------------


@dataclass(eq=False)
class _Line:
    a: Vec
    b: Vec
    layer: str
    block: str | None = None
    block_role: str | None = None
    hatch: bool = False
    role: str = "other"

    @property
    def length(self) -> float:
        return math.dist(self.a, self.b)


@dataclass(eq=False)
class _Arc:
    c: Vec
    r: float
    t0: float  # radians; the arc runs counter-clockwise from t0 through sweep
    sweep: float
    layer: str
    block: str | None = None
    block_role: str | None = None
    hatch: bool = False
    role: str = "other"

    def at(self, t: float) -> Vec:
        return (self.c[0] + self.r * math.cos(t), self.c[1] + self.r * math.sin(t))

    @property
    def mid(self) -> Vec:
        return self.at(self.t0 + self.sweep / 2.0)

    def samples(self) -> list[Vec]:
        return [self.at(self.t0 + self.sweep * k / 4.0) for k in range(5)]


@dataclass(eq=False)
class _Text:
    text: str
    at: Vec
    height: float
    layer: str
    role: str = "other"


@dataclass
class _Drawing:
    lines: list[_Line] = field(default_factory=list)
    arcs: list[_Arc] = field(default_factory=list)
    texts: list[_Text] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    blocks: Counter[str] = field(default_factory=Counter)
    layers: Counter[str] = field(default_factory=Counter)

    def scale(self, k: float) -> None:
        if k == 1.0:
            return
        for ln in self.lines:
            ln.a, ln.b = _mul(ln.a, k), _mul(ln.b, k)
        for arc in self.arcs:
            arc.c, arc.r = _mul(arc.c, k), arc.r * k
        for t in self.texts:
            t.at, t.height = _mul(t.at, k), t.height * k


def _tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", name.upper()) if t]


def _token_role(name: str, roles: tuple[str, ...] = tuple(ROLE_TOKENS)) -> str | None:
    toks = _tokens(name)
    for role in roles:
        if any(t.startswith(k) for t in toks for k in ROLE_TOKENS[role]):
            return role
    return None


def _block_role(name: str) -> str | None:
    if name.startswith("*"):
        return None
    role = _token_role(name, ("door", "window"))
    if role:
        return role
    return "furniture" if FURNITURE_BLOCK.search(name) else None


def _arc_geometry(e: Any) -> tuple[Vec, float, float, float]:
    """Centre, radius, start angle and counter-clockwise sweep of an ARC or CIRCLE in WCS."""
    r = float(e.dxf.radius)
    ext = e.dxf.extrusion
    plain = abs(ext.x) < 1e-9 and abs(ext.y) < 1e-9 and ext.z > 0
    c3 = e.dxf.center if plain else e.ocs().to_wcs(e.dxf.center)
    c = (float(c3.x), float(c3.y))
    if e.dxftype() == "CIRCLE":
        return c, r, 0.0, math.tau
    sweep = math.radians((float(e.dxf.end_angle) - float(e.dxf.start_angle)) % 360.0) or math.tau
    if plain:
        return c, r, math.radians(float(e.dxf.start_angle)), sweep
    start = e.end_point if ext.z < 0 else e.start_point
    return c, r, math.atan2(start.y - c[1], start.x - c[0]), sweep


def _bulge_arc(a: Vec, b: Vec, bulge: float) -> tuple[Vec, float, float, float]:
    """The arc from a to b with this bulge as centre, radius, start angle and ccw sweep."""
    chord = math.dist(a, b)
    theta = 4.0 * math.atan(bulge)
    r = chord / (2.0 * math.sin(abs(theta) / 2.0))
    mid = _mul(_add(a, b), 0.5)
    d = _mul(_sub(b, a), 1.0 / chord)
    left = (-d[1], d[0])
    h = r * math.cos(abs(theta) / 2.0)
    side = 1.0 if theta > 0 else -1.0
    if abs(theta) > math.pi:
        h = -h
    c = _add(mid, _mul(left, side * h))
    start = a if theta > 0 else b
    return c, r, math.atan2(start[1] - c[1], start[0] - c[0]), abs(theta)


class _Reader:
    """Model space as lines, arcs and texts in drawing units, blocks exploded."""

    def __init__(self) -> None:
        self.out = _Drawing()

    def read(self, doc: Drawing) -> _Drawing:
        for e in doc.modelspace():
            self.entity(e, None, None, None, 0)
        return self.out

    def line(self, a: Any, b: Any, layer: str, block: str | None, role: str | None, hatch: bool = False) -> None:
        pa, pb = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
        if math.dist(pa, pb) > 1e-9:
            self.out.lines.append(_Line(pa, pb, layer, block, role, hatch))

    def arc(
        self, c: Vec, r: float, t0: float, sweep: float, layer: str, block: str | None, role: str | None, hatch=False
    ) -> None:
        if r > 1e-9 and sweep > 1e-9:
            self.out.arcs.append(_Arc(c, r, t0, sweep, layer, block, role, hatch))

    def bulged(self, pts: list[tuple[float, float, float]], closed: bool, layer, block, role, hatch) -> None:
        n = len(pts)
        for i in range(n if closed else n - 1):
            x0, y0, bulge = pts[i]
            x1, y1, _ = pts[(i + 1) % n]
            if math.dist((x0, y0), (x1, y1)) <= 1e-9:
                continue
            if abs(bulge) > 1e-9:
                self.arc(*_bulge_arc((x0, y0), (x1, y1), bulge), layer, block, role, hatch)
            else:
                self.line((x0, y0), (x1, y1), layer, block, role, hatch)

    def hatch(self, e: Any, layer: str, block: str | None, role: str | None) -> None:
        mirror = e.dxf.extrusion.z < 0

        def p(v: Any) -> tuple[float, float]:
            return (-float(v[0]), float(v[1])) if mirror else (float(v[0]), float(v[1]))

        for path in e.paths:
            if hasattr(path, "vertices"):
                pts = [(*p(v), (-float(v[2]) if mirror else float(v[2])) if len(v) > 2 else 0.0) for v in path.vertices]
                self.bulged(pts, True, layer, block, role, True)
                continue
            for edge in path.edges:
                kind = type(edge).__name__
                if kind == "LineEdge":
                    self.line(p(edge.start), p(edge.end), layer, block, role, True)
                elif kind == "ArcEdge":
                    ct = edge.construction_tool()
                    t0, t1 = math.radians(ct.start_angle), math.radians(ct.end_angle)
                    c = p(ct.center)
                    if mirror:
                        t0, t1 = math.pi - t1, math.pi - t0
                    self.arc(c, float(ct.radius), t0, (t1 - t0) % math.tau or math.tau, layer, block, role, True)
                else:
                    self.out.skipped[f"HATCH {kind}"] += 1

    def text(self, e: Any, layer: str, block_role: str | None = None) -> None:
        kind = e.dxftype()
        if block_role == "furniture" or (kind == "ATTRIB" and e.is_invisible):
            return
        if kind == "MTEXT":
            raw = e.plain_text(split=False)
            at = e.dxf.insert
            height = float(e.dxf.get("char_height", 0.0) or 0.0)
        else:
            raw = e.plain_text()
            aligned = e.dxf.get("halign", 0) or e.dxf.get("valign", 0)
            at = e.dxf.get("align_point") if aligned and e.dxf.hasattr("align_point") else e.dxf.insert
            height = float(e.dxf.get("height", 0.0) or 0.0)
        raw = raw.replace("\\U+00B2", "²").replace("\\U+00b2", "²")
        if raw.strip():
            self.out.texts.append(_Text(raw.strip(), (float(at.x), float(at.y)), height, layer))

    def entity(self, e: Any, parent_layer: str | None, block: str | None, brole: str | None, depth: int) -> None:
        kind = e.dxftype()
        layer = e.dxf.get("layer", "0")
        if parent_layer is not None and layer == "0":
            layer = parent_layer
        self.out.layers[layer] += 1
        if kind == "INSERT":
            if depth > 8:
                self.out.skipped["INSERT nested deeper than 8"] += 1
                return
            name = e.dxf.name
            outer = block or (None if name.startswith("*") else name)
            role = brole or _block_role(name)
            if not name.startswith("*"):
                self.out.blocks[name] += 1
            for att in e.attribs:
                self.text(att, layer if att.dxf.layer == "0" else att.dxf.layer, role)
            try:
                children = list(e.virtual_entities())
            except Exception:  # a block ezdxf cannot explode is skipped and counted
                self.out.skipped["INSERT that could not be exploded"] += 1
                return
            for child in children:
                self.entity(child, layer, outer, role, depth + 1)
            return
        if kind == "LINE":
            self.line(e.dxf.start, e.dxf.end, layer, block, brole)
        elif kind in ("ARC", "CIRCLE"):
            self.arc(*_arc_geometry(e), layer, block, brole)
        elif kind == "LWPOLYLINE" or (kind == "POLYLINE" and (e.is_2d_polyline or e.is_3d_polyline)):
            for child in e.virtual_entities():
                self.entity(child, layer, block, brole, depth + 1)
        elif kind == "ELLIPSE":
            ratio = float(e.dxf.ratio)
            if ratio > 0.98 and e.dxf.extrusion.z > 0:
                major = e.dxf.major_axis
                r = math.hypot(major.x, major.y) * (1.0 + ratio) / 2.0
                rot = math.atan2(major.y, major.x)
                sweep = (float(e.dxf.end_param) - float(e.dxf.start_param)) % math.tau or math.tau
                c = e.dxf.center
                self.arc((float(c.x), float(c.y)), r, rot + float(e.dxf.start_param), sweep, layer, block, brole)
            else:
                pts = [(p.x, p.y) for p in e.flattening(5.0)]
                for p, q in itertools.pairwise(pts):
                    self.line(p, q, layer, block, brole)
        elif kind == "SPLINE":
            pts = [(p.x, p.y) for p in e.flattening(5.0)]
            for p, q in itertools.pairwise(pts):
                self.line(p, q, layer, block, brole)
        elif kind == "HATCH":
            self.hatch(e, layer, block, brole)
        elif kind in ("SOLID", "TRACE"):
            v = [e.dxf.vtx0, e.dxf.vtx1, e.dxf.vtx3, e.dxf.vtx2]  # DXF order 0 1 3 2 walks the outline
            ring = [(float(q.x), float(q.y)) for q in v]
            for p, q in zip(ring, ring[1:] + ring[:1], strict=True):
                self.line(p, q, layer, block, brole, True)
        elif kind in ("TEXT", "MTEXT", "ATTRIB"):
            self.text(e, layer, brole)
        else:
            self.out.skipped[kind] += 1


# --- layer roles ----------------------------------------------------------------------------

MAPPED = (("ignore", "ignore"), ("walls", "wall"), ("doors", "door"), ("windows", "window"), ("rooms", "room"))


def _layer_roles(layers: list[str], mapping: LayerRoles | None) -> tuple[dict[str, str], dict[str, str]]:
    """Role of every layer (upper-case name -> role) and how each role was set (mapping or names)."""
    roles: dict[str, str] = {}
    how: dict[str, str] = {}
    given = mapping.model_dump() if mapping else {}
    for key, role in MAPPED:
        pats = given.get(key)
        if pats is None:
            continue
        how[role] = "mapping"
        for layer in layers:
            up = layer.upper()
            if up not in roles and any(fnmatch.fnmatchcase(up, p.upper()) for p in pats):
                roles[up] = role
    auto = tuple(r for r in ROLE_TOKENS if r in ("other", "ignore") or r not in how)  # ignore adds to the names
    for role in auto:
        how.setdefault(role, "names")
    if how["ignore"] == "mapping":
        how["ignore"] = "mapping and names"
    for layer in layers:
        up = layer.upper()
        if up in roles:
            continue
        roles[up] = _token_role(layer, auto) or "other"
    return roles, how


def _effective_role(layer_role: str, block_role: str | None) -> str:
    if block_role in ("door", "window"):
        return block_role
    if block_role == "furniture":
        return "other"
    return layer_role


# --- pairing ----------------------------------------------------------------------------------


@dataclass(eq=False)
class _Face:
    """Fused collinear wall lines: one straight face line."""

    a: Vec
    b: Vec
    members: list[_Line]
    phi: float = 0.0
    u: Vec = (1.0, 0.0)
    n: Vec = (0.0, 1.0)
    length: float = 0.0
    x0: float = 0.0  # extent along u from a
    x1: float = 0.0
    paired: list[tuple[float, float]] = field(default_factory=list)  # in the frame's x

    def __post_init__(self) -> None:
        d = _sub(self.b, self.a)
        self.length = math.hypot(*d)
        self.phi = _canon(d)
        self.u = _dir(self.phi)
        self.n = (-self.u[1], self.u[0])
        e = _dot(self.u, d)
        self.x0, self.x1 = min(0.0, e), max(0.0, e)

    def at(self, x: float) -> Vec:
        return _add(self.a, _mul(self.u, x))


@dataclass(eq=False)
class _ArcFace:
    c: Vec
    r: float
    t0: float
    sweep: float
    members: list[_Arc]
    paired: list[tuple[float, float]] = field(default_factory=list)  # angles from t0


@dataclass(eq=False)
class _Piece:
    kind: str  # "line" or "arc"
    t: float
    p0: Vec = (0.0, 0.0)  # line axis ends
    p1: Vec = (0.0, 0.0)
    c: Vec = (0.0, 0.0)  # arc axis
    r: float = 0.0
    th0: float = 0.0
    sweep: float = 0.0
    stack: int = 0  # lines found inside the wall between its faces

    @property
    def length(self) -> float:
        return math.dist(self.p0, self.p1) if self.kind == "line" else self.r * self.sweep


def _fuse(lines: list[_Line]) -> list[_Face]:
    """Collinear lines that overlap or touch, fused into faces (duplicates, hatch edges, split lines)."""
    keyed = []
    for ln in lines:
        d = _sub(ln.b, ln.a)
        if math.hypot(*d) < 1e-6:
            continue
        phi = _canon(d)
        u = _dir(phi)
        n = (-u[1], u[0])
        keyed.append((phi, u, n, ln))
    keyed.sort(key=lambda k: k[0])
    faces: list[_Face] = []
    i = 0
    while i < len(keyed):
        j = i + 1
        while j < len(keyed) and keyed[j][0] - keyed[j - 1][0] <= FUSE_ANGLE:
            j += 1
        cluster = keyed[i:j]
        _, u, n, _ = cluster[0]
        rows = sorted((((_dot(n, k[3].a) + _dot(n, k[3].b)) / 2.0, k[3]) for k in cluster), key=lambda r: r[0])
        k = 0
        while k < len(rows):
            m = k + 1
            while m < len(rows) and rows[m][0] - rows[m - 1][0] <= FUSE_MM:
                m += 1
            spans = sorted(
                ((min(_dot(u, ln.a), _dot(u, ln.b)), max(_dot(u, ln.a), _dot(u, ln.b)), ln) for _, ln in rows[k:m]),
                key=lambda r: (r[0], r[1]),
            )
            hi, group = spans[0][1], [spans[0][2]]
            for s0, s1, ln in spans[1:]:
                if s0 <= hi + FUSE_MM:
                    hi = max(hi, s1)
                    group.append(ln)
                    continue
                faces.append(_fused(group))
                hi, group = s1, [ln]
            faces.append(_fused(group))
            k = m
        i = j
    return faces


def _fused(group: list[_Line]) -> _Face:
    """One face through collinear lines: the direction of the longest, offset to their weighted mean, from
    the first to the last of their endpoints along it."""
    ref = max(group, key=lambda ln: ln.length)
    if len(group) == 1:
        return _Face(ref.a, ref.b, group)
    u = _mul(_sub(ref.b, ref.a), 1.0 / ref.length)
    n = (-u[1], u[0])
    total = sum(ln.length for ln in group)
    shift = sum(ln.length * _dot(n, _sub(_mul(_add(ln.a, ln.b), 0.5), ref.a)) for ln in group) / total
    xs = [_dot(u, _sub(p, ref.a)) for ln in group for p in (ln.a, ln.b)]
    base = _add(ref.a, _mul(n, shift))
    return _Face(_add(base, _mul(u, min(xs))), _add(base, _mul(u, max(xs))), group)


def _fuse_arcs(arcs: list[_Arc]) -> list[_ArcFace]:
    out: list[_ArcFace] = []
    for arc in sorted(arcs, key=lambda a: (round(a.c[0], 1), round(a.c[1], 1), round(a.r, 1), a.t0)):
        for f in out:
            if math.dist(f.c, arc.c) <= FUSE_MM and abs(f.r - arc.r) <= FUSE_MM:
                rel = (arc.t0 - f.t0) % math.tau
                if f.sweep >= math.tau - 1e-9:
                    f.members.append(arc)
                    break
                if rel <= f.sweep + FUSE_MM / f.r:
                    f.sweep = min(math.tau, max(f.sweep, rel + arc.sweep))
                    f.members.append(arc)
                    break
                back = (f.t0 - arc.t0) % math.tau
                if back <= arc.sweep + FUSE_MM / f.r:
                    f.sweep = min(math.tau, max(arc.sweep, back + f.sweep))
                    f.t0 = arc.t0
                    f.members.append(arc)
                    break
        else:
            out.append(_ArcFace(arc.c, arc.r, arc.t0, arc.sweep, [arc]))
    return out


def _envelope(cands: list[tuple[float, float, float, int]]) -> list[tuple[float, float, float, int]]:
    """Nearest candidate over every stretch: (lo, hi, distance, index) runs, ties to the lower index."""
    cuts = sorted({c[0] for c in cands} | {c[1] for c in cands})
    out: list[tuple[float, float, float, int]] = []
    for p, q in itertools.pairwise(cuts):
        if q - p < 1e-6:
            continue
        m = (p + q) / 2.0
        best = min(((d, j) for lo, hi, d, j in cands if lo <= m <= hi), default=None)
        if best is None:
            continue
        if out and out[-1][3] == best[1] and abs(out[-1][1] - p) < 1e-6:
            out[-1] = (out[-1][0], q, out[-1][2], best[1])
        else:
            out.append((p, q, best[0], best[1]))
    return out


def _pair_faces(faces: list[_Face], tmin: float, tmax: float, min_overlap: float, mark: bool = True) -> list[_Piece]:
    """Mutually nearest parallel face pairs as straight axis pieces (see the module docstring)."""
    buckets: dict[int, list[tuple[float, int]]] = defaultdict(list)
    ref: dict[int, Vec] = {}
    lmax: dict[int, float] = defaultdict(float)
    for i, f in enumerate(faces):
        k = math.floor(math.degrees(f.phi + ANGLE_TOL)) % 180
        for kk in (k - 1, k, k + 1):
            kk %= 180
            if kk not in ref:
                ang = math.radians(kk + 0.5) - ANGLE_TOL
                ref[kk] = (-math.sin(ang), math.cos(ang))
            mid = _mul(_add(f.a, f.b), 0.5)
            buckets[kk].append((_dot(ref[kk], mid), i))
            lmax[kk] = max(lmax[kk], f.length)
    for rows in buckets.values():
        rows.sort()
    cands: list[dict[int, list[tuple[float, float, float, int]]]] = [{1: [], -1: []} for _ in faces]
    for i, s in enumerate(faces):
        k = math.floor(math.degrees(s.phi + ANGLE_TOL)) % 180
        rows = buckets[k]
        mid = _mul(_add(s.a, s.b), 0.5)
        w = _dot(ref[k], mid)
        slack = math.sin(math.radians(2.5)) * (s.length + lmax[k]) / 2.0 + 1.0
        lo_i = bisect.bisect_left(rows, (w - tmax - slack, -1))
        hi_i = bisect.bisect_right(rows, (w + tmax + slack, len(faces)))
        for _, j in rows[lo_i:hi_i]:
            if j == i:
                continue
            c = faces[j]
            if _angle_diff(s.phi, c.phi) > ANGLE_TOL:
                continue
            xa, ya = _dot(s.u, _sub(c.a, s.a)), _dot(s.n, _sub(c.a, s.a))
            xb, yb = _dot(s.u, _sub(c.b, s.a)), _dot(s.n, _sub(c.b, s.a))
            if xa > xb:
                xa, ya, xb, yb = xb, yb, xa, ya
            lo, hi = max(s.x0, xa), min(s.x1, xb)
            if hi - lo < 1.0:
                continue

            def y(x: float, xa=xa, ya=ya, xb=xb, yb=yb) -> float:
                return ya if xb - xa < 1e-9 else ya + (yb - ya) * (x - xa) / (xb - xa)

            d0, d1 = y(lo), y(hi)
            if d0 * d1 <= 0 or abs(d0 - d1) > DRIFT_MM:
                continue
            d = (abs(d0) + abs(d1)) / 2.0
            if not tmin <= d <= tmax:
                continue
            cands[i][1 if d0 > 0 else -1].append((lo, hi, d, j))
    env = [{side: _envelope(c[side]) for side in (1, -1)} for c in cands]
    pairs: list[_Pair] = []
    for i, s in enumerate(faces):
        for side in (1, -1):
            for lo, hi, d, j in env[i][side]:
                if j < i:
                    continue
                c = faces[j]
                for lo2, hi2, _, back in env[j][1] + env[j][-1]:
                    if back != i:
                        continue
                    xs = sorted(_dot(s.u, _sub(c.at(x), s.a)) for x in (lo2, hi2))
                    a, b = max(lo, xs[0]), min(hi, xs[1])
                    if b - a >= max(min_overlap, d):
                        pairs.append(_Pair(i, j, a, b, d))
    pairs = _sandwiches(faces, pairs, tmax, min_overlap)
    pieces: list[_Piece] = []
    for pr in pairs:
        s, c = faces[pr.i], faces[pr.j]
        pa, pb = s.at(pr.lo), s.at(pr.hi)
        qa = _add(c.a, _mul(c.u, _dot(c.u, _sub(pa, c.a))))
        qb = _add(c.a, _mul(c.u, _dot(c.u, _sub(pb, c.a))))
        t = (math.dist(pa, qa) + math.dist(pb, qb)) / 2.0
        pieces.append(_Piece("line", t, _mul(_add(pa, qa), 0.5), _mul(_add(pb, qb), 0.5), stack=pr.inner))
        if mark:
            for k in (pr.i, pr.j, *pr.inner_faces):
                f = faces[k]
                f.paired.append(tuple(sorted(_dot(f.u, _sub(q, f.a)) for q in (pa, pb))))
    return pieces


@dataclass(eq=False)
class _Pair:
    """Faces i and j paired over lo..hi (in face i's frame), d apart, with inner faces between them."""

    i: int
    j: int
    lo: float
    hi: float
    d: float
    inner: int = 0
    inner_faces: tuple[int, ...] = ()


def _sandwiches(faces: list[_Face], pairs: list[_Pair], tmax: float, min_overlap: float) -> list[_Pair]:
    """A face paired on both sides over one stretch is a line inside a wall (a window symbol, a cavity) when
    the two outer faces are within tmax of each other: the outer faces pair instead and the inner line is
    remembered. Farther apart, the nearer pair wins (a counter edge beside a wall face)."""

    def frame_x(f: int, k: int, x: float) -> float:
        return _dot(faces[f].u, _sub(faces[k].at(x), faces[f].a))

    def in_frame(f: int, pr: _Pair) -> tuple[float, float]:
        if pr.i == f:
            return pr.lo, pr.hi
        a, b = frame_x(f, pr.i, pr.lo), frame_x(f, pr.i, pr.hi)
        return min(a, b), max(a, b)

    def side(f: int, other: int) -> int:
        g = faces[other]
        return 1 if _dot(faces[f].n, _sub(_mul(_add(g.a, g.b), 0.5), faces[f].a)) > 0 else -1

    def cut(pr: _Pair, f: int, lo: float, hi: float) -> list[_Pair]:
        """pr without the stretch lo..hi of face f's frame."""
        a, b = sorted((frame_x(pr.i, f, lo), frame_x(pr.i, f, hi)))
        out = []
        for x0, x1 in ((pr.lo, min(pr.hi, a)), (max(pr.lo, b), pr.hi)):
            if x1 - x0 >= max(min_overlap, pr.d):
                out.append(_Pair(pr.i, pr.j, x0, x1, pr.d, pr.inner, pr.inner_faces))
        return out

    for _ in range(4 * len(pairs) + 4):
        by_face: dict[int, list[_Pair]] = defaultdict(list)
        for pr in pairs:
            by_face[pr.i].append(pr)
            by_face[pr.j].append(pr)
        hit = None
        for f in sorted(by_face):
            group = by_face[f]
            for k, p1 in enumerate(group):
                o1 = p1.j if p1.i == f else p1.i
                for p2 in group[k + 1 :]:
                    o2 = p2.j if p2.i == f else p2.i
                    if side(f, o1) == side(f, o2):
                        continue
                    (a1, b1), (a2, b2) = in_frame(f, p1), in_frame(f, p2)
                    lo, hi = max(a1, a2), min(b1, b2)
                    if hi - lo >= min_overlap:
                        hit = (f, p1, p2, o1, o2, lo, hi)
                        break
                if hit:
                    break
            if hit:
                break
        if hit is None:
            return pairs
        f, p1, p2, o1, o2, lo, hi = hit
        rest = [pr for pr in pairs if pr is not p1 and pr is not p2]
        if p1.d + p2.d <= tmax:
            x0, x1 = sorted((frame_x(o1, f, lo), frame_x(o1, f, hi)))
            inner = (*p1.inner_faces, *p2.inner_faces, f)
            rest.append(_Pair(o1, o2, x0, x1, p1.d + p2.d, p1.inner + p2.inner + 1, inner))
            rest += cut(p1, f, lo, hi) + cut(p2, f, lo, hi)
        else:
            near, far = (p1, p2) if p1.d <= p2.d else (p2, p1)
            rest += [near, *cut(far, f, lo, hi)]
        pairs = rest
    return pairs


def _pair_arcs(faces: list[_ArcFace], tmin: float, tmax: float, min_overlap: float) -> list[_Piece]:
    """Mutually nearest concentric arc pairs as curved axis pieces."""
    cands: list[dict[int, list[tuple[float, float, float, int]]]] = [{1: [], -1: []} for _ in faces]
    order = sorted(range(len(faces)), key=lambda i: faces[i].c[0])
    xs = [faces[i].c[0] for i in order]
    for i, s in enumerate(faces):
        lo_i = bisect.bisect_left(xs, s.c[0] - CENTRE_TOL_MM)
        hi_i = bisect.bisect_right(xs, s.c[0] + CENTRE_TOL_MM)
        for j in order[lo_i:hi_i]:
            c = faces[j]
            if j == i or math.dist(s.c, c.c) > CENTRE_TOL_MM:
                continue
            dr = c.r - s.r
            if not tmin <= abs(dr) <= tmax:
                continue
            for lo, hi in _arc_overlap(s, c):
                cands[i][1 if dr > 0 else -1].append((lo, hi, abs(dr), j))
    env = [{side: _envelope(c[side]) for side in (1, -1)} for c in cands]
    pieces: list[_Piece] = []
    for i, s in enumerate(faces):
        for side in (1, -1):
            for lo, hi, d, j in env[i][side]:
                if j < i:
                    continue
                c = faces[j]
                shift = (c.t0 - s.t0) % math.tau
                for lo2, hi2, _, back in env[j][1] + env[j][-1]:
                    if back != i:
                        continue
                    for k in (-1, 0, 1):
                        a = max(lo, lo2 + shift + k * math.tau)
                        b = min(hi, hi2 + shift + k * math.tau)
                        rm = (s.r + c.r) / 2.0
                        if (b - a) * rm < max(min_overlap, d):
                            continue
                        centre = _mul(_add(s.c, c.c), 0.5)
                        pieces.append(_Piece("arc", d, c=centre, r=rm, th0=s.t0 + a, sweep=b - a))
                        s.paired.append((a, b))
                        c.paired.append((a - shift - k * math.tau, b - shift - k * math.tau))
    return pieces


def _arc_overlap(s: _ArcFace, c: _ArcFace) -> list[tuple[float, float]]:
    """Stretches of s (angles from s.t0) that c also covers."""
    rel = (c.t0 - s.t0) % math.tau
    out = []
    for k in (-1, 0):
        lo, hi = max(0.0, rel + k * math.tau), min(s.sweep, rel + k * math.tau + c.sweep)
        if hi - lo > 1e-9:
            out.append((lo, hi))
    if c.sweep >= math.tau - 1e-9:
        out = [(0.0, s.sweep)]
    return out


def _snap(t: float) -> float:
    m = round(t / SNAP_STEP_MM) * SNAP_STEP_MM
    return float(m) if abs(t - m) <= SNAP_WITHIN_MM else t


# --- runs -----------------------------------------------------------------------------------


@dataclass(eq=False)
class _Frame:
    """An axis line (u, n, w: the points p with n.p = w) or circle (c, r, ref) with arc-length s and a
    signed offset n, positive on the left of increasing s (towards the centre on a circle)."""

    kind: str
    t: float
    phi: float = 0.0
    u: Vec = (1.0, 0.0)
    n: Vec = (0.0, 1.0)
    w: float = 0.0
    c: Vec = (0.0, 0.0)
    r: float = 0.0
    ref: float = 0.0
    s_mid: float = 0.0
    stacks: list[tuple[float, float]] = field(default_factory=list)  # stretches with lines inside the wall

    def point(self, s: float, off: float = 0.0) -> Vec:
        if self.kind == "line":
            return _add(_mul(self.n, self.w + off), _mul(self.u, s))
        th = self.ref + s / self.r
        return _add(self.c, _mul(_dir(th), self.r - off))

    def tangent(self, s: float) -> Vec:
        if self.kind == "line":
            return self.u
        th = self.ref + s / self.r
        return (-math.sin(th), math.cos(th))

    def normal(self, s: float) -> Vec:
        t = self.tangent(s)
        return (-t[1], t[0])

    def local(self, p: Vec) -> tuple[float, float]:
        if self.kind == "line":
            return _dot(self.u, p), _dot(self.n, p) - self.w
        d = _sub(p, self.c)
        th = math.atan2(d[1], d[0])
        mid = self.ref + self.s_mid / self.r
        return self.s_mid + self.r * _wrap(th - mid), self.r - math.hypot(*d)


@dataclass(eq=False)
class _Gap:
    s0: float
    s1: float
    kind: str = ""  # door, window, or "" (unclassified)
    hinge: float | None = None  # s of the hinge jamb
    swing: int = 0  # +1 left of increasing s, -1 right, 0 not drawn
    evidence: str = ""
    assumed: list[str] = field(default_factory=list)
    emitted: bool = True
    entry: dict[str, Any] | None = None  # its line in the report's unclassified_gaps


@dataclass(eq=False)
class _Run:
    frame: _Frame
    a: float
    b: float
    paired: list[tuple[float, float]]
    openings: list[_Gap] = field(default_factory=list)
    closed: bool = False
    a0: float = 0.0
    b0: float = 0.0
    pts: list[Vec | None] = field(default_factory=lambda: [None, None])
    stems: list[tuple[float, float]] = field(default_factory=list)  # junction bands on this run (s0, s1)
    links: int = 0  # junctions this run takes part in
    id: str = ""

    def __post_init__(self) -> None:
        self.a0, self.b0 = self.a, self.b

    @property
    def t(self) -> float:
        return self.frame.t

    def s_end(self, end: int) -> float:
        return self.a if end == 0 else self.b

    def end_point(self, end: int) -> Vec:
        p = self.pts[end]
        return p if p is not None else self.frame.point(self.s_end(end))

    def out_dir(self, end: int) -> Vec:
        t = self.frame.tangent(self.s_end(end))
        return _mul(t, -1.0) if end == 0 else t

    def beyond(self, end: int, p: Vec) -> float:
        """How far past this end (along the axis) a point on the axis lies; negative inside the run."""
        s = self.frame.local(p)[0]
        return s - self.b if end == 1 else self.a - s

    def move(self, end: int, p: Vec) -> None:
        s = self.frame.local(p)[0]
        if end == 0:
            self.a = s
        else:
            self.b = s
        self.pts[end] = p

    def bbox(self, grow: float) -> tuple[float, float, float, float]:
        pts = [self.frame.point(self.a + (self.b - self.a) * k / 4.0) for k in range(5)]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return min(xs) - grow, min(ys) - grow, max(xs) + grow, max(ys) + grow


def _group_lines(pieces: list[_Piece]) -> list[tuple[_Frame, list[tuple[float, float]]]]:
    """Straight pieces on one axis line and of one thickness: (frame, intervals in s). The frame is fitted
    to all its pieces (length-weighted principal axis of their ends), so one short piece does not tilt a
    long wall."""
    groups: list[tuple[_Frame, list[_Piece]]] = []
    for p in sorted((p for p in pieces if p.kind == "line"), key=lambda p: -p.length):
        phi = _canon(_sub(p.p1, p.p0))
        for frame, members in groups:
            if _angle_diff(frame.phi, phi) > AXIS_ANGLE or abs(frame.t - p.t) > THICK_TOL_MM:
                continue
            if abs(_dot(frame.n, p.p0) - frame.w) > AXIS_TOL_MM or abs(_dot(frame.n, p.p1) - frame.w) > AXIS_TOL_MM:
                continue
            members.append(p)
            break
        else:
            u = _dir(phi)
            n = (-u[1], u[0])
            groups.append((_Frame("line", p.t, phi=phi, u=u, n=n, w=(_dot(n, p.p0) + _dot(n, p.p1)) / 2.0), [p]))
    out = []
    for frame, members in groups:
        weights = [m.length for m in members]
        total = sum(weights)
        cx = sum(wt * (m.p0[0] + m.p1[0]) / 2.0 for wt, m in zip(weights, members, strict=True)) / total
        cy = sum(wt * (m.p0[1] + m.p1[1]) / 2.0 for wt, m in zip(weights, members, strict=True)) / total
        sxx = syy = sxy = 0.0
        for wt, m in zip(weights, members, strict=True):
            for q in (m.p0, m.p1):
                dx, dy = q[0] - cx, q[1] - cy
                sxx, syy, sxy = sxx + wt * dx * dx, syy + wt * dy * dy, sxy + wt * dx * dy
        phi = _canon(_dir(0.5 * math.atan2(2.0 * sxy, sxx - syy)))
        if _angle_diff(phi, frame.phi) > AXIS_ANGLE:
            phi = frame.phi
        frame.phi = phi
        frame.u = _dir(phi)
        frame.n = (-frame.u[1], frame.u[0])
        frame.w = _dot(frame.n, (cx, cy))
        frame.t = _snap(sum(wt * m.t for wt, m in zip(weights, members, strict=True)) / total)
        spans = [tuple(sorted((_dot(frame.u, m.p0), _dot(frame.u, m.p1)))) for m in members]
        frame.stacks = [sp for sp, m in zip(spans, members, strict=True) if m.stack]
        out.append((frame, spans))
    return out


def _group_arcs(pieces: list[_Piece]) -> list[tuple[_Frame, list[tuple[float, float]], bool]]:
    """Curved pieces on one circle and of one thickness: (frame, intervals in s, full circle)."""
    groups: list[tuple[_Frame, list[tuple[float, float]], list[float]]] = []
    for p in sorted((p for p in pieces if p.kind == "arc"), key=lambda p: -p.length):
        for frame, spans, ts in groups:
            near = math.dist(frame.c, p.c) <= CENTRE_TOL_MM and abs(frame.r - p.r) <= AXIS_TOL_MM
            if near and abs(frame.t - p.t) <= THICK_TOL_MM:
                spans.append((p.th0, p.sweep))
                ts.append(p.t)
                break
        else:
            groups.append((_Frame("arc", p.t, c=p.c, r=p.r), [(p.th0, p.sweep)], [p.t]))
    out = []
    for frame, spans, ts in groups:
        frame.t = _snap(sum(ts) / len(ts))
        # cut the circle in its widest uncovered stretch so the intervals are linear
        rel = sorted(((th - spans[0][0]) % math.tau, sw) for th, sw in spans)
        covered: list[tuple[float, float]] = []
        for lo, sw in rel:
            covered.append((lo, lo + sw))
        merged: list[list[float]] = []
        for lo, hi in sorted(covered):
            if merged and lo <= merged[-1][1] + 1e-9:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        if merged[-1][1] - math.tau >= merged[0][0] - 1e-6 and len(merged) >= 1:
            span_total = merged[-1][1] - merged[0][0]
        else:
            span_total = 0.0
        full = _union_length([(lo, hi) for lo, hi in covered] + [(lo - math.tau, hi - math.tau) for lo, hi in covered])
        if full >= math.tau - TOUCH_MM / frame.r or span_total >= math.tau - TOUCH_MM / frame.r:
            frame.ref = spans[0][0]
            out.append((frame, [(0.0, frame.r * math.tau)], True))
            continue
        gaps = []
        for k, (_, hi) in enumerate(merged):
            nxt = merged[(k + 1) % len(merged)][0] + (math.tau if k + 1 == len(merged) else 0.0)
            gaps.append((nxt - hi, hi))
        width, cut = max(gaps)
        frame.ref = spans[0][0] + cut + width / 2.0  # the middle of the widest uncovered stretch
        intervals = []
        for th, sw in spans:
            lo = (th - frame.ref) % math.tau
            intervals.append((lo * frame.r, (lo + sw) * frame.r))
        frame.s_mid = frame.r * math.pi
        out.append((frame, intervals, False))
    return out


def _merge_spans(spans: list[tuple[float, float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1] + TOUCH_MM:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


# --- evidence for openings ---------------------------------------------------------------------


@dataclass
class _Evidence:
    arcs: list[_Arc]  # swing candidates
    doors: list[tuple[list[Vec], str]]  # door-role items: sample points, description
    windows: list[tuple[list[Vec], str]]
    glass: list[_Line]  # loose straight lines that may be glass
    used_arcs: set[int] = field(default_factory=set)  # id() of the arcs a door took
    grids: dict[str, _Grid] = field(default_factory=dict)

    def index(self) -> _Evidence:
        for key, items, box in (
            ("arcs", self.arcs, lambda a: _box([a.c], a.r)),
            ("doors", self.doors, lambda it: _box(it[0])),
            ("windows", self.windows, lambda it: _box(it[0])),
            ("glass", self.glass, lambda ln: _box([ln.a, ln.b])),
        ):
            grid = self.grids[key] = _Grid()
            for item in items:
                grid.add(box(item), item)
        return self

    def near(self, box: tuple[float, float, float, float]) -> _Evidence:
        """The evidence whose boxes come near box, sharing the record of used arcs."""
        g = self.grids
        return _Evidence(
            g["arcs"].query(box), g["doors"].query(box), g["windows"].query(box), g["glass"].query(box), self.used_arcs
        )


def _is_leaf(ln: _Line, swings: list[_Arc]) -> bool:
    """A line from a swing arc's centre out along one of its end radii: a door leaf drawn open."""
    for arc in swings:
        for p, q in ((ln.a, ln.b), (ln.b, ln.a)):
            if math.dist(p, arc.c) > JAMB_REACH_MM:
                continue
            if abs(math.dist(q, arc.c) - arc.r) > max(JAMB_REACH_MM, 0.1 * arc.r) and math.dist(q, arc.c) > arc.r:
                continue
            d = math.atan2(q[1] - p[1], q[0] - p[0])
            for t in (arc.t0, arc.t0 + arc.sweep):
                if abs(_wrap(d - t)) <= math.radians(3.0):
                    return True
    return False


def _items(lines: list[_Line], arcs: list[_Arc], role: str) -> list[tuple[list[Vec], str]]:
    out: list[tuple[list[Vec], str]] = []
    for ln in lines:
        if ln.role == role:
            out.append(([ln.a, _mul(_add(ln.a, ln.b), 0.5), ln.b], ln.block or ln.layer))
    for arc in arcs:
        if arc.role == role:
            out.append((arc.samples(), arc.block or arc.layer))
    return out


def _classify(frame: _Frame, g0: float, g1: float, ev: _Evidence) -> _Gap:
    """What the drawing puts in a gap of a run: a door, a window, or nothing recognisable."""
    w, half = g1 - g0, frame.t / 2.0
    gap = _Gap(g0, g1)
    best: tuple[float, int] | None = None
    double: list[int] = []
    for k, arc in enumerate(ev.arcs):
        sc, nc = frame.local(arc.c)
        if abs(nc) > half + JAMB_REACH_MM:
            continue
        jamb = g0 if abs(sc - g0) <= abs(sc - g1) else g1
        if abs(sc - jamb) > half + JAMB_REACH_MM:
            continue
        ratio = arc.r / w
        if DOOR_RADIUS[0] <= ratio <= DOOR_RADIUS[1]:
            score = abs(ratio - 1.0)
            if best is None or score < best[0]:
                best = (score, k)
        elif DOOR_RADIUS[0] / 2 <= ratio <= DOOR_RADIUS[1] / 2:
            double.append(k)
    pick = best[1] if best else None
    if pick is None and len(double) >= 2:
        jambs = {round(abs(frame.local(ev.arcs[k].c)[0] - g0) < abs(frame.local(ev.arcs[k].c)[0] - g1)) for k in double}
        if len(jambs) == 2:
            pick = double[0]
            gap.assumed.append("double door drawn with two leaves; built as one leaf")
    if pick is not None:
        arc = ev.arcs[pick]
        ev.used_arcs.add(id(arc))
        sc, _ = frame.local(arc.c)
        gap.kind = "door"
        gap.hinge = g0 if abs(sc - g0) <= abs(sc - g1) else g1
        nm = frame.local(arc.mid)[1]
        gap.swing = 1 if nm > 0 else -1
        gap.evidence = f"swing arc r {_r(arc.r)} on {arc.block or arc.layer}"
        return gap

    def overlaps(points: list[Vec], reach: float) -> float:
        loc = [frame.local(p) for p in points]
        if min(abs(n) for _, n in loc) > half + reach:
            return 0.0
        lo, hi = min(s for s, _ in loc), max(s for s, _ in loc)
        return max(0.0, min(hi, g1) - max(lo, g0))

    door = [(pts, name) for pts, name in ev.doors if overlaps(pts, max(w, JAMB_REACH_MM)) >= 0.5 * w]
    if door:
        gap.kind, gap.evidence = "door", f"door linework on {door[0][1]}"
        gap.assumed.append("swing and hand not drawn: in, left")
        return gap
    win = [(pts, name) for pts, name in ev.windows if overlaps(pts, JAMB_REACH_MM) >= 0.5 * w]
    if win:
        gap.kind, gap.evidence = "window", f"window linework on {win[0][1]}"
        return gap
    for ln in ev.glass:
        (sa, na), (sb, nb) = frame.local(ln.a), frame.local(ln.b)
        if frame.kind == "line" and abs(na - nb) > DRIFT_MM:
            continue
        if max(abs(na), abs(nb)) >= half - 1.0:
            continue
        if min(sa, sb) < g0 - frame.t or max(sa, sb) > g1 + frame.t:
            continue  # longer than the gap: an axis or grid line, not glass
        if min(max(sa, sb), g1) - max(min(sa, sb), g0) >= 0.8 * w:
            gap.kind, gap.evidence = "window", f"glass line on {ln.layer}"
            return gap
    return gap


# --- junctions --------------------------------------------------------------------------------


def _intersections(f1: _Frame, f2: _Frame) -> list[Vec]:
    if f1.kind == "line" and f2.kind == "line":
        den = _cross(f1.u, f2.u)
        if abs(den) < math.sin(math.radians(MIN_JUNCTION_DEG)):
            return []
        o1, o2 = _mul(f1.n, f1.w), _mul(f2.n, f2.w)
        s = _cross(_sub(o2, o1), f2.u) / den
        return [_add(o1, _mul(f1.u, s))]
    if f1.kind == "arc" and f2.kind == "line":
        return _intersections(f2, f1)
    if f1.kind == "line":
        o = _mul(f1.n, f1.w)
        s0 = _dot(f1.u, _sub(f2.c, o))
        foot = _add(o, _mul(f1.u, s0))
        h2 = f2.r**2 - math.dist(foot, f2.c) ** 2
        if h2 < 0:
            return []
        h = math.sqrt(h2)
        return [_add(foot, _mul(f1.u, -h)), _add(foot, _mul(f1.u, h))]
    d = math.dist(f1.c, f2.c)
    if d < 1e-9 or d > f1.r + f2.r or d < abs(f1.r - f2.r):
        return []
    a = (f1.r**2 - f2.r**2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, f1.r**2 - a * a))
    e = _mul(_sub(f2.c, f1.c), 1.0 / d)
    m = _add(f1.c, _mul(e, a))
    return [_add(m, _mul((-e[1], e[0]), h)), _add(m, _mul((e[1], -e[0]), h))]


def _sin_at(r1: _Run, r2: _Run, p: Vec) -> float:
    t1 = r1.frame.tangent(r1.frame.local(p)[0])
    t2 = r2.frame.tangent(r2.frame.local(p)[0])
    return abs(_cross(t1, t2))


def _reach(t_self: float, t_other: float, sin: float) -> float:
    """How far from the crossing of the axes an end may stop (or run past) and still join: the mitre or
    butt of a t_self wall against a t_other one at this angle, plus the tolerance."""
    cos = math.sqrt(max(0.0, 1.0 - sin * sin))
    return t_other / 2.0 / sin + t_self / 2.0 * cos / sin + JUNCTION_TOL_MM


def _band(host: _Run, stem: _Run, p: Vec) -> tuple[float, float]:
    """The stretch of host's axis a junction with stem at p occupies."""
    s = host.frame.local(p)[0]
    sin = max(_sin_at(host, stem, p), 1e-6)
    cos = math.sqrt(max(0.0, 1.0 - sin * sin))
    h = stem.t / 2.0 / sin + host.t / 2.0 * cos / sin
    return s - h, s + h


class _Junctions:
    """Extends and trims run ends to L corners and T stems; collects what it could not join."""

    def __init__(self, runs: list[_Run], report: dict[str, Any]) -> None:
        self.runs = runs
        self.report = report
        self.nodes: dict[int, list[tuple[_Run, int]]] = {}
        self.node_of: dict[tuple[int, int], int] = {}
        self.stems: list[tuple[_Run, int, _Run]] = []
        self.serial = 0

    def _node(self, members: list[tuple[_Run, int]]) -> None:
        ids = {self.node_of[(id(r), e)] for r, e in members if (id(r), e) in self.node_of}
        if ids:
            key = min(ids)
        else:
            self.serial += 1
            key = self.serial
        group = self.nodes.setdefault(key, [])
        for other in ids - {key}:
            for m in self.nodes.pop(other):
                group.append(m)
                self.node_of[(id(m[0]), m[1])] = key
        for r, e in members:
            if (id(r), e) not in self.node_of:
                group.append((r, e))
                self.node_of[(id(r), e)] = key

    def free(self, r: _Run, e: int) -> bool:
        return not r.closed and (id(r), e) not in self.node_of

    def _end_grid(self, ends: list[tuple[_Run, int]]) -> _Grid:
        grid = _Grid()
        for k, (r, e) in enumerate(ends):
            grid.add(_box([r.end_point(e)]), k)
        return grid

    def corners(self) -> None:
        ends = [(r, e) for r in self.runs if not r.closed for e in (0, 1)]
        grid = self._end_grid(ends)
        tmax = max((r.t for r in self.runs), default=0.0)
        cands = []
        for i, (r1, e1) in enumerate(ends):
            p1 = r1.end_point(e1)
            for j in grid.query(_box([p1], 6.0 * (r1.t + tmax) + 2 * JUNCTION_TOL_MM)):
                if j <= i:
                    continue
                r2, e2 = ends[j]
                if r2 is r1:
                    continue
                p2 = r2.end_point(e2)
                if math.dist(p1, p2) > 6.0 * (r1.t + r2.t) + 2 * JUNCTION_TOL_MM:
                    continue
                for x in _intersections(r1.frame, r2.frame):
                    sin = _sin_at(r1, r2, x)
                    if sin < math.sin(math.radians(MIN_JUNCTION_DEG)):
                        continue
                    l1, l2 = r1.beyond(e1, x), r2.beyond(e2, x)
                    if abs(l1) > _reach(r1.t, r2.t, sin) or abs(l2) > _reach(r2.t, r1.t, sin):
                        continue
                    if (r1.b - r1.a) + l1 < r1.t or (r2.b - r2.a) + l2 < r2.t:
                        continue
                    cands.append((abs(l1) + abs(l2), i, j, x))
        used: set[int] = set()
        for _, i, j, x in sorted(cands, key=lambda c: (c[0], c[1], c[2])):
            if i in used or j in used:
                continue
            used |= {i, j}
            (r1, e1), (r2, e2) = ends[i], ends[j]
            r1.move(e1, x)
            r2.move(e2, x)
            self._node([(r1, e1), (r2, e2)])

    def smooth(self) -> None:
        ends = [(r, e) for r in self.runs if not r.closed for e in (0, 1)]
        grid = self._end_grid(ends)
        for i, (r1, e1) in enumerate(ends):
            for j in grid.query(_box([r1.end_point(e1)], SMOOTH_MM)):
                if j <= i:
                    continue
                r2, e2 = ends[j]
                if r2 is r1 or not (self.free(r1, e1) and self.free(r2, e2)):
                    continue
                if r1.frame.kind == "line" and r2.frame.kind == "line":
                    continue
                if abs(r1.t - r2.t) > THICK_TOL_MM:
                    continue
                p1, p2 = r1.end_point(e1), r2.end_point(e2)
                if math.dist(p1, p2) > SMOOTH_MM:
                    continue
                if _dot(r1.out_dir(e1), r2.out_dir(e2)) > -math.cos(math.radians(SMOOTH_DEG)):
                    continue
                if r1.frame.kind == "line":
                    x = r1.frame.point(r1.frame.local(p2)[0])
                elif r2.frame.kind == "line":
                    x = r2.frame.point(r2.frame.local(p1)[0])
                else:
                    x = _mul(_add(p1, p2), 0.5)
                r1.move(e1, x)
                r2.move(e2, x)
                self._node([(r1, e1), (r2, e2)])

    def tees(self) -> None:
        self.grid = _Grid()
        tmax = max((r.t for r in self.runs), default=0.0)
        for r in self.runs:
            self.grid.add(r.bbox(6.0 * (r.t + tmax) + JUNCTION_TOL_MM + tmax), r)
        for _ in range(2):
            for r in self.runs:
                for e in (0, 1):
                    if self.free(r, e):
                        self._tee(r, e)

    def _tee(self, r: _Run, e: int) -> None:
        p = r.end_point(e)
        best = None
        for s in self.grid.query(_box([p])):
            if s is r:
                continue
            for x in _intersections(r.frame, s.frame):
                sin = _sin_at(r, s, x)
                if sin < math.sin(math.radians(MIN_JUNCTION_DEG)):
                    continue
                lam = r.beyond(e, x)
                if abs(lam) > _reach(r.t, s.t, sin) or (r.b - r.a) + lam < r.t:
                    continue
                sx = s.frame.local(x)[0]
                if not s.closed and not (s.a - 1.0 <= sx <= s.b + 1.0):
                    continue
                if best is None or abs(lam) < best[0]:
                    best = (abs(lam), s, x, sx)
        if best is None:
            return
        _, s, x, sx = best
        for end in (0, 1):
            if s.closed or abs(sx - s.s_end(end)) > r.t / 2.0 + 1.0:
                continue
            if (id(s), end) in self.node_of:
                x = s.end_point(end)
                r.move(e, x)
                self._node([(r, e), (s, end)])
                self.stems.append((r, e, s))
                return
            self.report["issues"].append(
                f"a wall end at {_pt(p)} meets the free end of another wall at {_pt(x)}; left free"
            )
            return
        r.move(e, x)
        self._node([(r, e)])
        self.stems.append((r, e, s))
        s.stems.append(_band(s, r, x))
        s.links += 1
        r.links += 1


# --- the inferrer -----------------------------------------------------------------------------


class _Inferrer:
    def __init__(self, path: Path, opts: InferOptions) -> None:
        self.path = path
        self.o = opts
        self.report: dict[str, Any] = {
            "file": str(path),
            "verdict": "",
            "units": {},
            "layers": {},
            "counts": {},
            "walls": [],
            "openings": [],
            "unclassified_gaps": [],
            "unmatched_evidence": [],
            "unpaired": {},
            "dropped": [],
            "assumptions": [],
            "issues": [],
            "ignored": {},
        }

    # 1. load

    def load(self) -> _Drawing:
        if not self.path.is_file():
            raise InferError(f"path {str(self.path)!r} is not a file")
        try:
            source = as_dxf(self.path)
        except DxfPlanError as exc:
            raise InferError(str(exc)) from None
        try:
            doc = ezdxf.readfile(source)
        except (OSError, ezdxf.DXFStructureError) as exc:
            raise InferError(f"cannot read {self.path.name} as DXF: {exc}") from None
        finally:
            if source != self.path:
                shutil.rmtree(source.parent, ignore_errors=True)
        self.doc = doc
        return _Reader().read(doc)

    # 2. roles and units

    def roles(self, dwg: _Drawing) -> None:
        layers = sorted(dwg.layers)
        self.layer_role, self.how = _layer_roles(layers, self.o.layers)
        for prim in [*dwg.lines, *dwg.arcs]:
            prim.role = _effective_role(self.layer_role.get(prim.layer.upper(), "other"), prim.block_role)
        for t in dwg.texts:
            t.role = self.layer_role.get(t.layer.upper(), "other")

    def units(self, dwg: _Drawing) -> float:
        code = int(self.doc.header.get("$INSUNITS", 0) or 0)
        header = INSUNITS.get(code)
        info: dict[str, Any] = {"insunits": code, "header": header}
        self.walls_pair = False
        if self.o.units != "auto":
            k = UNIT_MM[self.o.units]
            info.update(unit=self.o.units, scale_to_mm=k, source="units parameter")
            self.report["units"] = info
            return k
        walls = [ln for ln in dwg.lines if ln.role == "wall"]
        probe = walls or [ln for ln in dwg.lines if ln.role not in ("ignore", "door", "window")]
        faces = sorted(_fuse(probe), key=lambda f: -f.length)[:4000]  # the longest faces decide
        hk = UNIT_MM[header] if header else None
        evidence: dict[float, int] = {}
        for k in ([hk] if hk else []) + [s for s in SCALES if s != hk]:
            evidence[k] = len(
                _pair_faces(
                    faces, self.o.thickness_min / k, self.o.thickness_max / k, self.o.min_overlap / k, mark=False
                )
            )
        best = max(evidence, key=lambda k: (evidence[k], k == 1.0))
        name = {v: n for n, v in UNIT_MM.items()}
        if hk and evidence[hk] >= max(1, 0.5 * evidence[best]):
            k, source = hk, "$INSUNITS"
        elif evidence[best] > 0:
            k, source = best, "wall evidence"
            if header:
                self.report["assumptions"].append(
                    f"$INSUNITS says {header}, but the walls pair only as {name[best]} "
                    f"({evidence[best]} face pairs against {evidence[hk]}); read as {name[best]}"
                )
            else:
                self.report["assumptions"].append(
                    f"no $INSUNITS; the walls pair as {name[best]} ({evidence[best]} face pairs); read as {name[best]}"
                )
        else:
            k, source = (hk or 1.0), "$INSUNITS" if hk else "default"
        info.update(unit=name[k], scale_to_mm=k, source=source, face_pairs={name[s]: n for s, n in evidence.items()})
        self.report["units"] = info
        self.walls_pair = bool(walls) and evidence[k] > 0
        return k

    def geometric_walls(self, dwg: _Drawing) -> None:
        """No wall layer by name or mapping (or none of them pairs): the layers whose lines pair are walls."""
        walls = [ln for ln in dwg.lines if ln.role == "wall"]
        if walls and self.how.get("wall") == "mapping":
            return
        if walls and (
            self.walls_pair
            or _pair_faces(_fuse(walls), self.o.thickness_min, self.o.thickness_max, self.o.min_overlap, False)
        ):
            return
        score: dict[str, float] = {}
        by_layer: dict[str, list[_Line]] = defaultdict(list)
        for ln in dwg.lines:
            if ln.role == "other" and ln.block_role != "furniture":
                by_layer[ln.layer.upper()].append(ln)
        for layer, lines in by_layer.items():
            pieces = _pair_faces(_fuse(lines), self.o.thickness_min, self.o.thickness_max, self.o.min_overlap, False)
            score[layer] = sum(p.length for p in pieces if not p.stack)
        top = max(score.values(), default=0.0)
        if top <= 0:
            return
        chosen = sorted(layer for layer, v in score.items() if v >= 0.25 * top)
        for layer in chosen:
            self.layer_role[layer] = "wall"
        for prim in [*dwg.lines, *dwg.arcs]:
            if prim.layer.upper() in chosen and prim.block_role is None:
                prim.role = "wall"
        self.how["wall"] = "geometry"
        self.report["assumptions"].append(
            "no layer is named as walls; walls read from the layers whose lines pair as wall faces: "
            + ", ".join(f"{layer} ({_r(score[layer] / 1000.0)} m of axis)" for layer in chosen)
        )

    # 3-4. pieces and runs

    def runs(self, dwg: _Drawing) -> list[_Run]:
        o = self.o
        wall_arcs = [a for a in dwg.arcs if a.role == "wall"]
        self.arc_faces = _fuse_arcs(wall_arcs)
        arc_pieces = _pair_arcs(self.arc_faces, o.thickness_min, o.thickness_max, o.min_overlap)
        paired_arcs = {id(a) for f in self.arc_faces if f.paired for a in f.members}
        swings = [
            a
            for a in dwg.arcs
            if id(a) not in paired_arcs
            and a.role in ("wall", "door", "other")
            and math.radians(60) <= a.sweep <= math.radians(120)
            and 250.0 <= a.r <= o.max_opening
        ]
        leaves = 0
        swing_grid = _Grid()
        for a in swings:
            swing_grid.add(_box([a.c], JAMB_REACH_MM), a)
        for ln in dwg.lines:
            if ln.role != "wall" or ln.hatch:
                continue
            near = swing_grid.query(_box([ln.a, ln.b]))
            if near and _is_leaf(ln, near):
                ln.role = "door"
                leaves += 1
        if leaves:
            self.report["assumptions"].append(
                f"{leaves} lines on wall layers run from a swing arc's centre along its radius: read as door leaves"
            )
        wall_lines = [ln for ln in dwg.lines if ln.role == "wall"]
        self.faces = _fuse(wall_lines)
        pieces = _pair_faces(self.faces, o.thickness_min, o.thickness_max, o.min_overlap) + arc_pieces
        self.pieces = pieces
        snapped = sum(1 for p in pieces if abs(_snap(p.t) - p.t) > 1e-3)
        if snapped:
            self.report["assumptions"].append(
                f"{snapped} wall thicknesses within {SNAP_WITHIN_MM:g} mm of a multiple of {SNAP_STEP_MM:g} mm "
                "were snapped to it"
            )
        used_arcs = {id(a) for f in self.arc_faces if f.paired for a in f.members}
        loose = [ln for ln in dwg.lines if ln.role in ("wall", "window", "other") and not ln.hatch]
        self.evidence = _Evidence(
            arcs=[
                a
                for a in dwg.arcs
                if id(a) not in used_arcs
                and a.role not in ("ignore", "room")
                and a.block_role != "furniture"
                and not a.hatch
                and 250.0 <= a.r <= o.max_opening
                and math.radians(20) <= a.sweep <= math.radians(200)
            ],
            doors=_items(dwg.lines, dwg.arcs, "door"),
            windows=_items(dwg.lines, dwg.arcs, "window"),
            glass=loose,
        ).index()
        self.face_grid = _Grid()
        for f in self.faces:
            self.face_grid.add(_box([f.a, f.b]), f)
        groups: list[tuple[_Frame, list[list[float]], bool]] = []
        for frame, spans in _group_lines(pieces):
            groups.append((frame, _merge_spans(spans), False))
        for frame, spans, full in _group_arcs(pieces):
            groups.append((frame, _merge_spans(spans), full))
        runs: list[_Run] = []
        self.gap_log: list[dict[str, Any]] = []
        for frame, spans, full in groups:
            if full:
                runs.append(_Run(frame, 0.0, frame.r * math.tau, [(0.0, frame.r * math.tau)], closed=True))
                continue
            runs.extend(self._split(frame, spans, groups))
        return runs

    def _bridged(self, frame: _Frame, g0: float, g1: float) -> bool:
        """A wall face line runs across the gap on either side of the axis."""
        half = frame.t / 2.0
        if frame.kind == "arc":
            for f in self.arc_faces:
                if math.dist(f.c, frame.c) > CENTRE_TOL_MM:
                    continue
                if min(abs(f.r - (frame.r - half)), abs(f.r - (frame.r + half))) > FACE_TOL_MM:
                    continue
                if f.sweep >= math.tau - 1e-9:
                    return True
                lo = frame.local(_add(f.c, _mul(_dir(f.t0), f.r)))[0]
                if lo <= g0 + 1.0 and lo + f.sweep * frame.r >= g1 - 1.0:
                    return True
            return False
        box = _box([frame.point(g0, -half), frame.point(g0, half), frame.point(g1, -half), frame.point(g1, half)], 5.0)
        for f in self.face_grid.query(box):
            if _angle_diff(f.phi, frame.phi) > ANGLE_TOL:
                continue
            (sa, na), (sb, nb) = frame.local(f.a), frame.local(f.b)
            on = [abs(abs(n) - half) <= FACE_TOL_MM for n in (na, nb)]
            if not all(on) or na * nb < 0:
                continue
            if min(sa, sb) <= g0 + 1.0 and max(sa, sb) >= g1 - 1.0:
                return True
        return False

    def _crossed(self, frame: _Frame, g0: float, g1: float, groups: list) -> bool:
        """Another wall's band fills the gap: the run crosses it."""
        if frame.kind != "line":
            return False
        for other, spans, _ in groups:
            if other is frame or other.kind != "line":
                continue
            den = _cross(frame.u, other.u)
            if abs(den) < math.sin(math.radians(MIN_JUNCTION_DEG)):
                continue
            x = _intersections(frame, other)[0]
            sx, so = frame.local(x)[0], other.local(x)[0]
            h = other.t / 2.0 / abs(den)
            if not any(lo - frame.t <= so <= hi + frame.t for lo, hi in spans):
                continue
            if sx - h <= g0 + 2.0 and sx + h >= g1 - 2.0:
                return True
        return False

    def _split(self, frame: _Frame, spans: list[list[float]], groups: list) -> list[_Run]:
        """One axis line's merged pieces as runs, joined across closed gaps and openings."""
        o = self.o
        out: list[_Run] = []
        cur = _Run(frame, spans[0][0], spans[0][1], [tuple(spans[0])])
        for lo, hi in spans[1:]:
            g0, g1 = cur.b, lo
            width = g1 - g0
            join = None
            if self._bridged(frame, g0, g1) or self._crossed(frame, g0, g1, groups):
                join = "closed"
            elif width < o.gap_min:
                join = "closed"
                self.report["issues"].append(
                    f"closed an unexplained {_r(width)} mm gap in a {_r(frame.t)} mm wall at "
                    f"{_pt(frame.point((g0 + g1) / 2.0))}"
                )
            elif width <= o.max_opening:
                gap = _classify(frame, g0, g1, self._near(frame, g0, g1))
                if gap.kind:
                    join = "opening"
                elif width <= o.gap_max:
                    self._unclassified(frame, gap)
                    join = "opening" if o.default_opening != "skip" else None
            if join is None:
                out.append(cur)
                cur = _Run(frame, lo, hi, [(lo, hi)])
                continue
            if join == "opening":
                cur.openings.append(gap)
            cur.b = hi
            cur.paired.append((lo, hi))
        out.append(cur)
        for run in out:
            run.a0, run.b0 = run.a, run.b
            if frame.kind == "arc":
                run.frame = replace(frame, s_mid=(run.a + run.b) / 2.0)
        return out

    def _near(self, frame: _Frame, g0: float, g1: float) -> _Evidence:
        """The evidence that can explain a gap: within its width (a swing) of its footprint."""
        reach = (g1 - g0) + frame.t + JAMB_REACH_MM
        pts = [frame.point(g0), frame.point(g1), frame.point((g0 + g1) / 2.0)]
        return self.evidence.near(_box(pts, reach))

    def _unclassified(self, frame: _Frame, gap: _Gap) -> None:
        policy = self.o.default_opening
        entry = {
            "at": _pt(frame.point((gap.s0 + gap.s1) / 2.0)),
            "width": _r(gap.s1 - gap.s0),
            "wall_thickness": _r(frame.t),
            "emitted_as": policy,
        }
        self.report["unclassified_gaps"].append(entry)
        gap.evidence = "unclassified gap"
        if policy != "skip":
            gap.kind = policy
            gap.assumed.append(f"nothing drawn in the gap; emitted as a {policy} (default_opening)")
        gap.emitted = policy != "skip"
        gap.entry = entry

    def windows_in_walls(self, runs: list[_Run]) -> None:
        """Window linework or blocks drawn inside a continuous wall (faces unbroken) are windows, and so are
        lines drawn inside a wall between its faces over an opening's length; longer ones (a cavity or
        insulation line) are ignored and reported."""
        for run in runs:
            if run.frame.kind != "line":
                continue
            for lo, hi in _merge_spans(run.frame.stacks):
                lo, hi = max(lo, run.a), min(hi, run.b)
                if hi - lo < 300.0 or any(g.s0 - 1.0 <= hi and lo <= g.s1 + 1.0 for g in run.openings):
                    continue
                if hi - lo > self.o.max_opening:
                    self.report["assumptions"].append(
                        f"a line runs inside the {_r(run.t)} mm wall from {_pt(run.frame.point(lo))} to "
                        f"{_pt(run.frame.point(hi))} (a cavity or insulation line?); ignored"
                    )
                    continue
                run.openings.append(_Gap(lo, hi, "window", evidence="lines inside the wall between its faces"))
            half = run.t / 2.0
            spans = []
            near = self.evidence.near(run.bbox(run.t)).windows
            for pts, name in near:
                loc = [run.frame.local(p) for p in pts]
                if max(abs(n) for _, n in loc) > half + FACE_TOL_MM:
                    continue
                lo, hi = max(run.a, min(s for s, _ in loc)), min(run.b, max(s for s, _ in loc))
                if hi - lo >= 300.0:
                    spans.append((lo, hi, name))
            for lo, hi, name in spans:
                if any(g.s0 - 1.0 <= lo <= g.s1 + 1.0 or g.s0 - 1.0 <= hi <= g.s1 + 1.0 for g in run.openings):
                    continue
                merged = [s for s in spans if s[0] <= hi and s[1] >= lo]
                lo2, hi2 = min(s[0] for s in merged), max(s[1] for s in merged)
                if any(abs(g.s0 - lo2) < 1.0 and abs(g.s1 - hi2) < 1.0 for g in run.openings):
                    continue
                run.openings.append(_Gap(lo2, hi2, "window", evidence=f"window linework inside the wall on {name}"))

    # 5. junctions

    def join(self, runs: list[_Run]) -> _Junctions:
        j = _Junctions(runs, self.report)
        j.corners()
        j.smooth()
        j.tees()
        self.end_gaps(runs, j)
        j.tees()
        return j

    def end_gaps(self, runs: list[_Run], j: _Junctions) -> None:
        """A free end that faces another wall across an opening-sized gap: the gap is classified like any
        other, and an opening extends the run to the wall it faces."""
        o = self.o
        for r in runs:
            for e in (0, 1):
                if not j.free(r, e) or r.frame.kind != "line":
                    continue
                p, d = r.end_point(e), r.out_dir(e)
                ahead = j.grid.query(_box([p, _add(p, _mul(d, o.max_opening + o.thickness_max))], r.t))
                best = None
                for s in ahead:
                    if s is r:
                        continue
                    for x in _intersections(r.frame, s.frame):
                        lam = r.beyond(e, x)
                        sin = _sin_at(r, s, x)
                        if sin < math.sin(math.radians(MIN_JUNCTION_DEG)):
                            continue
                        face = lam - s.t / 2.0 / sin
                        sx = s.frame.local(x)[0]
                        if not (o.gap_min <= face <= o.max_opening) or not (s.a - 1.0 <= sx <= s.b + 1.0):
                            continue
                        if best is None or lam < best[0]:
                            best = (lam, s, x, face)
                if best is None:
                    continue
                lam, s, x, face = best
                g0 = r.s_end(e)
                g1 = g0 + face if e == 1 else g0 - face
                lo, hi = min(g0, g1), max(g0, g1)
                if any(t is not r and t is not s and self._blocks(t, r, p, d, face) for t in ahead):
                    continue
                gap = _classify(r.frame, lo, hi, self._near(r.frame, lo, hi))
                if not gap.kind:
                    if face > o.gap_max:
                        continue
                    self._unclassified(r.frame, gap)
                    if not gap.emitted:
                        continue
                r.openings.append(gap)
                r.move(e, x)
                sx = s.frame.local(x)[0]
                hit_end = next((k for k in (0, 1) if not s.closed and abs(sx - s.s_end(k)) <= r.t / 2.0 + 1.0), None)
                if hit_end is not None and j.free(s, hit_end):
                    s.move(hit_end, x)
                    j._node([(r, e), (s, hit_end)])
                elif hit_end is not None:
                    r.move(e, s.end_point(hit_end))
                    j._node([(r, e), (s, hit_end)])
                    j.stems.append((r, e, s))
                else:
                    j._node([(r, e)])
                    j.stems.append((r, e, s))
                    s.stems.append(_band(s, r, x))
                    s.links += 1
                    r.links += 1

    @staticmethod
    def _blocks(t: _Run, r: _Run, p: Vec, d: Vec, dist: float) -> bool:
        """Run t stands in the way of the ray from p along d within dist: it crosses the ray, or it continues
        r's own axis ahead (that gap between the two was classified already)."""
        n = (-d[1], d[0])
        if t.frame.kind == "line" and abs(_cross(t.frame.u, d)) < math.sin(ANGLE_TOL):
            for end in (0, 1):
                q = t.end_point(end)
                if abs(_dot(n, _sub(q, p))) <= (t.t + r.t) / 2.0 and 0.0 < _dot(_sub(q, p), d) < dist:
                    return True
            return False
        ray = _Frame("line", 0.0, phi=_canon(d), u=d, n=n, w=_dot(n, p))
        for x in _intersections(t.frame, ray):
            along = _dot(_sub(x, p), d)
            if 1.0 < along < dist - 1.0 and t.a <= t.frame.local(x)[0] <= t.b:
                return True
        return False

    # 6. nodes, isolation, chains

    def resolve(self, runs: list[_Run], j: _Junctions) -> list[list[tuple[_Run, int]]]:
        """Chains of runs as walls: [(run, forward 1 / reversed -1), ...]; closed when it loops."""
        partner: dict[tuple[int, int], tuple[_Run, int]] = {}
        for members in j.nodes.values():
            ends = [(r, e) for r, e in members]
            if len(ends) < 2:
                continue
            for r, _ in ends:
                r.links += 1
            pair = self._chain_pair(ends)
            if pair is not None:
                (r1, e1), (r2, e2) = pair
                partner[(id(r1), e1)] = (r2, e2)
                partner[(id(r2), e2)] = (r1, e1)
            elif len(ends) >= 3:
                keep = sorted(ends, key=lambda m: -(m[0].b - m[0].a))[:2]
                for r, e in ends:
                    if (r, e) in keep:
                        continue
                    p = r.end_point(e)
                    back = max(x.t for x, _ in keep) / 2.0 + 1.0
                    r.move(e, r.frame.point(r.s_end(e) + (back if e == 0 else -back)))
                    self.report["issues"].append(
                        f"{len(ends)} walls end at {_pt(p)} and no two of them share a thickness; one was pulled "
                        f"back {_r(back)} mm and left free"
                    )
            if len(ends) == 2 and pair is None:
                (r1, e1), (r2, e2) = ends
                if _dot(r1.out_dir(e1), r2.out_dir(e2)) < -math.cos(math.radians(MIN_JUNCTION_DEG)):
                    r1.move(e1, r1.frame.point(r1.s_end(e1) + (1.0 if e1 == 0 else -1.0)))
                    self.report["issues"].append(
                        f"two walls of {_r(r1.t)} and {_r(r2.t)} mm continue in line at {_pt(r2.end_point(e2))}; "
                        "the solver cannot join different thicknesses in line, so one stops 1 mm short"
                    )
        # isolation
        keep: list[_Run] = []
        for r in runs:
            if r.closed or r.links > 0 or len(runs) == 1:
                keep.append(r)
                continue
            self.report["dropped"].append(
                {
                    "what": "wall run",
                    "axis": [_pt(r.frame.point(r.a)), _pt(r.frame.point(r.b))],
                    "thickness": _r(r.t),
                    "reason": "touches no other wall (furniture, a detail or a stray pair of lines?)",
                }
            )
        alive = {id(r) for r in keep}
        chains: list[list[tuple[_Run, int]]] = []
        seen: set[int] = set()

        def walk(start: _Run, forward: int) -> list[tuple[_Run, int]]:
            chain = [(start, forward)]
            seen.add(id(start))
            r, d = start, forward
            while True:
                nxt = partner.get((id(r), 1 if d == 1 else 0))
                if nxt is None:
                    return chain
                r2, e2 = nxt
                if id(r2) not in alive or id(r2) in seen:
                    return chain
                d = 1 if e2 == 0 else -1
                chain.append((r2, d))
                seen.add(id(r2))
                r = r2

        for r in keep:
            if id(r) in seen or r.closed:
                continue
            open_end = next(
                (e for e in (0, 1) if partner.get((id(r), e)) is None or id(partner[(id(r), e)][0]) not in alive),
                None,
            )
            if open_end is None:
                continue
            chains.append(walk(r, 1 if open_end == 0 else -1))
        for r in keep:
            if id(r) in seen:
                continue
            if r.closed:
                seen.add(id(r))
                chains.append([(r, 1)])
                continue
            chains.append(walk(r, 1))
        self.partner = partner
        self.joined: dict[tuple[int, int], list[_Run]] = defaultdict(list)
        for members in j.nodes.values():
            for r, e in members:
                self.joined[(id(r), e)] += [m for m, _ in members if m is not r]
        for r, e, host in j.stems:
            self.joined[(id(r), e)].append(host)
        return chains

    @staticmethod
    def _chain_pair(ends: list[tuple[_Run, int]]) -> tuple[tuple[_Run, int], tuple[_Run, int]] | None:
        best = None
        for i, (r1, e1) in enumerate(ends):
            for r2, e2 in ends[i + 1 :]:
                if r1 is r2 or abs(r1.t - r2.t) > 0.5:
                    continue
                cos = _dot(r1.out_dir(e1), r2.out_dir(e2))
                if cos > math.cos(math.radians(MIN_JUNCTION_DEG)):
                    continue
                score = abs(cos + 1.0) if r1.frame.kind != r2.frame.kind else abs(cos)
                if best is None or score < best[0]:
                    best = (score, (r1, e1), (r2, e2))
        return None if best is None else (best[1], best[2])

    # 7. walls and openings

    def walls(self, chains: list[list[tuple[_Run, int]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        walls: list[tuple[tuple[float, float], dict[str, Any], list[tuple[_Run, int]]]] = []
        for chain in chains:
            verts: list[list[float]] = []
            closed = len(chain) > 0 and self._loops(chain)
            for k, (r, d) in enumerate(chain):
                s0, s1 = (r.a, r.b) if d == 1 else (r.b, r.a)
                p0 = r.end_point(0 if d == 1 else 1)
                if r.closed:
                    half = r.frame.point(r.a + (r.b - r.a) / 2.0)
                    verts += [[*p0, 1.0], [*half, 1.0]]
                    continue
                if r.frame.kind == "line":
                    verts.append(list(p0))
                else:
                    sweep = (s1 - s0) / r.frame.r  # signed: positive counter-clockwise
                    if abs(sweep) > math.pi + 1e-9:
                        midp = r.frame.point((s0 + s1) / 2.0)
                        b = math.tan(sweep / 8.0)
                        verts += [[*p0, b], [*midp, b]]
                    else:
                        verts.append([*p0, math.tan(sweep / 4.0)])
                if k == len(chain) - 1 and not closed:
                    verts.append(list(r.end_point(1 if d == 1 else 0)))
            closed = closed or chain[0][0].closed
            verts = self._orient(verts, closed)
            raw = {
                "v": 1,
                "kind": "wall",
                "id": "",
                "axis": [
                    [_r(v[0]), _r(v[1])] + ([round(v[2], 9)] if len(v) > 2 and abs(v[2]) > 1e-9 else []) for v in verts
                ],
                "thickness": _r(chain[0][0].t),
                "justification": "center",
                "material": "brick",
                "closed": closed,
            }
            first = raw["axis"][0]
            walls.append(((first[1], first[0]), raw, chain))
        walls.sort(key=lambda w: (w[0], -len(w[1]["axis"])))
        out_walls, out_open = [], []
        doors = windows = 0
        pending: list[tuple[int, float, dict[str, Any]]] = []
        for i, (_, raw, chain) in enumerate(walls, start=1):
            raw["id"] = f"W{i}"
            for r, _ in chain:
                r.id = raw["id"]
            out_walls.append(raw)
            for op in self._openings(raw, chain):
                pending.append((i, op["offset"], op))
        for _, _, op in sorted(pending, key=lambda p: (p[0], p[1])):
            if op["opening_kind"] == "door":
                doors += 1
                op["id"] = f"D{doors}"
            else:
                windows += 1
                op["id"] = f"V{windows}"
            out_open.append(op)
        return out_walls, out_open

    @staticmethod
    def _loops(chain: list[tuple[_Run, int]]) -> bool:
        if len(chain) < 2:
            return False
        r0, d0 = chain[0]
        rl, dl = chain[-1]
        start = r0.end_point(0 if d0 == 1 else 1)
        end = rl.end_point(1 if dl == 1 else 0)
        return math.dist(start, end) < 1e-6

    @staticmethod
    def _orient(verts: list[list[float]], closed: bool) -> list[list[float]]:
        """Closed rings counter-clockwise from their bottom-left vertex; open polylines from the bottom-left end."""

        def bulges(vs: list[list[float]]) -> list[float]:
            return [v[2] if len(v) > 2 else 0.0 for v in vs]

        def reverse(vs: list[list[float]]) -> list[list[float]]:
            n = len(vs)
            b = bulges(vs)
            pts = [v[:2] for v in vs]
            if closed:
                rp = [pts[0], *pts[1:][::-1]]
                rb = [-b[(-k - 1) % n] for k in range(n)]
            else:
                rp = pts[::-1]
                rb = [-b[n - k - 2] for k in range(n - 1)] + [0.0]
            return [[*p, bb] if abs(bb) > 1e-12 else list(p) for p, bb in zip(rp, rb, strict=True)]

        if not closed:
            if (round(verts[-1][1], 1), round(verts[-1][0], 1)) < (round(verts[0][1], 1), round(verts[0][0], 1)):
                return reverse(verts)
            return [list(v) if len(v) < 3 or abs(v[2]) > 1e-12 else v[:2] for v in verts]
        if ring_area([tuple(v) for v in verts]) < 0:
            verts = reverse(verts)
        k = min(range(len(verts)), key=lambda i: (round(verts[i][1], 1), round(verts[i][0], 1)))
        return verts[k:] + verts[:k]

    def _openings(self, wall: dict[str, Any], chain: list[tuple[_Run, int]]) -> list[dict[str, Any]]:
        """The openings of a wall's runs as records, offsets measured along the final axis."""
        axis = wall["axis"]
        n = len(axis)
        segs = []
        cum = 0.0
        for i in range(n if wall["closed"] else n - 1):
            p, q = axis[i], axis[(i + 1) % n]
            b = p[2] if len(p) > 2 else 0.0
            if abs(b) < 1e-12:
                length = math.dist(p[:2], q[:2])
                segs.append(("line", tuple(p[:2]), tuple(q[:2]), b, cum, length))
            else:
                _, r, _, sweep = _bulge_arc(tuple(p[:2]), tuple(q[:2]), b)
                length = r * sweep
                segs.append(("arc", tuple(p[:2]), tuple(q[:2]), b, cum, length))
            cum += length
        out = []
        for run, _ in chain:
            bands = list(run.stems)
            for end in (0, 1):
                if self.joined.get((id(run), end)):
                    bands.append(self._end_zone(run, end))
            for g in run.openings:
                if not g.emitted or not g.kind:
                    continue
                lo, hi = g.s0, g.s1
                blocked = False
                for b0, b1 in bands:
                    if lo < b1 - OPENING_GUARD_MM and b0 < hi - OPENING_GUARD_MM:
                        inside = lo >= b0 - 1.0 and hi <= b1 + 1.0
                        if inside or (hi - lo) - (min(hi, b1) - max(lo, b0)) < 300.0:
                            blocked = True
                            break
                        if lo < b0 < hi:
                            hi = b0 - OPENING_GUARD_MM
                        else:
                            lo = b1 + OPENING_GUARD_MM
                    elif abs(lo - b1) <= OPENING_GUARD_MM:
                        lo = b1 + OPENING_GUARD_MM
                    elif abs(hi - b0) <= OPENING_GUARD_MM:
                        hi = b0 - OPENING_GUARD_MM
                lo, hi = max(lo, run.a + OPENING_GUARD_MM), min(hi, run.b - OPENING_GUARD_MM)
                where = _pt(run.frame.point((g.s0 + g.s1) / 2.0))
                if blocked or hi - lo < 100.0:
                    self.report["dropped"].append(
                        {
                            "what": f"{g.kind} opening",
                            "at": where,
                            "width": _r(g.s1 - g.s0),
                            "reason": "runs into a junction",
                        }
                    )
                    continue
                rec = self._record(run, g, lo, hi, segs, wall["id"])
                if rec is None:
                    self.report["dropped"].append(
                        {"what": f"{g.kind} opening", "at": where, "width": _r(g.s1 - g.s0), "reason": "spans a corner"}
                    )
                    continue
                out.append(rec)
        return out

    def _end_zone(self, run: _Run, end: int) -> tuple[float, float]:
        """The stretch at a joined end that the other walls' footprints and the mitre occupy."""
        s = run.s_end(end)
        p = run.end_point(end)
        h = 0.0
        for other in self.joined.get((id(run), end), []):
            sin = _sin_at(run, other, p)
            if sin < math.sin(math.radians(MIN_JUNCTION_DEG)):
                continue  # runs in line with this end: it occupies none of it
            cos = math.sqrt(max(0.0, 1.0 - sin * sin))
            h = max(h, other.t / 2.0 / sin + run.t / 2.0 * cos / sin)
        return (s - h, s) if end == 1 else (s, s + h)

    def _record(self, run: _Run, g: _Gap, lo: float, hi: float, segs: list, wall_id: str) -> dict[str, Any] | None:
        mid = run.frame.point((lo + hi) / 2.0)
        jamb0, jamb1 = run.frame.point(lo), run.frame.point(hi)
        best = None
        for kind, p, q, b, cum, length in segs:
            if kind == "line":
                d = _sub(q, p)
                u = _mul(d, 1.0 / math.hypot(*d))
                s = _dot(_sub(mid, p), u)
                lat = abs(_cross(u, _sub(mid, p)))
                if -1.0 <= s <= length + 1.0 and (best is None or lat < best[0]):
                    best = (lat, ("line", p, u, cum, length))
            else:
                c, r, t0, sweep = _bulge_arc(p, q, b)
                th = math.atan2(mid[1] - c[1], mid[0] - c[0])
                rel = (th - t0) % math.tau
                lat = abs(math.dist(mid, c) - r)
                if rel <= sweep + 1e-6 and (best is None or lat < best[0]):
                    best = (lat, ("arc", (c, r, t0, sweep, b > 0, p), None, cum, length))
        if best is None or best[0] > max(run.t, 5.0):
            return None
        kind, geo, u, cum, length = best[1]

        def along(pt: Vec) -> float:
            if kind == "line":
                return _dot(_sub(pt, geo), u)
            c, r, t0, sweep, ccw, _ = geo
            th = math.atan2(pt[1] - c[1], pt[0] - c[0])
            rel = (th - t0) % math.tau
            if rel > sweep + 0.5:
                rel -= math.tau
            return r * rel if ccw else r * (sweep - rel)

        def left_normal(pt: Vec) -> Vec:
            if kind == "line":
                return (-u[1], u[0])
            c, _, _, _, ccw, _ = geo
            inward = _sub(c, pt)
            k = 1.0 / max(math.hypot(*inward), 1e-9)
            return _mul(inward, k) if ccw else _mul(inward, -k)

        a0, a1 = along(jamb0), along(jamb1)
        near, far = min(a0, a1), max(a0, a1)
        if near < -1.0 or far > length + 1.0:
            return None
        near, far = max(near, 0.0), min(far, length)
        offset = math.ceil((cum + near) * 10.0 - 1e-6) / 10.0
        width = math.floor((cum + far) * 10.0 + 1e-6) / 10.0 - offset
        swing, hand = "in", "left"
        if g.kind == "door" and g.swing and g.hinge is not None:
            side_pt = run.frame.point((lo + hi) / 2.0, g.swing * run.t)
            swing_in = _dot(_sub(side_pt, mid), left_normal(mid)) > 0
            hinge_far = along(run.frame.point(g.hinge)) > (near + far) / 2.0
            swing = "in" if swing_in else "out"
            hand = "left" if hinge_far == swing_in else "right"
        rec = {
            "v": 1,
            "kind": "opening",
            "id": "",
            "wall": wall_id,
            "opening_kind": g.kind,
            "offset": max(0.0, offset),
            "width": _r(width),
            "sill": None if g.kind == "door" else self.o.window_sill,
            "height": self.o.door_height if g.kind == "door" else self.o.window_height,
            "swing": swing,
            "hand": hand,
            "tag": None,
        }
        self.report["openings"].append(
            {
                "at": _pt(mid),
                "kind": g.kind,
                "width": rec["width"],
                "evidence": g.evidence,
                **({"assumed": g.assumed} if g.assumed else {}),
                "_rec": rec,
            }
        )
        if g.entry is not None:
            g.entry["wall"] = wall_id
            g.entry["_rec"] = rec
        return rec

    # 8. rooms

    def rooms(self, dwg: _Drawing, walls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        texts = [t for t in dwg.texts if t.role == "room"]
        source = "room layers"
        if not texts:
            xs = [v[0] for w in walls for v in w["axis"]]
            ys = [v[1] for w in walls for v in w["axis"]]
            if not xs:
                return []
            box = (min(xs), min(ys), max(xs), max(ys))
            texts = [
                t
                for t in dwg.texts
                if t.role not in ("ignore",) and box[0] <= t.at[0] <= box[2] and box[1] <= t.at[1] <= box[3]
            ]
            source = "texts inside the walls (no room layer)"
        names, numbers, areas = [], [], []
        for t in texts:
            lines = [s.strip() for s in t.text.splitlines() if s.strip()]
            if not lines:
                continue
            head = lines[0]
            if m := AREA_TEXT.match(head):
                areas.append((t, float(m.group(1).replace(",", ".")) * 1e6))
                continue
            if NUMBER_TEXT.match(head) and not head.isalpha():
                numbers.append((t, head))
                continue
            if DIMENSION_TEXT.match(head) or sum(ch.isalpha() for ch in head) < 2:
                continue
            extra_num, extra_area = None, None
            for line in lines[1:]:
                if m := AREA_TEXT.match(line):
                    extra_area = float(m.group(1).replace(",", ".")) * 1e6
                elif NUMBER_TEXT.match(line):
                    extra_num = line
            names.append((t, head, extra_num, extra_area))
        used_n: set[int] = set()
        used_a: set[int] = set()
        out = []
        for t, name, num, area in sorted(names, key=lambda n: (round(n[0].at[1], 1), round(n[0].at[0], 1))):
            reach = max(t.height, 100.0) * 4.0

            def near(items: list, used: set[int], t: _Text = t, reach: float = reach) -> int | None:
                best = None
                for k, (o, _) in enumerate(items):
                    if k in used:
                        continue
                    dx, dy = o.at[0] - t.at[0], o.at[1] - t.at[1]
                    if abs(dx) > 2.0 * reach or abs(dy) > reach:
                        continue
                    d = math.hypot(dx, dy)
                    if best is None or d < best[0]:
                        best = (d, k)
                return None if best is None else best[1]

            if num is None and (k := near(numbers, used_n)) is not None:
                used_n.add(k)
                num = numbers[k][1]
            if area is None and (k := near(areas, used_a)) is not None:
                used_a.add(k)
                area = areas[k][1]
            out.append(
                {"v": 1, "kind": "room", "id": "", "name": name, "number": num, "at": _pt(t.at), "area": area or 0.0}
            )
        for i, r in enumerate(out, start=1):
            r["id"] = f"R{i}"
        self.report["layers"]["rooms_from"] = source
        return out

    # 9. the report

    def unpaired(self) -> None:
        count, length, longest = 0, 0.0, []
        for f in self.faces:
            if all(m.hatch for m in f.members):
                continue
            covered = _union_length(f.paired)
            rest = f.length - covered
            if rest <= max(self.o.thickness_max, 1.0) + 5.0 and covered > 0:
                continue
            if covered == 0 and f.length <= self.o.thickness_max + 5.0:
                continue  # a jamb or end cap
            if rest > 1.0:
                count += 1
                length += rest
                longest.append((rest, f))
        longest.sort(key=lambda x: -x[0])
        self.report["unpaired"] = {
            "faces": count,
            "length_mm": _r(length),
            "longest": [
                {"from": _pt(f.a), "to": _pt(f.b), "unpaired_mm": _r(rest), "layer": f.members[0].layer}
                for rest, f in longest[:LIST_MAX]
            ],
        }

    def finish(self, walls: list[dict], openings: list[dict], rooms: list[dict]) -> Plan:
        """The plan, every record checked on its own first: one that fails is dropped and reported."""
        kept_walls = []
        for w in walls:
            try:
                Wall.model_validate(w)
                kept_walls.append(w)
            except ValidationError as exc:
                self.report["dropped"].append(
                    {"what": "wall", "id": w["id"], "axis": w["axis"], "reason": describe_error(exc, f"wall {w['id']}")}
                )
        ids = {w["id"] for w in kept_walls}
        kept_open = []
        for op in openings:
            try:
                Opening.model_validate(op)
            except ValidationError as exc:
                self.report["dropped"].append({"what": "opening", "id": op["id"], "reason": describe_error(exc)})
                continue
            if op["wall"] in ids:
                kept_open.append(op)
            else:
                self.report["dropped"].append(
                    {"what": "opening", "id": op["id"], "reason": f"its wall {op['wall']} was dropped"}
                )
        kept_rooms = []
        for r in rooms:
            try:
                Room.model_validate(r)
                kept_rooms.append(r)
            except ValidationError as exc:
                self.report["dropped"].append({"what": "room", "id": r["id"], "reason": describe_error(exc)})
        raw = {"walls": kept_walls, "openings": kept_open, "rooms": kept_rooms}
        if not kept_walls:
            self.report["verdict"] = "no walls found"
            raise InferError(self._nothing())
        return parse_plan(raw)

    def _nothing(self) -> str:
        layers = self.report["layers"]
        wall_layers = layers.get("walls") or []
        if not wall_layers:
            return (
                f"{self.path.name}: no layer is named or mapped as walls and no layer's lines pair as wall faces "
                f"({self.o.thickness_min:g}-{self.o.thickness_max:g} mm apart); layers are "
                f"{', '.join(layers.get('all', [])) or 'none'}. Map the wall layers with layers={{'walls': [...]}}"
            )
        return (
            f"{self.path.name}: the wall layers {', '.join(wall_layers)} hold no pair of parallel faces "
            f"{self.o.thickness_min:g}-{self.o.thickness_max:g} mm apart overlapping {self.o.min_overlap:g} mm; "
            "check the units and the thickness range"
        )

    def run(self) -> InferResult:
        dwg = self.load()
        self.roles(dwg)
        k = self.units(dwg)
        dwg.scale(k)
        self.geometric_walls(dwg)
        by_role: dict[str, list[str]] = defaultdict(list)
        for layer in sorted(dwg.layers):
            by_role[self.layer_role.get(layer.upper(), "other")].append(layer)
        self.report["layers"] = {
            "walls": by_role.get("wall", []),
            "doors": by_role.get("door", []),
            "windows": by_role.get("window", []),
            "rooms": by_role.get("room", []),
            "ignored": by_role.get("ignore", []),
            "other": by_role.get("other", []),
            "how": {role: self.how.get(role, "names") for role in ("wall", "door", "window", "room", "ignore")},
            "all": sorted(dwg.layers),
        }
        role_blocks = Counter()
        for name, count in dwg.blocks.items():
            role_blocks[_block_role(name) or "other"] += count
        self.report["ignored"] = {
            "entity_types": dict(sorted(dwg.skipped.items())),
            "blocks": {
                name: n for name, n in sorted(dwg.blocks.items()) if _block_role(name) not in ("door", "window")
            },
        }
        runs = self.runs(dwg)
        self.windows_in_walls(runs)
        j = self.join(runs)
        chains = self.resolve(runs, j)
        walls, openings = self.walls(chains)
        rooms = self.rooms(dwg, walls)
        plan = self.finish(walls, openings, rooms)
        self.unpaired()
        self.summarise(plan, chains, runs, j)
        return InferResult(plan, self.report)

    def summarise(self, plan: Plan, chains: list, runs: list[_Run], j: _Junctions) -> None:
        rep = self.report
        kept = {o.id for o in plan.openings}
        order = {o.id: k for k, o in enumerate(plan.openings)}
        rep["openings"] = sorted(
            (
                {"id": e["_rec"]["id"], "wall": e["_rec"]["wall"], **{k: v for k, v in e.items() if k != "_rec"}}
                for e in rep["openings"]
                if e["_rec"]["id"] in kept
            ),
            key=lambda e: order[e["id"]],
        )
        for e in rep["unclassified_gaps"]:
            rec = e.pop("_rec", None)
            if rec is not None and rec["id"] in kept:
                e["id"] = rec["id"]
        by_wall: dict[str, list[_Run]] = defaultdict(list)
        for r in runs:
            if r.id:
                by_wall[r.id].append(r)
        free_ends = []
        for w in plan.walls:
            paired = total = 0.0
            for r in by_wall.get(w.id, []):
                spans = r.paired + r.stems  # a T stem explains the face it interrupts
                inner = [(max(lo, r.a), min(hi, r.b)) for lo, hi in spans if min(hi, r.b) > max(lo, r.a)]
                ext = max(0.0, r.a0 - r.a) + max(0.0, r.b - r.b0)
                holes = sum(g.s1 - g.s0 for g in r.openings if g.kind)
                paired += _union_length(inner)
                total += (r.b - r.a) - ext - holes
                for e in (0, 1):
                    if not r.closed and j.free(r, e) and (id(r), e) not in self.partner:
                        free_ends.append(_pt(r.end_point(e)))
            rep["walls"].append(
                {
                    "id": w.id,
                    "thickness": w.thickness,
                    "closed": w.closed,
                    "vertices": len(w.axis),
                    "length_mm": _r(polyline_length(w.axis, w.closed)),
                    "paired_pct": _r(100.0 * min(1.0, paired / total)) if total > 1.0 else 100.0,
                }
            )
        if free_ends:
            rep["issues"].append(
                f"{len(free_ends)} wall ends join nothing (a stub, or a missed junction): "
                + ", ".join(str(p) for p in free_ends[:LIST_MAX])
            )
        unused = [a for a in self.evidence.arcs if id(a) not in self.evidence.used_arcs and a.role == "door"]
        rep["unmatched_evidence"] = [
            {"what": "door swing arc", "centre": _pt(a.c), "radius": _r(a.r), "layer": a.block or a.layer}
            for a in unused[:LIST_MAX]
        ]
        o = self.o
        doors = sum(op.opening_kind == "door" for op in plan.openings)
        windows = len(plan.openings) - doors
        rep["counts"] = {
            "faces": len(self.faces),
            "pieces": len(self.pieces),
            "runs": len(runs),
            "walls": len(plan.walls),
            "doors": doors,
            "windows": windows,
            "rooms": len(plan.rooms),
        }
        units = rep["units"]
        rep["assumptions"] += [
            f"drawing units {units['unit']} ({units['source']}), 1 unit = {units['scale_to_mm']:g} mm",
            f"door height {o.door_height:g} mm, window sill {o.window_sill:g} mm and height {o.window_height:g} mm: "
            "a plan does not draw them",
            "every wall is centre-justified on the midline of its faces, material brick, storey height",
            "furniture, stairs, slabs, roofs and the site are not inferred",
        ]
        review = []
        if rep["unclassified_gaps"]:
            review.append(f"{len(rep['unclassified_gaps'])} unclassified gaps")
        if rep["dropped"]:
            review.append(f"{len(rep['dropped'])} dropped pieces")
        if rep["issues"]:
            review.append(f"{len(rep['issues'])} issues")
        low = [w["id"] for w in rep["walls"] if w["paired_pct"] < 90.0]
        if low:
            review.append(f"walls {', '.join(low)} under 90 % paired")
        head = f"draft: {len(plan.walls)} walls, {doors} doors, {windows} windows, {len(plan.rooms)} rooms"
        rep["verdict"] = head + ("; review " + ", ".join(review) if review else "; nothing flagged, still a draft")


def infer_plan(path: str | Path, options: InferOptions | None = None) -> InferResult:
    """Reads an ordinary architectural DXF and infers a draft plan and a report (see the module docstring).
    Raises InferError when the file cannot be read or holds no walls."""
    return _Inferrer(Path(path), options or InferOptions()).run()
