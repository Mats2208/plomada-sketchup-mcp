# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.6.1] - 2026-10-18

### Fixed

- **A heavy site no longer brings SketchUp down.** The site's library models now have a face budget:
  `site_max_faces`, 8,000,000 by default.
  - **How.** When the site's objects would go over the budget, the heaviest kinds stand in as massing, counted as
    faces of one model times how many are placed. Vegetation becomes an ellipsoid and anything else a box, each
    filling the model's own bounding box, so every object keeps its place and size. The build warns which kinds
    and why.
  - **The bug.** A 10 ha site with 32 `ARBUSTO` (the hedge model has 537,600 faces), 104 `ARBOL` (66,586) and 44
    `AUTO` (111,922) came to about 29 M faces. SketchUp 2025 crashed twice seconds after building it.
    `plant_max_faces` only protected planters.
  - **Verified live.** 32 hedges and 5 pines: the hedges became 96-face ellipsoids and the pines stayed real.

## [0.6.0] - 2026-10-18

0.6.0, ANY CAD. A plan no longer has to come from AutoCAD MCP Pro: any CAD or AutoCAD MCP that draws lines,
arcs, polylines, text and blocks on named layers hands Plomada the exact building, and what Plomada writes is a
drawing a person reads in AutoCAD. And a drawing made without Plomada in mind becomes a draft
plan with `plan_from_dxf`.

### Added

- **The PLOMADA-* layer standard** ([docs/drawing-standard.md](docs/drawing-standard.md)), read with no XDATA:
  walls as LINE, ARC, CIRCLE, LWPOLYLINE, 2D POLYLINE, SPLINE or ELLIPSE on `PLOMADA-MURO-<t>` (LINE and ARC pieces
  chained through nodes of exactly two ends within 1 mm, a loop closing the wall); doors and windows drawn from jamb
  to jamb on the host axis on `PLOMADA-PUERTA[-<h>]` and `PLOMADA-VENTANA[-<sill>-<h>]`, hosted on any wall source,
  offset and width measured along the axis (arc length on arcs); rooms from TEXT or MTEXT on `PLOMADA-AMBIENTE` with
  an optional `12.5 m2` line; straight stairs as a line up the run on `PLOMADA-ESCALERA-<w>`, risers derived from
  floor to floor; furniture as any block on `PLOMADA-MUEBLE`. English aliases for every layer (`PLOMADA-WALL-200`,
  `PLOMADA-DOOR`, `PLOMADA-SLAB`...). `PLOMADA` payloads of kind `opening`, `room` and `stair` fill in what a layer
  cannot say. An opening on no wall or on two, or across a closed wall's first vertex, is refused naming its handle.
- **Units.** `build_from_autocad` and `read_plan` take `units` (`auto`, `mm`, `cm`, `m`, `in`, `ft`); `auto` follows
  `$INSUNITS` for drawings without records and reports the unit read. Numbers in layer names are always mm.
- **DWG in and out** through the ODA File Converter (`PLOMADA_ODA`) or LibreDWG, with a refusal naming both when
  neither is installed.
- **`write_plan_dxf` draws.** Style `drawing` (the default) adds the documentation on AIA-style layers over the same
  records: wall faces with real arcs, mitred, cut at openings, trimmed at tees, with a solid poché; door leaves and
  swing arcs; window lines; room names and areas; stair flights with nosings and an arrow; hatched columns. The data
  layers turn grey and stop plotting. Style `layers` writes the plan in the layer standard alone and returns `lost`,
  every field the standard did not carry, after reading the file back. `format="dwg"` and `open=true` (the file
  opens in AutoCAD without any AutoCAD MCP).
- **New tool `preview_plan`** (read-only): a PNG under 350 KB of a plan or of a DXF or DWG as it plots, without
  SketchUp, through ezdxf's drawing add-on on PyMuPDF (new dependencies `pymupdf` and `pillow`).
- **`plan_from_dxf`: a draft plan from an ordinary DXF.** An architect's drawing, or linework from any
  AutoCAD MCP, with no ACADMCP_ARCH or PLOMADA records: walls as parallel lines, closed outlines or
  hatches (blocks exploded, units checked against the walls), doors as a leaf and a swing arc or a block,
  windows as lines across a gap or a block, rooms as TEXT with their area. Faces pair when they are
  parallel, mutually nearest and longer than they are apart; runs are joined into L corners, T stems and
  chained polylines the solver accepts; gaps become doors, windows or reported unclassified gaps. The tool
  is read-only and returns the plan with a report (layers used, units, per-wall paired share, unclassified
  gaps, dropped pieces, unpaired lines, issues, assumptions, a verdict) for the agent to review before
  `build_plan`. The four AutoCAD MCP Pro fixtures, read without their records, match them exactly (walls
  and openings recall and precision 1.000). See [docs/infer.md](docs/infer.md).

### Changed

- The refusal for a drawing with nothing readable names the three routes and the standard.
- `$INSUNITS` inches and feet must be confirmed with `units` (AutoCAD's imperial template declares inches over
  drawings made in mm), and metres that would make the walls span more than 1 km are read as mm with a
  `units_note` (ezdxf's and AutoCAD MCP Pro's default header). Drawings with records stay millimetres, as their
  records are: the 0.4 and 0.5 fixtures read byte for byte as before.
- An entity on an unknown `PLOMADA-*` layer, or of a type its layer does not take, is skipped with a warning.

### Fixed

- `write_plan` drew both faces of a centred arc wall at r - t/2; the outer face is at r + t/2.
- An LWPOLYLINE or 2D POLYLINE under a downward extrusion (a mirrored or exploded block) was read in its OCS, x
  mirrored and its bulges turned the wrong way; so were the SITIO and catalogue inserts. They are read in world
  coordinates now, and such a furniture insert is skipped as mirrored. 0.5 records and 0.4 site polylines had it too.
- A damaged DXF that ezdxf rejects with ValueError, StopIteration and the like (not only DXFStructureError) is
  refused as "cannot read ... as DXF" instead of escaping as a raw error; 0.5 had it too.

## [0.5.0] - 2026-10-17

CURVAS. One plan, written once, now builds a non-orthogonal building: curved walls with real openings, a curved slab
edge on every storey with cantilevered balconies and their bands, glass railings, planters, round columns, curtain
walls, arches, pools on any storey, ramps, a basement and free storey heights.

### Added

- **Curves everywhere a polyline goes.** Wall axes, slab outlines and holes, railing and curtain-wall paths, planters,
  pools, ramps and site polygons take `[x, y, bulge]` vertices with the DXF convention; a bulge over 1 (more than a
  semicircle) is refused naming the segment. One kernel, `Geometry::Arc`, tessellates every arc at a 5 mm chord
  (4 to 96 facets), so elements that share an arc share its vertices.
- **Curved walls with real openings.** An arc segment keeps the solver's (s, t) frame through polar coordinates;
  joints intersect the offset curves (line-circle, circle-circle), a stem can tee into an arc, and the walls stay
  manifold by construction. A door or window on an arc is split into flat panels no wider than 1200 mm that may bow
  at most 60 mm; the facets of a curved face are soft and smooth.
- **Slab records.** `slab` with holes, a top offset, a material and an `edge_band` fascia; the slabs at one level are
  unioned by a new plane boolean (`Geometry::Boolean`), stair wells are cut, and the band runs only along the free
  edge. New tool **`set_slab`**; `add_slab` takes ids, holes, bands and curves.
- **Free storey elevations and a ground level.** Explicit elevations from -10 m to 60 m (basements, half levels, a
  raised ground floor), `ground_level_mm` stored in the model, the site on the storey nearest it, the terrain at it
  with holes for basements, ramps and pools, stair wells chosen by elevation.
- **Railings** (glass panels per facet, posts and balusters within a 110 mm gap, solid parapets), **planters**
  (curb, soil, Bridson Poisson-disk plants seeded by spacing and id), **columns** (round or any outline),
  **curtain walls** (equal mullions, transoms, the void they cut in a wall), **arched openings** on straight runs
  (the wall above the spring line built with the arch hole, arched frames and glass), **pools** on any storey
  (recessed or a raised basin) and **ramps** (ruled, up to 20 %, sloped side walls). Tags `Barandas` and
  `Estructura`, materials `MAT_borde`, `MAT_tierra`, `MAT_vegetacion`.
- **The DXF route for 0.5.** Entities with XDATA app `PLOMADA` and the `PLOMADA-*` layers are read as 0.5 records,
  SPLINE and ELLIPSE flattened at 5 mm. New tool **`write_plan_dxf`** writes a plan as a millimetre DXF with real
  arcs that AutoCAD opens and `build_from_autocad` reads back to identical records.
- **An isolated development SketchUp.** `PLOMADA_DEV_DIR` loads the extension from a checkout, `PLOMADA_PORT`
  overrides the port for one process without touching the stored one, `scripts/sketchup-dev.cmd` starts it on 7884
  with its own token folder; `scripts/dev.py`, `dev_restart.ps1` and `dev_open.ps1` drive it.

### Changed

- `build_plan` and `build_from_autocad` take `ground_level_mm`; the plan's own storey may carry an explicit
  elevation with a floor-to-floor height.
- A library plant heavier than `plant_max_faces` (50 000) is shown as a massing sphere with a warning: the bundled
  hedge has 537 600 faces, and a planter of them stalled SketchUp.
- The stair check measures floor to floor with the plan's slab records when it has them.
- Facets of curved slabs, bands, roofs, columns and paving are softened; a plan without curves solves byte for byte
  as in 0.4.0.

### Fixed

- `Plomada::App.reload!` reloads from the folder the extension was loaded from.
- The plane boolean keeps pieces shorter than its probe (a 6 mm closing chord left a slab ring open) and drops
  vertices microns apart; bands are grown by booleans instead of mitred offsets, which spiked on tiny edges.

### Measured

- **0.4 unchanged.** The solver gives byte-identical output for all six 0.4 fixtures, and `casa_completa_N00.dxf`
  built in SketchUp by 0.4.0 and by 0.5 has 858 faces and the same bounding box in both.
- **Live curves.** A quarter-circle wall of R 2000 with a 1500 mm window: 44 faces, manifold, two panels; the 0.5
  e2e passes 10 of 10 checks.
- **Tests.** 29 tools; 208 minitest and 76 pytest tests (92 new).

## [0.4.0] - 2026-10-08

The 2D drawings of the model, as a tool. Until now they needed `execute_ruby`.

### Added

- **`export_drawings`** writes the drawing set as PNG images, each in a textured (`_render`) and a black hidden-line
  (`_linea`) style:
  - a plan cut per storey and one of the whole plot;
  - the four elevations, with trees, cars and fences hidden and what is buried cut away;
  - sections, by default A-A and B-B through the middle, or your own;
  - two axonometrics, one cut under the roof.

  Every plan, elevation and section shares one scale (`px_per_m`). Camera, render settings, hidden entities and
  section planes are restored, so nothing in the model changes. The framing is pure Ruby with its own tests.
- **A test that compiles every file of the extension.** The SketchUp-only files are not loaded by the other tests, so
  a syntax error there showed up only as "Some Extensions Failed to Load" in SketchUp.

### Fixed

- **`auto_scenes` steps around site objects that came in as groups.** A part picked from a library file (the outdoor
  dining set) is placed as a Group, which the exterior camera clearance missed.

### Measured

- **Complete house.** 20 drawings at 110 px/m in 21–51 s. It takes longer with the site's trees visible in the
  sections and axonometrics.
- **Tests.** 27 tools; 136 minitest and 56 pytest tests.

## [0.3.0] - 2026-10-08

Real furniture and the site around the house. The test was a complete house drawn with AutoCAD MCP Pro
(`tests/fixtures/casa_completa_N00.dxf`): a house, a separate garage with a sectional door, a driveway with two cars,
an entrance path, a deck, a pool, a fence, trees and people. It was built live in SketchUp 2025 and checked in its 10
automatic scenes.

### Added

- **Component library.** A furniture block becomes a real 3D model wherever `mapa_componentes.json` maps it, and a
  massing box otherwise. The library is a folder: `PLOMADA_LIBRARY`, or `biblioteca` next to the repo. Each map entry
  takes these fields:
  - `fit`:
    - `real`: own size, back on the block's back edge.
    - `footprint`: scaled into the block, for models drawn at the wrong scale.
    - `native`: site objects, centred on the block.
  - `rot`: turns a model whose front does not face -y.
  - `part`: one object out of a file that holds several.
  - `scale`

  A model is loaded once per model and cleaned of what downloads carry besides the object: hidden geometry,
  background images, texts, dimensions and guides. `build_from_autocad` and `build_plan` take
  `furniture="library"` (the default) or `"massing"`.
- **The site, read from the ground-floor DXF**, on tag `Entorno`:
  - **Objects.** `SITIO_<NAME>` blocks (origin at the object's centre) become library components standing on the
    ground, or on the paving or deck they fall on. The names are `ARBOL`, `PALMERA`, `PINO`, `ARBUSTO`, `SETO`,
    `MACETA`, `AUTO`, `SUV`, `PICKUP`, `REPOSERA`, `PARRILLA`, `MESA_JARDIN`, `PERGOLA`, `PERSONA` and `FAROLA`.
  - **Paving and decks.** Closed polylines on `SITIO-PAVIMENTO` and `SITIO-DECK`.
  - **Pools.** Closed polylines on `SITIO-PILETA`, built with a coping, walls, floor and water 250 mm down.
    `add_terrain` leaves the coping open.
  - **Fences.** Polylines on `SITIO-CERCO`, 1.8 m boards.
- **Sectional garage doors.** A door at least 2.2 m wide (`garage_door_min_width_mm`) is built as a frame and four
  panels instead of a hinged leaf.
- **New materials:** `MAT_pavimento`, `MAT_agua` and `MAT_porton`.

### Changed

- **Exterior cameras of `auto_scenes`** frame the buildings, not the whole site. An eye that would look across a
  fence moves inside it, and an eye that would stand inside a parked car or a tree trunk moves past it.
- **Tests.** 128 minitest tests and 55 pytest tests, up from 112 and 52. Still 26 tools.

### Measured

- **Complete house.** 6 walls, 13 openings, 17 furniture pieces (10 real models; the 7 the library has no model for
  stay massing), 21 site objects, 2 pavings, a deck, a pool and a fence. Built in 14.0 s with the wall group
  manifold.

### Known limits

- **Loading a library model blocks SketchUp.** It blocks for up to 3.3 s per model, the first time that model enters
  a SketchUp model.
- **The site is read from the ground storey only.** On upper storeys Plomada warns and skips it.
- **The terrain is flat.**
- **`get_plan` does not return the site.**
- **The library does not ship with Plomada.** The models come from 3D Warehouse under its terms; the repo carries
  only the map format (`docs/mapa_componentes.example.json`).

## [0.2.0] - 2026-10-08

Several floors, stairs, hip roofs, furniture, terrain and cameras that need no coordinates. Every feature was drawn in
AutoCAD with AutoCAD MCP Pro, built live in SketchUp 2025 and checked on screen, and is covered by tests that run
without SketchUp.

### Added

- **Storeys.** `build_from_autocad` and `build_plan` take `storey` (`N00`, `N01`...) and `elevation`. Each floor is
  one DXF and one call, and a new storey stacks on the one below at that storey's wall height plus its slab
  thickness. Its slab gets the wells of the stairs coming up from below, the roof under it is erased, and `replace`
  only touches its own storey. Edit tools and `get_plan` take `storey`; with several storeys and none named they
  refuse instead of guessing.
- **Stairs** from the AutoCAD MCP Pro stair records (`stair_kind` straight, L or U, turning left or right). Each
  flight is a closed concrete solid with a sloped soffit, and L and U stairs get a landing block, all on tag
  `Escaleras`. Plomada warns when the rise does not match floor to floor or 2R + G falls outside 600-650 mm.
- **Hip roofs** (`roof="hip"`, also in `add_roof`) on any simple outline: one plane per eave meeting along the
  straight skeleton, which gives a ridge on rectangles, an apex on squares and valleys on L, T and U plans. They
  follow the same conventions as the gable, so both have the same ridge height over a rectangle.
- **Furniture.** The catalogue blocks AutoCAD MCP Pro inserts (`ARCH_<NAME>`, all 20 items) are read from the DXF and
  built as massing components on tag `Mobiliario`. The new materials are `MAT_mobiliario` and `MAT_sanitario`.
- **`add_terrain`** builds a lawn slab on tag `Entorno` around everything Plomada built, its top at the underside of the
  ground-floor slab.
- **`auto_scenes`** creates the scenes, so you don't place cameras by hand:
  - **Interiors.** One per room. The camera stands in the corner with the longest view across, kept clear of walls,
    stairs, wells and furniture.
  - **Exteriors.** Four at eye level, each backed off until every vertex of the building fits the frame.
  - **Aerial.** One view from above.

  Room labels are hidden in these scenes.
- Fixtures: the two-storey house (`tests/fixtures/casa_2_plantas_N00.dxf`, `_N01.dxf`). It has a 17-riser L stair and 16
  catalogue pieces.

### Changed

- **The wall solve is split per building.** Each building, made of walls that touch, is solved on its own and in its
  own job step. One house's sill heights no longer slice the walls of the others. The four-building plan's longest
  tick drops from 124 ms to 33 ms, or 39 ms with hip roofs.
- **Refused plans still change nothing.** A plan the solver refuses is now caught in the first job steps, which change
  nothing. The model stays as it was, and the -32004 message is the same as before.
- **26 tools,** up from 24.
- **Tests.** 112 minitest tests and 52 pytest tests, up from 75 and 48.

### Measured

- **Reference house.** 0.91 s p50 and a longest tick of 34 ms (`bench/results-2026-10-08.json`). The e2e passes 16/16.
- **Two-storey house.** 0.57 s for N00 and 0.69 s for N01, both wall groups manifold. Its 10 automatic scenes take
  0.41 s.

### Known limits

- **Storeys stack straight up**, so split levels are not supported.
- **Stair wells are not recut.** Rebuilding a lower storey does not recut the well above; Plomada warns.
- **Stairs and furniture are massing only.**
- **Hip roofs take no courtyards.**

## [0.1.1] - 2026-10-08

Fixes found by building four plans the 0.1.0 solver had never seen (`tests/fixtures/casos_prueba.dxf`): the reference
house, an L-shaped house, a square split by an X crossing, and a six-room house.

### Fixed

- A partition that ends exactly on a mitred corner of another wall, for example the reflex corner of an L, was refused.
  It now tees into the arm it crosses, and the shared mitre face stays manifold.
- A plan with several buildings got a slab and a roof only on the largest one. Every building now gets its own:
  `N00_losa` / `N00_techo` for the first, then `N00_losa_2` / `N00_techo_2` and so on.

### Added

- `tests/fixtures/casos_prueba.dxf` and its records: 15 walls, 44 openings and 12 rooms, covered by 4 minitest tests
  and 1 pytest test. Totals: 75 minitest tests and 48 pytest tests.

### Measured

- The four-building plan builds in 2.7 s, with manifold walls and 0 internal faces.
- The reference house is unchanged: 0.81 s, longest build tick 29 ms, all 16 e2e checks pass.

### Known limits

- The pure-Ruby solve for a whole plan runs inside a single tick. For the four-building plan that tick takes about
  120 ms, over the 100 ms target. Solving building by building across ticks is planned for 0.2.

## [0.1.0] - 2026-10-08

First release. The SketchUp extension and the Python bridge version together.

### Added

- `build_from_autocad`: an AutoCAD MCP Pro DXF (ACADMCP_ARCH records, v 1) becomes a house in one call and one undo
  step. You get manifold walls with real openings and reveals, window and door components, a floor slab, a flat or gable
  roof, and room labels.
- `Plomada::Geometry`, a pure-Ruby wall solver: mitred L corners, T junctions trimmed to the near face, X crossings, and
  openings as voids in a conforming cell grid. It produces closed manifold walls without booleans.
- A single-thread pump on `UI.start_timer`: length-prefixed JSON-RPC 2.0 on loopback TCP, a hello handshake with a
  token, a FIFO queue, cancel, deadlines, and progress notifications on every step.
- 24 MCP tools on mcp 2.3.0 (`MCPServer`), with typed `Annotated` fields and honest read-only, destructive and
  idempotent hints.
- Edit tools that re-solve from the plan stored in the model: `add_wall`, `add_opening`, `move_opening`,
  `set_wall_height`, `add_slab`, `add_roof`, `set_material`.
- View tools: `capture_view` (JPEG under 350 KB), `create_scene` (two-point, eye height), `export_scene_images` and
  `export_model` (skp, fbx, obj).
- Security: loopback only, a 64-hex token compared in constant time, `execute_ruby` off by default, and an audit log
  without params.
- `scripts/install.ps1`, `scripts/e2e_house.py`, `scripts/bench.py --certify`, `scripts/build_rbz.py` and
  `scripts/check_compat.py`.
- Tests: 68 minitest tests without SketchUp, 47 pytest tests against a fake extension, and CI on Windows and Ubuntu.

### Measured on the reference house

`build_from_autocad` 0.81 s p50, `model_info` 31.3 ms p50 and 34.6 ms p95, `capture_view` 120 ms max, longest pump tick
46.4 ms. See `bench/results-2026-10-08.json`.

### Known limits

- One storey, no stairs, straight walls only.
- A viewport capture and a model export are single native calls that can block the UI for longer than 100 ms.
- PBR is detected through `Sketchup::Material#metallic_factor=`, the method SketchUp 2025 actually has, not through
  `metalness=`.

[0.6.1]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.6.1
[0.6.0]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.6.0
[0.5.0]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.5.0
[0.4.0]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.4.0
[0.3.0]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.3.0
[0.2.0]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.2.0
[0.1.1]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.1.1
[0.1.0]: https://github.com/Mats2208/plomada-sketchup-mcp/releases/tag/v0.1.0
