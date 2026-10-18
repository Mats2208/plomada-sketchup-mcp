"""The documentation of a written plan: the drawing a person reads in AutoCAD, on AIA-style layers.

read_plan never reads these layers (it reads records and PLOMADA-* / SITIO-*
layers only), so the documentation can follow drafting conventions freely:

- walls: both faces as polylines with real arcs, mitred where the axis turns
  (line-line, line-arc and arc-arc intersections of the offset curves), cut
  at every opening with a cap line across the wall at each jamb and at the
  wall's free ends; the solid poché between the faces as a HATCH;
- doors: the leaf drawn open at 90 degrees on the swing side, hinged as the
  extension hinges it (Geometry.door_parts: swing in opens to the left of the
  host axis; hand left hinges the far jamb for in, the near one for out), and
  the quarter swing ARC from the closed leaf to the open one; arched heads
  change nothing in plan;
- windows: the classic three lines across the opening, at both faces and the
  middle of the wall;
- rooms: the name centred on the room point and, when the plan gives an area,
  a second line in square metres (a room's area is not computed here: the
  plan does not say which walls bound it);
- stairs: the outline of every flight and landing as Geometry.stair_layout
  builds it, a line at every nosing, an arrow up the run and the label SUBE;
- columns outlined and solid hatched; slab edges and holes, railings, curtain
  wall faces, planters, pools and ramp edges outlined.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from ezdxf.enums import TextEntityAlignment
from ezdxf.layouts import Modelspace

from . import planar
from .models import Opening, Plan, Stair, Wall, arc_radius, segment_length

# Documentation layers: name -> (ACI colour, lineweight in 1/100 mm).
DOC_LAYERS: dict[str, tuple[int, int]] = {
    "A-WALL": (7, 50),  # wall faces and caps
    "A-WALL-PATT": (8, 0),  # solid poché between the faces
    "A-DOOR": (2, 25),  # door leaves and swings
    "A-GLAZ": (4, 25),  # window lines
    "A-GLAZ-CURT": (4, 35),  # curtain wall faces
    "A-AREA-IDEN": (7, 18),  # room names and areas
    "A-FLOR-STRS": (3, 25),  # stair outlines, nosings, arrows, labels
    "A-FLOR-OTLN": (8, 18),  # slab edges and holes
    "A-FLOR-HRAL": (6, 25),  # railings
    "A-FLOR-RAMP": (3, 25),  # ramp edges
    "A-COLS": (7, 35),  # column outlines
    "A-COLS-PATT": (8, 0),  # column hatch
    "A-PLNT": (3, 18),  # planters
    "A-POOL": (5, 25),  # pools
}
ROOM_TEXT_MM = 250.0  # letter height of a room name
AREA_TEXT_MM = 180.0  # letter height of a room area
STAIR_TEXT_MM = 150.0  # letter height of SUBE
ARROW_MM = 150.0  # half width of a stair arrow head

Vertices = Sequence[Sequence[float]]
Point = tuple[float, float]


def face_offsets(wall: Wall) -> tuple[float, float]:
    """The two face offsets of a wall, mm to the left of its axis."""
    t = wall.thickness
    return {"center": (t / 2.0, -t / 2.0), "left": (t, 0.0), "right": (0.0, -t)}[wall.justification]


# --- offset curves -------------------------------------------------------------------------


def _primitive(seg: planar.Segment, d: float) -> tuple[str, Point, Any]:
    """The segment's offset curve, unbounded: ("line", point, unit direction) or ("circle", centre, radius)."""
    p0, p1, b = seg
    if abs(b) <= 1e-12:
        _, t = planar.on_segment(p0, p1, b, 0.0)
        n = planar.left_normal(t)
        return "line", (p0[0] + n[0] * d, p0[1] + n[1] * d), t
    return "circle", planar.centre(p0, p1, b), arc_radius(math.dist(p0[:2], p1[:2]), b) - d * math.copysign(1.0, b)


def _crossings(a: tuple[str, Point, Any], b: tuple[str, Point, Any]) -> list[Point]:
    """Where two unbounded offset curves cross (none for parallel lines or circles apart)."""
    if a[0] == "circle" and b[0] == "line":
        a, b = b, a
    if a[0] == "line" and b[0] == "line":
        (p, t), (q, v) = (a[1], a[2]), (b[1], b[2])
        cross = t[0] * v[1] - t[1] * v[0]
        if abs(cross) <= 1e-12:
            return []
        s = ((q[0] - p[0]) * v[1] - (q[1] - p[1]) * v[0]) / cross
        return [(p[0] + t[0] * s, p[1] + t[1] * s)]
    if a[0] == "line":
        (p, t), (c, r) = (a[1], a[2]), (b[1], b[2])
        fx, fy = p[0] - c[0], p[1] - c[1]
        half_b = fx * t[0] + fy * t[1]
        disc = half_b * half_b - (fx * fx + fy * fy - r * r)
        if r <= 0 or disc < 0:
            return []
        root = math.sqrt(disc)
        return [(p[0] + t[0] * s, p[1] + t[1] * s) for s in (-half_b - root, -half_b + root)]
    (c1, r1), (c2, r2) = (a[1], a[2]), (b[1], b[2])
    d = math.dist(c1, c2)
    if r1 <= 0 or r2 <= 0 or d <= 1e-9 or d > r1 + r2 or d < abs(r1 - r2):
        return []
    k = (r1 * r1 - r2 * r2 + d * d) / (2.0 * d)
    h = math.sqrt(max(r1 * r1 - k * k, 0.0))
    mx, my = c1[0] + (c2[0] - c1[0]) * k / d, c1[1] + (c2[1] - c1[1]) * k / d
    ux, uy = -(c2[1] - c1[1]) / d, (c2[0] - c1[0]) / d
    return [(mx + ux * h, my + uy * h), (mx - ux * h, my - uy * h)]


class Faces:
    """Offset curves of one polyline (a wall axis, a ramp or curtain wall path): face(d, sa, sb) is the
    polyline d mm to the left from station sa to sb, mitred where the axis turns; on a closed polyline sb
    may pass the length to run across the first vertex."""

    def __init__(self, vertices: Vertices, closed: bool) -> None:
        self.closed = closed
        self.segs = planar.segments(vertices, closed)
        self.lens = [segment_length(*s) for s in self.segs]
        self.length = sum(self.lens)

    def _locate(self, s: float, after: bool) -> tuple[int, float]:
        """(unrolled segment index, distance into it) of station s; past the length on a closed polyline
        the segments repeat."""
        n = len(self.segs)
        count = 2 * n if self.closed else n
        base = 0.0
        for u in range(count):
            ln = self.lens[u % n]
            if s < base + ln - 1e-9 or (not after and s <= base + ln + 1e-9) or u == count - 1:
                return u, min(max(s - base, 0.0), ln)
            base += ln
        return 0, 0.0

    def _at(self, i: int, t: float, d: float) -> Point:
        (x, y), tan = planar.on_segment(*self.segs[i], t)
        n = planar.left_normal(tan)
        return x + n[0] * d, y + n[1] * d

    def joint(self, k: int, d: float) -> Point:
        """The face corner at vertex k (between segments k - 1 and k): where their offset curves cross,
        nearest the unmitred ends; those ends' midpoint when the curves do not cross near them."""
        n = len(self.segs)
        i, j = (k - 1) % n, k % n
        q_in, q_out = self._at(i, self.lens[i], d), self._at(j, 0.0, d)
        mid = ((q_in[0] + q_out[0]) / 2.0, (q_in[1] + q_out[1]) / 2.0)
        if math.dist(q_in, q_out) <= 1e-9:
            return q_in
        found = _crossings(_primitive(self.segs[i], d), _primitive(self.segs[j], d))
        best = min(found, key=lambda p: math.dist(p, mid), default=None)
        if best is None or math.dist(best, mid) > 10.0 * abs(d) + 1.0:
            return mid
        return best

    def _bulge(self, i: int, a: Point, b: Point, d: float) -> float:
        p0, p1, bulge = self.segs[i]
        if abs(bulge) <= 1e-12 or arc_radius(math.dist(p0[:2], p1[:2]), bulge) - d * math.copysign(1.0, bulge) <= 0:
            return 0.0
        return planar.bulge_between(planar.centre(p0, p1, bulge), a, b, bulge > 0)

    def face(self, d: float, sa: float, sb: float) -> list[list[float]]:
        """The face d mm left of the axis from station sa to sb, as [x, y] / [x, y, bulge] vertices. The ends
        are square to the axis; an inner vertex is the mitred joint."""
        n = len(self.segs)
        ua, ta = self._locate(sa, after=True)
        ub, tb = self._locate(sb, after=False)
        out: list[list[float]] = [list(self._at(ua % n, ta, d))]
        for u in range(ua, ub + 1):
            end = self._at(ub % n, tb, d) if u == ub else self.joint((u + 1) % n, d)
            if b := self._bulge(u % n, (out[-1][0], out[-1][1]), end, d):
                out[-1] = [out[-1][0], out[-1][1], b]
            out.append(list(end))
        return out

    def ring(self, d: float) -> list[list[float]]:
        """The closed face d mm left of a closed axis: a joint at every vertex."""
        n = len(self.segs)
        pts = [self.joint(k, d) for k in range(n)]
        out = []
        for k in range(n):
            b = self._bulge(k, pts[k], pts[(k + 1) % n], d)
            out.append([pts[k][0], pts[k][1], b] if b else [pts[k][0], pts[k][1]])
        return out


def solids(length: float, closed: bool, cuts: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """The stretches of an axis between cuts (opening spans, sorted, not overlapping), as station pairs;
    on a closed axis the last stretch runs across the first vertex (its end passes the length). None for a
    closed axis without cuts: its faces are rings."""
    if closed and not cuts:
        return None
    out: list[tuple[float, float]] = []
    if closed:
        for (_, b), (a, _) in zip(cuts, [*cuts[1:], (cuts[0][0] + length, 0.0)], strict=True):
            if a - b > 1e-6:
                out.append((b, a))
        return out
    edges = [0.0, *[s for cut in cuts for s in cut], length]
    for a, b in zip(edges[::2], edges[1::2], strict=True):
        if b - a > 1e-6:
            out.append((a, b))
    return out


def _tee(wall: Wall, s: float, length: float, walls: Sequence[Wall]) -> float:
    """How far the end of an open wall at station s (0 or its length) runs inside another wall it tees
    into: the distance along the axis to that wall's face, found by bisection; 0 when the end is free or
    meets the other wall within half its thickness of one of its own ends (a corner, not a tee)."""
    end = planar.point_at(wall.axis, False, s)
    inward = 1.0 if s == 0.0 else -1.0
    for other in walls:
        if other.id == wall.id:
            continue
        half = other.thickness / 2.0
        d, so = planar.project(other.axis, other.closed, end)
        if d > half + 1e-6:
            continue
        if not other.closed and not half < so < planar.length(other.axis, False) - half:
            return 0.0
        lo, hi = 0.0, min(length / 2.0, 4.0 * other.thickness)
        if planar.project(other.axis, other.closed, planar.point_at(wall.axis, False, s + inward * hi))[0] < half:
            return 0.0
        for _ in range(40):
            mid = (lo + hi) / 2.0
            inside = planar.project(other.axis, other.closed, planar.point_at(wall.axis, False, s + inward * mid))
            lo, hi = (mid, hi) if inside[0] < half else (lo, mid)
        return hi
    return 0.0


def wall_pieces(
    wall: Wall, openings: Sequence[Opening], walls: Sequence[Wall] = ()
) -> list[tuple[list[list[float]], list[list[float]], bool, bool, bool]]:
    """The wall as solid pieces (left face, right face, closed, cap at start, cap at end): a pair of rings for
    a closed wall with no openings, else one open pair per stretch between openings. An open wall's end that
    tees into another of walls stops at that wall's face, without a cap."""
    faces = Faces(wall.axis, wall.closed)
    left, right = face_offsets(wall)
    cuts = sorted((o.offset, o.offset + o.width) for o in openings if o.wall == wall.id)
    stretches = solids(faces.length, wall.closed, cuts)
    if stretches is None:
        return [(faces.ring(left), faces.ring(right), True, False, False)]
    head = tail = 0.0
    if not wall.closed:
        head, tail = _tee(wall, 0.0, faces.length, walls), _tee(wall, faces.length, faces.length, walls)
    out = []
    for a, b in stretches:
        a2, b2 = a + head if a == 0.0 else a, b - tail if b == faces.length else b
        if b2 - a2 > 1e-6:
            caps = (a2 == a or not head, b2 == b or not tail)
            out.append((faces.face(left, a2, b2), faces.face(right, a2, b2), False, *caps))
    return out


# --- entities -------------------------------------------------------------------------------


def _pl(msp: Modelspace, verts: Vertices, layer: str, closed: bool = False) -> Any:
    pts = [(v[0], v[1], 0.0, 0.0, v[2] if len(v) > 2 else 0.0) for v in verts]
    return msp.add_lwpolyline(pts, format="xyseb", close=closed, dxfattribs={"layer": layer})


def _hatch(msp: Modelspace, rings: list[Vertices], layer: str) -> Any:
    hatch = msp.add_hatch(color=256, dxfattribs={"layer": layer})
    for ring in rings:
        hatch.paths.add_polyline_path([(v[0], v[1], v[2] if len(v) > 2 else 0.0) for v in ring], is_closed=True)
    return hatch


def _walls(msp: Modelspace, plan: Plan) -> int:
    count = 0
    for wall in plan.walls:
        for left, right, closed, cap_start, cap_end in wall_pieces(wall, plan.openings, plan.walls):
            _pl(msp, left, "A-WALL", closed)
            _pl(msp, right, "A-WALL", closed)
            if closed:
                _hatch(msp, [left, right], "A-WALL-PATT")
                count += 3
                continue
            for cap, i in ((cap_start, 0), (cap_end, -1)):
                if cap:
                    msp.add_line(left[i][:2], right[i][:2], dxfattribs={"layer": "A-WALL"})
                    count += 1
            _hatch(msp, [left + planar.reverse(right)], "A-WALL-PATT")
            count += 3
    return count


def _openings(msp: Modelspace, plan: Plan) -> int:
    walls = {w.id: w for w in plan.walls}
    count = 0
    for o in plan.openings:
        wall = walls[o.wall]
        faces = Faces(wall.axis, wall.closed)
        left, right = face_offsets(wall)
        s0, s1 = o.offset, o.offset + o.width
        if o.opening_kind == "window":
            for d in (left, (left + right) / 2.0, right):
                _pl(msp, faces.face(d, s0, s1), "A-GLAZ")
            count += 3
            continue
        side = 1.0 if o.swing == "in" else -1.0
        d = left if o.swing == "in" else right
        hinge_far = (o.hand == "left") == (o.swing == "in")
        sh, so = (s1, s0) if hinge_far else (s0, s1)
        (hx, hy), th = planar.station(wall.axis, wall.closed, sh, after=not hinge_far)
        (ox, oy), to = planar.station(wall.axis, wall.closed, so, after=hinge_far)
        nh, no = planar.left_normal(th), planar.left_normal(to)
        hinge = (hx + nh[0] * d, hy + nh[1] * d)
        shut = (ox + no[0] * d, oy + no[1] * d)
        leaf = math.dist(hinge, shut)
        tip = (hinge[0] + nh[0] * side * leaf, hinge[1] + nh[1] * side * leaf)
        msp.add_line(hinge, tip, dxfattribs={"layer": "A-DOOR"})
        a_shut = math.degrees(math.atan2(shut[1] - hinge[1], shut[0] - hinge[0]))
        a_tip = math.degrees(math.atan2(tip[1] - hinge[1], tip[0] - hinge[0]))
        ccw = (shut[0] - hinge[0]) * (tip[1] - hinge[1]) - (shut[1] - hinge[1]) * (tip[0] - hinge[0]) > 0
        start, end = (a_shut, a_tip) if ccw else (a_tip, a_shut)
        msp.add_arc(hinge, leaf, start, end, dxfattribs={"layer": "A-DOOR"})
        count += 2
    return count


def _area(area_mm2: float) -> str:
    return f"{area_mm2 / 1_000_000.0:.2f} m²"


def _rooms(msp: Modelspace, plan: Plan) -> int:
    count = 0
    for r in plan.rooms:
        name = msp.add_text(r.name, height=ROOM_TEXT_MM, dxfattribs={"layer": "A-AREA-IDEN"})
        name.set_placement(r.at, align=TextEntityAlignment.BOTTOM_CENTER)
        count += 1
        if r.area > 0:
            below = (r.at[0], r.at[1] - AREA_TEXT_MM * 0.6)
            text = msp.add_text(_area(r.area), height=AREA_TEXT_MM, dxfattribs={"layer": "A-AREA-IDEN"})
            text.set_placement(below, align=TextEntityAlignment.TOP_CENTER)
            count += 1
    return count


def stair_flights(stair: Stair) -> list[tuple[Point, Point, int]]:
    """The flights of a stair in its own frame (x up the first flight, y to its left) as Geometry.stair_layout
    lays them: (origin at the middle of the bottom riser, unit direction, risers) per flight. An L or U
    splits the risers, (risers + 1) // 2 in the first flight, around a square landing."""
    n, g, h = stair.risers, stair.going, stair.width / 2.0
    if stair.stair_kind == "straight":
        return [((0.0, 0.0), (1.0, 0.0), n)]
    first = (n + 1) // 2
    landing = (first - 1) * g
    second = ((landing + h, h), (0.0, 1.0)) if stair.stair_kind == "l" else ((landing, 2.0 * h), (-1.0, 0.0))
    return [((0.0, 0.0), (1.0, 0.0), first), (*second, n - first)]


def stair_path(stair: Stair) -> list[Point]:
    """The walking line in the stair frame: up the middle of every flight, through the landing's middle."""
    g, h = stair.going, stair.width / 2.0
    flights = stair_flights(stair)
    if stair.stair_kind == "straight":
        return [(g / 2.0, 0.0), ((stair.risers - 1) * g, 0.0)]
    landing = (flights[0][2] - 1) * g
    rest = (flights[1][2] - 1) * g
    if stair.stair_kind == "l":
        return [(g / 2.0, 0.0), (landing + h, 0.0), (landing + h, h + rest)]
    return [(g / 2.0, 0.0), (landing + h, 0.0), (landing + h, 2.0 * h), (landing - rest, 2.0 * h)]


def _to_world(stair: Stair, p: Sequence[float]) -> Point:
    """The stair frame to world, mirrored across x for turn right (Geometry.stair_to_world)."""
    x, y = p[0], -p[1] if stair.turn == "right" else p[1]
    a = math.radians(stair.direction_deg)
    return stair.start[0] + x * math.cos(a) - y * math.sin(a), stair.start[1] + x * math.sin(a) + y * math.cos(a)


def _stairs(msp: Modelspace, plan: Plan) -> int:
    count = 0
    for s in plan.stairs:
        g, h = s.going, s.width / 2.0

        def world(u: float, v: float, origin: Point, d: Point, s: Stair = s) -> Point:
            return _to_world(s, (origin[0] + d[0] * u - d[1] * v, origin[1] + d[1] * u + d[0] * v))

        for origin, d, n in stair_flights(s):
            if n < 2:
                continue  # a flight of one riser is the step onto the landing: no tread
            run = (n - 1) * g
            corners = [
                world(0, -h, origin, d),
                world(run, -h, origin, d),
                world(run, h, origin, d),
                world(0, h, origin, d),
            ]
            _pl(msp, corners, "A-FLOR-STRS", closed=True)
            for k in range(1, n - 1):
                msp.add_line(
                    world(k * g, -h, origin, d), world(k * g, h, origin, d), dxfattribs={"layer": "A-FLOR-STRS"}
                )
            count += 1 + max(n - 2, 0)
        if s.stair_kind != "straight":
            landing = (stair_flights(s)[0][2] - 1) * g
            top = 3.0 * h if s.stair_kind == "u" else h
            box = [(landing, -h), (landing + s.width, -h), (landing + s.width, top), (landing, top)]
            _pl(msp, [_to_world(s, c) for c in box], "A-FLOR-STRS", closed=True)
            count += 1
        line = [_to_world(s, p) for p in stair_path(s)]
        _pl(msp, line, "A-FLOR-STRS")
        (ax, ay), (bx, by) = line[-2], line[-1]
        ln = math.dist((ax, ay), (bx, by)) or 1.0
        ux, uy = (bx - ax) / ln, (by - ay) / ln
        cx, cy = bx - ux * 2.0 * ARROW_MM, by - uy * 2.0 * ARROW_MM
        head = [(cx - uy * ARROW_MM, cy + ux * ARROW_MM), (bx, by), (cx + uy * ARROW_MM, cy - ux * ARROW_MM)]
        _pl(msp, head, "A-FLOR-STRS")
        label = msp.add_text("SUBE", height=STAIR_TEXT_MM, dxfattribs={"layer": "A-FLOR-STRS"})
        label.set_placement(line[0], align=TextEntityAlignment.TOP_CENTER)
        count += 3
    return count


def _structure(msp: Modelspace, plan: Plan) -> int:
    count = 0
    for c in plan.columns:
        hatch = msp.add_hatch(color=256, dxfattribs={"layer": "A-COLS-PATT"})
        if c.outline is None:
            msp.add_circle(c.at, c.diameter / 2.0, dxfattribs={"layer": "A-COLS"})
            hatch.paths.add_edge_path().add_arc(c.at, c.diameter / 2.0, 0.0, 360.0)
        else:
            _pl(msp, c.outline, "A-COLS", closed=True)
            hatch.paths.add_polyline_path([(v[0], v[1], v[2] if len(v) > 2 else 0.0) for v in c.outline])
        count += 2
    for s in plan.slabs:
        for ring in (s.outline, *s.holes):
            _pl(msp, ring, "A-FLOR-OTLN", closed=True)
            count += 1
    for r in plan.railings:
        _pl(msp, r.path, "A-FLOR-HRAL", r.closed)
        count += 1
    for cw in plan.curtain_walls:
        faces = Faces(cw.path, cw.closed)
        half = cw.profile[1] / 2.0
        for d in (half, -half):
            if cw.closed:
                _pl(msp, faces.ring(d), "A-GLAZ-CURT", True)
            else:
                _pl(msp, faces.face(d, 0.0, faces.length), "A-GLAZ-CURT")
            count += 1
    for p in plan.planters:
        _pl(msp, p.outline, "A-PLNT", closed=True)
        count += 1
    for p in plan.pools:
        _pl(msp, p.outline, "A-POOL", closed=True)
        count += 1
    for r in plan.ramps:
        faces = Faces(r.axis, False)
        for d in (r.width / 2.0, -r.width / 2.0):
            _pl(msp, faces.face(d, 0.0, faces.length), "A-FLOR-RAMP")
            count += 1
    return count


def draft(msp: Modelspace, plan: Plan) -> int:
    """Draws the documentation of the plan on DOC_LAYERS; returns the number of entities drawn."""
    return _walls(msp, plan) + _openings(msp, plan) + _rooms(msp, plan) + _stairs(msp, plan) + _structure(msp, plan)
