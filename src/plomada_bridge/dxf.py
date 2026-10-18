"""Reads the plan records AutoCAD MCP Pro and Plomada store in a DXF, and the PLOMADA-* layer standard.

A drawing reaches a plan by three routes, all exact (nothing is inferred):

1. ACADMCP_ARCH records. Every architectural object of AutoCAD MCP Pro travels
   as XDATA under the registered application ACADMCP_ARCH on one entity: a 1001
   group with the app name, then 1000 strings that, concatenated in order, form
   one JSON object with "v": 1 and a "kind" of wall, opening, stair or room. A
   record with any other version is refused by name; nothing is guessed.
2. PLOMADA records (docs/records-0.5.md) travel the same way under the
   application PLOMADA, but their geometry is the entity itself and the payload
   carries only the parameters:

   - an LWPOLYLINE is a wall axis (its closed flag is the wall's), a slab,
     planter or pool outline, a railing or curtain wall path (closed flag too),
     a column outline or a ramp axis, with its bulges; a SPLINE or ELLIPSE is
     flattened within FLATTEN_MM into straight vertices (nothing else is
     tessellated here); a 2D POLYLINE, LINE, ARC or CIRCLE is read exactly;
   - a CIRCLE is a round column: at its centre, diameter twice its radius (for
     any other kind but a ramp, a ring of two half circles);
   - a slab names its holes by entity handle, ``"holes": ["2F", "30"]``: each
     is a polyline (any layer, write_plan_dxf uses PLOMADA-LOSA-HUECO) read as
     one hole ring and never as a record of its own;
   - ``"kind": "furniture"`` on an ARCH_<ITEM> insert and ``"kind": "site"`` on
     a SITIO_<NAME> insert or SITIO-* polyline carry what the drawing cannot
     hold (id, item, pool depth, fence height), so a written plan reads back
     exactly; ``"kind": "opening"``, ``"room"`` and ``"stair"`` do the same on
     the layer-standard entities below (swing, hand, number, an L stair...);
   - ``"kind": "storey"`` on any entity (write_plan_dxf uses a POINT) carries
     the storey settings ``{name, height, elevation_mode?, elevation?}`` plus
     the plan flag ``site_on_ground`` when true.

3. The layer standard (docs/drawing-standard.md): with no XDATA at all, what any
   CAD draws on a PLOMADA-* layer becomes a record with the entity handle as id.
   Layer names are case-insensitive, Spanish or English; numbers in a layer name
   are millimetres whatever the drawing unit:

   - walls on PLOMADA-MURO-<thickness> / PLOMADA-WALL-<thickness> (centre
     justified): a polyline, SPLINE or ELLIPSE is one wall, a CIRCLE a round
     one; LINE and ARC pieces of one thickness whose ends meet within JOIN_MM at
     a node of exactly two ends are chained into one wall (a loop is a closed
     wall), and so are railing and curtain wall pieces;
   - PLOMADA-LOSA / -SLAB, -BARANDA / -RAILING, -JARDINERA / -PLANTER,
     -MURO-CORTINA / -CURTAIN-WALL, -PILETA / -POOL, -COLUMNA / -COLUMN with the
     record defaults; a ramp needs z_end, which only XDATA carries: a bare
     entity on PLOMADA-RAMPA is skipped with a warning;
   - openings on PLOMADA-PUERTA[-<height>] / -DOOR and
     PLOMADA-VENTANA[-<sill>-<height>] / -WINDOW: a LINE, ARC or two-point
     polyline from jamb to jamb on the host wall's axis (read_opening);
   - rooms: TEXT or MTEXT on PLOMADA-AMBIENTE / -ROOM (read_room);
   - straight stairs on PLOMADA-ESCALERA-<width> / -STAIR-<width>: a LINE from
     the midpoint of the bottom riser up the run (read_stair);
   - furniture: INSERTs of any block on PLOMADA-MUEBLE / -FURNITURE (furniture).

Coordinates are read in WCS and scaled to millimetres by the drawing unit
(read_units), except that records are always millimetres (_entity_scale). An
entity on a PLOMADA-* layer the standard does not know, or of a type its layer
does not take, is skipped with a warning naming it.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import ezdxf
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.math import Vec3

from . import planar
from .config import CONFIG
from .models import MAX_MM, RECORD_VERSION, Plan, Wall, bulge_of, fmt, fmt3, parse_plan

APP_ID = "ACADMCP_ARCH"
PLOMADA_APP = "PLOMADA"
BLOCK_PREFIX = "ARCH_"  # AutoCAD MCP Pro catalogue blocks: ARCH_<NAME>
SITE_PREFIX = "SITIO_"  # site blocks: SITIO_<NAME>, origin at the object's centre
SITE_LAYERS = {"SITIO-PAVIMENTO": "paving", "SITIO-DECK": "decks", "SITIO-PILETA": "pools", "SITIO-CERCO": "fences"}
KINDS = ("wall", "opening", "stair", "room")
STANDARD_DOC = "docs/drawing-standard.md"

# PLOMADA kinds drawn by their entity -> (plan list, field the entity's vertices fill).
GEOMETRY_KINDS = {
    "wall": ("walls", "axis"),
    "slab": ("slabs", "outline"),
    "railing": ("railings", "path"),
    "planter": ("planters", "outline"),
    "column": ("columns", "outline"),
    "curtain_wall": ("curtain_walls", "path"),
    "pool": ("pools", "outline"),
    "ramp": ("ramps", "axis"),
}
# Payloads on entities the other readers place.
ANNOTATION_KINDS = ("furniture", "site", "storey", "opening", "room", "stair")
PLOMADA_KINDS = (*GEOMETRY_KINDS, *ANNOTATION_KINDS)
OPEN_KINDS = ("wall", "railing", "curtain_wall")  # the polyline's closed flag is the record's
LAYER_KINDS = {
    "PLOMADA-LOSA": "slab",
    "PLOMADA-SLAB": "slab",
    "PLOMADA-BARANDA": "railing",
    "PLOMADA-RAILING": "railing",
    "PLOMADA-JARDINERA": "planter",
    "PLOMADA-PLANTER": "planter",
    "PLOMADA-MURO-CORTINA": "curtain_wall",
    "PLOMADA-CURTAIN-WALL": "curtain_wall",
    "PLOMADA-PILETA": "pool",
    "PLOMADA-POOL": "pool",
    "PLOMADA-RAMPA": "ramp",
    "PLOMADA-RAMP": "ramp",
    "PLOMADA-COLUMNA": "column",
    "PLOMADA-COLUMN": "column",
    "PLOMADA-AMBIENTE": "room",
    "PLOMADA-ROOM": "room",
    "PLOMADA-MUEBLE": "furniture",
    "PLOMADA-FURNITURE": "furniture",
    "PLOMADA-LOSA-HUECO": "hole",
    "PLOMADA-SLAB-HOLE": "hole",
    "PLOMADA-PLANTA": "storey",
    "PLOMADA-STOREY": "storey",
}
NUMBER = r"(\d+(?:\.\d+)?)"
WALL_LAYER = re.compile(rf"^PLOMADA-(?:MURO|WALL)-{NUMBER}$")
DOOR_LAYER = re.compile(rf"^PLOMADA-(?:PUERTA|DOOR)(?:-{NUMBER})?$")
WINDOW_LAYER = re.compile(rf"^PLOMADA-(?:VENTANA|WINDOW)(?:-{NUMBER}-{NUMBER})?$")
STAIR_LAYER = re.compile(rf"^PLOMADA-(?:ESCALERA|STAIR)-{NUMBER}$")
AREA_LINE = re.compile(r"^(?:\w+\s*[:=]\s*)?(\d+(?:[.,]\d+)?)\s*m(?:2|²|\^2)$", re.IGNORECASE)

CURVES = ("LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE")
PIECES = ("LINE", "ARC")  # chained into one polyline per layer
RINGS = (*CURVES, "CIRCLE")
TAKES = {  # entity types each layer kind takes
    "wall": (*PIECES, *RINGS),
    "railing": (*PIECES, *RINGS),
    "curtain_wall": (*PIECES, *RINGS),
    "slab": RINGS,
    "planter": RINGS,
    "pool": RINGS,
    "column": RINGS,
    "ramp": (*PIECES, *CURVES),
    "door": ("LINE", "ARC", "LWPOLYLINE", "POLYLINE"),
    "window": ("LINE", "ARC", "LWPOLYLINE", "POLYLINE"),
    "room": ("TEXT", "MTEXT"),
    "stair": ("LINE", "LWPOLYLINE", "POLYLINE"),
    "furniture": ("INSERT",),
    "hole": RINGS,
    "storey": ("POINT",),
}
FLATTEN_MM = 5.0  # largest sagitta of a flattened SPLINE or ELLIPSE
JOIN_MM = 1.0  # LINE and ARC ends this close are one node
RISER_MM = 175.0  # target riser of a stair drawn on a layer
ROUND = 6  # decimals (nm) of the lengths the reader derives: ARC ends, opening offsets, stair goings
# What ezdxf raises on a file it cannot parse: DXFError (DXFStructureError...), and from damaged tag streams
# ValueError (an invalid handle, a number that is not one, bad UTF-8), KeyError, IndexError, TypeError and
# StopIteration (the file ends early).
READ_ERRORS = (OSError, ezdxf.DXFError, ValueError, KeyError, IndexError, TypeError, StopIteration)

Units = Literal["auto", "mm", "cm", "m", "in", "ft"]
UNIT_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
INSUNITS = {0: "mm", 4: "mm", 5: "cm", 6: "m", 1: "in", 2: "ft"}  # $INSUNITS codes Plomada reads
UNIT_NAMES = {"mm": "millimetres", "cm": "centimetres", "m": "metres", "in": "inches", "ft": "feet"}


class DxfPlanError(ValueError):
    """The DXF cannot be turned into a plan; the message says why."""


@dataclass
class DxfPlan:
    plan: Plan
    path: Path
    records: int
    warnings: list[str] = field(default_factory=list)
    units: str = "mm"  # the unit the coordinates were read in
    units_note: str | None = None  # why $INSUNITS was not followed, when it was not


def _payload(entity: Any, app: str = APP_ID, kinds: tuple[str, ...] = KINDS) -> dict[str, Any]:
    tags = entity.get_xdata(app)
    text = "".join(str(tag.value) for tag in tags if tag.code == 1000)
    handle = entity.dxf.handle
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DxfPlanError(f"{app} XDATA on entity {handle} is not JSON ({exc.msg})") from None
    if not isinstance(payload, dict):
        raise DxfPlanError(f"{app} XDATA on entity {handle} is a {type(payload).__name__}, not an object")
    version = payload.get("v")
    if version != RECORD_VERSION:
        raise DxfPlanError(
            f"{app} record on entity {handle} has version {version!r}; Plomada reads version {RECORD_VERSION} only"
        )
    kind = payload.get("kind")
    if kind not in kinds:
        raise DxfPlanError(f"{app} record on entity {handle} has kind {kind!r}; kinds are {', '.join(kinds)}")
    return payload


def _has(entity: Any, app: str) -> bool:
    return hasattr(entity, "has_xdata") and entity.has_xdata(app)


def _plomada(entity: Any) -> dict[str, Any] | None:
    """The PLOMADA payload on an entity, or None."""
    if not _has(entity, PLOMADA_APP):
        return None
    return _payload(entity, PLOMADA_APP, PLOMADA_KINDS)


def _params(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in ("v", "kind")}


def _annotation(entity: Any, kind: str) -> dict[str, Any]:
    """The fields a PLOMADA payload of this kind adds to a record read from the entity ({} without one), in
    mm as every record is."""
    payload = _plomada(entity)
    if payload is None:
        return {}
    if payload["kind"] != kind:
        raise DxfPlanError(
            f"{PLOMADA_APP} record on entity {entity.dxf.handle} has kind {payload['kind']!r}; "
            f"a {entity.dxftype()} there carries {kind}"
        )
    return _params(payload)


def records(doc: Drawing) -> list[dict[str, Any]]:
    """Every ACADMCP_ARCH payload in the drawing, in database order, deduplicated."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for entity in doc.entitydb.values():
        if not _has(entity, APP_ID):
            continue
        payload = _payload(entity)
        key = (payload["kind"], str(payload.get("id")))
        if key in found and found[key] != payload:
            raise DxfPlanError(f"two different {payload['kind']} records share the id {key[1]!r}")
        found[key] = payload
    return list(found.values())


# --- units ----------------------------------------------------------------------------------


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scale_vertices(vertices: Any, k: float) -> Any:
    if not isinstance(vertices, list):
        return vertices
    return [
        [v[0] * k, v[1] * k, *v[2:]] if isinstance(v, (list, tuple)) and len(v) >= 2 and _num(v[0]) and _num(v[1])
        else v
        for v in vertices
    ]  # fmt: skip


def _entity_scale(entity: Any, scale: float, with_records: bool) -> float:
    """mm per drawing unit for one entity: 1 for an entity that carries an ACADMCP_ARCH or PLOMADA record
    (records are millimetres whatever the drawing unit) and, in a drawing with records, for one off the
    PLOMADA-* layers (the ARCH_ and SITIO_ blocks and SITIO-* polylines AutoCAD MCP Pro and write_plan_dxf
    draw with the records); scale for the rest, the bare layer standard."""
    if _has(entity, APP_ID) or _has(entity, PLOMADA_APP):
        return 1.0
    if with_records and not entity.dxf.layer.upper().startswith("PLOMADA-"):
        return 1.0
    return scale


def read_units(doc: Drawing, units: str, has_records: bool, name: str) -> tuple[str, float, str | None]:
    """(unit, mm per drawing unit, note) for a drawing. units other than auto is taken as given, for the
    entities it applies to (_entity_scale: never a record). auto:
    a drawing with ACADMCP_ARCH or PLOMADA records is millimetres, the records' contract (AutoCAD MCP Pro
    leaves $INSUNITS at its template's inches or metres over millimetre records); otherwise $INSUNITS
    0 (unitless) and 4 are mm, 5 cm and 6 m. Inches and feet (1, 2) must be confirmed with units: AutoCAD's
    imperial template declares inches over drawings made in millimetres. Other codes are refused."""
    if units != "auto":
        if units not in UNIT_MM:
            raise DxfPlanError(f"units must be one of auto, {', '.join(UNIT_MM)}; got {units!r}")
        return units, UNIT_MM[units], None
    code = doc.header.get("$INSUNITS", 0)
    unit = INSUNITS.get(code)
    if has_records:
        note = None
        if unit != "mm":
            note = f"$INSUNITS {code} ignored: ACADMCP_ARCH and PLOMADA records are millimetres"
        return "mm", 1.0, note
    if unit is None:
        raise DxfPlanError(
            f"{name} declares $INSUNITS {code}, which is not a unit Plomada reads; "
            'pass units ("mm", "cm", "m", "in" or "ft") with the unit the drawing is in'
        )
    if unit in ("in", "ft"):
        raise DxfPlanError(
            f"{name} declares $INSUNITS {code} ({UNIT_NAMES[unit]}), the default of AutoCAD's imperial template "
            f'that drawings made in millimetres often keep; pass units="{unit}" if it is in {UNIT_NAMES[unit]}, '
            'or units="mm"'
        )
    return unit, UNIT_MM[unit], None


def _span(walls: list[dict[str, Any]]) -> float:
    """The larger side of the box around every wall axis vertex, mm (0 without walls)."""
    pts = [v for w in walls for v in (w.get("axis") or []) if isinstance(v, (list, tuple)) and len(v) >= 2]
    pts = [v for v in pts if _num(v[0]) and _num(v[1])]
    if not pts:
        return 0.0
    return max(max(p[0] for p in pts) - min(p[0] for p in pts), max(p[1] for p in pts) - min(p[1] for p in pts))


# --- geometry from the entity -------------------------------------------------------------


def _r(value: float) -> float:
    return round(value, ROUND) + 0.0  # + 0.0 turns -0.0 into 0.0


def _arc_vertices(arc: Any) -> list[list[float]]:
    """An ARC (counter-clockwise in its OCS, clockwise seen from +z when its extrusion points down) as
    vertices with bulges; an arc over 180 degrees is split at its middle."""
    start, end = float(arc.dxf.start_angle), float(arc.dxf.end_angle)
    sweep = (end - start) % 360.0
    if sweep <= 1e-9:
        raise DxfPlanError(f"ARC {arc.dxf.handle} on {arc.dxf.layer} sweeps no angle")
    parts = 1 if sweep <= 180.0 + 1e-9 else 2
    pts = list(arc.vertices([start + sweep * i / parts for i in range(parts + 1)]))
    sign = 1.0 if arc.dxf.extrusion.z >= 0 else -1.0
    b = sign * math.tan(math.radians(sweep / parts) / 4.0)
    return [[_r(p.x), _r(p.y), b] for p in pts[:-1]] + [[_r(pts[-1].x), _r(pts[-1].y)]]


def _plain(entity: Any) -> bool:
    """True when the entity's OCS is the WCS: its extrusion is +z, the DXF default."""
    ext = entity.dxf.extrusion
    return abs(ext.x) < 1e-9 and abs(ext.y) < 1e-9 and ext.z > 0


def _in_wcs(entity: Any, pts: list[tuple[float, float, float, float]], ocs: bool = True) -> list[list[float]]:
    """OCS (x, y, z, bulge) of a 2D polyline as WCS [x, y] / [x, y, bulge] vertices, zero bulges dropped. A
    downward extrusion (a mirrored or exploded block) mirrors the plane seen from +z: x turns to -x and the
    bulges turn the other way. A +z extrusion, or ocs False (points already in WCS), passes them through."""
    if ocs and not _plain(entity):
        sign = -1.0 if entity.dxf.extrusion.z < 0 else 1.0
        wcs = entity.ocs().points_to_wcs(Vec3(x, y, z) for x, y, z, _ in pts)
        pts = [(p.x + 0.0, p.y + 0.0, 0.0, sign * b) for p, (_, _, _, b) in zip(wcs, pts, strict=True)]
    return [[float(x), float(y), float(b)] if b else [float(x), float(y)] for x, y, _, b in pts]


def _placed(ins: Any) -> tuple[list[float], float]:
    """An INSERT's WCS point and the WCS direction of its block's x axis, degrees CCW (the insert point
    and rotation are in its OCS)."""
    at, rotation = ins.dxf.insert, float(ins.dxf.get("rotation", 0.0))
    if _plain(ins):
        return [float(at.x), float(at.y)], rotation
    ocs = ins.ocs()
    p, x = ocs.to_wcs(at), ocs.to_wcs(Vec3.from_deg_angle(rotation))
    return [p.x + 0.0, p.y + 0.0], _r(math.degrees(math.atan2(x.y, x.x)) % 360.0)


def vertices(entity: Any, scale: float = 1.0) -> tuple[list[list[float]], bool]:
    """The entity as [x, y] / [x, y, bulge] WCS vertices in mm and its closed flag: an LWPOLYLINE or 2D
    POLYLINE (drawn in its OCS, _in_wcs) with its bulges, zeros dropped, a 3D POLYLINE, a LINE, an ARC with
    its bulge, a CIRCLE as a ring of two half circles, a SPLINE or ELLIPSE flattened to straight vertices
    within FLATTEN_MM (mm: FLATTEN_MM / scale drawing units)."""
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        z = float(entity.dxf.elevation)
        pts = _in_wcs(entity, [(x, y, z, b) for x, y, b in entity.get_points("xyb")])
        closed = bool(entity.closed)
    elif kind == "POLYLINE":
        if entity.is_poly_face_mesh or entity.is_polygon_mesh:
            raise DxfPlanError(f"entity {entity.dxf.handle} is a POLYLINE mesh, not a polyline")
        raw = []
        for v in entity.vertices:
            if v.dxf.flags & 16:  # a spline frame control point, not on the curve
                continue
            loc = v.dxf.location
            raw.append((float(loc.x), float(loc.y), float(loc.z), float(v.dxf.get("bulge", 0.0))))
        pts = _in_wcs(entity, raw, ocs=entity.is_2d_polyline)  # a 3D polyline is drawn in WCS
        closed = bool(entity.is_closed)
    elif kind == "LINE":
        s, e = entity.dxf.start, entity.dxf.end
        pts, closed = [[float(s.x), float(s.y)], [float(e.x), float(e.y)]], False
    elif kind == "ARC":
        pts, closed = _arc_vertices(entity), False
    elif kind == "CIRCLE":
        c = entity.ocs().to_wcs(entity.dxf.center)
        r = float(entity.dxf.radius)
        pts, closed = [[_r(c.x + r), _r(c.y), 1.0], [_r(c.x - r), _r(c.y), 1.0]], True
    elif kind in ("SPLINE", "ELLIPSE"):
        if kind == "SPLINE":
            closed = bool(entity.closed)
        else:
            closed = math.isclose(abs(entity.dxf.end_param - entity.dxf.start_param), math.tau, abs_tol=1e-9)
        pts = [[float(p.x), float(p.y)] for p in entity.flattening(FLATTEN_MM / scale)]
        if len(pts) > 2 and math.dist(pts[0], pts[-1]) <= 1e-6:
            pts.pop()
            closed = True
    else:
        raise DxfPlanError(f"entity {entity.dxf.handle} is a {kind}; polylines are {', '.join(CURVES)}")
    return (_scale_vertices(pts, scale) if scale != 1.0 else pts), closed


def _hole(doc: Drawing, handle: Any, slab: str, scale: float) -> list[list[float]]:
    entity = doc.entitydb.get(handle) if isinstance(handle, str) else None
    if entity is None or entity.dxftype() not in RINGS:
        raise DxfPlanError(f"slab {slab} names hole {handle!r}, which is not a polyline in the drawing")
    return vertices(entity, scale)[0]


def _geometry_record(doc: Drawing, entity: Any, kind: str, params: dict[str, Any], scale: float) -> dict[str, Any]:
    """A 0.5 record: its parameters, its id (default the entity handle) and the entity's geometry."""
    rec: dict[str, Any] = {"v": RECORD_VERSION, "kind": kind, **params}
    rec.setdefault("id", entity.dxf.handle)
    if entity.dxftype() == "CIRCLE":
        if kind == "column":
            c = entity.ocs().to_wcs(entity.dxf.center)
            rec["at"] = [float(c.x) * scale, float(c.y) * scale]
            rec["diameter"] = 2.0 * float(entity.dxf.radius) * scale
            return rec
        if kind == "ramp":
            raise DxfPlanError(f"ramp {rec['id']} is drawn as a CIRCLE; a ramp is an open axis")
    pts, closed = vertices(entity, scale)
    rec[GEOMETRY_KINDS[kind][1]] = pts
    if kind in OPEN_KINDS:
        rec["closed"] = closed
    if kind == "slab":
        rec["holes"] = [_hole(doc, h, rec["id"], scale) for h in params.get("holes", [])]
    return rec


# --- the layer standard ---------------------------------------------------------------------


def standard_layer(layer: str) -> tuple[str, dict[str, Any]] | None:
    """The kind a PLOMADA-* layer stands for and the parameters its name carries (mm), or None:
    ("wall", {thickness, justification}), ("door", {sill, height}), ("window", {sill, height}),
    ("stair", {width}), or a LAYER_KINDS kind with no parameters."""
    name = layer.upper()
    if m := WALL_LAYER.match(name):
        return "wall", {"thickness": float(m[1]), "justification": "center"}
    if name in LAYER_KINDS:
        return LAYER_KINDS[name], {}
    if m := DOOR_LAYER.match(name):
        return "door", {"sill": None, "height": float(m[1]) if m[1] else None}
    if m := WINDOW_LAYER.match(name):
        return "window", {"sill": float(m[1]), "height": float(m[2])} if m[1] else {"sill": None, "height": None}
    if m := STAIR_LAYER.match(name):
        return "stair", {"width": float(m[1])}
    return None


def _takes(entity: Any, kind: str) -> str | None:
    """None when the layer's kind takes this entity type, else the warning that skips it."""
    if entity.dxftype() in TAKES[kind]:
        return None
    return (
        f"{entity.dxftype()} {entity.dxf.handle} on {entity.dxf.layer} skipped: a {kind.replace('_', ' ')} "
        f"layer takes {', '.join(TAKES[kind])}"
    )


def _standard_entities(doc: Drawing, kinds: tuple[str, ...]) -> Iterable[tuple[Any, str, dict[str, Any]]]:
    """(entity, kind, layer parameters) for the model space entities on a layer of these kinds that carry
    no ACADMCP_ARCH record (that entity is read as its record)."""
    for entity in doc.modelspace():
        found = standard_layer(entity.dxf.layer)
        if found is not None and found[0] in kinds and not _has(entity, APP_ID):
            yield entity, found[0], found[1]


def _join(pieces: list[list[list[float]]]) -> list[list[float]]:
    """One polyline from pieces that follow each other; a joint takes the coordinates of the earlier
    piece's end and the bulge of the later piece's start."""
    out = [list(v) for v in pieces[0]]
    for verts in pieces[1:]:
        last = out.pop()
        b = bulge_of(verts[0])
        out.append([last[0], last[1], b] if b else [last[0], last[1]])
        out.extend(list(v) for v in verts[1:])
    return out


def chain(pieces: Sequence[tuple[str, list[list[float]]]]) -> list[tuple[str, list[list[float]], bool]]:
    """Chains open pieces (handle, vertices) into polylines (handle, vertices, closed): ends within
    JOIN_MM are one node, and two pieces follow each other through a node that exactly two ends share. A
    chain keeps the direction and the handle of its earliest piece in drawing order; a loop is closed."""
    nodes: list[tuple[float, float]] = []
    grid: dict[tuple[int, int], list[int]] = {}

    def node(p: Sequence[float]) -> int:
        gx, gy = math.floor(p[0] / JOIN_MM), math.floor(p[1] / JOIN_MM)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n in grid.get((gx + dx, gy + dy), ()):
                    if math.dist(nodes[n], p[:2]) <= JOIN_MM:
                        return n
        nodes.append((p[0], p[1]))
        grid.setdefault((gx, gy), []).append(len(nodes) - 1)
        return len(nodes) - 1

    ends = [(node(v[0]), node(v[-1])) for _, v in pieces]
    at: dict[int, list[tuple[int, int]]] = {}
    for i, (a, b) in enumerate(ends):
        at.setdefault(a, []).append((i, 0))
        at.setdefault(b, []).append((i, 1))
    used = [False] * len(pieces)

    def follow(n: int) -> tuple[int, int] | None:
        if len(at[n]) != 2:
            return None
        free = [(j, e) for j, e in at[n] if not used[j]]
        return free[0] if len(free) == 1 else None

    out: list[tuple[str, list[list[float]], bool]] = []
    for i, (handle, verts) in enumerate(pieces):
        if used[i]:
            continue
        used[i] = True
        head, tail = ends[i]
        before: list[list[list[float]]] = []
        after: list[list[list[float]]] = []
        closed = False
        if head != tail:
            n = tail
            while (nxt := follow(n)) is not None:
                j, e = nxt
                used[j] = True
                after.append(pieces[j][1] if e == 0 else planar.reverse(pieces[j][1]))
                n = ends[j][1 - e]
            closed = n == head and len(at[head]) == 2 and bool(after)
            n = head
            while not closed and (nxt := follow(n)) is not None:
                j, e = nxt
                used[j] = True
                before.append(pieces[j][1] if e == 1 else planar.reverse(pieces[j][1]))
                n = ends[j][1 - e]
        joined = _join([*before[::-1], verts, *after])
        if closed:
            joined.pop()
        out.append((handle, joined, closed))
    return out


def plomada_records(
    doc: Drawing, scale: float = 1.0
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None, list[str]]:
    """The 0.5 records of model space by plan list (PLOMADA records and the geometry layers of the
    standard, in drawing order, then the chained LINE and ARC walls, railings and curtain walls); the
    storey payload (or None); warnings for the entities skipped. scale (mm per drawing unit) applies to the
    layer standard only: a PLOMADA record, its geometry and its holes are millimetres."""
    out: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in GEOMETRY_KINDS.values()}
    storey: dict[str, Any] | None = None
    warnings: list[str] = []
    unknown: dict[str, int] = {}
    pieces: dict[tuple[str, tuple[tuple[str, Any], ...]], list[tuple[str, list[list[float]]]]] = {}
    tagged = [(e, _plomada(e)) for e in doc.modelspace()]
    holes = {h for _, p in tagged if p and p["kind"] == "slab" for h in p.get("holes", []) if isinstance(h, str)}
    for entity, payload in tagged:
        handle = entity.dxf.handle
        if handle in holes:
            continue
        if payload is not None:
            kind = payload["kind"]
            if kind == "storey":
                if storey is not None:
                    raise DxfPlanError(f"two {PLOMADA_APP} storey records; a DXF holds one storey")
                storey = _params(payload)
            elif kind in GEOMETRY_KINDS:
                out[GEOMETRY_KINDS[kind][0]].append(_geometry_record(doc, entity, kind, _params(payload), 1.0))
            continue
        if _has(entity, APP_ID):
            continue
        layer = entity.dxf.layer
        found = standard_layer(layer)
        if found is None:
            if layer.upper().startswith("PLOMADA-"):
                unknown[layer] = unknown.get(layer, 0) + 1
            continue
        kind, params = found
        if kind in ("hole", "storey"):
            what = "only a slab's PLOMADA record names its holes" if kind == "hole" else "the storey travels in XDATA"
            warnings.append(f"{entity.dxftype()} {handle} on {layer} skipped: {what}")
            continue
        if kind not in GEOMETRY_KINDS:
            continue  # openings, rooms, stairs and furniture are read once the walls are known
        if skip := _takes(entity, kind):
            warnings.append(skip)
            continue
        if kind == "ramp":
            warnings.append(f"ramp {handle} on {layer} skipped: its z_end travels only in XDATA")
            continue
        if entity.dxftype() in PIECES and kind in OPEN_KINDS:
            pieces.setdefault((kind, tuple(params.items())), []).append((handle, vertices(entity, scale)[0]))
            continue
        out[GEOMETRY_KINDS[kind][0]].append(_geometry_record(doc, entity, kind, params, scale))
    for (kind, params), group in pieces.items():
        key, name = GEOMETRY_KINDS[kind]
        for handle, verts, closed in chain(group):
            out[key].append(
                {"v": RECORD_VERSION, "kind": kind, **dict(params), "id": handle, name: verts, "closed": closed}
            )
    for layer, count in unknown.items():
        warnings.append(
            f"layer {layer} is not part of the drawing standard ({STANDARD_DOC}): "
            f"{count} entit{'y' if count == 1 else 'ies'} skipped"
        )
    return out, storey, warnings


def _has_plomada_layer(doc: Drawing) -> bool:
    return any(e.dxf.layer.upper().startswith("PLOMADA-") for e in doc.modelspace())


def _two_points(entity: Any, kind: str, scale: float) -> list[list[float]]:
    """The two ends of a LINE, ARC or two-point polyline on an opening or stair layer."""
    pts = vertices(entity, scale)[0]
    if entity.dxftype() == "ARC":
        return [pts[0][:2], pts[-1][:2]]
    if len(pts) != 2:
        what = "one LINE, ARC or two-point polyline" if kind != "stair" else "one LINE or two-point polyline"
        raise DxfPlanError(
            f"{kind} {entity.dxf.handle} on {entity.dxf.layer} is a {entity.dxftype()} of {len(pts)} points; "
            f"draw it as {what}"
        )
    return [pts[0][:2], pts[1][:2]]


def _point(p: Sequence[float]) -> str:
    return f"({fmt3(p[0])}, {fmt3(p[1])})"


def read_opening(entity: Any, kind: str, params: dict[str, Any], walls: list[Wall], scale: float) -> dict[str, Any]:
    """An opening drawn from jamb to jamb on its host wall's axis: the host is the one wall whose axis
    lies within half its thickness of both jambs; offset is the station of the near jamb along the host
    axis (arc length on arcs, from the axis's first vertex), width the distance to the far one along the
    axis. On a closed wall the opening sits inside one run, never across the first vertex. sill and height
    come from the layer name (null keeps the record defaults); a PLOMADA opening payload supplies the rest
    (swing, hand, tag, head...)."""
    handle, layer = entity.dxf.handle, entity.dxf.layer
    p, q = _two_points(entity, kind, scale)
    hosts = []
    for w in walls:
        (d0, s0), (d1, s1) = planar.project(w.axis, w.closed, p), planar.project(w.axis, w.closed, q)
        half = w.thickness / 2.0 + 1e-6
        if d0 <= half and d1 <= half:
            hosts.append((w, s0, s1))
    if not hosts:
        raise DxfPlanError(
            f"{kind} {handle} on {layer} from {_point(p)} to {_point(q)} is on no wall: no wall axis lies within "
            "half the wall's thickness of both jambs; draw it from jamb to jamb on its wall's axis"
        )
    if len(hosts) > 1:
        names = ", ".join(w.id for w, _, _ in hosts)
        raise DxfPlanError(f"{kind} {handle} on {layer} lies on walls {names} at once; draw it on one wall's axis")
    w, s0, s1 = hosts[0]
    lo, hi = sorted((s0, s1))
    total = planar.length(w.axis, w.closed)
    if w.closed and hi - lo > total / 2.0:
        if lo <= JOIN_MM:  # the far jamb is the first vertex, reached from the end of the loop
            lo, hi = hi, total
        elif total - hi <= JOIN_MM:  # the near jamb is the first vertex
            lo, hi = 0.0, lo
        else:
            raise DxfPlanError(
                f"{kind} {handle} on {layer} straddles the first vertex of closed wall {w.id}; "
                "an opening sits inside one run of its wall"
            )
    width = _r(hi - lo)
    if width <= 0.0:
        raise DxfPlanError(f"{kind} {handle} on {layer} has no width along wall {w.id}")
    rec = {
        "v": RECORD_VERSION,
        "kind": "opening",
        "id": handle,
        "wall": w.id,
        "opening_kind": kind,
        "offset": _r(lo),
        "width": width,
        "sill": params["sill"],
        "height": params["height"],
    }
    return {**rec, **_annotation(entity, "opening")}


def _text_point(entity: Any) -> list[float]:
    """Where a TEXT or MTEXT stands: MTEXT its insertion (attachment) point; TEXT its insertion point when
    left-aligned on the baseline, else its alignment point, and the middle of the two for ALIGNED and FIT."""
    if entity.dxftype() == "MTEXT":
        p = entity.dxf.insert
    else:
        align, p1, p2 = entity.get_placement()
        if align in (TextEntityAlignment.ALIGNED, TextEntityAlignment.FIT) and p2 is not None:
            p1 = p1.lerp(p2)
        p = entity.ocs().to_wcs(p1)
    return [float(p.x), float(p.y)]


def read_room(entity: Any, scale: float) -> dict[str, Any] | None:
    """A room from its label: the name is the plain text (MTEXT formatting stripped, lines joined by a
    space), at the text's point; a line after the first such as '12.5 m2', '12,5 m²' or 'A: 12.5 m2' is
    the area in square metres (whatever the drawing unit), else the area is 0. None for an empty label."""
    text = entity.plain_text(split=False) if entity.dxftype() == "MTEXT" else entity.plain_text()
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    area = 0.0
    names = []
    for i, ln in enumerate(lines):
        if i and (m := AREA_LINE.match(ln)):
            area = round(float(m[1].replace(",", ".")) * 1_000_000.0, ROUND)
        else:
            names.append(ln)
    if not names:
        return None
    x, y = _text_point(entity)
    rec = {
        "v": RECORD_VERSION,
        "kind": "room",
        "id": entity.dxf.handle,
        "name": " ".join(names),
        "at": [x * scale, y * scale],
        "area": area,
    }
    return {**rec, **_annotation(entity, "room")}


def read_stair(entity: Any, params: dict[str, Any], floor_to_floor: float, scale: float) -> dict[str, Any]:
    """A straight stair from a LINE drawn from the midpoint of its bottom riser up its run: start and
    direction from the line, the run its length. risers = floor_to_floor / RISER_MM rounded (at least 2),
    riser_height = floor_to_floor / risers, going = run / (risers - 1): the last riser lands on the floor
    above (Geometry.stair_layout). A PLOMADA stair payload (an L or U, its own risers) wins."""
    p, q = _two_points(entity, "stair", scale)
    run = math.dist(p, q)
    if run < JOIN_MM:
        raise DxfPlanError(f"stair {entity.dxf.handle} on {entity.dxf.layer} has no length")
    risers = max(2, round(floor_to_floor / RISER_MM))
    rec = {
        "v": RECORD_VERSION,
        "kind": "stair",
        "id": entity.dxf.handle,
        "start": p,
        "direction_deg": _r(math.degrees(math.atan2(q[1] - p[1], q[0] - p[0]))),
        "width": params["width"],
        "risers": risers,
        "riser_height": _r(floor_to_floor / risers),
        "going": _r(run / (risers - 1)),
        "stair_kind": "straight",
        "turn": "left",
    }
    return {**rec, **_annotation(entity, "stair")}


def _host_walls(walls: list[dict[str, Any]], name: str) -> list[Wall]:
    if not walls:
        return []
    try:
        return parse_plan({"walls": walls}).walls
    except ValueError as exc:
        raise DxfPlanError(f"{name}: {exc}") from None


def floor_to_floor(storey: dict[str, Any] | None, slabs: list[dict[str, Any]]) -> float:
    """The rise a stair climbs, as the extension checks it: an explicit storey's own height, else the storey
    height plus the slab (the thickest slab record, else CONFIG.slab_thickness_mm)."""
    height = storey.get("height") if storey else None
    height = float(height) if _num(height) else CONFIG.storey_height_mm
    if storey and (storey.get("elevation_mode") == "explicit" or storey.get("elevation") is not None):
        return height
    thick = [float(s.get("thickness", 150.0)) for s in slabs if _num(s.get("thickness", 150.0))]
    return height + (max(thick) if thick else CONFIG.slab_thickness_mm)


# --- furniture and site -----------------------------------------------------------------


def furniture(doc: Drawing, scale: float = 1.0, with_records: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """The furniture inserts of model space as records: AutoCAD MCP Pro catalogue blocks (ARCH_<NAME>, on
    any layer) and inserts of any block on PLOMADA-MUEBLE / PLOMADA-FURNITURE. The item comes from the
    block's ITEM attribute, else its name lowercased without the ARCH_ prefix; at is the insert point,
    which stands for the piece's back-left corner (the block origin, as arch_catalogue_insert places it),
    and rotation is degrees CCW about it. A PLOMADA furniture payload on the insert supplies the id and
    item. A scaled or mirrored insert (a negative scale, or an extrusion pointing down) is skipped with a
    warning. scale applies as _entity_scale says."""
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    for ins in doc.modelspace().query("INSERT"):
        name = ins.dxf.name
        arch = name.upper().startswith(BLOCK_PREFIX)
        if not arch and standard_layer(ins.dxf.layer) != ("furniture", {}):
            continue
        base = name[len(BLOCK_PREFIX) :] if arch else name
        item = next((a.dxf.text for a in ins.attribs if a.dxf.tag.upper() == "ITEM"), "") or base
        scale_xy = (ins.dxf.get("xscale", 1.0), ins.dxf.get("yscale", 1.0))
        if any(abs(s - 1.0) > 1e-6 for s in scale_xy):
            warnings.append(f"furniture {ins.dxf.handle} ({name}) skipped: scaled or mirrored {scale_xy}")
            continue
        if ins.dxf.extrusion.z < 0:
            warnings.append(f"furniture {ins.dxf.handle} ({name}) skipped: mirrored (its extrusion points down)")
            continue
        at, rotation = _placed(ins)
        k = _entity_scale(ins, scale, with_records)
        out.append(
            {
                "v": 1,
                "kind": "furniture",
                "id": ins.dxf.handle,
                "item": item.strip().lower(),
                "at": [at[0] * k, at[1] * k],
                "rotation": rotation,
                **_annotation(ins, "furniture"),
            }
        )
    for entity, kind, _ in _standard_entities(doc, ("furniture",)):
        if skip := _takes(entity, kind):
            warnings.append(skip)
    return out, warnings


def site(doc: Drawing, scale: float = 1.0, with_records: bool = False) -> dict[str, list[dict[str, Any]]]:
    """The site: SITIO_<NAME> inserts (a library component centred on the insert point) and
    the lightweight polylines, bulges included, on the SITIO-PAVIMENTO, -DECK, -PILETA (closed)
    and -CERCO layers. A PLOMADA site payload supplies the id, item, depth or height. scale applies as
    _entity_scale says."""
    out: dict[str, list[dict[str, Any]]] = {"objects": [], "paving": [], "decks": [], "pools": [], "fences": []}
    msp = doc.modelspace()
    for ins in msp.query("INSERT"):
        name = ins.dxf.name
        if name.upper().startswith(SITE_PREFIX):
            at, rotation = _placed(ins)
            k = _entity_scale(ins, scale, with_records)
            out["objects"].append(
                {
                    "id": ins.dxf.handle,
                    "item": name[len(SITE_PREFIX) :].upper(),
                    "at": [at[0] * k, at[1] * k],
                    "rotation": rotation,
                    **_annotation(ins, "site"),
                }
            )
    for pl in msp.query("LWPOLYLINE"):
        key = SITE_LAYERS.get(pl.dxf.layer.upper())
        if key is None:
            continue
        pts, closed = vertices(pl, _entity_scale(pl, scale, with_records))
        rec: dict[str, Any] = {"id": pl.dxf.handle, "points": pts}
        if key == "fences":
            rec["closed"] = closed
        out[key].append({**rec, **_annotation(pl, "site")})
    return out


# --- the plan -------------------------------------------------------------------------------


def load_drawing(p: Path) -> Drawing:
    """The drawing at p; a .dwg is read through a temporary DXF (cadfile.as_dxf), removed afterwards. A file
    ezdxf cannot parse raises DxfPlanError "cannot read <name> as DXF: ...", whatever ezdxf raised (READ_ERRORS:
    a damaged file surfaces as ValueError, KeyError or StopIteration as often as as DXFStructureError)."""
    from .cadfile import TEMP_PREFIX, as_dxf  # cadfile raises DxfPlanError, defined here

    src = as_dxf(p)
    try:
        return ezdxf.readfile(src)
    except READ_ERRORS as exc:
        raise DxfPlanError(f"cannot read {p.name} as DXF: {str(exc) or 'it ends early or is malformed'}") from None
    finally:
        if src != p and src.parent.name.startswith(TEMP_PREFIX):
            shutil.rmtree(src.parent, ignore_errors=True)


def _read(
    doc: Drawing, name: str, scale: float, storey_height: float | None, with_records: bool
) -> tuple[dict[str, Any], int, list[str]]:
    """The raw plan of the drawing in mm, the number of records read and the warnings. scale (mm per
    drawing unit) applies entity by entity (_entity_scale): records are millimetres."""
    recs = records(doc)
    extra, storey, warnings = plomada_records(doc, scale)
    found = sum(len(v) for v in extra.values())
    pieces, furniture_warnings = furniture(doc, scale, with_records)
    walls = [r for r in recs if r["kind"] == "wall"] + extra.pop("walls")
    raw: dict[str, Any] = {
        "walls": walls,
        "openings": [r for r in recs if r["kind"] == "opening"],
        "rooms": [r for r in recs if r["kind"] == "room"],
        "stairs": [r for r in recs if r["kind"] == "stair"],
        "furniture": pieces,
        "site": site(doc, scale, with_records),
        **extra,
    }
    if storey is not None:
        raw["site_on_ground"] = storey.pop("site_on_ground", False)
        storey = storey or None  # a payload with the flag alone sets no storey
    if storey_height is not None:
        storey = {**(storey or {}), "height": storey_height}
    if storey is not None:
        raw["storey"] = storey
    layered = 0
    hosts: list[Wall] | None = None
    rise = floor_to_floor(storey, raw["slabs"])
    for entity, kind, params in _standard_entities(doc, ("door", "window", "room", "stair")):
        if skip := _takes(entity, kind):
            warnings.append(skip)
            continue
        k = _entity_scale(entity, scale, with_records)
        if kind == "room":
            room = read_room(entity, k)
            if room is None:
                warnings.append(f"{entity.dxftype()} {entity.dxf.handle} on {entity.dxf.layer} skipped: no room name")
                continue
            raw["rooms"].append(room)
        elif kind == "stair":
            raw["stairs"].append(read_stair(entity, params, rise, k))
        else:
            if hosts is None:
                hosts = _host_walls(walls, name)
            raw["openings"].append(read_opening(entity, kind, params, hosts, k))
        layered += 1
    return raw, len(recs) + found + layered, furniture_warnings + warnings


def read_plan(path: str | Path, storey_height: float | None = None, units: Units | str = "auto") -> DxfPlan:
    """Parses the DXF (or DWG, through cadfile) with ezdxf and validates the records into a Plan, in mm.
    storey_height, when given, overrides the height of a PLOMADA storey record. units is the drawing unit
    (read_units; auto follows $INSUNITS); a cm or m from $INSUNITS that would make the walls span more
    than 1 km (MAX_MM) is read as mm instead, with units_note saying so: ezdxf's new drawings and AutoCAD
    MCP Pro's template declare metres over drawings made in millimetres."""
    p = Path(path)
    if not p.is_file():
        raise DxfPlanError(f"dxf_path {str(p)!r} is not a file")
    doc = load_drawing(p)
    acad = any(_has(e, APP_ID) for e in doc.entitydb.values())
    plomada = any(_has(e, PLOMADA_APP) for e in doc.modelspace())
    if not acad and not plomada and not _has_plomada_layer(doc):
        raise DxfPlanError(
            f"{p.name} holds no {APP_ID} records, no {PLOMADA_APP} records and nothing on a PLOMADA-* layer: "
            f"draw it with AutoCAD MCP Pro's arch_* tools, write it with write_plan_dxf, or draw the PLOMADA-* "
            f"layer standard with any CAD ({STANDARD_DOC})"
        )
    unit, scale, note = read_units(doc, units, acad or plomada, p.name)
    if units == "auto" and scale != 1.0 and (span := _span(plomada_records(doc, scale)[0]["walls"])) > MAX_MM:
        note = (
            f"$INSUNITS {doc.header.get('$INSUNITS')} ({UNIT_NAMES[unit]}) would make the walls span "
            f"{fmt(round(span / 1000.0, 1))} m; read as millimetres (pass units to force a unit)"
        )
        unit, scale = "mm", 1.0
    raw, count, warnings = _read(doc, p.name, scale, storey_height, acad or plomada)
    if (acad or plomada) and scale != 1.0:
        warnings.append(
            f'units="{unit}" applied only to the entities on PLOMADA-* layers without XDATA: the {APP_ID} and '
            f"{PLOMADA_APP} records, and the {BLOCK_PREFIX} and {SITE_PREFIX} blocks and SITIO-* polylines drawn "
            "with them, are millimetres"
        )
    try:
        plan = parse_plan(raw)
    except ValueError as exc:
        raise DxfPlanError(f"{p.name}: {exc}") from None
    return DxfPlan(plan=plan, path=p, records=count, warnings=warnings, units=unit, units_note=note)
