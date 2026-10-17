"""Writes a plan as a DXF (write_plan_dxf) that read_plan reads back exactly.

The drawing is in millimetres ($INSUNITS 4) and follows dxf.py's conventions:

- every wall is its axis, an LWPOLYLINE with real bulges and the wall's closed
  flag on PLOMADA-MURO-<thickness mm>, carrying a PLOMADA payload with the
  other wall fields; its two faces are drawn on A-WALL-E-N as offset curves,
  segment by segment (concentric arcs, parallel lines), for documentation only;
- every 0.5 record is its entity on its PLOMADA-* layer with a PLOMADA payload
  of everything but the geometry; a slab's holes are polylines on
  PLOMADA-LOSA-HUECO that the slab names by handle;
- openings, rooms and stairs are ACADMCP_ARCH records, as AutoCAD MCP Pro
  writes them: an opening on the LINE between its jambs, a room on its visible
  name TEXT, a stair on its bottom riser;
- furniture and site objects are ARCH_<ITEM> and SITIO_<NAME> inserts, site
  paving, decks, pools and fences polylines on the SITIO-* layers, each with a
  PLOMADA payload carrying its id (and item, depth or height);
- the storey settings are a PLOMADA payload of kind storey on a POINT.

Payloads are JSON chunked into 1000 strings of at most 255 characters.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import units
from ezdxf.document import Drawing
from ezdxf.layouts import Modelspace

from .dxf import APP_ID, BLOCK_PREFIX, GEOMETRY_KINDS, OPEN_KINDS, PLOMADA_APP, SITE_LAYERS, SITE_PREFIX
from .models import EXTRA_LISTS, Plan, Wall, arc_radius, bulge_of, segment_length

CHUNK = 255  # characters per 1000 string, as AutoCAD MCP Pro writes them
FACE_LAYER = "A-WALL-E-N"
OPENING_LAYERS = {"door": "A-DOOR-E-N", "window": "A-WIND-E-N"}
ROOM_LAYER = "A-ANNO-ROOM-N"
STAIR_LAYER = "A-STAIR-E-N"
FURNITURE_LAYER = "A-FURN-E-N"
SITE_OBJECT_LAYER = "SITIO"
HOLE_LAYER = "PLOMADA-LOSA-HUECO"
STOREY_LAYER = "PLOMADA-PLANTA"
RECORD_LAYERS = {
    "slab": "PLOMADA-LOSA",
    "column": "PLOMADA-COLUMNA",
    "curtain_wall": "PLOMADA-MURO-CORTINA",
    "railing": "PLOMADA-BARANDA",
    "planter": "PLOMADA-JARDINERA",
    "pool": "PLOMADA-PILETA",
    "ramp": "PLOMADA-RAMPA",
}
ROOM_TEXT_MM = 250.0  # letter height of the room names
MARKER_MM = 100.0  # half size of the cross that stands for a block's contents

Vertices = Sequence[Sequence[float]]


# --- arcs ---------------------------------------------------------------------------------


def _centre(p0: Sequence[float], p1: Sequence[float], bulge: float) -> tuple[float, float]:
    """Centre of the arc from p0 to p1: on the chord's left for a counter-clockwise arc under 180 degrees."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    chord = math.hypot(dx, dy)
    off = (chord / 2.0) * (1.0 - bulge * bulge) / (2.0 * bulge)
    return (p0[0] + p1[0]) / 2.0 - dy / chord * off, (p0[1] + p1[1]) / 2.0 + dx / chord * off


def _offset(p0: Sequence[float], p1: Sequence[float], bulge: float, d: float) -> tuple[list[float], list[float]] | None:
    """The segment moved d mm to its left: a parallel line, or the concentric arc of radius r - d (r + d
    for a clockwise arc), which keeps the bulge. None when the arc would shrink to nothing."""
    if abs(bulge) <= 1e-12:
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = math.hypot(dx, dy)
        nx, ny = -dy / n * d, dx / n * d
        return [p0[0] + nx, p0[1] + ny], [p1[0] + nx, p1[1] + ny]
    c = _centre(p0, p1, bulge)
    r = arc_radius(math.dist(p0[:2], p1[:2]), bulge)
    scale = (r - math.copysign(d, bulge)) / r
    if scale <= 1e-9:
        return None
    return [c[0] + (p0[0] - c[0]) * scale, c[1] + (p0[1] - c[1]) * scale], [
        c[0] + (p1[0] - c[0]) * scale,
        c[1] + (p1[1] - c[1]) * scale,
    ]


def _segments(vertices: Vertices, closed: bool) -> list[tuple[Sequence[float], Sequence[float], float]]:
    count = len(vertices) if closed else len(vertices) - 1
    return [(vertices[i], vertices[(i + 1) % len(vertices)], bulge_of(vertices[i])) for i in range(count)]


def point_at(vertices: Vertices, closed: bool, s: float) -> tuple[float, float]:
    """The point at arc length s along the polyline (clamped to its ends)."""
    segments = _segments(vertices, closed)
    for i, (p0, p1, b) in enumerate(segments):
        length = segment_length(p0, p1, b)
        if s <= length or i == len(segments) - 1:
            t = min(max(s, 0.0), length)
            if abs(b) <= 1e-12:
                k = t / length if length else 0.0
                return p0[0] + (p1[0] - p0[0]) * k, p0[1] + (p1[1] - p0[1]) * k
            c = _centre(p0, p1, b)
            r = math.hypot(p0[0] - c[0], p0[1] - c[1])
            a = math.atan2(p0[1] - c[1], p0[0] - c[0]) + math.copysign(t / r, b)
            return c[0] + r * math.cos(a), c[1] + r * math.sin(a)
        s -= length
    return vertices[0][0], vertices[0][1]


def face_offsets(wall: Wall) -> tuple[float, float]:
    """The two face offsets of a wall, mm to the left of its axis."""
    t = wall.thickness
    return {"center": (t / 2.0, -t / 2.0), "left": (t, 0.0), "right": (0.0, -t)}[wall.justification]


# --- entities -------------------------------------------------------------------------------


def _xdata(entity: Any, app: str, payload: dict[str, Any]) -> None:
    text = json.dumps(payload)
    entity.set_xdata(app, [(1000, text[i : i + CHUNK]) for i in range(0, len(text), CHUNK)])


def _polyline(msp: Modelspace, vertices: Vertices, closed: bool, layer: str) -> Any:
    points = [(v[0], v[1], 0.0, 0.0, bulge_of(v)) for v in vertices]
    return msp.add_lwpolyline(points, format="xyseb", close=closed, dxfattribs={"layer": layer})


def _block(doc: Drawing, name: str) -> str:
    """A block definition with a small cross at its origin (the model comes from the library)."""
    if name not in doc.blocks:
        block = doc.blocks.new(name)
        block.add_line((-MARKER_MM, 0), (MARKER_MM, 0))
        block.add_line((0, -MARKER_MM), (0, MARKER_MM))
    return name


def _block_name(prefix: str, item: str) -> str:
    return prefix + re.sub(r"[^A-Za-z0-9_-]", "_", item).upper()


def _without(record: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in keys}


def _walls(msp: Modelspace, plan: Plan) -> int:
    for wall in plan.walls:
        axis = _polyline(msp, wall.axis, wall.closed, f"PLOMADA-MURO-{round(wall.thickness)}")
        _xdata(axis, PLOMADA_APP, _without(wall.wire(), "axis", "closed"))
        for d in face_offsets(wall):
            for p0, p1, b in _segments(wall.axis, wall.closed):
                if face := _offset(p0, p1, b, d):
                    _polyline(msp, [[*face[0], b], face[1]], False, FACE_LAYER)
    return len(plan.walls)


def _extra_records(msp: Modelspace, plan: Plan) -> int:
    count = 0
    for key in EXTRA_LISTS:
        for record in getattr(plan, key):
            payload = record.wire()
            kind, field = payload["kind"], GEOMETRY_KINDS[payload["kind"]][1]
            layer = RECORD_LAYERS[kind]
            if kind == "column" and record.outline is None:
                entity = msp.add_circle(record.at, record.diameter / 2.0, dxfattribs={"layer": layer})
                payload = _without(payload, "at", "diameter")
            else:
                if kind == "slab":
                    payload["holes"] = [_polyline(msp, h, True, HOLE_LAYER).dxf.handle for h in record.holes]
                closed = record.closed if kind in OPEN_KINDS else kind != "ramp"
                entity = _polyline(msp, getattr(record, field), closed, layer)
                payload = _without(payload, field, "closed")
            _xdata(entity, PLOMADA_APP, payload)
            count += 1
    return count


def _arch_records(msp: Modelspace, plan: Plan) -> int:
    """Openings, rooms and stairs as ACADMCP_ARCH records, on the entities AutoCAD MCP Pro uses."""
    walls = {w.id: w for w in plan.walls}
    for o in plan.openings:
        host = walls[o.wall]
        a = point_at(host.axis, host.closed, o.offset)
        b = point_at(host.axis, host.closed, o.offset + o.width)
        _xdata(msp.add_line(a, b, dxfattribs={"layer": OPENING_LAYERS[o.opening_kind]}), APP_ID, o.wire())
    for r in plan.rooms:
        text = msp.add_text(r.name, height=ROOM_TEXT_MM, dxfattribs={"layer": ROOM_LAYER, "insert": r.at})
        _xdata(text, APP_ID, r.wire())
    for s in plan.stairs:
        a = math.radians(s.direction_deg)
        nx, ny = -math.sin(a) * s.width / 2.0, math.cos(a) * s.width / 2.0
        riser = msp.add_line(
            (s.start[0] - nx, s.start[1] - ny), (s.start[0] + nx, s.start[1] + ny), dxfattribs={"layer": STAIR_LAYER}
        )
        _xdata(riser, APP_ID, s.wire())
    return len(plan.openings) + len(plan.rooms) + len(plan.stairs)


def _furniture_and_site(doc: Drawing, msp: Modelspace, plan: Plan) -> int:
    count = 0
    for f in plan.furniture:
        name = _block(doc, _block_name(BLOCK_PREFIX, f.item))
        ins = msp.add_blockref(name, f.at, dxfattribs={"rotation": f.rotation, "layer": FURNITURE_LAYER})
        _xdata(ins, PLOMADA_APP, {"v": 1, "kind": "furniture", "id": f.id, "item": f.item})
        count += 1
    for o in plan.site.objects:
        name = _block(doc, _block_name(SITE_PREFIX, o.item))
        ins = msp.add_blockref(name, o.at, dxfattribs={"rotation": o.rotation, "layer": SITE_OBJECT_LAYER})
        _xdata(ins, PLOMADA_APP, {"v": 1, "kind": "site", "id": o.id, "item": o.item})
        count += 1
    for layer, key in SITE_LAYERS.items():
        for item in getattr(plan.site, key):
            fence = key == "fences"
            pl = _polyline(msp, item.points, item.closed if fence else True, layer)
            extra = {"height": item.height} if fence else ({"depth": item.depth} if item.depth is not None else {})
            _xdata(pl, PLOMADA_APP, {"v": 1, "kind": "site", "id": item.id, **extra})
            count += 1
    return count


def _storey(msp: Modelspace, plan: Plan) -> int:
    if plan.storey is None and not plan.site_on_ground:
        return 0
    payload: dict[str, Any] = {"v": 1, "kind": "storey", **(plan.storey.wire() if plan.storey else {})}
    if plan.site_on_ground:
        payload["site_on_ground"] = True
    _xdata(msp.add_point((0, 0), dxfattribs={"layer": STOREY_LAYER}), PLOMADA_APP, payload)
    return 1


def write_plan(plan: Plan, path: str | Path) -> dict[str, Any]:
    """Writes the plan as a millimetre DXF; returns {path, entities, layers, records}."""
    p = Path(path)
    doc = ezdxf.new("R2013")
    doc.units = units.MM
    for app in (APP_ID, PLOMADA_APP):
        doc.appids.new(app)
    msp = doc.modelspace()
    count = _walls(msp, plan) + _extra_records(msp, plan) + _arch_records(msp, plan)
    count += _furniture_and_site(doc, msp, plan) + _storey(msp, plan)
    layers = sorted({e.dxf.layer for e in msp})
    for name in layers:
        if name not in doc.layers:
            doc.layers.add(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(p)
    return {"path": str(p), "entities": len(msp), "layers": layers, "records": count}
