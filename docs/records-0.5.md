# Plomada 0.5 record contract

The wire format build_plan takes (`Plan.to_wire()` in Python, `Plan.normalize` in Ruby). Everything is
millimetres, storey-local world coordinates (the storey is built at z 0 and lifted), z up. Every record may
carry `"v": 1`; any other version is refused. Ids are unique per list.

## Points and bulges

A plan point is `[x, y]`. A polyline vertex (wall `axis`, every `outline`, `holes` ring, `path`, ramp
`axis`) is `[x, y]` or `[x, y, bulge]` with the DXF LWPOLYLINE convention:

- the bulge on vertex i applies to the segment from vertex i to vertex i+1 (on a closed ring, the last
  vertex's bulge applies to the closing segment back to vertex 0);
- `bulge = tan(theta / 4)`, theta the swept angle; positive is counter-clockwise, 0 is straight;
- `|bulge| <= 1.0` (a semicircle at most). Larger is refused with
  `segment <i> of <id> sweeps <deg> degrees; split arcs above 180 degrees`.

A zero bulge is dropped on normalisation (`[x, y, 0]` becomes `[x, y]`), so a 0.4 plan is byte-identical.
Python never tessellates; Ruby tessellates every arc with `Geometry::Arc.tessellate` at
`arc_chord_tol_mm` (5 mm), between `arc_min_facets` and `arc_max_facets` facets per arc.

## Existing kinds (0.4), extended

### wall
`{id, axis:[[x,y,b?]...], thickness, justification: center|left|right, material, closed, height?}`.
Axis segments may be arcs. Refused: an arc whose inner face radius is under 300 mm.

### opening
`{id, wall, opening_kind: door|window, offset, width, sill?, height?, swing, hand, tag?,
head: "flat"|"arch" (default flat), arch_rise? (default width / 2), panel_width? (default 1200)}`.
`offset` and `width` are arc length along the axis. On an arc segment the opening is split into equal flat
panels no wider than `panel_width`; each panel sagitta (at the wall's mid-thickness) must be at most 60 mm.
`head: "arch"` only on straight segments in 0.5; `arch_rise` in `(0, width / 2]`; the arch springs at
`sill + height - arch_rise` (so `height` is the crown height above the sill).

### room, stair, furniture, site
Unchanged. Site `paving`, `decks`, `pools` `points` and `fences` `points` accept bulged vertices.

### storey (plan.storey)
`{name, height, elevation_mode: "auto"|"explicit" (default auto), elevation? (mm, -10000..60000, required
when explicit)}`. `height` is floor to floor. Recommended names: SS, N00, N01, N02, N03, TER.

### plan-level flags
`site_on_ground: bool` (default false): place this plan's site at ground_level_mm itself.

## New kinds

### slab
```json
{"v": 1, "kind": "slab", "id": "L1", "outline": [[0,0],[8000,0,0.4142],[12000,4000],[0,4000]],
 "holes": [[[1000,1000],[2000,1000],[2000,2000],[1000,2000]]], "thickness": 150, "top_offset": 0,
 "material": "MAT_piso_porcelanato", "edge_band": {"height": 450, "depth": 150}}
```
`thickness` > 0 (default 150), `top_offset` mm relative to the storey floor (default 0, -2000..2000),
`material` a Plomada material name (default MAT_piso_porcelanato), `edge_band` null or
`{height > 0, depth > 0}`. With one or more slab records the storey's automatic slab is not built.

### railing
```json
{"v": 1, "kind": "railing", "id": "B1", "path": [[0,0],[4000,0,0.4142],[8000,4000]], "closed": false,
 "style": "glass", "height": 1100, "inset": 50, "on": "L1", "rail": false}
```
`style` glass|bars|solid (default glass), `height` default 1100, `inset` default 50 (toward the slab),
`on` a slab id of this plan or null (the storey floor), `rail` (glass only) adds a 50 x 50 top rail.

### planter
```json
{"v": 1, "kind": "planter", "id": "J1", "outline": [[0,0],[3000,0,0.3],[3000,1200],[0,1200]],
 "curb_thickness": 100, "height": 450, "soil_level": 400, "on": null,
 "plants": {"keys": ["hedge", "potted_plant"], "spacing": 600, "seed": 0}}
```
Defaults: curb 100, height 450 above its base, soil_level 400 (< height), `plants` null for soil only.
Plant keys are keys of the `sitio` section of mapa_componentes.json (or the SITIO names ARBUSTO, MACETA,
ARBOL); a key missing from the library becomes a 600 mm massing sphere.

### column
```json
{"v": 1, "kind": "column", "id": "C1", "at": [4000, 2500], "diameter": 600, "height": null}
{"v": 1, "kind": "column", "id": "C2", "outline": [[0,0],[300,0],[300,600],[0,600]]}
```
Round (`at` + `diameter`) or any outline. `height` null is the storey height. `material` default MAT_hormigon.

### curtain_wall
```json
{"v": 1, "kind": "curtain_wall", "id": "M1", "path": [[0,0],[6000,0,0.2]], "sill": 0, "head": null,
 "mullion_spacing": 1200, "transoms": [1100], "profile": [50, 100], "glass": 10}
```
`head` null is the storey height. Mullions divide each segment equally at most `mullion_spacing` apart.
When the path runs inside a wall of the plan, the wall cells it covers are left out (a full-height hole).

### pool
```json
{"v": 1, "kind": "pool", "id": "P1", "outline": [[0,0],[8000,0],[8000,3500],[0,3500]], "depth": 1400,
 "coping": 300, "raised": "auto", "on": null}
```
`raised` auto|true|false: auto means recessed on the ground storey and a raised basin (200 mm walls on the
slab, water `depth` above the slab) on any other storey.

### ramp
```json
{"v": 1, "kind": "ramp", "id": "R1", "axis": [[0,0],[9000,0]], "width": 3000, "z_start": 0,
 "z_end": -1620, "side_walls": true, "wall_height": 900}
```
Slope `|z_end - z_start| / axis length` at most `ramp_max_slope` (0.20); refused above with both numbers.

## Build options (build_plan)

`elevation` (explicit storey elevation, as 0.4), `ground_level_mm` (default 0, stored in the model).

## DXF (0.5 adapter)

An entity with XDATA app `PLOMADA` (1000 strings joined into one JSON object, `"v": 1`, same chunking as
ACADMCP_ARCH) is a 0.5 record: its geometry comes from the entity (LWPOLYLINE -> outline / path / axis
with bulges; CIRCLE -> column at + diameter), its parameters from the payload. Layer fallbacks without
XDATA: PLOMADA-LOSA (slab), PLOMADA-BARANDA (railing), PLOMADA-JARDINERA (planter),
PLOMADA-MURO-<thickness mm> (wall), PLOMADA-MURO-CORTINA (curtain_wall), PLOMADA-PILETA (pool),
PLOMADA-RAMPA (ramp), CIRCLE on PLOMADA-COLUMNA (column); ids from the entity handle.
