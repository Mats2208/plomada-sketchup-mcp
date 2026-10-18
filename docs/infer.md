# plan_from_dxf: a draft plan from an ordinary DXF

`build_from_autocad` reads the records AutoCAD MCP Pro and Plomada store in a drawing. Most drawings have
none: an architect's DXF, or one drawn by any AutoCAD MCP, is linework. `plan_from_dxf` reads that linework
and proposes a plan, plus a report of everything it was not sure about. The plan is a **draft**: the agent
reviews the report, fixes the plan, then calls `build_plan`. The inferrer never reads XDATA (a drawing with
records goes to `build_from_autocad`), never touches SketchUp, and never invents silently: what it assumes,
what it could not classify and what it threw away are all in the report.

The code is `src/plomada_bridge/infer.py`; its module docstring is the short version of this page.

## How it works

1. **Load.** Model space is read with every INSERT exploded virtually, nested blocks included, with their
   transforms. An entity on layer 0 inside a block takes the insert's layer; the outermost block name
   travels with every entity, so a door or window block is recognised by name (PUERTA, DOOR, PORTON;
   VENTANA, WINDOW, WIND, GLAZ...) whatever layer its lines are on, and furniture blocks (ARCH_*, SITIO_*,
   CAMA, SOFA, MESA, ROPERO, WC...) never become walls. LWPOLYLINE and 2D POLYLINE become lines and arcs
   (bulges kept as arcs), HATCH boundaries and SOLID outlines too, marked as hatch: evidence for walls,
   never counted as unpaired. TEXT, MTEXT and visible block attributes are kept for rooms. DIMENSION,
   LEADER, tables and the rest are skipped and counted.
2. **Units.** `$INSUNITS` gives the unit (in, ft, mm, cm, dm, m). With `units="auto"` the walls are paired
   once per candidate unit; when the header's unit gives under half the pairs of the best one, the best
   one wins and the report says so. Two of the four AutoCAD MCP Pro fixtures are millimetre drawings whose
   header says inches and metres.
3. **Layer roles.** A mapping (`layers`) names the layers of each role, case-insensitive with `*` and `?`
   wildcards; a role it leaves out is found from the layer name's tokens, ES, EN and AIA: walls MURO,
   PARED, TABIQUE, WALL, MAMPOST; doors PUERTA, DOOR, PORTON; windows VENTANA, WINDOW, WIND, GLAZ, VIDRIO;
   rooms TEXTO, TEXT, AMBIENTE, ROOM, AREA, LOCAL; ignored COTA, DIM, EJE, GRID, AXIS, SYMB, ANNO, NOTA.
   Ignored layers named in a mapping add to the ones found by name. With no wall layer (or none whose lines
   pair), every layer is paired on its own and the ones carrying at least a quarter of the best layer's
   axis length are the walls.
4. **Faces and pairs.** Collinear wall lines that overlap or touch fuse into one face line (duplicates,
   hatch edges, a face split at a jamb). Two faces pair when they are parallel within 1 degree, 60 to 600
   mm apart (`thickness_min`, `thickness_max`) and diverge at most 3 mm over their overlap, overlap by at
   least 150 mm **and** by at least their gap (a wall is longer than it is thick, so the two jamb lines of
   a short pier do not pair), and each is the other's nearest face on that side over that stretch. The
   midline is an axis piece, the gap its thickness, snapped to a multiple of 5 mm when within 1 mm of one
   (the report counts the snaps). A face paired on both sides over one stretch is a line inside a wall:
   when the two outer faces are within `thickness_max`, they pair instead (a window drawn as four lines, a
   cavity line) and the inner line is remembered; farther apart, the nearer pair wins (a counter edge
   beside a wall). Concentric arcs (centres within 2 mm) pair the same way into curved pieces.
5. **Runs and gaps.** Pieces on one axis line (or circle) of one thickness merge into runs, the axis fitted
   to all of them. A gap between two pieces closes when a wall face runs across it (a T stem interrupts
   only the near face), when a crossing wall's band fills it, or when it is under 500 mm (reported). A gap
   from 500 mm to 8 m is an opening when something in it says so:
   - a **door**: an arc hinged within half the thickness plus 60 mm of a jamb, its radius 80-120 % of the
     gap (40-60 % with one at each jamb: a double door, reported). The arc's side gives the swing, the jamb
     the hand. Door-layer or door-block linework over the gap without an arc is a door with swing and hand
     reported as assumed (a sliding door);
   - a **window**: window-layer or window-block linework over the gap, or a glass line strictly inside the
     wall covering 80 % of the gap and not running past it (an axis line is not glass).

   A gap up to `gap_max` (4 m) with nothing in it is **unclassified**: reported with its position and
   width, and emitted as `default_opening` (door, window, or skip: the run splits and the gap stays open).
   Window linework or a line between the faces inside a continuous wall (faces unbroken) is a window too.
   With walls, doors and windows on one layer, a line from a swing arc's centre out along its radius is a
   door leaf drawn open, not a wall face.
6. **Junctions.** Run ends are extended or trimmed to the axis of the wall they meet, as far as the mitre
   or butt at that angle reaches (half the other's thickness over the sine, plus the own half thickness
   over the tangent, plus 20 mm). Two ends whose axes cross within reach of both are an **L** at the
   crossing; an end within reach of another wall's axis is a **T** stem ending on that axis (on the
   vertex, when it lands on another L); a curve continuing a run within 5 mm and 3 degrees is joined in
   line. A free end that looks at another wall across an opening-sized gap (a door hard against a tee) is
   classified like any gap; an opening extends it to that wall. Runs of one thickness meeting in an L are
   chained into one polyline, closed when the chain returns. Where three or more ends meet, two of one
   thickness are chained and the others tee into the vertex; with no such pair the extras are pulled back
   and reported. Walls of different thickness meeting end to end stay two walls that the solver mitres; in
   line, the thinner stops 1 mm short (the solver cannot join them) and the report says so. A run that
   touches no other wall is dropped and reported.
7. **Openings, rooms, ids.** Offsets are measured along the final axis, so they follow chaining and
   orientation; an opening is kept 0.5 mm clear of every junction band and dropped (reported) if it runs
   into one. Rooms are texts with at least two letters on room layers (any text inside the walls when no
   room layer exists); a number such as `01` and an area such as `12,5 m2` or `58.48 m²` within four text
   heights become its number and area in mm2. Ids are W1.., D1.. for doors, V1.. for windows, R1..: walls
   by their first vertex (closed rings start at their bottom-left vertex, counter-clockwise; open walls at
   their bottom-left end), openings by wall then offset, rooms bottom-left first. The same drawing always
   gives the same plan. Every record is validated on its own; one that fails is dropped and reported, so
   the plan always passes `parse_plan`.

## The tool

`plan_from_dxf` is read-only and answers with SketchUp closed.

| Parameter | Default | |
|---|---|---|
| `path` | | the DXF. A DWG is refused: save it as DXF first. |
| `layers` | `null` | `{"walls": [...], "doors": [...], "windows": [...], "rooms": [...], "ignore": [...]}`, wildcards allowed; a role left out is found by name |
| `units` | `auto` | `mm`, `cm`, `dm`, `m`, `in`, `ft` force the unit |
| `thickness_min`, `thickness_max` | 60, 600 | mm |
| `default_opening` | `skip` | what an empty gap becomes: `door`, `window`, or `skip` |
| `door_height` | 2100 | mm, every door |
| `window_sill`, `window_height` | 900, 1200 | mm, every window |

`InferOptions` in Python also takes `min_overlap` (150), `gap_min` (500), `gap_max` (4000) and
`max_opening` (8000).

It returns `{"plan": ..., "report": ...}`; the plan is the PlanInput `build_plan` takes.

## The report

| Key | What it holds |
|---|---|
| `verdict` | one line: counts, then `review ...` with what needs looking at, or `nothing flagged, still a draft` |
| `units` | `$INSUNITS`, the unit used, its source (`$INSUNITS`, `wall evidence`, `units parameter`, `default`) and the face pairs found per candidate unit |
| `layers` | the layers of each role, how each role was set (`mapping`, `names`, `geometry`), every layer, where rooms came from |
| `counts` | fused faces, axis pieces, runs, walls, doors, windows, rooms |
| `walls` | per wall: thickness, closed, vertices, length, `paired_pct` (the share of its axis, openings and junction extensions aside, backed by a pair of faces or by a tee stem's band) |
| `openings` | per opening: id, wall, position, kind, width, the evidence (`swing arc r 900 on PUERTAS`, `window linework on VENTANA_1500x200`, `glass line on 0`, `unclassified gap`...) and any `assumed` notes |
| `unclassified_gaps` | position, width, wall thickness, what it was emitted as, and the opening id when emitted |
| `unmatched_evidence` | swing arcs on door layers that matched no gap |
| `unpaired` | wall-layer face lines left unpaired beyond a jamb's length: count, length, the longest 20 |
| `dropped` | runs that touch nothing, openings that run into a junction, records that failed validation, with the reason |
| `assumptions` | units, snapped thicknesses, door leaves, cavity lines, default heights and sills, centre justification and brick, what is never inferred |
| `issues` | free wall ends, closed gaps, three-wall nodes, thickness changes in line |
| `ignored` | skipped entity types and furniture or other blocks |

## Measured

- **The AutoCAD MCP Pro fixtures, records ignored** (every XDATA accessor patched to fail during the run),
  against the records `read_plan` finds in the same file: `casa_minimalista`, `casa_2_plantas_N00`,
  `casos_prueba` (an L with a partition on its reflex corner, an X crossing, six rooms) and
  `casa_completa_N00` (a detached garage with a 2.7 m door, furniture and site): wall recall and precision
  1.000 by axis length (no axis farther than 0.00 mm), openings recall and precision 1.000 with no wrong
  kind, hinge or swing side, every room name. The same with walls, doors, windows and hatches all moved to
  layer 0.
- **Architect-style drawings** drawn by `tests/drafter.py` from known plans (lines, polylines, closed
  outlines or solid hatches; door and window blocks or loose linework; noise): a house with tees, an L,
  a curved wall at 46 degrees to its neighbours, two thicknesses end to end with an X crossing, metres, a
  lying `$INSUNITS`, walls inside a block, rotations of 7.5, 30 and 135 degrees: every axis within 2 mm
  (0.07 mm worst), thickness exact, every opening within 5 mm with the right kind, hinge and swing side,
  rooms by name and position.
- **The solver accepts them.** The extension's own wall solver (`tests/solve_walls.rb`, pure Ruby) builds
  every inferred plan above, fixtures included, as one closed manifold.
- **Imprecise linework.** Every endpoint moved at random by up to 0.5 mm: 18 of 18 drawings read back
  exactly; up to 1 mm: 16 of 18 (a face pair drifting past 3 mm is lost and its wall splits, reported as a
  free end or an unclassified gap).
- **Size.** 144 copies of the test house (432 walls, 1152 openings) in 1.6 s, 400 copies in 4.3 s.

## Limits

- **A plan has no heights.** Every door is `door_height`, every window `window_sill` and
  `window_height`, every wall centre-justified, brick, storey height. Arched heads are not inferred.
- **Walls must have two faces.** A wall drawn as a single line, or thinner or thicker than the range, is
  not a wall. A pier shorter than its thickness cannot pair (its gap merges with the next one).
- **Furniture on wall layers.** A piece standing free is dropped (reported), but furniture touching a wall
  with no recognisable block name (an L-shaped counter, a wardrobe in a corner) can become a stub wall:
  look for free ends in `issues`.
- **Windows drawn only as two face lines on the wall layer** (no glass line, no window layer or block) read
  as continuous wall; sliding doors without a door layer or block read as unclassified gaps.
- **Curves** are concentric ARCs or bulged polyline segments. A curved wall drawn as SPLINEs, or as many
  short straight lines, is not paired as a curve. Door swings in non-uniformly scaled blocks (ellipses)
  are approximated by a circle.
- **Junctions the solver refuses** are not repaired: a wall teeing into the free end of another (reported),
  three walls at one point with no two of one thickness (one pulled back, reported), an X crossing that
  involves a curve.
- **Rooms** are where their name is written, without a point-in-room check (a label drawn outside its room
  stays outside); names need two letters; an area must be written near the name.
- **One drawing, one storey.** Stairs, slabs, columns, roofs, furniture and the site are not inferred.
- **DWG** is not read in this version: save it as DXF.
- **Imprecision.** Robust to 0.5 mm of endpoint noise, mostly to 1 mm, not to 2 mm.
