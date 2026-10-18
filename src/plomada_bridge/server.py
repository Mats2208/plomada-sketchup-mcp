"""The MCP server: tool definitions over the SketchUp connection.

Every length is millimetres. Read-only tools may be retried by the bridge on
a stale socket; mutating tools never are. Each mutating call is one undo step
in SketchUp.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import ezdxf
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from . import __version__
from .cadfile import TEMP_PREFIX, as_dxf, open_file, to_dwg
from .config import BridgeConfig, load_config
from .connection import SketchUpClient
from .dxf import DxfPlanError, read_plan
from .dxfwrite import build_doc, write_plan
from .errors import NOT_RESPONDING, BridgeError, NotResponding, describe
from .models import (
    ELEVATION_MAX_MM,
    ELEVATION_MIN_MM,
    SLAB_TOP_OFFSET_MAX_MM,
    Column,
    CurtainWall,
    DrawingSection,
    EdgeBand,
    Furniture,
    Opening,
    Plan,
    Planter,
    Pool,
    Railing,
    Ramp,
    Room,
    Site,
    Slab,
    Stair,
    Vertex,
    Wall,
    describe_error,
)
from .preview import PreviewError, render, save

INSTRUCTIONS = """\
Plomada models architecture inside a running SketchUp 2025 (Windows) from plans drawn in any CAD.
All lengths are millimetres in world coordinates; z is up, the floor is z 0.

Typical flow: status -> build_from_autocad(dxf_path) -> capture_view -> get_plan.
build_from_autocad turns a DXF or DWG into manifold walls with real openings, window and door
components, a floor slab, a roof, stairs and room labels, as ONE undo step. It reads AutoCAD MCP
Pro's ACADMCP_ARCH records, Plomada's PLOMADA records, or the PLOMADA-* layer standard any CAD or
AutoCAD MCP can draw (docs/drawing-standard.md): walls as lines, arcs or polylines on
PLOMADA-MURO-<thickness mm>, doors and windows as a line from jamb to jamb on the wall axis on
PLOMADA-PUERTA[-<height>] and PLOMADA-VENTANA[-<sill>-<height>], room names on PLOMADA-AMBIENTE,
stairs as a line up the run on PLOMADA-ESCALERA-<width>, blocks on PLOMADA-MUEBLE.
Several floors: one DXF per floor, built in order with storey="N00", "N01", ...; each new storey
is stacked on the one below, its slab gets the stair wells, the roof below is replaced.
Furniture blocks become real models from the component library where it maps them (furniture=
"library", the default) or simple boxes ("massing"). The ground-floor DXF can carry the site:
SITIO_<NAME> blocks (ARBOL, PALMERA, PINO, ARBUSTO, MACETA, AUTO, SUV, PICKUP, REPOSERA,
PARRILLA, MESA_JARDIN, PERSONA, FAROLA) and polylines on SITIO-PAVIMENTO, SITIO-DECK,
SITIO-PILETA (closed) and SITIO-CERCO; then add_terrain lays the lawn around it all.
For a drawing set (plans, elevations, sections, axonometrics) use export_drawings, never execute_ruby.
Edit afterwards with move_opening, set_wall_height, add_wall, add_opening, add_slab, set_slab,
add_roof, set_material; they read the plan back from the model, never from the DXF, and take storey
when the model has several.
Curves: any polyline vertex (wall axis, outline, hole, path) may be [x, y, bulge], bulge = tan(sweep / 4),
positive counter-clockwise, at most 1. Plans also hold slabs, columns, curtain_walls, railings, planters,
pools, ramps and arched openings; elevation puts a storey at any height (SS below 0), ground_level_mm sets
the ground. Route: author PlanInput JSON once -> build_plan -> write_plan_dxf (open=true lands it in
AutoCAD); preview_plan shows a plan or a DXF as a PNG without SketchUp.
If a mutating call times out or the connection drops, its outcome is unknown: call get_plan or
status before retrying. execute_ruby is off unless the user enables it in SketchUp.
"""

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _mut(destructive: bool, idempotent: bool) -> ToolAnnotations:
    return ToolAnnotations(
        read_only_hint=False, destructive_hint=destructive, idempotent_hint=idempotent, open_world_hint=False
    )


def exact_errors(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Returns refusals as an error result carrying exactly our sentence (the
    SDK would prefix a raised ToolError with "Error executing tool <name>:")."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError as exc:
            return CallToolResult(content=[TextContent(type="text", text=str(exc))], is_error=True)

    return wrapper


Mm = float
DeadlineMs = Annotated[
    int, Field(ge=1_000, le=600_000, description="ms the job may run in SketchUp before it aborts and reverts")
]
STOREY_PATTERN = r"^[A-Za-z0-9_-]{1,16}$"
STYLE_HELP = (
    "shaded = materials and edges; hidden_line = visible edges only on white faces, the input for line or canny "
    "ControlNets; lines_only = wireframe, every edge including the hidden ones behind walls"
)
GroundLevel = Annotated[
    float | None,
    Field(
        ge=ELEVATION_MIN_MM,
        le=ELEVATION_MAX_MM,
        description="mm of the ground the site and terrain sit on (stored in the model); default 0",
    ),
]
Ring = list[Vertex]
TopOffset = Annotated[
    float | None,
    Field(
        ge=-SLAB_TOP_OFFSET_MAX_MM,
        le=SLAB_TOP_OFFSET_MAX_MM,
        description="mm of the slab top above the storey floor (a step down is negative)",
    ),
]
StoreyName = Annotated[
    str | None,
    Field(
        pattern=STOREY_PATTERN,
        description="storey: N00 ground, N01 first floor...; required when the model has several",
    ),
]


class PlanInput(BaseModel):
    """A plan in the AutoCAD MCP Pro record format (mm)."""

    walls: Annotated[list[Wall], Field(min_length=1, description="wall records")]
    openings: Annotated[list[Opening], Field(default_factory=list, description="door and window records")]
    rooms: Annotated[list[Room], Field(default_factory=list, description="room records")]
    stairs: Annotated[
        list[Stair], Field(default_factory=list, description="stair records: from this storey up to the next")
    ]
    furniture: Annotated[
        list[Furniture], Field(default_factory=list, description="catalogue pieces: item, back-left corner, rotation")
    ]
    site: Annotated[Site, Field(default_factory=Site, description="site objects, paving, decks, pools, fences")]
    slabs: Annotated[
        list[Slab], Field(default_factory=list, description="slabs with holes; any slab replaces the automatic one")
    ]
    columns: Annotated[list[Column], Field(default_factory=list, description="round (at + diameter) or outline")]
    curtain_walls: Annotated[list[CurtainWall], Field(default_factory=list, description="glazed walls on a path")]
    railings: Annotated[list[Railing], Field(default_factory=list, description="glass, bars or solid, on a slab")]
    planters: Annotated[list[Planter], Field(default_factory=list, description="curbed soil, planted from the library")]
    pools: Annotated[list[Pool], Field(default_factory=list, description="recessed or raised pools, any storey")]
    ramps: Annotated[list[Ramp], Field(default_factory=list, description="ramps from z_start to z_end, at most 20 %")]
    site_on_ground: Annotated[bool, Field(description="place this plan's site at ground_level_mm itself")] = False


UnitsChoice = Annotated[
    Literal["auto", "mm", "cm", "m", "in", "ft"],
    Field(description="drawing unit; auto follows $INSUNITS (records are always mm; inches and feet must be given)"),
]


class Bridge:
    """Holds the one SketchUp connection the tools share."""

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or load_config()
        self.client = SketchUpClient(self.config)

    async def read(
        self, tool: str, method: str, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        try:
            return await self.client.call(method, params or {}, read_only=True, timeout=timeout)
        except BridgeError as err:
            raise ToolError(describe(err, tool)) from None

    async def mutate(
        self,
        tool: str,
        method: str,
        params: dict[str, Any],
        ctx: Context | None,
        deadline_ms: int | None = None,
    ) -> Any:
        async def progress(done: float, total: float | None, message: str | None) -> None:
            if ctx is not None:
                await ctx.report_progress(done, total, message)

        try:
            return await self.client.call(
                method,
                params,
                read_only=False,
                deadline_ms=deadline_ms or self.config.default_deadline_ms,
                on_progress=progress,
            )
        except BridgeError as err:
            raise ToolError(describe(err, tool)) from None


def build_options(
    storey_height: float,
    slab: bool,
    roof: str,
    overhang: float,
    replace: bool,
    storey: str = "N00",
    elevation: float | None = None,
    library: str | None = None,
    furniture: str = "library",
    ground_level_mm: float | None = None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "furniture": furniture,
        "storey_height": storey_height,
        "slab": slab,
        "roof": roof,
        "overhang": overhang,
        "replace": replace,
        "storey": storey,
    }
    if elevation is not None:
        opts["elevation"] = elevation
    if library is not None:
        opts["library"] = library
    if ground_level_mm is not None:
        opts["ground_level_mm"] = ground_level_mm
    return opts


def slab_fields(**fields: Any) -> dict[str, Any]:
    """The slab fields given (not None), as the extension takes them: rings as lists, edge_band false as null."""
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "outline":
            value = [list(p) for p in value]
        elif key == "holes":
            value = [[list(p) for p in ring] for ring in value]
        elif key == "edge_band":
            value = value.model_dump() if value is not False else None
        out[key] = value
    return out


def with_storey(params: dict[str, Any], storey: str | None) -> dict[str, Any]:
    if storey is not None:
        params["storey"] = storey
    return params


def create_server(bridge: Bridge | None = None) -> MCPServer:
    b = bridge or Bridge()
    cfg = b.config
    mcp = MCPServer("plomada", instructions=INSTRUCTIONS, version=__version__)
    library_path = str(cfg.library_dir) if cfg.library_dir else None

    # --- read-only ----------------------------------------------------------------------

    @mcp.tool(annotations=READ)
    @exact_errors
    async def status(
        reset_max_tick: Annotated[
            bool, Field(description="start measuring max_tick_ms afresh after this answer (for benchmarks)")
        ] = False,
    ) -> dict[str, Any]:
        """Connection to SketchUp, versions, capabilities (pro, entities_build, pbr, fbx_export), queue
        length, the running job with its progress, and the longest pump tick (max_tick_ms)."""
        params = {"reset_max_tick": True} if reset_max_tick else {}
        try:
            st = await b.client.call("status", params, read_only=True, timeout=cfg.status_timeout_s)
        except NotResponding:
            raise ToolError(NOT_RESPONDING) from None
        except BridgeError as err:
            raise ToolError(describe(err, "status")) from None
        st["bridge_version"] = __version__
        return st

    @mcp.tool(annotations=READ)
    @exact_errors
    async def model_info(
        detail: Annotated[
            bool, Field(description="also check the house: manifold walls, internal faces, doors, windows, glass panes")
        ] = False,
    ) -> dict[str, Any]:
        """Model path, units, top-level entity counts per tag and type, Plomada objects, and the bounding
        box in mm."""
        return await b.read("model_info", "model_info", {"detail": True} if detail else {})

    @mcp.tool(annotations=READ)
    @exact_errors
    async def list_entities(
        tag: Annotated[str | None, Field(description="only entities on this tag, e.g. Muros")] = None,
        cursor: Annotated[int | None, Field(description="next_cursor from the previous page (persistent_id)")] = None,
        limit: Annotated[int, Field(ge=1, le=200, description="entities per page")] = 200,
    ) -> dict[str, Any]:
        """Top-level entities ordered by persistent_id, a page at a time; bounds in mm."""
        params: dict[str, Any] = {"limit": limit}
        if tag is not None:
            params["tag"] = tag
        if cursor is not None:
            params["cursor"] = cursor
        return await b.read("list_entities", "list_entities", params)

    @mcp.tool(annotations=READ)
    @exact_errors
    async def list_tags() -> dict[str, Any]:
        """Tags (layers) with visibility, colour and top-level entity count."""
        return await b.read("list_tags", "list_tags")

    @mcp.tool(annotations=READ)
    @exact_errors
    async def list_materials() -> dict[str, Any]:
        """Materials with RGB, alpha, texture flag and PBR factors when set."""
        return await b.read("list_materials", "list_materials")

    @mcp.tool(annotations=READ)
    @exact_errors
    async def get_plan(
        storey: Annotated[str | None, Field(pattern=STOREY_PATTERN, description="storey; default the lowest")] = None,
    ) -> dict[str, Any]:
        """The plan of one storey read back from the model's plomada attributes: walls, openings, rooms,
        stairs and storey settings (name, height, elevation) in the AutoCAD MCP Pro record format (mm),
        plus every storey in the model."""
        return await b.read("get_plan", "get_plan", with_storey({}, storey))

    @mcp.tool(annotations=READ, structured_output=False)
    @exact_errors
    async def capture_view(
        width: Annotated[int, Field(ge=64, le=8192, description="image width, px")] = cfg.capture_width,
        height: Annotated[int, Field(ge=64, le=8192, description="image height, px")] = cfg.capture_height,
        style: Annotated[
            Literal["shaded", "hidden_line", "lines_only"], Field(description="this image only; " + STYLE_HELP)
        ] = "shaded",
        view: Annotated[
            Literal["current", "fit"], Field(description="current camera, or a south-west view framing the house")
        ] = "current",
    ) -> list[Any]:
        """A JPEG of the SketchUp viewport (quality 0.7, shrunk until under 350 KB). The camera and render
        mode are restored afterwards. Returns the image and its file path in %TEMP%."""
        res = await b.read(
            "capture_view", "capture_view", {"width": width, "height": height, "style": style, "view": view}
        )
        data = base64.b64decode(res.pop("data_base64"))
        return [Image(data=data, format="jpeg"), res]

    @mcp.tool(annotations=READ)
    @exact_errors
    async def job_status() -> dict[str, Any]:
        """The running job (id, method, progress), queued jobs, and the last finished jobs."""
        return await b.read("job_status", "job_status")

    # --- the flagship -----------------------------------------------------------------------

    @mcp.tool(annotations=_mut(destructive=True, idempotent=False))
    @exact_errors
    async def build_from_autocad(
        dxf_path: Annotated[
            str,
            Field(
                min_length=1,
                description="a .dxf or .dwg (a DWG needs the ODA File Converter or LibreDWG) holding AutoCAD MCP "
                "Pro records, PLOMADA records or the PLOMADA-* layer standard",
            ),
        ],
        ctx: Context,
        storey_height: Annotated[
            float, Field(gt=0, le=10_000, description="storey and wall height, mm")
        ] = cfg.storey_height_mm,
        slab: Annotated[bool, Field(description="build the 150 mm floor slab under the exterior wall")] = True,
        roof: Annotated[
            Literal["flat", "gable", "hip", "none"],
            Field(description="gable needs a rectangular footprint; hip takes any outline (L, T, U...)"),
        ] = "flat",
        overhang: Annotated[
            float, Field(ge=0, le=3_000, description="roof overhang past the outer face, mm")
        ] = cfg.overhang_mm,
        replace: Annotated[
            bool, Field(description="first erase the groups and components Plomada built before on this storey")
        ] = True,
        storey: Annotated[
            str,
            Field(
                pattern=STOREY_PATTERN, description="N00 ground, N01 first floor...; replace erases only this storey"
            ),
        ] = "N00",
        elevation: Annotated[
            float | None,
            Field(
                ge=-100_000, le=1_000_000, description="mm of this storey's floor; default stacked on the storey below"
            ),
        ] = None,
        furniture: Annotated[
            Literal["library", "massing"],
            Field(description="library: real 3D models from the component library where mapped; massing: simple boxes"),
        ] = "library",
        ground_level_mm: GroundLevel = None,
        deadline_ms: DeadlineMs = cfg.default_deadline_ms,
        units: UnitsChoice = "auto",
    ) -> dict[str, Any]:
        """Builds the exact 3D house of a CAD plan in one call and one undo step: manifold walls with real
        openings (jambs, sills, soffits), window and door components with glass and leaves, floor slab, roof
        and room labels. The plan comes from AutoCAD MCP Pro records, PLOMADA records or the PLOMADA-* layer
        standard of any CAD; the bridge reads the file, SketchUp only gets the plan."""
        try:
            parsed = await asyncio.to_thread(read_plan, dxf_path, storey_height, units)
        except DxfPlanError as exc:
            raise ToolError(
                f"build_from_autocad refused its input (-32004): {exc}. Fix the DXF or the path and call it again."
            ) from None
        params = {
            "plan": parsed.plan.to_wire(),
            "options": build_options(
                storey_height,
                slab,
                roof,
                overhang,
                replace,
                storey,
                elevation,
                library_path,
                furniture,
                ground_level_mm,
            ),
            "label": f"build from AutoCAD ({parsed.path.name})",
        }
        res = await b.mutate("build_from_autocad", "build_plan", params, ctx, deadline_ms)
        res["dxf"] = str(parsed.path)
        res["records"] = parsed.records
        res["units"] = parsed.units
        if parsed.units_note:
            res["units_note"] = parsed.units_note
        res["warnings"] = list(res.get("warnings", [])) + parsed.warnings
        return res

    @mcp.tool(annotations=_mut(destructive=True, idempotent=False))
    @exact_errors
    async def build_plan(
        plan: Annotated[PlanInput, Field(description="walls, openings, rooms and the 0.5 records, in mm")],
        ctx: Context,
        storey_height: Annotated[
            float, Field(gt=0, le=10_000, description="storey and wall height, mm")
        ] = cfg.storey_height_mm,
        slab: Annotated[bool, Field(description="build the floor slab under the exterior wall")] = True,
        roof: Annotated[
            Literal["flat", "gable", "hip", "none"],
            Field(description="gable needs a rectangular footprint; hip takes any outline (L, T, U...)"),
        ] = "flat",
        overhang: Annotated[float, Field(ge=0, le=3_000, description="roof overhang, mm")] = cfg.overhang_mm,
        replace: Annotated[bool, Field(description="first erase what Plomada built before on this storey")] = True,
        storey: Annotated[
            str,
            Field(
                pattern=STOREY_PATTERN, description="N00 ground, N01 first floor...; replace erases only this storey"
            ),
        ] = "N00",
        elevation: Annotated[
            float | None,
            Field(
                ge=-100_000, le=1_000_000, description="mm of this storey's floor; default stacked on the storey below"
            ),
        ] = None,
        furniture: Annotated[
            Literal["library", "massing"],
            Field(description="library: real 3D models from the component library where mapped; massing: simple boxes"),
        ] = "library",
        ground_level_mm: GroundLevel = None,
        deadline_ms: DeadlineMs = cfg.default_deadline_ms,
    ) -> dict[str, Any]:
        """Builds a house from plan JSON (the same records build_from_autocad reads from a DXF), as one undo step."""
        try:
            checked = Plan.model_validate(plan.model_dump())
        except ValidationError as exc:
            raise ToolError(
                f"build_plan refused its input (-32004): {describe_error(exc, 'plan')}. Fix it and call again."
            ) from None
        params = {
            "plan": checked.to_wire(),
            "options": build_options(
                storey_height,
                slab,
                roof,
                overhang,
                replace,
                storey,
                elevation,
                library_path,
                furniture,
                ground_level_mm,
            ),
        }
        return await b.mutate("build_plan", "build_plan", params, ctx, deadline_ms)

    # --- edits ------------------------------------------------------------------------------

    @mcp.tool(annotations=_mut(destructive=False, idempotent=False))
    @exact_errors
    async def add_wall(
        id: Annotated[str, Field(min_length=1, description="new wall id, unique")],
        axis: Annotated[list[tuple[Mm, Mm]], Field(min_length=2, description="axis polyline [[x, y], ...], mm")],
        thickness: Annotated[float, Field(gt=0, le=2_000, description="mm")],
        ctx: Context,
        justification: Annotated[
            Literal["center", "left", "right"],
            Field(description="side of the axis the wall lies on, from its first point"),
        ] = "center",
        material: Annotated[str, Field(description="record material, e.g. brick, gypsum_board")] = "brick",
        closed: Annotated[bool, Field(description="close the axis into a loop")] = False,
        height: Annotated[float | None, Field(gt=0, le=10_000, description="mm; default the storey height")] = None,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Adds a wall to the house and rebuilds the walls (T junctions and mitres resolved), one undo step."""
        params: dict[str, Any] = {
            "id": id,
            "axis": [list(p) for p in axis],
            "thickness": thickness,
            "justification": justification,
            "material": material,
            "closed": closed,
        }
        if height is not None:
            params["height"] = height
        return await b.mutate("add_wall", "add_wall", with_storey(params, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=False, idempotent=False))
    @exact_errors
    async def add_opening(
        id: Annotated[str, Field(min_length=1, description="new opening id, unique")],
        wall: Annotated[str, Field(min_length=1, description="host wall id")],
        opening_kind: Annotated[Literal["door", "window"], Field(description="door or window")],
        offset: Annotated[
            float, Field(ge=0, description="mm along the host axis from its first point to the near jamb")
        ],
        width: Annotated[float, Field(gt=0, description="mm")],
        ctx: Context,
        sill: Annotated[
            float | None, Field(ge=0, description="mm above the floor; null is 0 for doors, 900 for windows")
        ] = None,
        height: Annotated[float | None, Field(gt=0, description="mm; null is 2100 for doors, 1200 for windows")] = None,
        swing: Annotated[Literal["in", "out"], Field(description="in opens to the left of the host axis")] = "in",
        hand: Annotated[Literal["left", "right"], Field(description="hinge jamb seen from the swing side")] = "left",
        tag: Annotated[str | None, Field(description="component name, default the id")] = None,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Cuts a new door or window into a wall and places its component, one undo step."""
        params = {
            "id": id,
            "wall": wall,
            "opening_kind": opening_kind,
            "offset": offset,
            "width": width,
            "sill": sill,
            "height": height,
            "swing": swing,
            "hand": hand,
            "tag": tag,
        }
        return await b.mutate("add_opening", "add_opening", with_storey(params, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def move_opening(
        id: Annotated[str, Field(min_length=1, description="opening id, e.g. W2")],
        offset: Annotated[float, Field(ge=0, description="new mm along the host axis to the near jamb")],
        ctx: Context,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Moves a door or window along its wall: the hole is rebuilt and the component follows."""
        return await b.mutate("move_opening", "move_opening", with_storey({"id": id, "offset": offset}, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def set_wall_height(
        id: Annotated[str, Field(min_length=1, description="wall id")],
        height: Annotated[float, Field(gt=0, le=10_000, description="new wall height, mm")],
        ctx: Context,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Changes one wall's height and rebuilds the walls; openings must still fit under it."""
        return await b.mutate(
            "set_wall_height", "set_wall_height", with_storey({"id": id, "height": height}, storey), ctx
        )

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def add_slab(
        ctx: Context,
        thickness: Annotated[
            float, Field(gt=0, le=2_000, description="mm; the top stays at z 0")
        ] = cfg.slab_thickness_mm,
        outline: Annotated[
            Ring | None,
            Field(description="[[x, y] or [x, y, bulge], ...] mm; default the exterior wall's outer face"),
        ] = None,
        storey: StoreyName = None,
        id: Annotated[str | None, Field(min_length=1, description="slab id: a slab record next to the others")] = None,
        holes: Annotated[list[Ring] | None, Field(description="rings cut through the slab, mm")] = None,
        top_offset: TopOffset = None,
        material: Annotated[str | None, Field(min_length=1, description="default MAT_piso_porcelanato")] = None,
        edge_band: Annotated[EdgeBand | None, Field(description="downstand band on the outer edge, mm")] = None,
    ) -> dict[str, Any]:
        """Builds (or replaces) a storey's floor slab (N00_losa, N01_losa...) on tag Losas, with the wells of
        the stairs that come up from the storey below. With an id it adds (or replaces) that slab record."""
        fields = slab_fields(
            outline=outline, id=id, holes=holes, top_offset=top_offset, material=material, edge_band=edge_band
        )
        return await b.mutate("add_slab", "add_slab", with_storey({"thickness": thickness, **fields}, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def set_slab(
        id: Annotated[str, Field(min_length=1, description="slab id")],
        ctx: Context,
        outline: Annotated[Ring | None, Field(description="new outline [[x, y] or [x, y, bulge], ...], mm")] = None,
        holes: Annotated[list[Ring] | None, Field(description="new holes (replace the old ones); [] for none")] = None,
        thickness: Annotated[float | None, Field(gt=0, le=2_000, description="mm")] = None,
        top_offset: TopOffset = None,
        material: Annotated[str | None, Field(min_length=1, description="a Plomada material")] = None,
        edge_band: Annotated[
            EdgeBand | Literal[False] | None, Field(description="downstand band on the outer edge; false removes it")
        ] = None,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Changes one slab record (only the fields given) and rebuilds it, one undo step."""
        fields = slab_fields(
            outline=outline,
            holes=holes,
            thickness=thickness,
            top_offset=top_offset,
            material=material,
            edge_band=edge_band,
        )
        params = {"id": id, **fields}
        return await b.mutate("set_slab", "set_slab", with_storey(params, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def add_roof(
        ctx: Context,
        kind: Annotated[
            Literal["flat", "gable", "hip"],
            Field(description="gable needs a rectangular footprint; hip takes any outline (L, T, U...)"),
        ] = "flat",
        overhang: Annotated[float, Field(ge=0, le=3_000, description="mm past the outer face")] = cfg.overhang_mm,
        thickness: Annotated[float, Field(gt=0, le=2_000, description="mm")] = cfg.roof_thickness_mm,
        pitch: Annotated[float, Field(gt=0, lt=75, description="gable or hip slope, degrees")] = cfg.gable_pitch_deg,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Builds (or replaces) a storey's roof (N00_techo...): a flat slab on the wall tops, a gable with its
        ridge along the longer side, or a hip roof with one slope per eave (ridges, hips and valleys)."""
        params = {"kind": kind, "overhang": overhang, "thickness": thickness, "pitch": pitch}
        return await b.mutate("add_roof", "add_roof", with_storey(params, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def add_terrain(
        ctx: Context,
        margin: Annotated[float, Field(gt=0, le=200_000, description="mm of ground around the buildings")] = 6000.0,
        thickness: Annotated[float, Field(gt=0, le=5_000, description="mm, terrain slab")] = 200.0,
        material: Annotated[str, Field(min_length=1, description="a Plomada material")] = "MAT_cesped",
    ) -> dict[str, Any]:
        """Builds (or replaces) the ground, Terreno on tag Entorno: a lawn slab around everything Plomada
        built, its top at the underside of the ground-floor slab, with a hole under each building."""
        params = {"margin": margin, "thickness": thickness, "material": material}
        return await b.mutate("add_terrain", "add_terrain", params, ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def set_material(
        target: Annotated[str, Field(min_length=1, description="wall id, opening id, group name or tag name")],
        material: Annotated[str, Field(min_length=1, description="e.g. MAT_ladrillo, MAT_hormigon, MAT_madera")],
        ctx: Context,
        storey: StoreyName = None,
    ) -> dict[str, Any]:
        """Paints a wall (kept across rebuilds), an opening, a group, or everything on a tag."""
        params = with_storey({"target": target, "material": material}, storey)
        return await b.mutate("set_material", "set_material", params, ctx)

    # --- scenes and exports -----------------------------------------------------------------

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def write_plan_dxf(
        plan: Annotated[PlanInput, Field(description="the plan build_plan takes, in mm")],
        path: Annotated[
            str,
            Field(
                min_length=5, pattern=r"(?i)\.(dxf|dwg)$", description="output .dxf or .dwg file; its folder is created"
            ),
        ],
        storey: Annotated[str, Field(pattern=STOREY_PATTERN, description="storey name, e.g. N00, N01, SS")] = "N00",
        storey_height: Annotated[float, Field(gt=0, le=10_000, description="storey height, mm")] = cfg.storey_height_mm,
        elevation: Annotated[
            float | None,
            Field(ge=ELEVATION_MIN_MM, le=ELEVATION_MAX_MM, description="mm of this storey's floor; default stacked"),
        ] = None,
        style: Annotated[
            Literal["drawing", "records", "layers"],
            Field(
                description="drawing: an architectural drawing (faces cut at openings, poché, door swings, window "
                "lines, room areas, stairs) over the data; records: the 0.5 output; layers: the PLOMADA-* layer "
                "standard with no XDATA, returning lost: what it cannot carry"
            ),
        ] = "drawing",
        format: Annotated[
            Literal["dxf", "dwg"], Field(description="dwg converts through the ODA File Converter or LibreDWG")
        ] = "dxf",
        open: Annotated[bool, Field(description="open the written file with the OS default app (AutoCAD)")] = False,
    ) -> dict[str, Any]:
        """Writes the plan as a millimetre DXF or DWG for AutoCAD: wall axes with real arcs, slabs, columns,
        railings and the other records on PLOMADA-* layers, openings, rooms and stairs as AutoCAD MCP Pro
        records, furniture and site blocks, and (style drawing) the architectural documentation on A-* layers.
        build_from_autocad reads it back exactly. With open it lands in AutoCAD without any AutoCAD MCP.
        Writes the file only; SketchUp is not involved."""
        target = Path(path)
        if target.suffix.lower() != f".{format}":
            raise ToolError(
                f"write_plan_dxf refused its input (-32004): path ends in {target.suffix} but format is {format}. "
                "Fix it and call again."
            )
        raw = {**plan.model_dump(), "storey": {"name": storey, "height": storey_height, "elevation": elevation}}
        try:
            checked = Plan.model_validate(raw)
        except ValidationError as exc:
            raise ToolError(
                f"write_plan_dxf refused its input (-32004): {describe_error(exc, 'plan')}. Fix it and call again."
            ) from None

        def write() -> dict[str, Any]:
            if format == "dxf":
                return write_plan(checked, target, style)
            tmp = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
            try:
                out = write_plan(checked, tmp / (target.stem + ".dxf"), style)
                out["path"] = str(to_dwg(Path(out["path"]), target))
                return out
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        try:
            out = await asyncio.to_thread(write)
        except OSError as exc:
            raise ToolError(f"write_plan_dxf could not write {path}: {exc.strerror or exc}") from None
        except DxfPlanError as exc:
            raise ToolError(f"write_plan_dxf could not write {path}: {exc}") from None
        out["format"] = format
        if open:
            try:
                open_file(Path(out["path"]))
            except OSError as exc:
                raise ToolError(f"write_plan_dxf wrote {out['path']} but could not open it: {exc}") from None
            out["opened"] = True
        return out

    @mcp.tool(annotations=READ, structured_output=False)
    @exact_errors
    async def preview_plan(
        plan: Annotated[PlanInput | None, Field(description="a plan to draw, as write_plan_dxf draws it")] = None,
        dxf_path: Annotated[str | None, Field(min_length=1, description="or a .dxf / .dwg to draw as it plots")] = None,
        width: Annotated[int, Field(ge=320, le=4096, description="image width, px")] = 1600,
    ) -> list[Any]:
        """A PNG of a plan or a drawing without SketchUp or AutoCAD: a plan is drawn as write_plan_dxf's drawing
        style draws it (wall faces and poché, doors, windows, rooms, stairs), a DXF or DWG as it plots (layers
        that do not plot, are off or frozen are left out). Black on white, shrunk until under 350 KB. Returns
        the image and its file path in %TEMP%."""
        if (plan is None) == (dxf_path is None):
            raise ToolError("preview_plan refused its input (-32004): give plan or dxf_path, one of them. Fix it.")
        checked: Plan | None = None
        if plan is not None:
            try:
                checked = Plan.model_validate(plan.model_dump())
            except ValidationError as exc:
                raise ToolError(
                    f"preview_plan refused its input (-32004): {describe_error(exc, 'plan')}. Fix it and call again."
                ) from None

        def draw() -> tuple[bytes, int, int, str]:
            if checked is not None:
                return (*render(build_doc(checked, "drawing")[0], width), "plan")
            src = Path(dxf_path or "")
            if not src.is_file():
                raise DxfPlanError(f"dxf_path {str(src)!r} is not a file")
            dxf = as_dxf(src)
            try:
                return (*render(ezdxf.readfile(dxf), width), src.stem)
            except (OSError, ezdxf.DXFStructureError) as exc:
                raise DxfPlanError(f"cannot read {src.name} as DXF: {exc}") from None
            finally:
                if dxf != src:
                    shutil.rmtree(dxf.parent, ignore_errors=True)

        try:
            png, w, h, stem = await asyncio.to_thread(draw)
        except (DxfPlanError, PreviewError) as exc:
            raise ToolError(f"preview_plan refused its input (-32004): {exc}. Fix it and call again.") from None
        path = save(png, stem)
        return [Image(data=png, format="png"), {"path": str(path), "width": w, "height": h, "bytes": len(png)}]

    @mcp.tool(annotations=_mut(destructive=False, idempotent=True))
    @exact_errors
    async def create_scene(
        name: Annotated[str, Field(min_length=1, description="scene name")],
        eye: Annotated[tuple[Mm, Mm], Field(description="[x, y] of the camera, mm")],
        target: Annotated[tuple[Mm, Mm, Mm], Field(description="[x, y, z] the camera looks at, mm")],
        ctx: Context,
        eye_height: Annotated[
            float, Field(ge=-10_000, le=100_000, description="camera height, mm")
        ] = cfg.eye_height_mm,
        fov: Annotated[float, Field(gt=0, lt=180, description="field of view, degrees")] = cfg.fov_deg,
        two_point: Annotated[bool, Field(description="keep the camera level so verticals stay vertical")] = True,
        style: Annotated[Literal["shaded", "hidden_line", "lines_only"], Field(description=STYLE_HELP)] = "shaded",
    ) -> dict[str, Any]:
        """Creates (or updates) a scene with an eye-height camera; with two_point the target is levelled to
        the eye so verticals stay vertical."""
        params = {
            "name": name,
            "eye": list(eye),
            "target": list(target),
            "eye_height": eye_height,
            "fov": fov,
            "two_point": two_point,
            "style": style,
        }
        return await b.mutate("create_scene", "create_scene", params, ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def auto_scenes(
        ctx: Context,
        interior: Annotated[bool, Field(description="one scene per room, from a corner at eye height")] = True,
        exterior: Annotated[bool, Field(description="four eye-level corner views and one aerial")] = True,
        storey: Annotated[
            str | None, Field(pattern=STOREY_PATTERN, description="rooms of this storey only; default every storey")
        ] = None,
        style: Annotated[Literal["shaded", "hidden_line", "lines_only"], Field(description=STYLE_HELP)] = "shaded",
        interior_fov: Annotated[float, Field(gt=0, le=120, description="room cameras, vertical degrees")] = 60.0,
        exterior_fov: Annotated[float, Field(gt=0, le=120, description="exterior cameras, vertical degrees")] = 40.0,
        replace: Annotated[bool, Field(description="first erase the scenes an earlier auto_scenes made")] = True,
    ) -> dict[str, Any]:
        """Creates the scenes for rendering without coordinates: I_<storey>_<room> inside every room
        (two-point, from the corner with the longest view across), E1_suroeste..E4_noroeste at eye level
        around the building, and A_aerea. Then export_scene_images renders them all."""
        params: dict[str, Any] = {
            "interior": interior,
            "exterior": exterior,
            "style": style,
            "interior_fov": interior_fov,
            "exterior_fov": exterior_fov,
            "replace": replace,
        }
        return await b.mutate("auto_scenes", "auto_scenes", with_storey(params, storey), ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def export_scene_images(
        dir: Annotated[str, Field(min_length=1, description="folder for the images (created if missing)")],
        ctx: Context,
        width: Annotated[int, Field(ge=64, le=8192, description="px")] = cfg.export_width,
        height: Annotated[int, Field(ge=64, le=8192, description="px")] = cfg.export_height,
        style: Annotated[
            Literal["shaded", "hidden_line", "lines_only"] | None,
            Field(description="override every scene's style; " + STYLE_HELP),
        ] = None,
        format: Annotated[Literal["png", "jpg"], Field(description="image format")] = "png",
    ) -> dict[str, Any]:
        """Writes one image per scene (antialiased) and restores the camera afterwards."""
        params: dict[str, Any] = {"dir": dir, "width": width, "height": height, "format": format}
        if style is not None:
            params["style"] = style
        return await b.mutate("export_scene_images", "export_scene_images", params, ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def export_drawings(
        dir: Annotated[str, Field(min_length=1, description="folder for the images (created if missing)")],
        ctx: Context,
        views: Annotated[
            list[Literal["plans", "elevations", "sections", "axonometrics"]] | None,
            Field(description="which drawings; default all four kinds"),
        ] = None,
        styles: Annotated[
            list[Literal["render", "line"]] | None,
            Field(description="render: textured; line: black hidden-line drawing on white; default both"),
        ] = None,
        px_per_m: Annotated[
            float, Field(ge=20, le=400, description="pixels per metre, shared by every plan, elevation and section")
        ] = 110.0,
        cut_height: Annotated[
            float, Field(gt=0, le=10_000, description="mm above each floor where plans are cut")
        ] = 1200.0,
        sections: Annotated[
            list[DrawingSection] | None,
            Field(
                description="section cuts; default A-A across x and B-B across y through the middle of the buildings"
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Writes the 2D drawings of the model as PNG images, every orthographic one at the same scale:
        a plan cut per storey plus one of the whole plot (planta_N00, planta_lote), the four elevations with
        trees and cars hidden (elevacion_sur...), the sections (corte_AA...) and two axonometrics, one cut
        below the roof. Each comes as <name>_render.png and <name>_linea.png. Camera, render settings, hidden
        entities and section planes are restored afterwards; nothing in the model changes."""
        params: dict[str, Any] = {"dir": dir, "px_per_m": px_per_m, "cut_height": cut_height}
        if views is not None:
            params["views"] = views
        if styles is not None:
            params["styles"] = styles
        if sections is not None:
            params["sections"] = [s.model_dump() for s in sections]
        return await b.mutate("export_drawings", "export_drawings", params, ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def export_model(
        path: Annotated[str, Field(min_length=1, description="output file; the extension follows format")],
        format: Annotated[
            Literal["skp", "fbx", "obj"],
            Field(description="obj for Blender; skp needs a model saved at least once"),
        ],
        ctx: Context,
    ) -> dict[str, Any]:
        """Exports the model; a false return from SketchUp's exporter is reported as an error naming the format.
        For Blender use obj (one-sided, triangulated, materials by name): SketchUp writes FBX as ASCII, which
        Blender's FBX importer refuses."""
        return await b.mutate("export_model", "export_model", {"path": path, "format": format}, ctx)

    # --- housekeeping -----------------------------------------------------------------------

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def reset_plomada(ctx: Context) -> dict[str, Any]:
        """Erases every group, component and scene Plomada made (those carrying a plomada attribute); leaves
        everything else. Never opens a new file."""
        return await b.mutate("reset_plomada", "reset_plomada", {}, ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=False))
    @exact_errors
    async def undo(
        ctx: Context,
        steps: Annotated[int, Field(ge=1, le=10, description="undo steps; each Plomada call is one step")] = 1,
    ) -> dict[str, Any]:
        """Undoes the last steps in SketchUp (Edit > Undo)."""
        return await b.mutate("undo", "undo", {"steps": steps}, ctx)

    @mcp.tool(annotations=_mut(destructive=True, idempotent=True))
    @exact_errors
    async def job_cancel(
        job_id: Annotated[str | None, Field(description="job id from job_status; default the running job")] = None,
    ) -> dict[str, Any]:
        """Cancels a queued job, or stops the running one after its current step and reverts it."""
        params = {"job_id": job_id} if job_id else {}
        try:
            return await b.client.call("cancel", params, read_only=False, deadline_ms=10_000)
        except BridgeError as err:
            raise ToolError(describe(err, "job_cancel")) from None

    @mcp.tool(annotations=_mut(destructive=True, idempotent=False))
    @exact_errors
    async def execute_ruby(
        code: Annotated[str, Field(min_length=1, max_length=200_000, description="Ruby source to run in SketchUp")],
        ctx: Context,
    ) -> dict[str, Any]:
        """Runs Ruby inside SketchUp as one undo step, with a 10 s soft deadline and 64 KiB of captured
        output. Off unless the user enabled it in Extensions > Plomada > Settings; refuses system, exec,
        spawn, fork, backticks, %x, Thread.new, exit and Sketchup.quit."""
        return await b.mutate("execute_ruby", "execute_ruby", {"code": code}, ctx)

    return mcp


def main() -> None:
    create_server().run()
