"""Writes a plan as a DXF (write_plan_dxf) that read_plan reads back.

The drawing is in millimetres ($INSUNITS 4), in one of three styles:

- ``records`` (0.5): the data entities below, and both wall faces on A-WALL-E-N
  as offset curves, segment by segment, for documentation;
- ``drawing`` (write_plan_dxf's default): the same data entities with the
  documentation of drafting.py instead of the A-WALL-E-N faces: wall faces cut
  at openings with a solid poché, door swings, window lines, room names with
  areas, stairs, hatched columns, on AIA-style layers. The data layers
  (PLOMADA-*, the A-*-E-N record layers and SITIO) are grey and do not plot;
- ``layers``: the plan in the PLOMADA-* layer standard (docs/drawing-standard.md)
  with no XDATA at all, plus the same documentation: what any CAD can draw.
  write_plan reads it back and lists in ``lost`` every field the standard does
  not carry (ids always become entity handles), so an empty list proves the
  drawing holds the exact plan.

The data entities of records and drawing follow dxf.py's conventions, so
read_plan reads them back exactly:

- every wall is its axis, an LWPOLYLINE with real bulges and the wall's closed
  flag on PLOMADA-MURO-<thickness mm>, carrying a PLOMADA payload with the
  other wall fields;
- every 0.5 record is its entity on its PLOMADA-* layer with a PLOMADA payload
  of everything but the geometry; a slab's holes are polylines on
  PLOMADA-LOSA-HUECO that the slab names by handle;
- openings, rooms and stairs are ACADMCP_ARCH records, as AutoCAD MCP Pro
  writes them: an opening on the LINE between its jambs, a room on its name
  TEXT, a stair on its bottom riser;
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
from pathlib import Path
from typing import Any, Literal

import ezdxf
from ezdxf import units
from ezdxf.document import Drawing
from ezdxf.enums import MTextEntityAlignment
from ezdxf.layouts import Modelspace

from . import planar
from .drafting import DOC_LAYERS, draft
from .drafting import face_offsets as face_offsets
from .dxf import APP_ID, BLOCK_PREFIX, GEOMETRY_KINDS, OPEN_KINDS, PLOMADA_APP, SITE_LAYERS, SITE_PREFIX, read_plan
from .models import (
    DOOR_HEIGHT_MM,
    EXTRA_LISTS,
    WINDOW_HEIGHT_MM,
    WINDOW_SILL_MM,
    Opening,
    Plan,
    fmt,
    quote,
)
from .planar import Vertices, point_at

Style = Literal["records", "drawing", "layers"]
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
STANDARD_LAYERS = {"room": "PLOMADA-AMBIENTE", "furniture": "PLOMADA-MUEBLE"}
DATA_COLOUR = 8  # ACI grey of the data layers in the drawing and layers styles
TEXT_FONT = "arial.ttf"  # the Standard text style's font in the drawing and layers styles, as AutoCAD MCP Pro sets it
ROOM_TEXT_MM = 250.0  # letter height of the room names
MARKER_MM = 100.0  # half size of the cross that stands for a block's contents

# --- entities -------------------------------------------------------------------------------


def _xdata(entity: Any, app: str, payload: dict[str, Any]) -> None:
    text = json.dumps(payload)
    entity.set_xdata(app, [(1000, text[i : i + CHUNK]) for i in range(0, len(text), CHUNK)])


def _polyline(msp: Modelspace, vertices: Vertices, closed: bool, layer: str) -> Any:
    points = [(v[0], v[1], 0.0, 0.0, v[2] if len(v) > 2 else 0.0) for v in vertices]
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


def _walls(msp: Modelspace, plan: Plan, faces: bool) -> int:
    for wall in plan.walls:
        axis = _polyline(msp, wall.axis, wall.closed, f"PLOMADA-MURO-{round(wall.thickness)}")
        _xdata(axis, PLOMADA_APP, _without(wall.wire(), "axis", "closed"))
        if not faces:
            continue
        for d in face_offsets(wall):
            for p0, p1, b in planar.segments(wall.axis, wall.closed):
                if face := planar.offset_segment(p0, p1, b, d):
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


def _jambs(plan: Plan, o: Opening) -> tuple[tuple[float, float], tuple[float, float]]:
    host = next(w for w in plan.walls if w.id == o.wall)
    return point_at(host.axis, host.closed, o.offset), point_at(host.axis, host.closed, o.offset + o.width)


def _riser(s: Any) -> tuple[tuple[float, float], tuple[float, float]]:
    a = math.radians(s.direction_deg)
    nx, ny = -math.sin(a) * s.width / 2.0, math.cos(a) * s.width / 2.0
    return (s.start[0] - nx, s.start[1] - ny), (s.start[0] + nx, s.start[1] + ny)


def _arch_records(msp: Modelspace, plan: Plan) -> int:
    """Openings, rooms and stairs as ACADMCP_ARCH records, on the entities AutoCAD MCP Pro uses."""
    for o in plan.openings:
        a, b = _jambs(plan, o)
        _xdata(msp.add_line(a, b, dxfattribs={"layer": OPENING_LAYERS[o.opening_kind]}), APP_ID, o.wire())
    for r in plan.rooms:
        text = msp.add_text(r.name, height=ROOM_TEXT_MM, dxfattribs={"layer": ROOM_LAYER, "insert": r.at})
        _xdata(text, APP_ID, r.wire())
    for s in plan.stairs:
        _xdata(msp.add_line(*_riser(s), dxfattribs={"layer": STAIR_LAYER}), APP_ID, s.wire())
    return len(plan.openings) + len(plan.rooms) + len(plan.stairs)


def _furniture_and_site(doc: Drawing, msp: Modelspace, plan: Plan, tagged: bool = True) -> int:
    count = 0
    for f in plan.furniture:
        name = _block(doc, _block_name(BLOCK_PREFIX, f.item))
        ins = msp.add_blockref(name, f.at, dxfattribs={"rotation": f.rotation, "layer": FURNITURE_LAYER})
        _xdata(ins, PLOMADA_APP, {"v": 1, "kind": "furniture", "id": f.id, "item": f.item})
        count += 1
    count += _site(doc, msp, plan, tagged)
    return count


def _site(doc: Drawing, msp: Modelspace, plan: Plan, tagged: bool) -> int:
    count = 0
    for o in plan.site.objects:
        name = _block(doc, _block_name(SITE_PREFIX, o.item))
        ins = msp.add_blockref(name, o.at, dxfattribs={"rotation": o.rotation, "layer": SITE_OBJECT_LAYER})
        if tagged:
            _xdata(ins, PLOMADA_APP, {"v": 1, "kind": "site", "id": o.id, "item": o.item})
        count += 1
    for layer, key in SITE_LAYERS.items():
        for item in getattr(plan.site, key):
            fence = key == "fences"
            pl = _polyline(msp, item.points, item.closed if fence else True, layer)
            extra = {"height": item.height} if fence else ({"depth": item.depth} if item.depth is not None else {})
            if tagged:
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


# --- the layer standard ---------------------------------------------------------------------


def opening_layer(o: Opening) -> str:
    """The standard layer that carries an opening's kind, sill and height: PLOMADA-PUERTA[-<height>] or
    PLOMADA-VENTANA[-<sill>-<height>] (a window with one of the two gets the default of the other)."""
    if o.opening_kind == "door":
        return "PLOMADA-PUERTA" + (f"-{fmt(o.height)}" if o.height is not None else "")
    if o.sill is None and o.height is None:
        return "PLOMADA-VENTANA"
    sill = WINDOW_SILL_MM if o.sill is None else o.sill
    return f"PLOMADA-VENTANA-{fmt(sill)}-{fmt(WINDOW_HEIGHT_MM if o.height is None else o.height)}"


def _mtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def room_label(name: str, area: float) -> str:
    """The MTEXT of a room in the standard: its name and, when it has one, its area in m² to the mm²."""
    if area <= 0:
        return _mtext(name)
    return f"{_mtext(name)}\\P{f'{area / 1_000_000.0:.6f}'.rstrip('0').rstrip('.')} m²"


def _standard(doc: Drawing, msp: Modelspace, plan: Plan) -> tuple[int, list[str], set[tuple[str, str]]]:
    """The plan drawn in the layer standard with no XDATA: (entities written, the records left out with
    why, their (list, id) keys)."""
    count = 0
    out: list[str] = []
    skipped: set[tuple[str, str]] = set()
    for w in plan.walls:
        _polyline(msp, w.axis, w.closed, f"PLOMADA-MURO-{fmt(w.thickness)}")
    for o in plan.openings:
        msp.add_line(*_jambs(plan, o), dxfattribs={"layer": opening_layer(o)})
    for r in plan.rooms:
        text = msp.add_mtext(room_label(r.name, r.area), dxfattribs={"layer": STANDARD_LAYERS["room"]})
        text.set_location(r.at, attachment_point=MTextEntityAlignment.MIDDLE_CENTER)
        text.dxf.char_height = ROOM_TEXT_MM
    count += len(plan.walls) + len(plan.openings) + len(plan.rooms)
    for i, s in enumerate(plan.stairs):
        if s.stair_kind != "straight":
            out.append(f"stairs[{i}] {s.id} left out: an {s.stair_kind} stair travels only in XDATA")
            skipped.add(("stairs", s.id))
            continue
        a = math.radians(s.direction_deg)
        run = (s.risers - 1) * s.going
        end = (s.start[0] + math.cos(a) * run, s.start[1] + math.sin(a) * run)
        msp.add_line(s.start, end, dxfattribs={"layer": f"PLOMADA-ESCALERA-{fmt(s.width)}"})
        count += 1
    for f in plan.furniture:
        name = _block(doc, _block_name("", f.item))
        ins = msp.add_blockref(name, f.at, dxfattribs={"rotation": f.rotation, "layer": STANDARD_LAYERS["furniture"]})
        ins.add_attrib("ITEM", f.item)
        count += 1
    for key in EXTRA_LISTS:
        for i, record in enumerate(getattr(plan, key)):
            kind = record.KIND
            if kind == "ramp":
                out.append(f"ramps[{i}] {record.id} left out: a ramp's z_end travels only in XDATA")
                skipped.add(("ramps", record.id))
                continue
            layer = RECORD_LAYERS[kind]
            if kind == "column" and record.outline is None:
                msp.add_circle(record.at, record.diameter / 2.0, dxfattribs={"layer": layer})
            else:
                closed = record.closed if kind in OPEN_KINDS else True
                _polyline(msp, getattr(record, GEOMETRY_KINDS[kind][1]), closed, layer)
            count += 1
    count += _site(doc, msp, plan, tagged=False)
    if plan.storey is not None:
        out.append(
            f"storey {plan.storey.name} left out: the standard has no storey; build_from_autocad takes storey, "
            "storey_height and elevation"
        )
    if plan.site_on_ground:
        out.append("site_on_ground left out: it travels only in XDATA")
    return count, out, skipped


def _same(a: Any, b: Any) -> bool:
    """Equal JSON values, numbers within 1e-6 mm (the reader rounds what it derives to nm)."""
    number = (int, float)
    if isinstance(a, number) and isinstance(b, number) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-6)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return all(_same(a.get(k), b.get(k)) for k in set(a) | set(b))
    return a == b


def _show(value: Any) -> str:
    text = quote(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _opening_defaults(rec: dict[str, Any]) -> dict[str, Any]:
    door = rec["opening_kind"] == "door"
    sill = rec.get("sill")
    height = rec.get("height")
    return {
        **rec,
        "sill": (0.0 if door else WINDOW_SILL_MM) if sill is None else sill,
        "height": (DOOR_HEIGHT_MM if door else WINDOW_HEIGHT_MM) if height is None else height,
    }


def lost(plan: Plan, back: Plan, skipped: set[tuple[str, str]] | None = None) -> list[str]:
    """Every field of the plan that did not come back from the layer standard, record by record in order:
    '<list>[i] <id>: <field> <written> reads back as <read>'. Ids are not compared (the standard gives entity
    handles); an opening's host is compared through the wall it maps to; a null sill or height is its
    default. Records left out of the drawing (skipped) are not compared."""
    out: list[str] = []
    walls: dict[str, str] = {}
    pairs: list[tuple[str, list[Any], list[Any]]] = [
        (key, [r for r in getattr(plan, key) if (key, r.id) not in (skipped or ())], getattr(back, key))
        for key in ("walls", "openings", "rooms", "stairs", "furniture", *EXTRA_LISTS)
    ]
    pairs += [(f"site.{key}", getattr(plan.site, key), getattr(back.site, key)) for key in type(plan.site).model_fields]
    for key, mine, theirs in pairs:
        if len(mine) != len(theirs):
            out.append(f"{key}: {len(mine)} written, {len(theirs)} read back")
            continue
        for i, (a, b) in enumerate(zip(mine, theirs, strict=True)):
            wa = a.wire() if hasattr(a, "wire") else a.model_dump(mode="json")
            wb = b.wire() if hasattr(b, "wire") else b.model_dump(mode="json")
            if key == "walls":
                walls[b.id] = a.id
            if key == "openings":
                wb["wall"] = walls.get(wb["wall"], wb["wall"])
                wa, wb = _opening_defaults(wa), _opening_defaults(wb)
            diffs = [f for f in sorted(set(wa) | set(wb)) if f != "id" and not _same(wa.get(f), wb.get(f))]
            if diffs:
                said = ", ".join(f"{f} {_show(wa.get(f))} reads back as {_show(wb.get(f))}" for f in diffs)
                out.append(f"{key}[{i}] {a.id}: {said}")
    return out


# --- the document ---------------------------------------------------------------------------


DATA_LAYERS = (*OPENING_LAYERS.values(), ROOM_LAYER, STAIR_LAYER, FURNITURE_LAYER, SITE_OBJECT_LAYER)


def _is_data(layer: str) -> bool:
    """A layer the reader reads and a person does not need on paper: PLOMADA-* and the record layers."""
    return layer.upper().startswith("PLOMADA-") or layer in DATA_LAYERS


def _layer_table(doc: Drawing, names: list[str], style: Style) -> None:
    """Adds the layers in use; in the drawing and layers styles the documentation layers get their colour and
    lineweight, the data layers (_is_data) grey and not plotted, wall axes dash-dotted."""
    for name in names:
        if name in doc.layers:
            continue
        layer = doc.layers.add(name)
        if style == "records":
            continue
        if name in DOC_LAYERS:
            layer.color, layer.dxf.lineweight = DOC_LAYERS[name]
        elif _is_data(name):
            layer.color, layer.dxf.plot = DATA_COLOUR, 0
            if name.upper().startswith(("PLOMADA-MURO-", "PLOMADA-WALL-")) and "CENTER" in doc.linetypes:
                layer.dxf.linetype = "CENTER"


def build_doc(plan: Plan, style: Style = "records") -> tuple[Drawing, int, list[str], set[tuple[str, str]]]:
    """The DXF document of the plan in a style, unsaved: (document, data records written, records left out of
    a layers drawing with why, their (list, id) keys)."""
    doc = ezdxf.new("R2013") if style == "records" else ezdxf.new("R2013", setup=["linetypes"])
    doc.units = units.MM
    if style != "records":
        doc.styles.get("Standard").dxf.font = TEXT_FONT  # a TrueType font has the ² of m²
    msp = doc.modelspace()
    out: list[str] = []
    skipped: set[tuple[str, str]] = set()
    if style == "layers":
        count, out, skipped = _standard(doc, msp, plan)
    else:
        for app in (APP_ID, PLOMADA_APP):
            doc.appids.new(app)
        count = _walls(msp, plan, faces=style == "records") + _extra_records(msp, plan) + _arch_records(msp, plan)
        count += _furniture_and_site(doc, msp, plan) + _storey(msp, plan)
    if style != "records":
        draft(msp, plan)
    _layer_table(doc, sorted({e.dxf.layer for e in msp}), style)
    return doc, count, out, skipped


def write_plan(plan: Plan, path: str | Path, style: Style = "records") -> dict[str, Any]:
    """Writes the plan as a millimetre DXF in a style; returns {path, entities, layers, records, style} and,
    for the layers style, lost: what read_plan does not get back (empty when the drawing holds the plan)."""
    p = Path(path)
    doc, count, left_out, skipped = build_doc(plan, style)
    msp = doc.modelspace()
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(p)
    out: dict[str, Any] = {
        "path": str(p),
        "entities": len(msp),
        "layers": sorted({e.dxf.layer for e in msp}),
        "records": count,
    }
    if style != "records":
        out["style"] = style
    if style == "layers":
        back = read_plan(p, storey_height=plan.storey.height if plan.storey else None).plan
        out["lost"] = left_out + lost(plan, back, skipped)
    return out
