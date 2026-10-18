"""Exact plane geometry of bulged polylines, shared by the DXF reader, writer and drafting.

A polyline is a list of ``[x, y]`` / ``[x, y, bulge]`` vertices with the DXF
LWPOLYLINE convention (models.py): the bulge on vertex i bends the segment to
vertex i + 1, bulge = tan(sweep / 4), positive counter-clockwise. A station s
is the arc length from the first vertex. Nothing here tessellates an arc:
points, tangents, projections and offsets are computed on the true circle.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .models import arc_radius, bulge_of, segment_length

Vertices = Sequence[Sequence[float]]
Segment = tuple[Sequence[float], Sequence[float], float]
EPS = 1e-9


def centre(p0: Sequence[float], p1: Sequence[float], bulge: float) -> tuple[float, float]:
    """Centre of the arc from p0 to p1: on the chord's left for a counter-clockwise arc under 180 degrees."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    off = (chord / 2.0) * (1.0 - bulge * bulge) / (2.0 * bulge)
    return (p0[0] + p1[0]) / 2.0 - dy / chord * off, (p0[1] + p1[1]) / 2.0 + dx / chord * off


def segments(vertices: Vertices, closed: bool) -> list[Segment]:
    """(start, end, bulge) of every segment, the closing one last on a closed polyline."""
    count = len(vertices) if closed else len(vertices) - 1
    return [(vertices[i], vertices[(i + 1) % len(vertices)], bulge_of(vertices[i])) for i in range(count)]


def length(vertices: Vertices, closed: bool) -> float:
    return sum(segment_length(p0, p1, b) for p0, p1, b in segments(vertices, closed))


def on_segment(p0: Sequence[float], p1: Sequence[float], bulge: float, t: float) -> tuple[tuple[float, float], ...]:
    """The point t mm along one segment and the unit tangent there: ((x, y), (tx, ty))."""
    if abs(bulge) <= 1e-12:
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = math.hypot(dx, dy)
        k = t / n if n else 0.0
        return (p0[0] + dx * k, p0[1] + dy * k), ((dx / n, dy / n) if n else (1.0, 0.0))
    c = centre(p0, p1, bulge)
    r = math.hypot(p0[0] - c[0], p0[1] - c[1])
    sign = math.copysign(1.0, bulge)
    a = math.atan2(p0[1] - c[1], p0[0] - c[0]) + sign * t / r
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a)), (-sign * math.sin(a), sign * math.cos(a))


def station(vertices: Vertices, closed: bool, s: float, after: bool = True) -> tuple[tuple[float, float], ...]:
    """The point at station s (clamped to the ends) and the unit tangent there. At a vertex, after picks the
    segment that starts there (else the one that ends there)."""
    segs = segments(vertices, closed)
    base = 0.0
    for i, (p0, p1, b) in enumerate(segs):
        n = segment_length(p0, p1, b)
        last = i == len(segs) - 1
        if s < base + n - EPS or (not after and s <= base + n + EPS) or last:
            return on_segment(p0, p1, b, min(max(s - base, 0.0), n))
        base += n
    return (vertices[0][0], vertices[0][1]), (1.0, 0.0)


def point_at(vertices: Vertices, closed: bool, s: float) -> tuple[float, float]:
    """The point at arc length s along the polyline (clamped to its ends)."""
    segs = segments(vertices, closed)
    for i, (p0, p1, b) in enumerate(segs):
        n = segment_length(p0, p1, b)
        if s <= n or i == len(segs) - 1:
            return on_segment(p0, p1, b, min(max(s, 0.0), n))[0]
        s -= n
    return vertices[0][0], vertices[0][1]


def project(vertices: Vertices, closed: bool, p: Sequence[float]) -> tuple[float, float]:
    """(distance, station) of the point of the polyline nearest p; the first segment wins a tie."""
    best = (math.inf, 0.0)
    base = 0.0
    for p0, p1, b in segments(vertices, closed):
        n = segment_length(p0, p1, b)
        if abs(b) <= 1e-12:
            dx, dy = p1[0] - p0[0], p1[1] - p0[1]
            t = ((p[0] - p0[0]) * dx + (p[1] - p0[1]) * dy) / (n * n) if n else 0.0
            t = min(max(t, 0.0), 1.0)
            d, s = math.hypot(p[0] - p0[0] - dx * t, p[1] - p0[1] - dy * t), t * n
        else:
            c = centre(p0, p1, b)
            r = arc_radius(math.dist(p0[:2], p1[:2]), b)
            sweep = abs(4.0 * math.atan(b))
            a0 = math.atan2(p0[1] - c[1], p0[0] - c[0])
            rel = (math.copysign(1.0, b) * (math.atan2(p[1] - c[1], p[0] - c[0]) - a0)) % math.tau
            if rel <= sweep:
                d, s = abs(math.hypot(p[0] - c[0], p[1] - c[1]) - r), r * rel
            else:
                d0, d1 = math.dist(p[:2], p0[:2]), math.dist(p[:2], p1[:2])
                d, s = (d0, 0.0) if d0 <= d1 else (d1, n)
        if d < best[0] - 1e-9:
            best = (d, base + s)
        base += n
    return best


def offset_segment(
    p0: Sequence[float], p1: Sequence[float], bulge: float, d: float
) -> tuple[list[float], list[float]] | None:
    """The segment moved d mm to its left: a parallel line, or the concentric arc of radius r - d (r + d
    for a clockwise arc), which keeps the bulge. None when the arc would shrink to nothing."""
    if abs(bulge) <= 1e-12:
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = math.hypot(dx, dy)
        nx, ny = -dy / n * d, dx / n * d
        return [p0[0] + nx, p0[1] + ny], [p1[0] + nx, p1[1] + ny]
    c = centre(p0, p1, bulge)
    r = arc_radius(math.dist(p0[:2], p1[:2]), bulge)
    scale = (r - d * math.copysign(1.0, bulge)) / r
    if scale <= 1e-9:
        return None
    return [c[0] + (p0[0] - c[0]) * scale, c[1] + (p0[1] - c[1]) * scale], [
        c[0] + (p1[0] - c[0]) * scale,
        c[1] + (p1[1] - c[1]) * scale,
    ]


def reverse(vertices: Vertices) -> list[list[float]]:
    """The open polyline walked backwards: vertex order reversed, every bulge moved and negated."""
    out: list[list[float]] = []
    for i in range(len(vertices) - 1, -1, -1):
        b = -bulge_of(vertices[i - 1]) if i > 0 else 0.0
        x, y = float(vertices[i][0]), float(vertices[i][1])
        out.append([x, y, b] if b else [x, y])
    return out


def bulge_between(c: Sequence[float], a: Sequence[float], b: Sequence[float], ccw: bool) -> float:
    """Bulge of the arc about c from a to b turning counter-clockwise (ccw) or clockwise; 0 when a is b."""
    a0 = math.atan2(a[1] - c[1], a[0] - c[0])
    a1 = math.atan2(b[1] - c[1], b[0] - c[0])
    sweep = (a1 - a0) % math.tau if ccw else -((a0 - a1) % math.tau)
    if abs(sweep) <= 1e-12 or abs(abs(sweep) - math.tau) <= 1e-12:
        return 0.0
    return math.tan(sweep / 4.0)


def left_normal(t: Sequence[float]) -> tuple[float, float]:
    return -t[1], t[0]
