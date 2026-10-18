"""Draws a known plan the way an architect's DXF looks, for the inference tests: no records, only
linework. Wall faces are offset lines (or arcs) mitred at corners, trimmed at T stems (the host's
near face broken where the stem arrives), broken at openings with jamb lines; a door is a leaf
rectangle and a swing arc (loose or in a block), a window three lines across the gap (loose or in a
block); rooms are a TEXT name with an area below. Noise: dimensions on COTAS, a grid with bubbles
on EJES, furniture blocks on MOBILIARIO and a floor hatch.

The drafter solves only what the test plans need: straight and arc segments, mitred polyline
vertices, two walls meeting end to end, T stems on a straight run, free ends.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf

from plomada_bridge.models import Plan, bulge_of

Vec = tuple[float, float]


def _add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1])


def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1])


def _mul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: Vec, b: Vec) -> float:
    return a[0] * b[1] - a[1] * b[0]


@dataclass
class Seg:
    """One axis segment: s runs 0..length along it, off is the signed offset (left positive)."""

    wall: str
    a: Vec
    b: Vec
    bulge: float
    t: float
    cum: float = 0.0  # offset of a along the wall's axis
    trims: dict[int, list[float]] = field(default_factory=lambda: {1: [0.0, 0.0], -1: [0.0, 0.0]})
    breaks: dict[int, list[tuple[float, float]]] = field(default_factory=lambda: {1: [], -1: []})
    caps: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chord = math.dist(self.a, self.b)
        if abs(self.bulge) < 1e-12:
            self.u = _mul(_sub(self.b, self.a), 1.0 / self.chord)
            self.length = self.chord
        else:
            theta = 4.0 * math.atan(self.bulge)
            self.r = self.chord / (2.0 * math.sin(abs(theta) / 2.0))
            mid = _mul(_add(self.a, self.b), 0.5)
            d = _mul(_sub(self.b, self.a), 1.0 / self.chord)
            left = (-d[1], d[0])
            h = self.r * math.cos(abs(theta) / 2.0) * (1.0 if theta > 0 else -1.0)
            self.c = _add(mid, _mul(left, h))
            self.sign = 1.0 if theta > 0 else -1.0
            self.ta = math.atan2(self.a[1] - self.c[1], self.a[0] - self.c[0])
            self.length = self.r * abs(theta)
        self.trims = {1: [0.0, self.length], -1: [0.0, self.length]}

    @property
    def arc(self) -> bool:
        return abs(self.bulge) >= 1e-12

    def angle(self, s: float) -> float:
        return self.ta + self.sign * s / self.r

    def point(self, s: float, off: float = 0.0) -> Vec:
        if not self.arc:
            return _add(_add(self.a, _mul(self.u, s)), _mul((-self.u[1], self.u[0]), off))
        th = self.angle(s)
        return _add(self.c, _mul((math.cos(th), math.sin(th)), self.r - self.sign * off))

    def tangent(self, s: float) -> Vec:
        if not self.arc:
            return self.u
        th = self.angle(s)
        return _mul((-math.sin(th), math.cos(th)), self.sign)

    def normal(self, s: float) -> Vec:
        t = self.tangent(s)
        return (-t[1], t[0])

    def local(self, p: Vec) -> float:
        if not self.arc:
            return _dot(_sub(p, self.a), self.u)
        th = math.atan2(p[1] - self.c[1], p[0] - self.c[0])
        rel = (th - self.ta) * self.sign
        rel = (rel + math.pi) % math.tau - math.pi
        if rel < -math.pi / 2 and self.length / self.r > math.pi / 2:
            rel += math.tau
        return rel * self.r

    def lateral(self, p: Vec) -> float:
        if not self.arc:
            return _cross(self.u, _sub(p, self.a))
        return self.sign * (self.r - math.dist(p, self.c))

    def face(self, side: int) -> tuple[str, Vec, Vec | float]:
        off = side * self.t / 2.0
        if not self.arc:
            return ("line", self.point(0.0, off), self.u)
        return ("circle", self.c, self.r - self.sign * off)


def _meet(f1: tuple, f2: tuple, near: Vec) -> Vec:
    """The intersection of two face curves nearest a point."""
    if f1[0] == "circle" and f2[0] == "line":
        f1, f2 = f2, f1
    if f1[0] == "line" and f2[0] == "line":
        (_, p, u), (_, q, v) = f1, f2
        s = _cross(_sub(q, p), v) / _cross(u, v)
        return _add(p, _mul(u, s))
    if f1[0] == "line":
        (_, p, u), (_, c, r) = f1, f2
        s0 = _dot(_sub(c, p), u)
        foot = _add(p, _mul(u, s0))
        h = math.sqrt(max(0.0, r * r - math.dist(foot, c) ** 2))
        pts = [_add(foot, _mul(u, -h)), _add(foot, _mul(u, h))]
    else:
        (_, c1, r1), (_, c2, r2) = f1, f2
        d = math.dist(c1, c2)
        a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
        h = math.sqrt(max(0.0, r1 * r1 - a * a))
        e = _mul(_sub(c2, c1), 1.0 / d)
        m = _add(c1, _mul(e, a))
        pts = [_add(m, _mul((-e[1], e[0]), h)), _add(m, _mul((e[1], -e[0]), h))]
    return min(pts, key=lambda q: math.dist(q, near))


@dataclass
class Style:
    walls: str = "MUROS"
    doors: str = "PUERTAS"
    windows: str = "VENTANAS"
    rooms: str = "TEXTOS"
    faces: str = "lines"  # lines | polylines | outlines (closed polyline per piece, no loose faces)
    door_blocks: bool = False
    window_blocks: bool = False
    windows_drawn: bool = True
    scale: float = 1.0  # drawing units per mm (0.001 for metres)
    insunits: int = 4
    noise: bool = True
    in_block: Vec | None = None  # draw everything inside one block inserted here
    skip_openings: tuple[str, ...] = ()  # openings drawn as bare gaps (jambs only)


class Drafter:
    def __init__(self, plan: Plan, style: Style) -> None:
        self.plan = plan
        self.st = style
        self.segs: dict[str, list[Seg]] = {}
        for w in plan.walls:
            pts = w.axis
            n = len(pts)
            count = n if w.closed else n - 1
            cum = 0.0
            out = []
            for i in range(count):
                a, b = tuple(pts[i][:2]), tuple(pts[(i + 1) % n][:2])
                seg = Seg(w.id, a, b, bulge_of(pts[i]), w.thickness, cum)
                cum += seg.length
                out.append(seg)
            self.segs[w.id] = out
        self.walls = {w.id: w for w in plan.walls}

    # --- junctions ------------------------------------------------------------------------

    def solve(self) -> None:
        for w in self.plan.walls:
            segs = self.segs[w.id]
            pairs = list(itertools.pairwise(segs))
            if w.closed:
                pairs.append((segs[-1], segs[0]))
            for s1, s2 in pairs:
                for side in (1, -1):
                    p = _meet(s1.face(side), s2.face(side), s1.b)
                    s1.trims[side][1] = s1.local(p)
                    s2.trims[side][0] = s2.local(p)
        ends = []
        for w in self.plan.walls:
            if not w.closed:
                ends.append((self.segs[w.id][0], 0))
                ends.append((self.segs[w.id][-1], 1))
        done: set[int] = set()
        for i, (s, e) in enumerate(ends):
            if i in done:
                continue
            p = s.a if e == 0 else s.b
            mate = next(
                (
                    j
                    for j, (s2, e2) in enumerate(ends)
                    if j != i and j not in done and s2.wall != s.wall and math.dist(p, s2.a if e2 == 0 else s2.b) < 1e-6
                ),
                None,
            )
            if mate is not None:
                s2, e2 = ends[mate]
                done |= {i, mate}
                for side in (1, -1):
                    other = side if e != e2 else -side
                    q = _meet(s.face(side), s2.face(other), p)
                    s.trims[side][e] = s.local(q)
                    s2.trims[other][e2] = s2.local(q)
                continue
            host = self._host(s, p)
            if host is None:
                s.caps.append(0.0 if e == 0 else s.length)
                continue
            d = s.tangent(0.0) if e == 0 else _mul(s.tangent(s.length), -1.0)  # into the stem body
            hs = host.local(p)
            tau = 1 if _dot(host.normal(hs), d) > 0 else -1
            cut = []
            for side in (1, -1):
                q = _meet(s.face(side), host.face(tau), p)
                s.trims[side][e] = s.local(q)
                cut.append(host.local(q))
            host.breaks[tau].append((min(cut), max(cut)))

    def _host(self, s: Seg, p: Vec) -> Seg | None:
        for segs in self.segs.values():
            for h in segs:
                if h.wall == s.wall:
                    continue
                hs = h.local(p)
                if 1.0 < hs < h.length - 1.0 and abs(h.lateral(p)) < 1e-6:
                    return h
        return None

    # --- drawing ----------------------------------------------------------------------------

    def openings(self) -> None:
        for o in self.plan.openings:
            segs = self.segs[o.wall]
            seg = next(s for s in segs if o.offset < s.cum + s.length - 1e-6)
            s0 = o.offset - seg.cum
            s1 = s0 + o.width
            for side in (1, -1):
                seg.breaks[side].append((s0, s1))
            self.jambs.append((seg, s0))
            self.jambs.append((seg, s1))
            if o.id in self.st.skip_openings:
                continue
            if o.opening_kind == "door":
                self.door(seg, o, s0, s1)
            elif self.st.windows_drawn:
                self.window(seg, s0, s1)

    def door(self, seg: Seg, o: Any, s0: float, s1: float) -> None:
        sigma = 1 if o.swing == "in" else -1
        far = (o.hand == "left") == (o.swing == "in")
        sh, so = (s1, s0) if far else (s0, s1)
        hinge = seg.point(sh, sigma * seg.t / 2.0)
        closed = seg.point(so, sigma * seg.t / 2.0)
        w = math.dist(hinge, closed)
        n = _mul(seg.normal(sh), sigma)
        k = _mul(_sub(closed, hinge), 1.0 / w)
        a1, a2 = math.atan2(n[1], n[0]), math.atan2(k[1], k[0])
        if (a2 - a1) % math.tau > math.pi:
            a1, a2 = a2, a1
        tip = _add(hinge, _mul(n, w))
        leaf = [hinge, tip, _add(tip, _mul(k, 40.0)), _add(hinge, _mul(k, 40.0))]
        if not self.st.door_blocks:
            self.lw(leaf, self.st.doors, closed=True)
            self.target.add_arc(
                self.sc(hinge),
                w * self.st.scale,
                math.degrees(a1),
                math.degrees(a2),
                dxfattribs={"layer": self.st.doors},
            )
            return
        name = f"PUERTA_{round(w)}"
        if name not in self.doc.blocks:
            blk = self.doc.blocks.new(name)
            ws = w * self.st.scale
            blk.add_lwpolyline([(0, 0), (0, ws), (40 * self.st.scale, ws), (40 * self.st.scale, 0)], close=True)
            blk.add_arc((0, 0), ws, 0.0, 90.0)
        # block frame: x toward the closed jamb (k), y along the open leaf (n); mirror when k x n < 0
        rot = math.degrees(math.atan2(k[1], k[0]))
        mirror = _cross(k, n) < 0
        attrs = {"layer": self.st.doors, "rotation": rot}
        if mirror:
            attrs["yscale"] = -1.0
        self.target.add_blockref(name, self.sc(hinge), dxfattribs=attrs)

    def window(self, seg: Seg, s0: float, s1: float) -> None:
        if self.st.window_blocks and not seg.arc:
            w = s1 - s0
            name = f"VENTANA_{round(w)}x{round(seg.t)}"
            if name not in self.doc.blocks:
                blk = self.doc.blocks.new(name)
                for off in (-seg.t / 2.0, 0.0, seg.t / 2.0):
                    blk.add_line((0, off * self.st.scale), (w * self.st.scale, off * self.st.scale))
            rot = math.degrees(math.atan2(seg.u[1], seg.u[0]))
            self.target.add_blockref(
                name, self.sc(seg.point(s0)), dxfattribs={"layer": self.st.windows, "rotation": rot}
            )
            return
        for off in (-seg.t / 2.0, 0.0, seg.t / 2.0):
            self.curve(seg, s0, s1, off, self.st.windows)

    def curve(self, seg: Seg, s0: float, s1: float, off: float, layer: str) -> None:
        if s1 - s0 < 1e-6:
            return
        if not seg.arc:
            p, q = seg.point(s0, off), seg.point(s1, off)
            if self.st.faces == "polylines" and layer == self.st.walls:
                self.lw([p, q], layer)
            else:
                self.target.add_line(self.sc(p), self.sc(q), dxfattribs={"layer": layer})
            return
        r = seg.r - seg.sign * off
        a, b = math.degrees(seg.angle(s0)), math.degrees(seg.angle(s1))
        if seg.sign < 0:
            a, b = b, a
        self.target.add_arc(self.sc(seg.c), r * self.st.scale, a, b, dxfattribs={"layer": layer})

    def lw(self, pts: list[Vec], layer: str, closed: bool = False) -> None:
        self.target.add_lwpolyline([self.sc(p) for p in pts], close=closed, dxfattribs={"layer": layer})

    def sc(self, p: Vec) -> Vec:
        return (p[0] * self.st.scale, p[1] * self.st.scale)

    def faces(self) -> None:
        for segs in self.segs.values():
            for seg in segs:
                pieces = {}
                for side in (1, -1):
                    lo, hi = seg.trims[side]
                    cuts = sorted(seg.breaks[side])
                    spans, cur = [], lo
                    for b0, b1 in cuts:
                        if b0 > cur:
                            spans.append((cur, min(b0, hi)))
                        cur = max(cur, b1)
                    if cur < hi:
                        spans.append((cur, hi))
                    pieces[side] = spans
                if self.st.faces == "outlines":
                    self.outlines(seg, pieces)
                    continue
                for side in (1, -1):
                    for s0, s1 in pieces[side]:
                        self.curve(seg, s0, s1, side * seg.t / 2.0, self.st.walls)
                for s in seg.caps:
                    self.target.add_line(
                        self.sc(seg.point(s, -seg.t / 2.0)),
                        self.sc(seg.point(s, seg.t / 2.0)),
                        dxfattribs={"layer": self.st.walls},
                    )

    def outlines(self, seg: Seg, pieces: dict[int, list[tuple[float, float]]]) -> None:
        """One closed polyline per stretch between openings (both faces broken at the same places)."""
        assert len(pieces[1]) == len(pieces[-1]) and not seg.arc
        for (l0, l1), (r0, r1) in zip(pieces[1], pieces[-1], strict=True):
            ring = [
                seg.point(r0, -seg.t / 2),
                seg.point(r1, -seg.t / 2),
                seg.point(l1, seg.t / 2),
                seg.point(l0, seg.t / 2),
            ]
            self.lw(ring, self.st.walls, closed=True)

    def jamb_lines(self) -> None:
        if self.st.faces == "outlines":
            return
        for seg, s in self.jambs:
            self.target.add_line(
                self.sc(seg.point(s, -seg.t / 2.0)),
                self.sc(seg.point(s, seg.t / 2.0)),
                dxfattribs={"layer": self.st.walls},
            )

    def rooms(self) -> None:
        for r in self.plan.rooms:
            self.target.add_text(
                r.name, height=200 * self.st.scale, dxfattribs={"layer": self.st.rooms, "insert": self.sc(r.at)}
            )
            if r.area:
                below = (r.at[0], r.at[1] - 350.0)
                self.target.add_mtext(
                    f"{r.area / 1e6:.2f} m\\U+00B2",
                    dxfattribs={"layer": self.st.rooms, "insert": self.sc(below), "char_height": 150 * self.st.scale},
                )

    def noise(self) -> None:
        xs = [p[0] for w in self.plan.walls for p in w.axis]
        ys = [p[1] for w in self.plan.walls for p in w.axis]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        for k, x in enumerate(sorted({round(p[0]) for w in self.plan.walls for p in w.axis})[:6]):
            self.target.add_line(self.sc((x, y0 - 1500)), self.sc((x, y1 + 1500)), dxfattribs={"layer": "EJES"})
            self.target.add_circle(self.sc((x, y0 - 1900)), 400 * self.st.scale, dxfattribs={"layer": "EJES"})
            self.target.add_text(
                "ABCDEF"[k], height=300 * self.st.scale, dxfattribs={"layer": "EJES", "insert": self.sc((x, y0 - 1900))}
            )
        for k, y in enumerate(sorted({round(p[1]) for w in self.plan.walls for p in w.axis})[:6]):
            self.target.add_line(self.sc((x0 - 1500, y)), self.sc((x1 + 1500, y)), dxfattribs={"layer": "EJES"})
            self.target.add_text(
                str(k + 1), height=300 * self.st.scale, dxfattribs={"layer": "EJES", "insert": self.sc((x0 - 1900, y))}
            )
        dim = self.target.add_linear_dim(
            base=self.sc((x0, y0 - 800)), p1=self.sc((x0, y0)), p2=self.sc((x1, y0)), dxfattribs={"layer": "COTAS"}
        )
        dim.render()
        dim = self.target.add_aligned_dim(p1=self.sc((x1, y0)), p2=self.sc((x1, y1)), distance=800 * self.st.scale)
        dim.dimension.dxf.layer = "COTAS"
        dim.render()
        if "CAMA_2P" not in self.doc.blocks:
            bed = self.doc.blocks.new("CAMA_2P")
            s = self.st.scale
            bed.add_lwpolyline([(0, 0), (1600 * s, 0), (1600 * s, 2000 * s), (0, 2000 * s)], close=True)
            bed.add_lwpolyline(
                [(100 * s, 1600 * s), (750 * s, 1600 * s), (750 * s, 1900 * s), (100 * s, 1900 * s)], close=True
            )
            bed.add_lwpolyline(
                [(850 * s, 1600 * s), (1500 * s, 1600 * s), (1500 * s, 1900 * s), (850 * s, 1900 * s)], close=True
            )
            table = self.doc.blocks.new("MESA_4")
            table.add_lwpolyline([(0, 0), (1200 * s, 0), (1200 * s, 800 * s), (0, 800 * s)], close=True)
            for cx, cy in ((300, -300), (900, -300), (300, 1100), (900, 1100)):
                table.add_circle((cx * s, cy * s), 220 * s)
        for r in self.plan.rooms[:2]:
            self.target.add_blockref("CAMA_2P", self.sc(_add(r.at, (-800, 400))), dxfattribs={"layer": "MOBILIARIO"})
        if self.plan.rooms:
            r = self.plan.rooms[-1]
            self.target.add_blockref("MESA_4", self.sc(_add(r.at, (-600, -1600))), dxfattribs={"layer": "MOBILIARIO"})
        hatch = self.target.add_hatch(dxfattribs={"layer": "SOLADO"})
        hatch.set_pattern_fill("ANSI31", scale=50 * self.st.scale)
        hatch.paths.add_polyline_path(
            [
                self.sc((x0 + 300, y0 + 300)),
                self.sc((x0 + 2300, y0 + 300)),
                self.sc((x0 + 2300, y0 + 1800)),
                self.sc((x0 + 300, y0 + 1800)),
            ],
            is_closed=True,
        )

    def draw(self, path: Path) -> Path:
        self.doc = ezdxf.new("R2018", setup=True)
        self.doc.header["$INSUNITS"] = self.st.insunits
        for name in (
            self.st.walls,
            self.st.doors,
            self.st.windows,
            self.st.rooms,
            "EJES",
            "COTAS",
            "MOBILIARIO",
            "SOLADO",
        ):
            if name not in self.doc.layers:
                self.doc.layers.add(name)
        msp = self.doc.modelspace()
        self.target = self.doc.blocks.new("PLANTA") if self.st.in_block else msp
        self.jambs: list[tuple[Seg, float]] = []
        self.solve()
        self.openings()
        self.faces()
        self.jamb_lines()
        self.rooms()
        if self.st.noise:
            self.noise()
        if self.st.in_block:
            msp.add_blockref("PLANTA", self.st.in_block, dxfattribs={"layer": "0"})
        self.doc.saveas(path)
        return path


def draft(plan: Plan, path: Path, **style: Any) -> Path:
    """Draws the plan as dumb linework into path (see the module docstring)."""
    return Drafter(plan, Style(**style)).draw(path)
