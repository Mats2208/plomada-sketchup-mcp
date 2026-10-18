# PlanInput: the plan format

`build_plan`, `write_plan_dxf` and `preview_plan` take this plan, and `plan_from_dxf` returns it.
`build_from_autocad` reads the same records from a DXF. Rules for every record:

- **Units.** Millimetres, in storey-local coordinates: the storey is built at z 0 and lifted to its
  elevation. z is up.
- **Ids** are unique per list.
- **Unknown fields are ignored**, and a missing optional field takes its default.
- **Versions.** A record may carry `"v": 1`; any other version is refused.
- **Refusals name the field**, e.g. `walls[2].axis[3]`, and the exact value, so fix exactly that.

## Points, vertices and bulges

- A **point** is `[x, y]`.
- A **vertex** (in wall `axis`, every `outline`, `holes` rings, `path`, ramp `axis` and site `points`)
  is `[x, y]` or `[x, y, bulge]`, with the DXF LWPOLYLINE convention:
  - the bulge on vertex *i* curves the segment from *i* to *i+1*; on a closed ring, the last vertex's
    bulge curves the closing segment;
  - `bulge = tan(sweep / 4)`, positive counter-clockwise: 0.4142 is a quarter circle, 1 a semicircle;
  - `|bulge| <= 1`. A bigger arc must be split in two, or it's refused with
    `segment <i> of <id> sweeps <deg> degrees; split arcs above 180 degrees`.
- **Find a bulge from three points.** For an arc from A to B with centre C, take the signed sweep θ
  from CA to CB (counter-clockwise positive); then `bulge = tan(θ/4)`.

## walls

`{id, axis, thickness, justification="center"|"left"|"right", material="brick", closed=false, height?}`

- `axis`: at least 2 vertices (3 if closed; 2 arcs can close a ring). No zero-length segments.
- `justification` is the side of the axis the wall body lies on, looking from the first point.
- `height`: null is the storey height.
- **Junctions:**
  - an L where two axes meet end to end;
  - a T where an axis ends on another axis;
  - an X where two axes cross, only at 90° and exact.
- **Refused:**
  - three or more walls ending at one point;
  - junction angles under 5°;
  - an X crossing where one wall curves;
  - an inner face radius under 300 mm;
  - an end touching another wall's free end;
  - overlapping collinear walls.
- **Interior walls** end on the exterior wall's **axis**: Plomada trims them to the inner face.

## openings

`{id, wall, opening_kind="door"|"window", offset, width, sill?, height?, swing="in"|"out", hand="left"|"right", tag?, head="flat"|"arch", arch_rise?, panel_width?}`

- **`offset`** is the arc length along the host axis, from its first vertex to the near jamb. On a
  multi-segment or closed wall it accumulates over the segments. For example, an axis
  [[0,0],[10000,0],[10000,8000],...] puts offset 10500 at 500 mm up the east side.
- **Defaults.** Doors: `sill` 0, `height` 2100. Windows: `sill` 900, `height` 1200. A floor-to-ceiling
  window is `sill: 0, height: 2400`.
- **Swing and hand.** `swing: "in"` opens to the left of the axis direction. `hand` is the hinge jamb,
  seen from the swing side.
- **On a curved segment** the opening becomes flat panels no wider than `panel_width` (default 1200),
  each bowing at most 60 mm. If it doesn't fit, the error says which `panel_width` to use.
- **An opening cannot cross a vertex** between two segments.
- **`head: "arch"`** works on straight segments only. `arch_rise` defaults to `width / 2` (a round
  arch), and `height` is the crown above the sill. Wall must remain above the crown.
- **Refused:** two openings overlapping, or an opening running past the wall's end.

## rooms

`{id, name, number?, at:[x,y], area=0}`

`at` is where the label goes, inside the room. `auto_scenes` makes one interior camera per room, so
rooms matter.

## stairs

`{id, start:[x,y], direction_deg=0, width, risers, riser_height, going, stair_kind="straight"|"l"|"u", turn="left"|"right"}`

- `start` is the **midpoint of the bottom riser**; `direction_deg` is the run direction (0 = +x).
- **The stair belongs to the lower storey** and climbs to the next. `risers × riser_height` must equal
  floor to floor, which is the storey height plus the slab thickness: for example 2800 + 150 = 2950,
  as 17 × 173.5. Plomada warns otherwise, and warns when 2R + G falls outside 600–650.
- The storey above gets the stair well cut automatically.
- L and U stairs turn on a square landing.

## slabs

`{id, outline, holes=[], thickness=150, top_offset=0, material="MAT_piso_porcelanato", edge_band?:{height, depth}}`

- With one or more slab records, the storey's automatic slab is **not** built. Model each plate: the
  main slab, every cantilevered balcony, a terrace 150 mm lower (`top_offset: -150`).
- Slabs at the same level are unioned.
- `holes` are rings: stair, lift and shaft openings.
- `edge_band` hangs a fascia along the **free** edge only, curves included.
- `top_offset` stays within ±2000.

## railings

`{id, path, closed=false, style="glass"|"bars"|"solid", height=1100, inset=50, on?, rail=false}`

- `on` is a slab id (null: the storey floor). `inset` moves the railing toward the slab.
- `solid` is a parapet.
- Railings go on free edges only, never over a wall or a curtain wall.

## planters

`{id, outline, curb_thickness=100, height=450, soil_level?, on?, plants?:{keys, spacing=600, seed=0}}`

- `soil_level` defaults to `min(400, height)`.
- `plants.keys` are library site keys: `hedge`, `potted_plant`, `tree`, `palm`, `pine`.
  - A key missing from the library becomes a 600 mm massing sphere.
  - A model over 50 000 faces is also shown as a sphere.
- **Placement** is deterministic for the same `seed`.

## columns

- Round: `{id, at, diameter, height?}`. Any outline: `{id, outline, height?}`.
- `height` null is the storey height. `material` defaults to `MAT_hormigon`.

## curtain_walls

`{id, path, sill=0, head?, mullion_spacing=1200, transoms=[], profile=[50,100], glass=10}`

- Mullions divide each segment equally.
- `transoms` are heights in mm.
- A path running on a wall's axis cuts a full-height hole in that wall.

## pools

`{id, outline, depth=1400, coping=300, raised="auto"|true|false, on?}`

- `auto`: sunk on the ground storey, a raised basin on any other storey.
- `raised: false` sinks it into the slab: add the matching hole to the slab yourself.

## ramps

`{id, axis, width, z_start, z_end, side_walls=true, wall_height=900}`

- `z_start` and `z_end` are relative to the storey floor.
- The slope is at most 20 %; above that it's refused, quoting both numbers.

## furniture

`{id, item, at:[x,y], rotation=0}`

- `at` is the **back-left corner**; `rotation` is degrees counter-clockwise about it.
- **Items** (the AutoCAD MCP Pro catalogue): `double_bed`, `single_bed`, `wardrobe`, `nightstand`,
  `sofa_3_seat`, `armchair`, `chair`, `coffee_table`, `dining_table_4`, `desk`, `kitchen_counter`,
  `fridge`, `bookshelf`, `wc`, `bidet`, `wall_basin`, `shower_tray`, `bathtub`, `kitchen_sink`,
  `washing_machine`.
- **Models.** With `furniture="library"`, a mapped item becomes the real model from the user's
  component library. An unmapped item, or `furniture="massing"`, becomes a box.

## site (ground storey only)

`{objects:[{id, item, at, rotation}], paving:[{id, points}], decks:[...], pools:[{id, points, depth?}], fences:[{id, points, closed, height=1800}]}`

- **Object items:** `ARBOL`, `PALMERA`, `PINO`, `ARBUSTO`, `MACETA`, `AUTO`, `SUV`, `PICKUP`,
  `REPOSERA`, `PARRILLA`, `MESA_JARDIN`, `PERSONA`, `FAROLA`, each centred on `at`.
- **Not for AI renders:** a `PERSONA` forces ControlNet-based pipelines to draw a person there.
- **Terrain.** `add_terrain` lays the lawn around it all and leaves the pools open.

## storey and plan flags

- `storey`: `{name, height, elevation_mode="auto"|"explicit", elevation?}`, inside a plan or a DXF.
  `height` is floor to floor.
- **Build options win:** `build_plan(storey=..., elevation=..., storey_height=...)` overrides it.
- **Elevation** runs from -10 000 to 60 000 mm. Basements, half levels and raised ground floors are
  just explicit elevations.
- **Ground level.** `ground_level_mm`, from the first build, sets the terrain level. The site goes on
  the storey nearest it, and `site_on_ground: true` puts it on the level itself.

## Worked example: a curved hotel wing (one storey)

`build_plan(plan=<below>, storey="N01", elevation=4200, storey_height=3000, roof="none")`

```json
{
  "walls": [
    {"id": "EXT", "axis": [[0,0],[24000,0],[24000,4000,0.4142],[18000,10000],[0,10000]], "thickness": 200, "closed": true},
    {"id": "PAS", "axis": [[0,6000],[20800,6000]], "thickness": 150},
    {"id": "H1", "axis": [[3250,6000],[3250,10000]], "thickness": 150},
    {"id": "H2", "axis": [[6500,6000],[6500,10000]], "thickness": 150}
  ],
  "openings": [
    {"id": "V1", "wall": "EXT", "opening_kind": "window", "offset": 52775, "width": 1800, "sill": 0, "height": 2400},
    {"id": "VC", "wall": "EXT", "opening_kind": "window", "offset": 29500, "width": 2400, "sill": 0, "height": 2400},
    {"id": "A1", "wall": "EXT", "opening_kind": "window", "offset": 14000, "width": 1600, "sill": 500, "height": 2300, "head": "arch"},
    {"id": "P1", "wall": "H1", "opening_kind": "door", "offset": 200, "width": 800}
  ],
  "slabs": [
    {"id": "L1", "outline": [[-100,-100],[24100,-100],[24100,4000,0.4142],[18000,10100],[-100,10100]], "thickness": 250},
    {"id": "B1", "outline": [[0,10100],[18000,10100,-0.4142],[24100,4000],[26100,4000,0.4142],[18000,12100],[0,12100]],
     "thickness": 250, "edge_band": {"height": 400, "depth": 120}}
  ],
  "railings": [{"id": "BR1", "path": [[26100,4000,0.4142],[18000,12100],[0,12100]], "on": "B1", "style": "glass"}],
  "columns": [{"id": "C1", "at": [12000, 3000], "diameter": 600}],
  "curtain_walls": [{"id": "M1", "path": [[2000,0],[8000,0]], "transoms": [2400]}],
  "rooms": [{"id": "R1", "name": "Room 1", "at": [1625, 8000]}, {"id": "R2", "name": "Lobby", "at": [8000, 3000]}]
}
```

How the numbers work:
- **The quarter circle.** `[24000,4000,0.4142]` curves the segment from (24000,4000) to
  (18000,10000), counter-clockwise, with centre (18000,4000) and R 6000.
- **Offsets accumulate along EXT:**
  - south side: 24 000;
  - east side: 4 000;
  - the arc: 6000 × π/2 = 9 424.8.

  The north side therefore starts at 37 424.8, and V1 (offset 52 775) sits near its west end.
- **VC falls on the arc**, so it becomes two flat 1200 mm panels.
- **The balcony B1** is its own slab. It runs along the outer face, back around the arc clockwise
  (`-0.4142`), and out again at 2 m (`+0.4142`, R 8100). Its band runs only along the free edge.
- **The curtain wall M1** runs on EXT's axis, so it cuts a full-height hole in it.
