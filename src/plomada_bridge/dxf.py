"""Reads the plan records AutoCAD MCP Pro and Plomada store in a DXF.

Every architectural object of AutoCAD MCP Pro travels as XDATA under the
registered application ACADMCP_ARCH on one entity: a 1001 group with the app
name, then 1000 strings that, concatenated in order, form one JSON object with
"v": 1 and a "kind" of wall, opening, stair or room. A record with any other
version is refused by name; nothing is guessed. The extension never reads DXF:
the bridge sends it the normalized plan.

Plomada 0.5 records (docs/records-0.5.md) travel the same way under the
application PLOMADA, but their geometry is the entity itself and the payload
carries only the parameters:

- an LWPOLYLINE is a wall axis (its closed flag is the wall's), a slab, planter
  or pool outline, a railing or curtain wall path (closed flag too), a column
  outline or a ramp axis, with its bulges; a SPLINE or ELLIPSE is flattened
  within FLATTEN_MM into straight vertices (nothing else is tessellated here);
- a CIRCLE is a round column: at its centre, diameter twice its radius;
- a slab names its holes by entity handle, ``"holes": ["2F", "30"]``: each is
  a polyline (any layer, write_plan_dxf uses PLOMADA-LOSA-HUECO) read as one
  hole ring and never as a record of its own;
- ``"kind": "furniture"`` on an ARCH_<ITEM> insert and ``"kind": "site"`` on a
  SITIO_<NAME> insert or SITIO-* polyline carry what the drawing cannot hold
  (id, item, pool depth, fence height), so a written plan reads back exactly;
- ``"kind": "storey"`` on any entity (write_plan_dxf uses a POINT) carries the
  storey settings ``{name, height, elevation_mode?, elevation?}`` plus the plan
  flag ``site_on_ground`` when true.

Without XDATA, entities on the PLOMADA-* layers become records with default
parameters and the entity handle as id: PLOMADA-LOSA (slab), PLOMADA-BARANDA
(railing), PLOMADA-JARDINERA (planter), PLOMADA-MURO-<thickness mm> (wall,
centre justified), PLOMADA-MURO-CORTINA (curtain wall), PLOMADA-PILETA (pool),
PLOMADA-COLUMNA (a CIRCLE is a round column, a polyline an outline column). A
ramp needs z_end, which only XDATA carries: a bare polyline on PLOMADA-RAMPA is
skipped with a warning.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.document import Drawing

from .models import RECORD_VERSION, Plan, parse_plan

APP_ID = "ACADMCP_ARCH"
PLOMADA_APP = "PLOMADA"
BLOCK_PREFIX = "ARCH_"  # AutoCAD MCP Pro catalogue blocks: ARCH_<NAME>
SITE_PREFIX = "SITIO_"  # site blocks: SITIO_<NAME>, origin at the object's centre
SITE_LAYERS = {"SITIO-PAVIMENTO": "paving", "SITIO-DECK": "decks", "SITIO-PILETA": "pools", "SITIO-CERCO": "fences"}
KINDS = ("wall", "opening", "stair", "room")

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
ANNOTATION_KINDS = ("furniture", "site", "storey")  # payloads on entities the other readers place
PLOMADA_KINDS = (*GEOMETRY_KINDS, *ANNOTATION_KINDS)
OPEN_KINDS = ("wall", "railing", "curtain_wall")  # the polyline's closed flag is the record's
LAYER_KINDS = {
    "PLOMADA-LOSA": "slab",
    "PLOMADA-BARANDA": "railing",
    "PLOMADA-JARDINERA": "planter",
    "PLOMADA-MURO-CORTINA": "curtain_wall",
    "PLOMADA-PILETA": "pool",
    "PLOMADA-RAMPA": "ramp",
    "PLOMADA-COLUMNA": "column",
}
WALL_LAYER = re.compile(r"^PLOMADA-MURO-(\d+(?:\.\d+)?)$")
CURVES = ("LWPOLYLINE", "SPLINE", "ELLIPSE")
FLATTEN_MM = 5.0  # largest sagitta of a flattened SPLINE or ELLIPSE


class DxfPlanError(ValueError):
    """The DXF cannot be turned into a plan; the message says why."""


@dataclass
class DxfPlan:
    plan: Plan
    path: Path
    records: int
    warnings: list[str] = field(default_factory=list)


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


def _plomada(entity: Any) -> dict[str, Any] | None:
    """The PLOMADA payload on an entity, or None."""
    if not hasattr(entity, "has_xdata") or not entity.has_xdata(PLOMADA_APP):
        return None
    return _payload(entity, PLOMADA_APP, PLOMADA_KINDS)


def _params(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k not in ("v", "kind")}


def _annotation(entity: Any, kind: str) -> dict[str, Any]:
    """The fields a PLOMADA payload of this kind adds to a record read from the entity ({} without one)."""
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
        if not hasattr(entity, "has_xdata") or not entity.has_xdata(APP_ID):
            continue
        payload = _payload(entity)
        key = (payload["kind"], str(payload.get("id")))
        if key in found and found[key] != payload:
            raise DxfPlanError(f"two different {payload['kind']} records share the id {key[1]!r}")
        found[key] = payload
    return list(found.values())


# --- 0.5 records: geometry from the entity ---------------------------------------------


def vertices(entity: Any) -> tuple[list[list[float]], bool]:
    """The entity as [x, y] / [x, y, bulge] vertices and its closed flag: an LWPOLYLINE with its bulges
    (zeros dropped), a SPLINE or ELLIPSE flattened to straight vertices within FLATTEN_MM."""
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        pts = [[float(x), float(y), float(b)] if b else [float(x), float(y)] for x, y, b in entity.get_points("xyb")]
        return pts, bool(entity.closed)
    if kind == "SPLINE":
        closed = bool(entity.closed)
    elif kind == "ELLIPSE":
        closed = math.isclose(abs(entity.dxf.end_param - entity.dxf.start_param), math.tau, abs_tol=1e-9)
    else:
        raise DxfPlanError(f"entity {entity.dxf.handle} is a {kind}; polylines are {', '.join(CURVES)}")
    pts = [[float(p.x), float(p.y)] for p in entity.flattening(FLATTEN_MM)]
    if len(pts) > 2 and math.dist(pts[0], pts[-1]) <= 1e-6:
        pts.pop()
        closed = True
    return pts, closed


def _hole(doc: Drawing, handle: Any, slab: str) -> list[list[float]]:
    entity = doc.entitydb.get(handle) if isinstance(handle, str) else None
    if entity is None or entity.dxftype() not in CURVES:
        raise DxfPlanError(f"slab {slab} names hole {handle!r}, which is not a polyline in the drawing")
    return vertices(entity)[0]


def _geometry_record(doc: Drawing, entity: Any, kind: str, params: dict[str, Any]) -> dict[str, Any]:
    """A 0.5 record: its parameters, its id (default the entity handle) and the entity's geometry."""
    rec: dict[str, Any] = {"v": RECORD_VERSION, "kind": kind, **params}
    rec.setdefault("id", entity.dxf.handle)
    if entity.dxftype() == "CIRCLE":
        if kind != "column":
            raise DxfPlanError(f"{kind} {rec['id']} is drawn as a CIRCLE; only a column is")
        rec["at"] = [float(entity.dxf.center.x), float(entity.dxf.center.y)]
        rec["diameter"] = 2.0 * float(entity.dxf.radius)
        return rec
    pts, closed = vertices(entity)
    rec[GEOMETRY_KINDS[kind][1]] = pts
    if kind in OPEN_KINDS:
        rec["closed"] = closed
    if kind == "slab":
        rec["holes"] = [_hole(doc, h, rec["id"]) for h in params.get("holes", [])]
    return rec


def _layer_kind(entity: Any) -> tuple[str, dict[str, Any]] | None:
    """The record kind and default parameters a PLOMADA-* layer gives an entity without XDATA."""
    layer = entity.dxf.layer.upper()
    if entity.dxftype() == "CIRCLE":
        return ("column", {}) if layer == "PLOMADA-COLUMNA" else None
    if entity.dxftype() not in CURVES:
        return None
    if m := WALL_LAYER.match(layer):
        return "wall", {"thickness": float(m.group(1)), "justification": "center"}
    kind = LAYER_KINDS.get(layer)
    return (kind, {}) if kind else None


def plomada_records(doc: Drawing) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None, list[str]]:
    """The 0.5 records of model space in drawing order, by plan list; the storey payload (or None);
    warnings for the entities skipped."""
    out: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in GEOMETRY_KINDS.values()}
    storey: dict[str, Any] | None = None
    warnings: list[str] = []
    tagged = [(e, _plomada(e)) for e in doc.modelspace()]
    holes = {h for _, p in tagged if p and p["kind"] == "slab" for h in p.get("holes", []) if isinstance(h, str)}
    for entity, payload in tagged:
        if entity.dxf.handle in holes:
            continue
        if payload is not None:
            kind = payload["kind"]
            if kind == "storey":
                if storey is not None:
                    raise DxfPlanError(f"two {PLOMADA_APP} storey records; a DXF holds one storey")
                storey = _params(payload)
            elif kind in GEOMETRY_KINDS:
                out[GEOMETRY_KINDS[kind][0]].append(_geometry_record(doc, entity, kind, _params(payload)))
            continue
        found = _layer_kind(entity)
        if found is None:
            continue
        kind, params = found
        if kind == "ramp":
            warnings.append(f"ramp {entity.dxf.handle} on {entity.dxf.layer} skipped: its z_end travels only in XDATA")
            continue
        out[GEOMETRY_KINDS[kind][0]].append(_geometry_record(doc, entity, kind, params))
    return out, storey, warnings


def _has_plomada_layer(doc: Drawing) -> bool:
    return any(e.dxf.layer.upper().startswith("PLOMADA-") for e in doc.modelspace())


# --- furniture and site -----------------------------------------------------------------


def furniture(doc: Drawing) -> tuple[list[dict[str, Any]], list[str]]:
    """The AutoCAD MCP Pro catalogue blocks (ARCH_<NAME> inserts in model space) as furniture
    records: the item from the block's ITEM attribute (else its name), the insert point (the
    back-left corner) and rotation. A PLOMADA furniture payload on the insert supplies the id and
    item. A scaled or mirrored insert is skipped with a warning."""
    out: list[dict[str, Any]] = []
    warnings: list[str] = []
    for ins in doc.modelspace().query("INSERT"):
        name = ins.dxf.name
        if not name.upper().startswith(BLOCK_PREFIX):
            continue
        item = next((a.dxf.text for a in ins.attribs if a.dxf.tag.upper() == "ITEM"), "") or name[len(BLOCK_PREFIX) :]
        scale = (ins.dxf.get("xscale", 1.0), ins.dxf.get("yscale", 1.0))
        if any(abs(s - 1.0) > 1e-6 for s in scale):
            warnings.append(f"furniture {ins.dxf.handle} ({name}) skipped: scaled or mirrored {scale}")
            continue
        at = ins.dxf.insert
        out.append(
            {
                "v": 1,
                "kind": "furniture",
                "id": ins.dxf.handle,
                "item": item.strip().lower(),
                "at": [float(at.x), float(at.y)],
                "rotation": float(ins.dxf.get("rotation", 0.0)),
                **_annotation(ins, "furniture"),
            }
        )
    return out, warnings


def site(doc: Drawing) -> dict[str, list[dict[str, Any]]]:
    """The site: SITIO_<NAME> inserts (a library component centred on the insert point) and
    the lightweight polylines, bulges included, on the SITIO-PAVIMENTO, -DECK, -PILETA (closed)
    and -CERCO layers. A PLOMADA site payload supplies the id, item, depth or height."""
    out: dict[str, list[dict[str, Any]]] = {"objects": [], "paving": [], "decks": [], "pools": [], "fences": []}
    msp = doc.modelspace()
    for ins in msp.query("INSERT"):
        name = ins.dxf.name
        if name.upper().startswith(SITE_PREFIX):
            at = ins.dxf.insert
            out["objects"].append(
                {
                    "id": ins.dxf.handle,
                    "item": name[len(SITE_PREFIX) :].upper(),
                    "at": [float(at.x), float(at.y)],
                    "rotation": float(ins.dxf.get("rotation", 0.0)),
                    **_annotation(ins, "site"),
                }
            )
    for pl in msp.query("LWPOLYLINE"):
        key = SITE_LAYERS.get(pl.dxf.layer.upper())
        if key is None:
            continue
        pts, closed = vertices(pl)
        rec: dict[str, Any] = {"id": pl.dxf.handle, "points": pts}
        if key == "fences":
            rec["closed"] = closed
        out[key].append({**rec, **_annotation(pl, "site")})
    return out


def read_plan(path: str | Path, storey_height: float | None = None) -> DxfPlan:
    """Parses the DXF with ezdxf and validates the records into a Plan. storey_height, when given,
    overrides the height of a PLOMADA storey record."""
    p = Path(path)
    if not p.is_file():
        raise DxfPlanError(f"dxf_path {str(p)!r} is not a file")
    try:
        doc = ezdxf.readfile(p)
    except (OSError, ezdxf.DXFStructureError) as exc:
        raise DxfPlanError(f"cannot read {p.name} as DXF: {exc}") from None
    recs = records(doc)
    extra, storey, warnings = plomada_records(doc)
    found = sum(len(v) for v in extra.values())
    if not recs and not found and storey is None and not _has_plomada_layer(doc):
        raise DxfPlanError(
            f"{p.name} holds no {APP_ID} records; export it from AutoCAD MCP Pro, whose arch_* tools write them, "
            f"or write {PLOMADA_APP} records with write_plan_dxf"
        )
    pieces, furniture_warnings = furniture(doc)
    raw: dict[str, Any] = {
        "walls": [r for r in recs if r["kind"] == "wall"] + extra.pop("walls"),
        "openings": [r for r in recs if r["kind"] == "opening"],
        "rooms": [r for r in recs if r["kind"] == "room"],
        "stairs": [r for r in recs if r["kind"] == "stair"],
        "furniture": pieces,
        "site": site(doc),
        **extra,
    }
    if storey is not None:
        raw["site_on_ground"] = storey.pop("site_on_ground", False)
        storey = storey or None  # a payload with the flag alone sets no storey
    if storey_height is not None:
        storey = {**(storey or {}), "height": storey_height}
    if storey is not None:
        raw["storey"] = storey
    try:
        plan = parse_plan(raw)
    except ValueError as exc:
        raise DxfPlanError(f"{p.name}: {exc}") from None
    return DxfPlan(plan=plan, path=p, records=len(recs) + found, warnings=furniture_warnings + warnings)
