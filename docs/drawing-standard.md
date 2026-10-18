# Plomada drawing standard (PLOMADA-* layers)

Any CAD, and any AutoCAD MCP, that can put a LINE, LWPOLYLINE, POLYLINE, ARC, CIRCLE, TEXT, MTEXT or INSERT on a
named layer can hand Plomada an exact building. No XDATA, no plug-in, no particular MCP: the layer name says what
an entity is, the geometry says where. `build_from_autocad` reads the DXF (or DWG) and builds it in SketchUp;
nothing is guessed, and what cannot be read is refused or skipped by name.

This is one of three routes into a plan, and they mix freely in one drawing:

| Route | Written by | Carries |
|---|---|---|
| `ACADMCP_ARCH` records | AutoCAD MCP Pro's `arch_*` tools | walls, openings, rooms, stairs (every field) |
| `PLOMADA` records | `write_plan_dxf` ([records-0.5.md](records-0.5.md)) | every record of a plan, exactly |
| The layer standard (this page) | any CAD | geometry, sizes in layer names, defaults for the rest |

An entity that carries an `ACADMCP_ARCH` record is read as that record, whatever its layer.

## Units

Coordinates are read in the drawing unit and scaled to millimetres. **Numbers in layer names are always
millimetres** (`PLOMADA-MURO-200` is a 200 mm wall in a drawing in metres too), and a room area is always m².

| `$INSUNITS` | Read as |
|---|---|
| 0 (unitless), 4 | millimetres |
| 5 | centimetres |
| 6 | metres; but a drawing whose walls would then span more than 1 km is read as millimetres, with a `units_note` (ezdxf's `ezdxf.new()` and AutoCAD MCP Pro's template declare metres over drawings made in mm) |
| 1, 2 | refused until confirmed: pass `units="in"` / `"ft"` (or `"mm"`). AutoCAD's imperial template declares inches over drawings made in mm |
| anything else | refused: pass `units` |

`build_from_autocad(units=...)` (`"auto"`, `"mm"`, `"cm"`, `"m"`, `"in"`, `"ft"`) overrides all of this. A drawing
that carries `ACADMCP_ARCH` or `PLOMADA` records is millimetres in `auto`: the records are, whatever the header says.
Draw in millimetres with `$INSUNITS` 4 and none of this matters.

## The layers

Layer names are case-insensitive; Spanish and English names are the same layer.

| Layer | Entities | Becomes | Defaults |
|---|---|---|---|
| `PLOMADA-MURO-<t>` / `PLOMADA-WALL-<t>` | LINE, ARC, LWPOLYLINE, POLYLINE, CIRCLE, SPLINE, ELLIPSE | a wall axis, `<t>` mm thick, centred on the axis | brick, storey height |
| `PLOMADA-PUERTA[-<h>]` / `PLOMADA-DOOR[-<h>]` | LINE, ARC, two-point polyline | a door from jamb to jamb on its wall's axis | height 2100, swing in, hand left |
| `PLOMADA-VENTANA[-<sill>-<h>]` / `PLOMADA-WINDOW[-<sill>-<h>]` | LINE, ARC, two-point polyline | a window from jamb to jamb | sill 900, height 1200 |
| `PLOMADA-AMBIENTE` / `PLOMADA-ROOM` | TEXT, MTEXT | a room label: name, point, area | area 0 |
| `PLOMADA-ESCALERA-<w>` / `PLOMADA-STAIR-<w>` | LINE, two-point polyline | a straight stair `<w>` mm wide | risers of about 175 mm |
| `PLOMADA-MUEBLE` / `PLOMADA-FURNITURE` | INSERT of any block | a furniture piece from the library | |
| `PLOMADA-LOSA` / `PLOMADA-SLAB` | closed polyline, CIRCLE | a slab outline | 150 mm, `MAT_piso_porcelanato` |
| `PLOMADA-COLUMNA` / `PLOMADA-COLUMN` | CIRCLE (round), closed polyline | a column | storey height, `MAT_hormigon` |
| `PLOMADA-BARANDA` / `PLOMADA-RAILING` | LINE, ARC (chained), polyline | a railing path | glass, 1100 mm |
| `PLOMADA-MURO-CORTINA` / `PLOMADA-CURTAIN-WALL` | LINE, ARC (chained), polyline | a curtain wall path | sill 0 to storey height, mullions every 1200 |
| `PLOMADA-JARDINERA` / `PLOMADA-PLANTER` | closed polyline, CIRCLE | a planter | curb 100 x 450, no plants |
| `PLOMADA-PILETA` / `PLOMADA-POOL` | closed polyline, CIRCLE | a pool | 1400 deep, coping 300 |
| `SITIO-PAVIMENTO`, `SITIO-DECK`, `SITIO-PILETA`, `SITIO-CERCO` | LWPOLYLINE | site paving, decks, pools, fences | fence 1800 |
| any layer | INSERT of `SITIO_<NAME>` (ARBOL, AUTO, ...) or `ARCH_<ITEM>` | a site object, centred; a furniture piece | |

Every record read from a layer gets its entity handle as id. Arcs stay arcs: an ARC, a bulged polyline segment or a
CIRCLE becomes an exact `[x, y, bulge]` vertex; only SPLINE and ELLIPSE are flattened (within 5 mm).

### Walls

- A polyline, SPLINE or ELLIPSE is one wall; its closed flag is the wall's. A CIRCLE is a round closed wall.
- LINE and ARC pieces on layers of the same thickness are chained: ends closer than 1 mm are one node, and two
  pieces continue each other through a node shared by exactly two ends. A loop becomes a closed wall. A node of
  three or more ends (a T, a cross) ends the chains there, so each arm is its own wall and the solver joins them.
  A chain keeps the direction and handle of its first piece in drawing order.
- The axis is the wall's centre line. For a left- or right-justified wall, a material or a height of its own, use
  `write_plan_dxf` or a `PLOMADA` record.

### Doors and windows

Draw the opening **on the host wall's axis from one jamb to the other**: a LINE (an ARC on a curved wall, or a LINE
across the chord, both work). Plomada finds the host as the one wall whose axis lies within half its thickness of
both jambs, on any wall of the drawing (layer, `PLOMADA` or `ACADMCP_ARCH`). Then:

- `offset` is the distance along the host axis from its first vertex to the nearer jamb (arc length on arcs);
- `width` is the distance between the jambs along the axis;
- on a closed wall the opening sits inside one run: it may touch the first vertex, not straddle it.

Refused, naming the entity handle: an opening on no wall, on two walls at once, straddling a closed wall's first
vertex, a polyline of more than two points. The solver then checks what it always checks (an opening past the
end, across a corner, overlapping another, taller than its wall).

Sizes ride in the layer name: `PLOMADA-PUERTA-2400` is a 2400 mm high door, `PLOMADA-VENTANA-0-2400` a window from
the floor to 2400. A door's swing and hand, a tag or an arched head need a `PLOMADA` payload of kind `opening` on
the same entity, e.g. `{"v": 1, "kind": "opening", "swing": "out", "hand": "right"}`.

### Rooms

The label is the room: its plain text (MTEXT formatting stripped) is the name, its point the room point (an MTEXT's
insertion point; a TEXT's insertion point, or its alignment point when it is not left-aligned on the baseline). A
line after the name such as `12.5 m2`, `12,5 m²` or `A: 12.5 m2` is the area in square metres; without one the area
is 0. An empty label is skipped with a warning.

### Stairs

A LINE from the **midpoint of the bottom riser** along the run, as long as the run from the first riser to the last
nosing. Plomada derives the steps from the floor-to-floor height the build uses (storey height plus slab; an explicit
storey's own height):

```
risers       = round(floor_to_floor / 175), at least 2
riser_height = floor_to_floor / risers
going        = run length / (risers - 1)      # the last riser lands on the floor above
```

With 2800 mm storeys and the 150 mm slab, a 4480 mm line gives 17 risers of 173.5 and goings of 280. An L or U
stair, or a stair of its own risers, needs a `PLOMADA` payload of kind `stair` on the line.

### Furniture

Any block inserted on `PLOMADA-MUEBLE`: the item is the block's `ITEM` attribute, else the block name lowercased
(without an `ARCH_` prefix); `sofa_3_seat`, `double_bed`, `wc`... as the component library maps them. **The insert
point is the piece's back-left corner and the rotation turns it about that point**, as `arch_catalogue_insert`
places catalogue blocks. A scaled or mirrored insert is skipped with a warning.

### What is skipped

Warned by name, never guessed: an entity of a type its layer does not take, a layer that starts with `PLOMADA-` but
is not in this table (`PLOMADA-MUROS-200`, `PLOMADA-VENTANA-900`), a bare entity on `PLOMADA-RAMPA` (a ramp's `z_end`
travels only in XDATA), slab holes on `PLOMADA-LOSA-HUECO` (only a slab's `PLOMADA` record names its holes). Entities
on any other layer are ignored, so the drawing can carry dimensions, hatches, text and title blocks freely.

## Telling an AutoCAD MCP what to draw

Any MCP with line, polyline, arc, text and block tools can draw a plan Plomada reads. Ask it in these terms:

- "Create layer `PLOMADA-MURO-200`. Draw a closed polyline on it through (0,0), (8000,0), (8000,6000), (0,6000)."
- "On layer `PLOMADA-MURO-120` draw a line from (5000,0) to (5000,6000)."
- "On layer `PLOMADA-PUERTA` draw a line from (1000,0) to (1900,0)." (a 900 door in the bottom wall, 1000 from its
  corner)
- "On layer `PLOMADA-VENTANA-900-1200` draw a line from (8000,1500) to (8000,3000)."
- "On layer `PLOMADA-AMBIENTE` write the MTEXT `LIVING\P24 m2` centred at (2500,3000)."
- "On layer `PLOMADA-ESCALERA-1000` draw a line from (6000,1000) to (6000,5480)."
- "Insert block `sofa_3_seat` on layer `PLOMADA-MUEBLE` at (500,5000), rotation 270."
- "Set the drawing units to millimetres and save it as `C:\planos\casa.dxf`." Then call
  `build_from_autocad(dxf_path="C:\planos\casa.dxf")`, or `preview_plan(dxf_path=...)` to see it first.

## A one-room house with ezdxf

```python
import ezdxf
from ezdxf import units

doc = ezdxf.new("R2013")
doc.units = units.MM  # ezdxf's default is metres
msp = doc.modelspace()
msp.add_lwpolyline([(0, 0), (5000, 0), (5000, 4000), (0, 4000)], close=True, dxfattribs={"layer": "PLOMADA-MURO-200"})
msp.add_line((2000, 0), (2900, 0), dxfattribs={"layer": "PLOMADA-PUERTA"})
msp.add_line((5000, 1250), (5000, 2750), dxfattribs={"layer": "PLOMADA-VENTANA-900-1200"})
msp.add_mtext("ESTUDIO\\P18.4 m2", dxfattribs={"layer": "PLOMADA-AMBIENTE", "insert": (2500, 2000)})
doc.saveas("estudio.dxf")
```

`read_plan("estudio.dxf")` gives one closed 200 mm wall, a door at offset 2000 width 900, a window at offset 6250
width 1500 (sill 900, height 1200) and the room ESTUDIO of 18.4 m².

## What write_plan_dxf writes

`write_plan_dxf` writes the other way round, in three styles:

- `drawing` (default): the `PLOMADA` and `ACADMCP_ARCH` records, read back exactly, plus a drawing a person reads.
  The **data layers** (`PLOMADA-*`, `A-DOOR-E-N`, `A-WIND-E-N`, `A-ANNO-ROOM-N`, `A-STAIR-E-N`, `A-FURN-E-N`,
  `SITIO`) are grey and do not plot. The **documentation layers** plot and are never read:

  | Layer | Holds |
  |---|---|
  | `A-WALL` | wall faces, mitred at corners, cut at openings, with jamb caps |
  | `A-WALL-PATT` | the solid poché between the faces |
  | `A-DOOR` | door leaves, open at 90 degrees, and their swing arcs |
  | `A-GLAZ` | the three lines of every window |
  | `A-GLAZ-CURT` | curtain wall faces |
  | `A-AREA-IDEN` | room names and areas (m²) |
  | `A-FLOR-STRS` | stair flights, nosings, the walking line with its arrow, SUBE |
  | `A-COLS`, `A-COLS-PATT` | column outlines and their hatch |
  | `A-FLOR-OTLN`, `A-FLOR-HRAL`, `A-FLOR-RAMP`, `A-PLNT`, `A-POOL` | slab edges and holes, railings, ramp edges, planters, pools |

- `records`: the 0.5 output, the records with both wall faces on `A-WALL-E-N`.
- `layers`: this standard, with no XDATA at all, plus the same documentation. It reads the file back and returns
  `lost`, every field the standard did not carry (a material, a door's swing, an L stair, a ramp, the storey...).
  An empty `lost` means any CAD drawing those layers hands over the same building.

`format="dwg"` converts through the ODA File Converter (free; `PLOMADA_ODA` points at it if it is not under
`C:\Program Files\ODA`) or LibreDWG's `dxf2dwg`; `build_from_autocad` reads a `.dwg` the same way. `open=true`
opens the written file with the default application, so it lands in AutoCAD without any AutoCAD MCP.
