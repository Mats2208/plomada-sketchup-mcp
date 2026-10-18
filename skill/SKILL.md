---
name: plomada
description: >-
  Use when building or editing architecture in SketchUp through the Plomada MCP (tools build_plan,
  build_from_autocad, plan_from_dxf, write_plan_dxf, add_wall, add_opening, auto_scenes,
  export_drawings...): turning an AutoCAD/DXF/DWG floor plan, a plan drawn by ANY AutoCAD MCP, an
  architect's ordinary drawing, a plan image, or just a prompt into an exact 3D building with real
  openings, storeys, curved walls, slabs, balconies, railings, roofs, stairs, furniture and site; and
  going the other way, from a prompt to an AutoCAD drawing. This is the authoritative operating guide:
  which input route to pick, the exact plan format (PlanInput, millimetres, bulges), every tool and its
  rules, and what Plomada refuses and why. Load it before driving Plomada so you act from verified
  facts, not guesses.
---

# Plomada: operating guide

Plomada is an MCP server (`plomada-mcp`, a Python stdio bridge) plus a SketchUp extension. The bridge
reads plans, validates them and sends them over loopback TCP to the extension, which builds the
geometry inside a running **SketchUp 2025 Pro on Windows**. One tool call is one SketchUp undo step.

```
you ──▶ plomada-mcp (stdio) ──▶ 127.0.0.1 TCP + token ──▶ Plomada extension (pump in SketchUp) ──▶ model
         reads DXF/DWG, validates                            solves walls, cuts openings, builds
```

The DXF/DWG readers, `plan_from_dxf`, `write_plan_dxf` and `preview_plan` run in the bridge and work
with SketchUp closed. Everything else needs SketchUp open with the extension loaded.

## Prime directives

1. **Millimetres, always.** Every coordinate, length, height and offset is mm. Areas are mm².
   A plan in metres is wrong by 1000×: convert first, or pass `units` to the readers.
2. **Do not invent.** Tool names, parameters, record fields, materials, tags and catalogue items come
   from this skill, its references or a tool result. If something isn't here, say so; don't guess.
3. **`status` first.** It must report `protocol 1` and `capabilities.pro: true`. "SketchUp is not
   responding" means a modal dialog is open in SketchUp: ask the user to close it.
4. **One mutating call at a time, never blind retries.** If a build times out or the connection drops,
   its outcome is unknown: call `status` or `get_plan` before calling again.
5. **Look after every build.** Call `capture_view(view="fit")` and read the result's `warnings`. A plan
   Plomada can't build is refused **before** the model is touched, and the error names the wall and
   the exact measure. Fix the plan; don't work around the refusal.
6. **`execute_ruby` is off** unless the user enabled it in Extensions → Plomada → Settings. Never try
   to enable it, and never use Ruby for something a tool does (drawings, scenes, exports).

## Pick the input route

Every route ends in the same builder. Choose by what you have:

| You have | Route | Calls |
|---|---|---|
| **AutoCAD MCP Pro** (its `arch_*` tools) | A: records | draw with `arch_*` → export DXF → `build_from_autocad(dxf_path)` |
| **Any other AutoCAD/CAD MCP**, or a person drawing | B: layer standard | draw on the `PLOMADA-*` layers → save DXF/DWG → `build_from_autocad(dxf_path)` |
| **An existing drawing** (architect's DXF/DWG: plain lines, arcs, text) | C: inference | `plan_from_dxf(path)` → read the report → fix the plan → `build_plan(plan)` |
| **Only a prompt / a brief** | D: plan JSON | write the PlanInput → `preview_plan` → `build_plan`; `write_plan_dxf(open=true)` for the AutoCAD drawing |
| **An image or PDF of a plan** | E: read it yourself | measure it into a PlanInput (state every assumption) → `preview_plan` → confirm with the user → `build_plan` |

- **A and B are exact.** The drawing *is* the data, so what gets built is what was drawn.
- **C is a draft.** Never build an inferred plan whose report still lists unresolved items without
  telling the user what you assumed.
- **D and E are as good as your plan JSON.** Use `preview_plan` to check it before building, and keep
  the JSON: it is the source of truth for later edits.

Details, examples and the full layer standard: [reference/routes.md](reference/routes.md).

## The build loop (one storey)

1. `status`.
2. Build the storey with one of:
   - `build_from_autocad(dxf_path, storey="N00", ...)`;
   - `build_plan(plan, storey="N00", ...)`.

   Common options:
   - `storey_height`: walls, floor to underside of the next slab, mm;
   - `slab`: the automatic 150 mm floor slab;
   - `roof`: `flat` | `gable` (rectangular footprint only) | `hip` (any simple outline) | `none`;
   - `overhang`;
   - `elevation`: explicit floor level; the default stacks on the storey below;
   - `furniture`: `library` | `massing`;
   - `replace`: erase what Plomada built on that storey first;
   - `ground_level_mm`: the terrain level, from the first call.
3. `capture_view(view="fit", style="shaded")`. Look at it, then read the result's `warnings`.
4. Edit with the edit tools (below) instead of rebuilding, when the change is local.

**Several storeys.** Build them one call per storey in order: SS, N00, N01, N02... Each one stacks on
the one below, gets the stair well cut in its slab, and replaces the roof below. **Always pass
`storey`** to edit tools when the model has more than one storey; they refuse without it, because wall
ids repeat between storeys.

**Then, as needed:**
- `add_terrain(margin=2000)`;
- `auto_scenes()`: one camera per room, four exteriors and an aerial, no coordinates needed;
- `export_scene_images(dir, width, height, style)`;
- `export_drawings(dir)`: plans, elevations, sections and axonometrics as PNG, all at one scale;
- `export_model(path, "obj")` for Blender. Not FBX: SketchUp writes ASCII FBX, which Blender refuses.

## Editing an existing build

- `get_plan(storey)` returns the plan stored in the model. Edit from it, not from memory.
- The edit tools all re-solve from the stored plan, as one undo step each:
  - `add_wall`, `set_wall_height`;
  - `add_opening`, `move_opening`;
  - `add_slab`, `set_slab`;
  - `add_roof`;
  - `set_material`.
- `undo(steps)` undoes Plomada calls. `reset_plomada` erases only what Plomada made.
- **Never** open, create or save models through dialogs: a modal dialog freezes the pump.

## The plan, in one screen

Full contract with every field and default: [reference/plan-input.md](reference/plan-input.md).

```json
{
  "walls": [
    {"id": "EXT", "axis": [[0,0],[10000,0],[10000,8000],[0,8000]], "thickness": 200, "closed": true},
    {"id": "INT", "axis": [[6000,0],[6000,8000]], "thickness": 120}
  ],
  "openings": [
    {"id": "D1", "wall": "EXT", "opening_kind": "door", "offset": 2000, "width": 900},
    {"id": "W1", "wall": "EXT", "opening_kind": "window", "offset": 10000, "width": 1500, "sill": 900, "height": 1200}
  ],
  "rooms": [
    {"id": "R1", "name": "Living", "at": [3000, 4000]},
    {"id": "R2", "name": "Bedroom", "at": [8000, 4000]}
  ]
}
```

**Walls**
- A wall is an **axis** polyline plus a `thickness`, with `justification` `center` (default), `left`
  or `right`, seen from the first point.
- **Junctions.** L corners, T junctions (an axis ending *on* another wall's axis) and perpendicular X
  crossings are solved automatically. Draw interior walls to the exterior wall's **axis**, not to its
  face.
- **Refused:** three or more walls ending at one point; junctions sharper than 5°; oblique X crossings.

**Curves**
- A vertex is `[x, y]` or `[x, y, bulge]`. `bulge = tan(sweep/4)` on the segment that **starts** at that
  vertex: positive is counter-clockwise. A quarter circle is 0.4142, a semicircle is 1.
- **Never more than 1:** split bigger arcs.
- The inner face radius must be 300 mm or more.

**Openings**
- `offset` is the distance **along the host axis** (arc length on curves, accumulated over the
  segments, from the axis's first point) to the near jamb.
- Defaults: doors sill 0 and height 2100; windows sill 900 and height 1200.

**Everything else** is a list next to these: `stairs`, `slabs`, `railings`, `planters`, `columns`,
`curtain_walls`, `pools`, `ramps`, `furniture` and `site`.

## Reference files: read on demand

| Read | When |
|---|---|
| [reference/routes.md](reference/routes.md) | choosing or running an input route: AutoCAD MCP Pro, any CAD via the layer standard, inference from an ordinary drawing, prompt → plan → AutoCAD, image → plan |
| [reference/plan-input.md](reference/plan-input.md) | writing or fixing plan JSON: every record, field, default and limit, with a curved multi-storey example |
| [reference/tools.md](reference/tools.md) | choosing a tool or its arguments: the full catalogue with defaults |
| [reference/troubleshooting.md](reference/troubleshooting.md) | a refusal, a warning, a timeout, a wrong-looking model, the furniture library |
