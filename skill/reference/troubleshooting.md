# Troubleshooting

## Connection

| Symptom | Meaning | Do |
|---|---|---|
| `status` fails: connection refused / no token | SketchUp is closed, or the extension is not loaded | Ask the user to open SketchUp 2025 Pro. The extension shows under Extensions → Plomada. |
| "SketchUp is not responding" | A modal dialog is open (save prompt, error box, update notice), or a long native call is running | Ask the user to close the dialog. Then call `status` again. |
| A mutating call timed out | Unknown outcome: it may have finished | `status` and `get_plan` first. Retry only if the change is absent. |
| `capabilities.pro: false` | SketchUp Make or Free | Plomada needs the Pro Ruby API: export, PBR, and `entities.build` for speed. |
| "Some Extensions Failed to Load" on startup | A syntax error in a hand-edited extension | Reinstall the `.rbz` from Releases. |

## Refusals (the model is untouched)

Every refusal names the record, the field and the measure. Fix the plan and call again. **Don't**
retry the same input, and don't fall back to `execute_ruby`.

| Message contains | Fix |
|---|---|
| `sweeps <n> degrees; split arcs above 180 degrees` | Split the arc into two vertices with bulges ≤ 1. |
| `curves too tightly: its inner face has a radius of <n> mm` | Make the inner face radius ≥ 300 mm. |
| `bows <n> mm off its chord` | Set the opening's `panel_width` to the value the message proposes. |
| `three or more walls end at` | Make one wall run through, and end the others on its axis (T junctions). |
| `cross ... where one of them curves` / an oblique X | End one wall against the other instead of crossing. Straighten axes that are off by hundredths of a degree. |
| `overlaps` (openings) / `runs past the end` | Recompute the `offset`s along the axis, arcs included. Keep the openings apart. |
| `gable needs a rectangular footprint` | Use `roof: "hip"`, or `flat`. |
| `climbs <a> mm over <b> mm, a slope of <n> percent` | Lengthen the ramp: 20 % maximum. |
| `holds no ACADMCP_ARCH records ...` (DXF) | Nothing readable in the file. Use the layer standard (route B) or `plan_from_dxf` (route C); see routes.md. |
| `with several storeys, pass storey` | Add `storey` to the edit call. |

## Warnings worth reading (in every build result)

- **Stair doesn't climb exactly floor to floor**, or 2R+G is outside 600–650: fix `risers` and
  `riser_height`.
- **Furniture block not in the catalogue**, or a site object without a library model: it's built as
  massing.
- **Site drawn on an upper storey:** it's ignored. The site belongs to the ground storey.
- **Rebuilt a lower storey:** the stair well above is not recut. Rebuild the storey above too.
- **Plant model too heavy:** it's shown as a massing sphere.

## The furniture library

Real furniture and site models come from the user's own folder of `.skp` files. Nothing ships with
Plomada: models from 3D Warehouse keep their own terms.

- **Where it is.** `PLOMADA_LIBRARY` points at a folder holding `mapa_componentes.json`.
- **What's in the map.** It lists catalogue items, DesignCenter names and site keys, each to a model,
  with these fields:
  - `fit`: `real`, `footprint` or `native`;
  - `rot`: degrees to turn the model so its front faces −y;
  - `part`: which object to use when a file holds several;
  - `scale`.
- **Wrong model?** A model that comes out turned or at the wrong size is fixed in the map, not in the
  plan.
- **No library?** Furniture is massing boxes. `furniture="massing"` forces boxes and exports far lighter.
- **Loading time.** The first time a model enters a SketchUp model, it can block SketchUp up to ~3 s.

## Performance and output

- A one-storey house builds in about 1 s. A complete house with a library and a site took 14 s.
- `capture_view`, exports and drawings are native calls: the UI may stall briefly.
- **OBJ with library models is heavy.** One complete house gave a 258 MB file in 122 s, with SketchUp
  frozen meanwhile. Warn the user and export once. Collapse groups before importing into Blender.
- **`export_model` skp** needs a model that was saved at least once. Never trigger a Save dialog.

## Limits to tell the user up front

- **Platform:** Windows, SketchUp 2025 Pro tested. 2024 and 2026 are targeted but not equally tested.
- **Stairs are massing:** solid flights, no railings.
- **Curves:** no arched openings on curved walls; no double curvature; no structural design (a
  cantilever is as long as its outline).
- **Site:** flat terrain, ground storey only. `get_plan` doesn't return the site.
- **Inference (`plan_from_dxf`):** it is a draft. Hatches, blocks and unusual drawing styles can be
  missed, so always read the report.
