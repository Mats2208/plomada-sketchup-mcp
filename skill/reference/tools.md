# Tool catalogue

All lengths are mm. "Mutating" tools are one SketchUp undo step each. They run as jobs with progress,
and they **are not retried automatically**. "Bridge-only" tools run without SketchUp.

## Read

| Tool | Arguments (default) | Returns / notes |
|---|---|---|
| `status` | `reset_max_tick` (false) | Connection, versions, `capabilities` (`pro`, `entities_build`, `pbr`, `fbx_export`), queue, running job, `max_tick_ms`. Call it first. |
| `model_info` | `detail` (false) | Model path, units, entity counts per tag and type, Plomada objects, bounding box. `detail=true` also checks manifold walls, internal faces, doors, windows and panes. |
| `list_entities` | `tag`, `cursor`, `limit` (200) | Top-level entities by persistent id, paged. |
| `list_tags`, `list_materials` | none | Tags with visibility and counts; materials with RGB, alpha, texture and PBR. |
| `get_plan` | `storey` (lowest) | The stored plan of one storey (walls, openings, rooms, stairs, storey settings) and the list of storeys. Edit from this. |
| `capture_view` | `width`, `height`, `style` (`shaded` / `hidden_line` / `lines_only`), `view` (`current` / `fit`) | A JPEG under 350 KB plus its path. `fit` frames the building from the south-west. The camera is restored afterwards. |
| `job_status` | none | Running job, queue, last finished jobs. |

## Build

| Tool | Arguments (default) | Notes |
|---|---|---|
| `build_from_autocad` | `dxf_path`; `storey` (`N00`); `storey_height`; `slab` (true); `roof` (`flat`); `overhang`; `replace` (true); `elevation` (stacked); `furniture` (`library`); `ground_level_mm`; `deadline_ms`; `units` (auto, 0.6) | Reads a DXF (DWG through a converter, 0.6) with AutoCAD MCP Pro records, `PLOMADA` records or the `PLOMADA-*` layer standard, then builds the storey. The result has `records`, `warnings` and per-step counts. |
| `build_plan` | `plan` (PlanInput) + the same options | Builds the storey from plan JSON. |
| `plan_from_dxf` *(0.6, bridge-only)* | `path`; `layers` mapping; `units`; thickness range; `default_opening` (`skip`); door and window defaults | Infers a **draft** PlanInput from an ordinary drawing, plus a report (layers used, confidence, unresolved gaps, dropped pieces). Review it, fix it, then call `build_plan`. |
| `preview_plan` *(0.6, bridge-only)* | `plan` **or** `dxf_path` (.dxf / .dwg), `width` (1600) | PNG under 350 KB. A plan is drawn as `write_plan_dxf` draws it; a file is drawn as it plots. Check a plan before building, or look at a drawing before inferring it. |
| `revit_batches` *(bridge-only)* | `plan` **or** `dxf_path` **or** `storeys`; `roof` (`flat`); `slab` (true); `template` (`Default_M_ESP.rte`); `rvt_path`; `export_dir`; `exports` | The ordered Horizun Revit MCP calls that rebuild the plan in Revit, plus `not_mapped` and `assumptions`. See [revit.md](revit.md). |
| `write_plan_dxf` *(bridge-only)* | `plan`, `path` (.dxf / .dwg), `storey`, `storey_height`, `elevation`; `style` (`drawing` / `records` / `layers`, 0.6); `format` (`dxf` / `dwg`, 0.6); `open` (false, 0.6) | Writes the plan as an mm architectural DXF that AutoCAD opens: walls with real arcs and cut faces, doors with swings, windows, room labels, stairs, plus the data layers. `build_from_autocad` reads it back **exactly**. `open=true` opens it in the default app (AutoCAD). `style="layers"` writes the pure layer standard and returns `lost`: whatever it can't carry. |

## Edit (re-solve from the stored plan)

| Tool | Arguments | Notes |
|---|---|---|
| `add_wall` | `id`, `axis` [[x,y]...], `thickness`, `justification`, `material`, `closed`, `height`, `storey` | Re-solves the junctions. |
| `set_wall_height` | `id`, `height`, `storey` | The openings must still fit. |
| `add_opening` | `id`, `wall`, `opening_kind`, `offset`, `width`, `sill`, `height`, `swing` (`in` = to the left of the axis), `hand` (hinge side seen from the swing side), `tag`, `storey` | Cuts the hole and places the component. |
| `move_opening` | `id`, `offset`, `storey` | The hole and the component move together. |
| `add_slab` | `thickness`, `outline` (exterior outer face), `storey`, `id`, `holes`, `top_offset`, `material`, `edge_band` | Without `id`: the storey slab. With `id`: a slab record. |
| `set_slab` | `id` + only the fields to change; `edge_band=false` removes the band | |
| `add_roof` | `kind` (`flat` / `gable` / `hip`), `overhang`, `thickness`, `pitch`, `storey` | `gable` needs a rectangular footprint. `hip` takes any simple outline, but no courtyard. |
| `add_terrain` | `margin` (6000), `thickness` (200), `material` (`MAT_cesped`) | The lawn around everything, with holes for buildings, pools, basements and ramps. |
| `set_material` | `target` (wall id, opening id, group or tag), `material`, `storey` | A wall keeps its paint across rebuilds. |

## Scenes and output

| Tool | Arguments (default) | Notes |
|---|---|---|
| `create_scene` | `name`, `eye` [x,y], `target` [x,y,z], `eye_height` (1600), `fov`, `two_point` (true), `style` | On an upper storey, `eye_height` = storey elevation + 1600. |
| `auto_scenes` | `interior` (true), `exterior` (true), `storey`, `style`, `interior_fov` (60), `exterior_fov` (40), `replace` (true) | Makes `I_<storey>_<nn>_<room>`, `E1_suroeste`...`E4_noroeste` and `A_aerea`. |
| `axonometric_scenes` | `cut_height` (1200), `style` | Parallel 45-degree scenes framed on the building: `AX_general` and `AX_<storey>` cut above each floor, labels hidden; rerunning replaces their cuts. |
| `export_scene_images` | `dir`, `width`, `height`, `style`, `format` (`png`), `soft_objects` (`keep`) | One image per scene. `hidden_line` gives the visible edges only, for AI line passes. `soft_objects`: `massing` swaps the library cars and plants for their box/ellipsoid stand-ins, `hide` leaves them out (an AI pass draws them real from the prompt instead of copying the low-poly models); lamps and the rest stay. The model is restored afterwards. |
| `export_drawings` | `dir`, `views` (plans, elevations, sections, axonometrics), `styles` (`render`, `line`), `px_per_m` (110), `cut_height` (1200), `sections` | Every orthographic view at one scale. The model is unchanged. |
| `export_model` | `path`, `format` (`skp` / `fbx` / `obj`) | Use `obj` for Blender. `skp` needs a model saved at least once. A heavy furniture library makes huge OBJ files: warn the user first. |
| `library_prepare` | `keys` (every entry with `src`), `force` (false), `library` (`PLOMADA_LIBRARY`), `deadline_ms` | Converts the library's `.glb` / `.gltf` / `.dae` / `.stl` sources to cached `.skp`, normalised by `src_scale`, `front`, `up`, `drop`. Leaves the open model as it was. Run it after adding models, and with `force` after changing those fields; read `size_mm` and `warning` per entry. `.obj` and `.fbx` must be converted to `.glb` first. |

## Housekeeping

| Tool | Notes |
|---|---|
| `reset_plomada` | Erases every group, component and scene Plomada made. Never opens a new file. |
| `undo` | `steps` (1..10). Each Plomada call is one step. |
| `job_cancel` | Cancels a queued job, or stops the running one after its current step and reverts it. |
| `execute_ruby` | Off by default. A guard against mistakes, not a sandbox. Do not use it for anything a tool does. |

## Names Plomada uses

**Materials** (Blender and render pipelines read these by name):
`MAT_hormigon`, `MAT_revoque_blanco`, `MAT_ladrillo`, `MAT_madera`, `MAT_vidrio`, `MAT_metal`,
`MAT_piso_porcelanato`, `MAT_cesped`, `MAT_mobiliario`, `MAT_sanitario`, `MAT_pavimento`, `MAT_agua`,
`MAT_porton`, `MAT_borde`, `MAT_tierra`, `MAT_vegetacion`.

**Tags:** `Muros`, `Carpinterias`, `Losas`, `Escaleras`, `Ambientes`, `Mobiliario`, `Entorno`,
`Referencia_CAD`, `Barandas`, `Estructura`.

**Groups**, by storey prefix:
- `N00_muros`, `N01_losa`, `N01_techo`, `N00_escalera_S1`;
- `N01_canto_B1`, `N01_baranda_B1`, `N01_jardinera_J1`;
- `N00_columna_C1`, `N00_muro_cortina_M1`;
- `N03_pileta_P1`, `SS_rampa_R1`;
- `Terreno`.

**Storey names:** 1 to 16 letters, digits, `-` or `_`. Conventionally `SS`, `N00`, `N01`... `TER`.
