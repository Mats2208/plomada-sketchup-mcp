"""Pydantic models that mirror the AutoCAD MCP Pro plan records (ACADMCP_ARCH, v 1)
and the Plomada 0.5 records (docs/records-0.5.md).

All lengths are millimetres in world coordinates. Validation errors become
one line that starts with the field path, e.g.
``walls[2].thickness must be greater than 0, got -200`` - the same wording the
extension uses for its own checks. A refusal that names its object itself
(``segment 1 of wall P1 sweeps 200 degrees; ...``) is printed without a path,
as the extension does.

Polyline vertices are ``[x, y]`` or ``[x, y, bulge]`` with the DXF LWPOLYLINE
convention: the bulge on vertex i bends the segment to vertex i + 1 (on a
closed ring the last one bends the closing segment), bulge = tan(sweep / 4),
positive counter-clockwise. Zero bulges are dropped, so a plan without arcs
serialises exactly as in 0.4. Python never tessellates an arc.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError

RECORD_VERSION = 1
MAX_MM = 1_000_000.0  # 1 km: anything larger is a units mistake
MAX_BULGE = 1.0  # a semicircle
ELEVATION_MIN_MM = -10_000.0  # lowest explicit storey elevation
ELEVATION_MAX_MM = 60_000.0  # highest explicit storey elevation
SLAB_TOP_OFFSET_MAX_MM = 2_000.0  # largest step of a slab top off the storey floor
RAMP_MAX_SLOPE = 0.20  # rise over run
DOOR_HEIGHT_MM = 2100.0  # the extension's height for a door whose record has none
WINDOW_HEIGHT_MM = 1200.0  # the extension's height for a window whose record has none

Mm = Annotated[float, Field(allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, le=MAX_MM, description="mm")]
NonNegative = Annotated[float, Field(ge=0, le=MAX_MM, description="mm")]
Point = Annotated[tuple[Mm, Mm], Field(description="[x, y] in mm")]
Bulge = Annotated[float, Field(allow_inf_nan=False, description="tan(sweep / 4); positive counter-clockwise")]
Vertex = Annotated[
    tuple[Mm, Mm] | tuple[Mm, Mm, Bulge],
    Field(description="[x, y] or [x, y, bulge] in mm; the bulge bends the segment to the next vertex"),
]


# --- arcs (exact, never tessellated) --------------------------------------------------


def bulge_of(vertex: Sequence[float]) -> float:
    return float(vertex[2]) if len(vertex) > 2 else 0.0


def arc_radius(chord: float, bulge: float) -> float:
    """Radius of the arc over a chord with this bulge: chord (1 + b^2) / (4 |b|)."""
    return chord * (1.0 + bulge * bulge) / (4.0 * abs(bulge))


def segment_length(p0: Sequence[float], p1: Sequence[float], bulge: float) -> float:
    """Arc length from p0 to p1: the chord when straight, r |theta| on an arc (theta = 4 atan(b))."""
    chord = math.dist(p0[:2], p1[:2])
    if abs(bulge) <= 1e-12:
        return chord
    return arc_radius(chord, bulge) * abs(4.0 * math.atan(bulge))


def polyline_length(vertices: Sequence[Sequence[float]], closed: bool = False) -> float:
    count = len(vertices) if closed else len(vertices) - 1
    return sum(
        segment_length(vertices[i], vertices[(i + 1) % len(vertices)], bulge_of(vertices[i])) for i in range(count)
    )


def ring_area(vertices: Sequence[Sequence[float]]) -> float:
    """Signed area of a closed ring, counter-clockwise positive: the shoelace area of the vertices plus
    the circular segment of every arc (r^2 / 2 (theta - sin theta), on the side its bulge sends it)."""
    area = 0.0
    for i, v in enumerate(vertices):
        w = vertices[(i + 1) % len(vertices)]
        area += (v[0] * w[1] - w[0] * v[1]) / 2.0
        b = bulge_of(v)
        if abs(b) > 1e-12:
            theta = abs(4.0 * math.atan(b))
            r = arc_radius(math.dist(v[:2], w[:2]), b)
            area += math.copysign(r * r / 2.0 * (theta - math.sin(theta)), b)
    return area


# --- refusals ---------------------------------------------------------------------------


def fmt(value: float) -> str:
    """A number as the extension's Plan.fmt prints it: whole numbers without a decimal point."""
    return str(round(value)) if value == round(value) else repr(round(value, 6))


def fmt3(value: float) -> str:
    """A number as the extension's Geometry.fmt prints it: at most 3 decimals, no trailing zeros."""
    if abs(value - round(value)) < 1e-6:
        return str(round(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def quote(value: Any) -> str:
    """A JSON value as the extension quotes it in a refusal (Ruby's inspect)."""
    return "nil" if value is None else json.dumps(value, ensure_ascii=False)


def _named(message: str) -> PydanticCustomError:
    """A refusal whose sentence names its object itself; describe_error prints it without a path."""
    return PydanticCustomError("named", "{message}", {"message": message})


def _refuse(field: str, message: str) -> PydanticCustomError:
    """A refusal about one field of the record being validated, printed as '<record path>.<field> <message>'."""
    return PydanticCustomError("field", "{field} {message}", {"field": field, "message": message})


def normalize_vertices(vertices: list[Any], owner: str, closed: bool) -> list[Any]:
    """Drops zero bulges and the bulge of an open polyline's last vertex (no segment follows it), and
    refuses a bulge above a semicircle naming the segment, e.g. 'segment 1 of wall P1 sweeps ...'."""
    out = [tuple(v[:2]) if len(v) > 2 and abs(v[2]) <= 1e-12 else v for v in vertices]
    if out and not closed:
        out[-1] = tuple(out[-1][:2])
    for i, v in enumerate(out):
        if abs(bulge_of(v)) > MAX_BULGE + 1e-9:
            deg = abs(4.0 * math.atan(v[2])) * 180.0 / math.pi
            raise _named(f"segment {i} of {owner} sweeps {fmt3(round(deg, 3))} degrees; split arcs above 180 degrees")
    return out


def normalize_ring(vertices: list[Any], field: str, owner: str) -> list[Any]:
    """A closed ring of at least 3 vertices (or 2 joined by arcs) enclosing some area."""
    out = normalize_vertices(vertices, owner, True)
    if len(out) < 3 and not any(len(v) > 2 for v in out):
        raise _refuse(field, f"must be a list of at least 3 [x, y] or [x, y, bulge] points, got {len(out)}")
    if abs(ring_area(out)) < 1.0:
        raise _refuse(field, "encloses no area")
    return out


def _path(vertices: list[Any], owner: str, closed: bool) -> list[Any]:
    """An open or closed polyline (railing, curtain wall); a closed one needs 3 vertices."""
    out = normalize_vertices(vertices, owner, closed)
    if closed and len(out) < 3:
        raise _refuse("path", f"must hold at least 3 points when closed, got {len(out)}")
    return out


# --- records ------------------------------------------------------------------------------


class _Record(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    KIND: ClassVar[str] = ""

    v: int | None = Field(default=None, description="record version; only 1 is read")

    @field_validator("v")
    @classmethod
    def _version(cls, value: int | None) -> int | None:
        if value is not None and value != RECORD_VERSION:
            raise ValueError(f"is {value}; Plomada reads record version {RECORD_VERSION} only")
        return value

    def wire(self) -> dict[str, Any]:
        """The record as build_plan takes it: v and kind first, unset optional fields left out."""
        return {
            "v": RECORD_VERSION,
            "kind": self.KIND,
            **self.model_dump(mode="json", exclude={"v"}, exclude_none=True),
        }


class Wall(_Record):
    KIND: ClassVar[str] = "wall"

    id: Annotated[str, Field(min_length=1, description="wall id, unique in the plan")]
    axis: Annotated[list[Vertex], Field(min_length=2, description="axis polyline, mm; segments may be arcs")]
    thickness: Annotated[float, Field(gt=0, le=MAX_MM, description="wall thickness, mm")]
    justification: Literal["center", "left", "right"] = Field(
        default="center", description="side of the axis the wall lies on, looking from its first point"
    )
    material: str = Field(default="brick", min_length=1)
    closed: bool = False
    height: Annotated[float | None, Field(gt=0, le=MAX_MM, description="wall height, mm")] = None

    @model_validator(mode="after")
    def _shape(self) -> Wall:
        self.axis = normalize_vertices(self.axis, f"wall {self.id}", self.closed)
        need = 3 if self.closed else 2
        if self.closed and len(self.axis) == 2 and any(len(v) > 2 for v in self.axis):
            need = 2  # two arcs close a ring
        if len(self.axis) < need:
            kind = "closed" if self.closed else "open"
            raise _refuse("axis", f"must hold at least {need} points for a {kind} wall, got {len(self.axis)}")
        for i in range(1, len(self.axis)):
            if math.dist(self.axis[i - 1][:2], self.axis[i][:2]) <= 1e-9:
                raise _refuse(f"axis[{i}]", f"repeats axis[{i - 1}]; a zero-length segment has no direction")
        if self.closed and math.dist(self.axis[-1][:2], self.axis[0][:2]) <= 1e-9:
            raise _refuse(f"axis[{len(self.axis) - 1}]", "repeats axis[0]; closed already closes the loop")
        return self


class Opening(_Record):
    KIND: ClassVar[str] = "opening"

    id: Annotated[str, Field(min_length=1)]
    wall: Annotated[str, Field(min_length=1, description="id of the host wall")]
    opening_kind: Literal["door", "window"]
    offset: Annotated[float, Field(ge=0, le=MAX_MM, description="mm along the host axis to the near jamb")]
    width: Annotated[float, Field(gt=0, le=MAX_MM, description="mm")]
    sill: Annotated[float | None, Field(ge=0, le=MAX_MM, description="mm; null is 0 for doors")] = None
    height: Annotated[float | None, Field(gt=0, le=MAX_MM, description="mm")] = None
    swing: Literal["in", "out"] = "in"
    hand: Literal["left", "right"] = "left"
    tag: str | None = None
    head: Literal["flat", "arch"] = Field(default="flat", description="arch: a round head (straight walls only)")
    arch_rise: Annotated[
        float | None, Field(gt=0, le=MAX_MM, description="mm, at most width / 2; default width / 2")
    ] = None
    panel_width: Annotated[
        float | None, Field(gt=0, le=MAX_MM, description="widest flat panel on an arc wall, mm; default 1200")
    ] = None

    @model_validator(mode="after")
    def _arch(self) -> Opening:
        if self.head == "flat":
            self.arch_rise = None
            return self
        half = self.width / 2.0
        rise = half if self.arch_rise is None else self.arch_rise
        if rise > half + 1e-9:
            raise _refuse("arch_rise", f"must be at most half the width ({fmt(half)}), got {fmt(rise)}")
        height = self.height or (DOOR_HEIGHT_MM if self.opening_kind == "door" else WINDOW_HEIGHT_MM)
        if rise > height + 1e-9:
            raise _refuse("arch_rise", f"{fmt(rise)} is taller than the opening ({fmt(height)})")
        self.arch_rise = rise
        return self

    def wire(self) -> dict[str, Any]:
        """The 0.4 keys exactly as before (nulls kept); head, arch_rise and panel_width only when used."""
        out = {
            "v": RECORD_VERSION,
            "kind": self.KIND,
            **self.model_dump(mode="json", exclude={"v", "head", "arch_rise", "panel_width"}),
        }
        if self.head == "arch":
            out["head"], out["arch_rise"] = "arch", self.arch_rise
        if self.panel_width is not None:
            out["panel_width"] = self.panel_width
        return out


class Room(_Record):
    KIND: ClassVar[str] = "room"

    id: Annotated[str, Field(min_length=1)]
    name: Annotated[str, Field(min_length=1)]
    number: str | None = None
    at: Point
    area: Annotated[float, Field(ge=0, description="mm2")] = 0.0

    @field_validator("number", mode="before")
    @classmethod
    def _number_text(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    def wire(self) -> dict[str, Any]:
        return {"v": RECORD_VERSION, "kind": self.KIND, **self.model_dump(mode="json", exclude={"v"})}


class Stair(_Record):
    """A stair from this floor up to the next (AutoCAD MCP Pro): start is the midpoint of the
    bottom riser, direction_deg turns the stair frame, an L or U turns ``turn`` on a square landing."""

    KIND: ClassVar[str] = "stair"

    id: Annotated[str, Field(min_length=1)]
    start: Point
    direction_deg: Annotated[float, Field(allow_inf_nan=False, description="degrees, 0 = +x")] = 0.0
    width: Annotated[float, Field(gt=0, le=MAX_MM, description="flight width, mm")]
    risers: Annotated[int, Field(ge=2, le=200, description="every rise from this floor to the next")]
    riser_height: Annotated[float, Field(gt=0, le=1_000, description="mm")]
    going: Annotated[float, Field(gt=0, le=2_000, description="tread depth, mm")]
    stair_kind: Literal["straight", "l", "u"] = "straight"
    turn: Literal["left", "right"] = "left"


class Furniture(_Record):
    """A catalogue block placed in the plan (AutoCAD MCP Pro arch_catalogue_insert): at is the
    back-left corner, rotation degrees CCW about it."""

    KIND: ClassVar[str] = "furniture"

    id: Annotated[str, Field(min_length=1, description="the INSERT handle")]
    item: Annotated[str, Field(min_length=1, description="catalogue name, e.g. double_bed, wc")]
    at: Point
    rotation: Annotated[float, Field(allow_inf_nan=False, description="degrees CCW")] = 0.0


# --- 0.5 records ------------------------------------------------------------------------


class EdgeBand(BaseModel):
    """A downstand band along a slab's outer edge (a balcony fascia)."""

    model_config = ConfigDict(extra="ignore")
    height: Annotated[float, Field(gt=0, le=MAX_MM, description="mm below the slab top")]
    depth: Annotated[float, Field(gt=0, le=MAX_MM, description="mm, band thickness")]


class Slab(_Record):
    """A floor slab of any outline, with holes; with one or more slab records the storey's
    automatic slab is not built."""

    KIND: ClassVar[str] = "slab"

    id: Annotated[str, Field(min_length=1)]
    outline: Annotated[list[Vertex], Field(min_length=2, description="closed ring, mm")]
    holes: list[Annotated[list[Vertex], Field(min_length=2)]] = Field(default_factory=list)
    thickness: Positive = 150.0
    top_offset: Annotated[Mm, Field(description="mm of the slab top above the storey floor, within +-2000")] = 0.0
    material: str = Field(default="MAT_piso_porcelanato", min_length=1)
    edge_band: EdgeBand | None = None

    @field_validator("top_offset")
    @classmethod
    def _top_offset(cls, value: float) -> float:
        if abs(value) > SLAB_TOP_OFFSET_MAX_MM:
            raise ValueError(f"must be within +-{fmt(SLAB_TOP_OFFSET_MAX_MM)} mm, got {fmt(value)}")
        return value

    @model_validator(mode="after")
    def _rings(self) -> Slab:
        self.outline = normalize_ring(self.outline, "outline", f"slab {self.id}")
        self.holes = [normalize_ring(h, f"holes[{i}]", f"slab {self.id} hole {i}") for i, h in enumerate(self.holes)]
        return self


class Railing(_Record):
    """A railing along a path; on names the slab it stands on (null: the storey floor)."""

    KIND: ClassVar[str] = "railing"

    id: Annotated[str, Field(min_length=1)]
    path: Annotated[list[Vertex], Field(min_length=2, description="polyline, mm")]
    closed: bool = False
    style: Literal["glass", "bars", "solid"] = "glass"
    height: Positive = 1100.0
    inset: NonNegative = 50.0
    on: Annotated[str | None, Field(min_length=1, description="slab id")] = None
    rail: bool = Field(default=False, description="glass only: a 50 x 50 top rail")

    @model_validator(mode="after")
    def _shape(self) -> Railing:
        self.path = _path(self.path, f"railing {self.id}", self.closed)
        return self


class Plants(BaseModel):
    model_config = ConfigDict(extra="ignore")
    keys: list[Annotated[str, Field(min_length=1)]] = Field(description="library keys, e.g. hedge, ARBUSTO")
    spacing: Positive = 600.0
    seed: int = 0

    @field_validator("keys", mode="before")
    @classmethod
    def _keys(cls, value: Any) -> Any:
        if not isinstance(value, list) or not value:
            raise ValueError(f"must be a list of at least one library key, got {quote(value)}")
        return value

    @field_validator("seed", mode="before")
    @classmethod
    def _seed(cls, value: Any) -> Any:
        if value is None:
            return 0
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"must be a whole number, got {quote(value)}")
        return value


class Planter(_Record):
    """A planter: a curb around soil, planted from the library or soil only (plants null)."""

    KIND: ClassVar[str] = "planter"

    id: Annotated[str, Field(min_length=1)]
    outline: Annotated[list[Vertex], Field(min_length=2, description="closed ring, mm")]
    curb_thickness: Positive = 100.0
    height: Annotated[Positive, Field(description="curb top above the planter's base, mm")] = 450.0
    soil_level: Annotated[
        float | None, Field(ge=0, le=MAX_MM, description="soil top above the base, mm; default min(400, height)")
    ] = None
    on: Annotated[str | None, Field(min_length=1, description="slab id")] = None
    plants: Plants | None = None

    @model_validator(mode="after")
    def _shape(self) -> Planter:
        self.outline = normalize_ring(self.outline, "outline", f"planter {self.id}")
        if self.soil_level is None:
            self.soil_level = min(400.0, self.height)
        if self.soil_level > self.height + 1e-9:
            raise _refuse("soil_level", f"{fmt(self.soil_level)} is above the curb height {fmt(self.height)}")
        return self


class Column(_Record):
    """A column: round (at + diameter) or any outline; height null is the storey height."""

    KIND: ClassVar[str] = "column"

    id: Annotated[str, Field(min_length=1)]
    at: Point | None = None
    diameter: Annotated[float | None, Field(gt=0, le=MAX_MM, description="mm")] = None
    outline: Annotated[list[Vertex] | None, Field(min_length=2, description="closed ring, mm")] = None
    height: Annotated[float | None, Field(gt=0, le=MAX_MM, description="mm")] = None
    material: str = Field(default="MAT_hormigon", min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> Column:
        if self.outline is not None:
            if self.at is not None or self.diameter is not None:
                raise ValueError("takes either at and diameter (round) or outline, not both")
            self.outline = normalize_ring(self.outline, "outline", f"column {self.id}")
        elif self.at is None:
            raise _refuse("at", "is required")
        elif self.diameter is None:
            raise _refuse("diameter", "is required")
        return self


class CurtainWall(_Record):
    """A glazed curtain wall along a path; head null is the storey height."""

    KIND: ClassVar[str] = "curtain_wall"

    id: Annotated[str, Field(min_length=1)]
    path: Annotated[list[Vertex], Field(min_length=2, description="polyline, mm")]
    closed: bool = False
    sill: NonNegative = 0.0
    head: Annotated[float | None, Field(gt=0, le=MAX_MM, description="mm above the floor")] = None
    mullion_spacing: Positive = 1200.0
    transoms: list[Positive] = Field(default_factory=list, description="transom heights above the floor, mm")
    profile: tuple[Positive, Positive] = Field(default=(50.0, 100.0), description="[face width, depth] in mm")
    glass: Positive = 10.0

    @model_validator(mode="after")
    def _shape(self) -> CurtainWall:
        self.path = _path(self.path, f"curtain wall {self.id}", self.closed)
        if self.head is not None and self.head <= self.sill:
            raise _refuse("head", f"{fmt(self.head)} must be above the sill {fmt(self.sill)}")
        self.transoms = sorted(self.transoms)
        return self


class Pool(_Record):
    """A pool: raised auto is recessed on the ground storey and a raised basin on any other."""

    KIND: ClassVar[str] = "pool"

    id: Annotated[str, Field(min_length=1)]
    outline: Annotated[list[Vertex], Field(min_length=2, description="closed ring, mm")]
    depth: Positive = 1400.0
    coping: NonNegative = 300.0
    raised: Literal["auto"] | bool = "auto"
    on: Annotated[str | None, Field(min_length=1, description="slab id")] = None

    @field_validator("raised", mode="before")
    @classmethod
    def _raised(cls, value: Any) -> Any:
        if value is None:
            return "auto"
        if value != "auto" and value is not True and value is not False:
            raise ValueError(f"must be auto, true or false, got {quote(value)}")
        return value

    @model_validator(mode="after")
    def _outline(self) -> Pool:
        self.outline = normalize_ring(self.outline, "outline", f"pool {self.id}")
        return self


class Ramp(_Record):
    """A ramp along an axis from z_start to z_end (storey-local mm), at most a 20 % slope."""

    KIND: ClassVar[str] = "ramp"

    id: Annotated[str, Field(min_length=1)]
    axis: Annotated[list[Vertex], Field(min_length=2, description="centre line, mm")]
    width: Positive
    z_start: Mm = 0.0
    z_end: Mm
    side_walls: bool = True
    wall_height: Positive = 900.0

    @model_validator(mode="after")
    def _slope(self) -> Ramp:
        self.axis = normalize_vertices(self.axis, f"ramp {self.id}", False)
        run = polyline_length(self.axis)
        if run <= 1e-9:
            raise _refuse("axis", "has no length")
        rise = abs(self.z_end - self.z_start)
        slope = rise / run
        if slope > RAMP_MAX_SLOPE + 1e-9:
            raise _named(
                f"ramp {self.id} climbs {fmt(round(rise, 1))} mm over {fmt(round(run, 1))} mm, a slope of "
                f"{fmt(round(slope * 100, 1))} percent; the most is {fmt(round(RAMP_MAX_SLOPE * 100, 1))} percent"
            )
        return self


# --- site -----------------------------------------------------------------------------------


class SiteObject(BaseModel):
    """A SITIO_<NAME> block: a library component (tree, car, lounger...) centred on at."""

    model_config = ConfigDict(extra="ignore")
    id: Annotated[str, Field(min_length=1)]
    item: Annotated[str, Field(min_length=1, description="ARBOL, PALMERA, AUTO, SUV, REPOSERA, PERSONA...")]
    at: Point
    rotation: Annotated[float, Field(allow_inf_nan=False, description="degrees CCW")] = 0.0


class SitePolygon(BaseModel):
    """A closed polyline on SITIO-PAVIMENTO, SITIO-DECK or SITIO-PILETA."""

    model_config = ConfigDict(extra="ignore")
    id: Annotated[str, Field(min_length=1)]
    points: Annotated[list[Vertex], Field(min_length=3)]
    depth: Annotated[float | None, Field(gt=0, le=5000, description="pool depth, mm")] = None


class SiteFence(BaseModel):
    """A polyline on SITIO-CERCO."""

    model_config = ConfigDict(extra="ignore")
    id: Annotated[str, Field(min_length=1)]
    points: Annotated[list[Vertex], Field(min_length=2)]
    closed: bool = False
    height: Annotated[float, Field(gt=0, le=5000, description="mm")] = 1800.0


class Site(BaseModel):
    model_config = ConfigDict(extra="ignore")
    objects: list[SiteObject] = Field(default_factory=list)
    paving: list[SitePolygon] = Field(default_factory=list)
    decks: list[SitePolygon] = Field(default_factory=list)
    pools: list[SitePolygon] = Field(default_factory=list)
    fences: list[SiteFence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bulges(self) -> Site:
        for key in ("paving", "decks", "pools"):
            for i, poly in enumerate(getattr(self, key)):
                poly.points = normalize_vertices(poly.points, f"site.{key}[{i}] {poly.id}", True)
        for i, fence in enumerate(self.fences):
            fence.points = normalize_vertices(fence.points, f"site.fences[{i}] {fence.id}", fence.closed)
        return self


class DrawingSection(BaseModel):
    """A section cut for export_drawings: across x (at = x in mm) or y, looking along the other axis."""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=20, description="e.g. A-A")]
    axis: Literal["x", "y"]
    at: Annotated[float, Field(allow_inf_nan=False, description="mm")]
    look: Literal["north", "south", "east", "west"]


class Storey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "N00"
    height: Annotated[float, Field(gt=0, le=MAX_MM, description="storey and wall height, mm")] = 2800.0
    elevation_mode: Literal["auto", "explicit"] = Field(
        default="auto", description="auto: stacked on the storey below; explicit: the floor at elevation"
    )
    elevation: Annotated[float | None, Field(allow_inf_nan=False, description="mm of the floor when explicit")] = None

    @field_validator("elevation")
    @classmethod
    def _elevation(cls, value: float | None) -> float | None:
        if value is not None and not ELEVATION_MIN_MM <= value <= ELEVATION_MAX_MM:
            raise ValueError(
                f"must be between {fmt(ELEVATION_MIN_MM)} and {fmt(ELEVATION_MAX_MM)} mm, got {fmt(value)}"
            )
        return value

    @model_validator(mode="after")
    def _explicit(self) -> Storey:
        if self.elevation is not None:
            self.elevation_mode = "explicit"
        elif self.elevation_mode == "explicit":
            raise _refuse("elevation", "is required when storey.elevation_mode is explicit")
        return self

    def wire(self) -> dict[str, Any]:
        """name and height as in 0.4; elevation_mode and elevation only when explicit."""
        out: dict[str, Any] = {"name": self.name, "height": self.height}
        if self.elevation_mode == "explicit":
            out["elevation_mode"], out["elevation"] = "explicit", self.elevation
        return out


# The 0.5 record lists, in the extension's build order.
EXTRA_LISTS = ("slabs", "columns", "curtain_walls", "railings", "planters", "pools", "ramps")


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    walls: Annotated[list[Wall], Field(min_length=1)]
    openings: list[Opening] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    stairs: list[Stair] = Field(default_factory=list)
    furniture: list[Furniture] = Field(default_factory=list)
    site: Site = Field(default_factory=Site)
    slabs: list[Slab] = Field(default_factory=list)
    columns: list[Column] = Field(default_factory=list)
    curtain_walls: list[CurtainWall] = Field(default_factory=list)
    railings: list[Railing] = Field(default_factory=list)
    planters: list[Planter] = Field(default_factory=list)
    pools: list[Pool] = Field(default_factory=list)
    ramps: list[Ramp] = Field(default_factory=list)
    site_on_ground: bool = False
    storey: Storey | None = None

    @model_validator(mode="after")
    def _references(self) -> Plan:
        for key in ("walls", "openings", "rooms", "stairs", "furniture", *EXTRA_LISTS):
            _unique(key, [r.id for r in getattr(self, key)])
        ids = {w.id for w in self.walls}
        for i, o in enumerate(self.openings):
            if o.wall not in ids:
                raise ValueError(
                    f"openings[{i}].wall names {o.wall!r}, which is not in the plan; walls are {', '.join(ids)}"
                )
        slab_ids = [s.id for s in self.slabs]
        for key in ("railings", "planters", "pools"):
            for i, r in enumerate(getattr(self, key)):
                if r.on is not None and r.on not in slab_ids:
                    raise ValueError(
                        f"{key}[{i}].on names slab {quote(r.on)}, which is not in the plan; "
                        f"slabs are {', '.join(slab_ids) if slab_ids else 'none'}"
                    )
        return self

    def to_wire(self) -> dict[str, Any]:
        """The JSON the extension's build_plan takes (records carry v and kind). The 0.5 lists and
        site_on_ground appear only when used, so a 0.4 plan serialises exactly as before."""
        out: dict[str, Any] = {
            "walls": [w.wire() for w in self.walls],
            "openings": [o.wire() for o in self.openings],
            "rooms": [r.wire() for r in self.rooms],
            "stairs": [s.wire() for s in self.stairs],
            "furniture": [f.wire() for f in self.furniture],
            "site": self.site.model_dump(mode="json", exclude_none=True),
        }
        for key in EXTRA_LISTS:
            if records := getattr(self, key):
                out[key] = [r.wire() for r in records]
        if self.site_on_ground:
            out["site_on_ground"] = True
        if self.storey is not None:
            out["storey"] = self.storey.wire()
        return out


def _unique(path: str, ids: list[str]) -> None:
    seen: dict[str, int] = {}
    for i, value in enumerate(ids):
        if value in seen:
            raise ValueError(f"{path}[{i}].id repeats {value!r} from {path}[{seen[value]}]")
        seen[value] = i


# --- error wording ------------------------------------------------------------------


def field_path(loc: tuple[Any, ...]) -> str:
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _join(path: str, field: str) -> str:
    return f"{path}.{field}" if path else field


def _num(value: Any) -> str:
    if isinstance(value, bool):
        return repr(value)
    if isinstance(value, (int, float)):
        f = float(value)
        return str(int(f)) if f.is_integer() else f"{f:g}"
    return repr(value)


def _is_tag(part: Any) -> bool:
    """Loc parts pydantic adds for validators and union members ('function-after[...]', 'tuple[float, float]')."""
    return isinstance(part, str) and (part.startswith("function-") or "[" in part)


def describe_error(err: ValidationError, root: str = "") -> str:
    """The first validation error as 'path message', e.g. 'walls[2].thickness must be greater than 0, got -200'.
    root prefixes the path ('plan' gives 'plan.walls[2]...'); a refusal that names its object is never prefixed."""
    first = err.errors(include_url=False)[0]
    loc = tuple(p for p in first["loc"] if not _is_tag(p))
    path = _join(root, field_path(loc))
    kind = first["type"]
    ctx = first.get("ctx") or {}
    got = first.get("input")
    if kind == "named":
        return ctx["message"]
    if kind == "field":
        return f"{_join(path, ctx['field'])} {ctx['message']}"
    if kind == "greater_than":
        text = f"must be greater than {_num(ctx['gt'])}, got {_num(got)}"
    elif kind == "greater_than_equal":
        text = f"must be greater than or equal to {_num(ctx['ge'])}, got {_num(got)}"
    elif kind == "less_than_equal":
        text = f"must be at most {_num(ctx['le'])}, got {_num(got)}"
    elif kind == "missing":
        text = "is required"
    elif kind == "literal_error":
        text = f"must be one of {ctx['expected']}, got {got!r}"
    elif kind in {"float_parsing", "float_type", "int_parsing", "int_type"}:
        text = f"must be a number, got {got!r}"
    elif kind in {"finite_number"}:
        text = f"must be finite, got {got!r}"
    elif kind == "too_short":
        text = f"must hold at least {ctx.get('min_length')} items, got {ctx.get('actual_length')}"
    elif kind == "too_long":
        text = f"must hold at most {ctx.get('max_length')} items, got {ctx.get('actual_length')}"
    elif kind == "string_too_short":
        text = "must not be empty"
    elif kind == "bool_type" or kind == "bool_parsing":
        text = f"must be true or false, got {got!r}"
    elif kind == "value_error":
        text = str(ctx.get("error", first["msg"]))
        if not field_path(loc):
            return _join(root, text)  # a whole-plan check: its sentence starts with its own path
    else:
        text = first["msg"]
    return f"{path} {text}" if path else text


def parse_plan(raw: dict[str, Any]) -> Plan:
    """Validates a plan dict; raises ValueError with the path-style message."""
    try:
        return Plan.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(describe_error(exc)) from None
