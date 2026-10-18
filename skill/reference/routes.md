# Input routes

Five ways in, one builder. A and B are exact, C is a draft you review, and D and E are as good as the
plan JSON you write.

## A. AutoCAD MCP Pro (`arch_*` tools)

[AutoCAD MCP Pro](https://github.com/U-C4N/Autocad-MCP) (`pip install autocad-mcp-pro`) writes real
architectural records (`ACADMCP_ARCH` XDATA) on its entities. In its ezdxf backend it needs no
AutoCAD at all.

1. Draw each storey with `arch_plan_from_spec`, or with `arch_wall`, `arch_opening`, `arch_room` and
   `arch_stair`. Use the **same origin** on every storey.
2. **Furniture** with `arch_catalogue_insert` (`ARCH_<ITEM>` blocks). **Site** on the ground-storey
   drawing:
   - `SITIO_<NAME>` blocks;
   - closed polylines on `SITIO-PAVIMENTO`, `SITIO-DECK` and `SITIO-PILETA`;
   - polylines on `SITIO-CERCO`.
3. Export one DXF per storey (`drawing_export_dxf`): `..._N00.dxf`, `..._N01.dxf`.
4. Call `build_from_autocad(dxf_path=..._N00.dxf, storey="N00")`, then N01, and so on.

**The stair goes on the lower storey's drawing:** `risers × riser_height` = storey height + slab.

## B. Any AutoCAD / CAD MCP: the Plomada layer standard

Any tool that can draw LINE, LWPOLYLINE, ARC, CIRCLE, TEXT or INSERT on a named layer and save a
DXF (or a DWG, with a converter installed) can feed Plomada **exactly**. The layer says what an entity
is, and the geometry says where it is. Layer names are case-insensitive and have Spanish and English
aliases. The full standard is in `docs/drawing-standard.md` in the repo.

| Layer | Entity | Means |
|---|---|---|
| `PLOMADA-MURO-<t>` / `PLOMADA-WALL-<t>` | LWPOLYLINE, LINE, ARC (chained), SPLINE | A wall **axis** of thickness `<t>` mm (always mm, whatever the drawing units). A closed polyline is a closed wall. |
| `PLOMADA-PUERTA[-<h>]` / `PLOMADA-DOOR[-<h>]` | LINE (ARC on a curved wall) **on the wall axis**, jamb to jamb | A door; `<h>` is its height (default 2100). |
| `PLOMADA-VENTANA[-<sill>-<h>]` / `PLOMADA-WINDOW[-<sill>-<h>]` | same | A window (defaults: sill 900, height 1200). |
| `PLOMADA-AMBIENTE` / `PLOMADA-ROOM` | TEXT / MTEXT | A room label at the insertion point. An optional second line `12.5 m2` gives the area. |
| `PLOMADA-ESCALERA-<w>` / `PLOMADA-STAIR-<w>` | LINE from the bottom riser's midpoint along the run | A straight stair of width `<w>`. Risers are derived from floor to floor. |
| `PLOMADA-MUEBLE` / `PLOMADA-FURNITURE` | INSERT of any block | Furniture. The item is the `ITEM` attribute, or else the block name. The insert point is the back-left corner. |
| `PLOMADA-LOSA`, `-BARANDA`, `-JARDINERA`, `-MURO-CORTINA`, `-PILETA`, `-COLUMNA` (CIRCLE = round) | polylines / circles | Slab, railing, planter, curtain wall, pool, column, with defaults. |
| `SITIO-*` layers, `SITIO_<NAME>` blocks | as in route A | The site. |

**Units.**
- Coordinates are scaled from `$INSUNITS`: mm, cm and m are read as declared.
- Inches and feet are **refused** until you pass `units="in"` / `"ft"`, or `"mm"` when the header lies.
- Metres that would make a building over 1 km wide are read as mm, with a `units_note` in the result.
- A drawing with records is always mm.
- Numbers in layer names are always mm.

**How to instruct another AutoCAD MCP**, for example:
- "Draw a closed polyline (0,0) (10000,0) (10000,8000) (0,8000) on layer PLOMADA-MURO-200."
- "Draw a line from (2000,0) to (2900,0) on layer PLOMADA-PUERTA."
- "Put the text 'Living' at (3000,4000) on layer PLOMADA-AMBIENTE."
- "Save as DXF."

Then call `build_from_autocad`. Openings must lie **on the axis** of their wall: within half its
thickness, or they are refused, naming the entity.

## C. An ordinary drawing: inference (`plan_from_dxf`)

For drawings made without Plomada in mind: walls as two parallel lines, doors as a leaf with a swing
arc, windows as parallel lines or blocks, and rooms as text.

1. `plan_from_dxf(path)`. It works with SketchUp closed. Options:
   - a `layers` mapping like `{"walls": ["A-WALL*"], "doors": [...], "windows": [...], "rooms": [...], "ignore": [...]}`
     when the auto-detection picks the wrong layers;
   - `units`;
   - `default_opening` (`skip` | `door` | `window`) for gaps it can't classify.
2. **Read the report:**
   - which layers it used;
   - each wall's confidence;
   - unpaired linework;
   - unclassified gaps;
   - dropped pieces, with reasons;
   - its assumptions.
3. `preview_plan(plan)` and compare it with the drawing. Fix the plan JSON: thicknesses, missing walls,
   opening kinds, sill heights.
4. Tell the user what was inferred and assumed. Then `build_plan(plan, ...)`.
5. Keep the reviewed plan. `write_plan_dxf` turns it into an exact Plomada drawing for next time.

## D. Prompt only: plan JSON, and prompt → AutoCAD

1. Turn the brief into a PlanInput: walls first, then openings by `offset`, rooms, stairs and the 0.5
   records. Use realistic sizes:
   - walls: exterior 200–300 mm, interior 100–150 mm;
   - doors: 800–900 mm;
   - corridors: 1000 mm or more;
   - ceilings: 2600–3000 mm.
2. `preview_plan(plan)`: check closure, T junctions and opening positions.
3. `build_plan(plan, storey="N00", ...)`, then `capture_view`.
4. **For AutoCAD:** `write_plan_dxf(plan, path, open=true)` writes a proper drawing and opens it in
   the default CAD app:
   - wall faces cut at openings;
   - door swings, window symbols;
   - room names with areas, stairs.

   Use `format="dwg"` if a converter is installed. With an AutoCAD MCP you can also `drawing_open` the
   DXF. That drawing reads back exactly, so the user can edit it in AutoCAD and you rebuild with
   `build_from_autocad`.

## E. An image or PDF of a plan

1. Read the dimensions written on the plan. If there are none, scale from a known element: a door is
   ~800–900 mm, and a standard bed is 1400–1600 × 1900–2000 mm. Say which reference you used.
2. Write the PlanInput (route D). Snap to round numbers, and list every assumption: thicknesses,
   heights, unknown openings.
3. `preview_plan` and confirm with the user before building.
4. `build_plan`, then compare `capture_view` with the image.
