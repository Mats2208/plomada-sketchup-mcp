# Revit through Horizun

`revit_batches` (bridge-only, read-only) compiles a plan into the calls that rebuild it natively in
Revit with the **Horizun Revit MCP** (schemas of Horizun 3.0.6). Plomada never talks to Revit: you send
the calls to Horizun yourself, in order. Python: `plomada_bridge.revit_plan.plan_to_revit`.

## Call it

| Argument (default) | Notes |
|---|---|
| `plan` **or** `dxf_path` **or** `storeys` | One storey, or a list in build order: each `{plan or dxf_path, name, height, elevation}`. Unset elevations stack. |
| `roof` (`flat`), `overhang`, `pitch` | Roof of the top storey only, on a `TECHO` level at its wall tops. `gable` needs a rectangle, else hip. |
| `slab` (true) | A floor under each exterior wall where the storey has no `slabs`; upper floors get the stair wells of the storey below. |
| `template` (`Default_M_ESP.rte`), `revit_year` (2027) | A bare name is looked up in `C:\ProgramData\Autodesk\RVT <year>\Templates`. |
| `rvt_path`, `export_dir` | Left out, they become `{"$input": ...}` placeholders. |
| `exports` (`dwg`, `ifc`, `fbx`), `stair_railings` (true) | |

## What comes back

`stages`, in order: `document` → `discover` → `levels` → `types` → `walls` → `opening_types` →
`openings` → `floors` → `roofs` → `rooms` → `stairs` → `railings_columns_curtain` → `verify` → `exports`.
Each call is `{tool, write, args, binds?, when?, note?}`.

- **write=true**: send `args` (Horizun rehearses), then the same args plus `dry_run: false` and the
  `confirmation_token`. **write=false**: send once.
- **`{"$ref": name}`** is an id an earlier call made; that call's `binds` says where it is in its reply
  (`rows[i]`, `created_ids`, `key k`, or what to pick in `discover`). Names: `level:N00`,
  `wall:N00:EXT` (or `wall:N00:EXT#2`, one per segment), `opening:N00:D1`, `stair:N00:S1`,
  `type:wall:200:brick`, `source:door`...
- **`when`**: an optional call, e.g. load a door `.rfa` (`family_paths` / `$input door_rfa`) if the
  template has none.
- `expected` (element counts to compare in `verify`), `not_mapped`, `assumptions`, `warnings`.

## How the plan maps

| Plan | Revit (Horizun) |
|---|---|
| wall polyline | one `wall` per segment, at the storey level; an arc is one curved wall (`arc` centre/radius/clockwise through start, the bulge midpoint and end). Types: duplicate of a Basic wall, one structure layer of the thickness. |
| `justification` left/right | Horizun has no location line: the axis moves ±thickness/2 to the centreline, mitred at the joints. |
| exterior side | `flip: true` when the outside is on the right of the segment. |
| door / window | wall-hosted `family_instance` at the opening centre on the centreline; `facing_degrees` outward (windows) or against the swing (doors); `hand` right = `flip`; sill as level offset and `INSTANCE_SILL_HEIGHT_PARAM`; `tag` = Mark. One type per width x height. |
| slab, auto slab | `floor`, profile with holes; arcs tessellated (sagitta ≤ 5 mm). |
| stair | `stairs`, one per batch: centre-justified runs, landing for L/U, top level = next storey. |
| railing, column, curtain wall | `horizun_create_railing` on the path; `structural_column` (round, or a rectangle with rotation); a curtain-type `wall` per segment with fixed vertical spacing, transoms as `add_grid_line`. |

**Not mapped** (listed with a suggestion): furniture, site (objects, paving, decks, pools, fences),
pools, planters, ramps, slab edge bands, arched heads (placed flat), non-rectangular columns,
railing style/height/inset, curtain profile/glass, materials.

## Confirm in Revit before trusting a full run

The `assumptions` list: exterior side of `Wall.Create`, door facing vs swing and hand vs `flipHand`,
the sill parameter, the door/window and column type parameter names, the curtain spacing parameters,
internal origin = project base point, and corner joins passing Horizun's location check. Rehearse
one storey first and read every refusal.

## Permission profile

Under Horizun's default `safe_write` profile the tools that touch the document session or write files are **hidden**
from the client, not refused: `horizun_document_session`, `horizun_open_document`, `horizun_create_family`,
`horizun_export`, `horizun_capture_view`, `horizun_verify_changes` and `horizun_execute_python` do not appear in the
tool list at all. The `document`, `opening_types` (family loading), `verify` and `exports` stages need
`"permission_profile": "full_write"` in `%USERPROFILE%\.horizun\settings.json`, set by the machine's owner.
Without it, build into an already open project with the template's own families and save and export by hand.
