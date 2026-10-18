"""Route A without AutoCAD: AutoCAD MCP Pro's ezdxf backend draws a plan that Plomada reads exactly.

Needs AutoCAD MCP Pro installed (``pip install autocad-mcp-pro``, or a checkout with its venv) but no
AutoCAD and no SketchUp. Launches its server over stdio with AUTOCAD_MCP_BACKEND=ezdxf and
TOOL_PACKS=core,arch, draws a two-room house with arch_plan_from_spec, saves it as DXF, reads it with
plomada_bridge.dxf.read_plan and asserts the walls, openings and rooms came through unchanged.

    uv run python scripts/e2e_acadpro_ezdxf.py [path/to/autocad-mcp.exe]
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from plomada_bridge.dxf import read_plan

SPEC = {
    "walls": [
        {"id": "EXT", "axis": [[0, 0], [10000, 0], [10000, 8000], [0, 8000]], "thickness": 200, "closed": True},
        {"id": "INT", "axis": [[6000, 0], [6000, 8000]], "thickness": 120},
    ],
    "openings": [
        {"id": "D1", "wall": "EXT", "kind": "door", "offset": 2000, "width": 900},
        {"id": "W1", "wall": "EXT", "kind": "window", "offset": 11000, "width": 1500, "sill": 900, "height": 1200},
        {"id": "D2", "wall": "INT", "kind": "door", "offset": 3000, "width": 800},
    ],
    "rooms": [{"id": "R1", "name": "Living", "at": [3000, 4000]}, {"id": "R2", "name": "Bedroom", "at": [8000, 4000]}],
}


def server_command(argv: list[str]) -> str:
    if len(argv) > 1:
        return argv[1]
    found = shutil.which("autocad-mcp") or "C:/mcp/autocad-mcp-pro/.venv/Scripts/autocad-mcp.exe"
    if not Path(found).is_file():
        sys.exit("autocad-mcp not found: pip install autocad-mcp-pro, or pass the executable's path")
    return found


async def draw(command: str, out: Path) -> None:
    env = {"AUTOCAD_MCP_BACKEND": "ezdxf", "TOOL_PACKS": "core,arch", "ALLOWED_PATHS": str(out.parent)}
    env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "C:\\Windows")
    params = StdioServerParameters(command=command, env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        steps = [("drawing_new", {}), ("arch_plan_from_spec", {"spec": SPEC}), ("drawing_save_as", {"path": str(out)})]
        for name, args in steps:
            res = await s.call_tool(name, args)
            if res.is_error:
                text = " ".join(getattr(c, "text", "") for c in res.content)
                sys.exit(f"{name} failed: {text}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "acadpro_ezdxf.dxf"
        asyncio.run(draw(server_command(sys.argv), out))
        plan = read_plan(out).plan
    walls = {w.id: (w.thickness, w.closed, len(w.axis)) for w in plan.walls}
    openings = {o.id: (o.wall, o.opening_kind, o.offset, o.width) for o in plan.openings}
    assert walls == {"EXT": (200.0, True, 4), "INT": (120.0, False, 2)}, walls
    assert openings == {
        "D1": ("EXT", "door", 2000.0, 900.0),
        "W1": ("EXT", "window", 11000.0, 1500.0),
        "D2": ("INT", "door", 3000.0, 800.0),
    }, openings
    assert [r.name for r in plan.rooms] == ["Living", "Bedroom"] and all(r.area > 0 for r in plan.rooms)
    print(f"ok: {len(walls)} walls, {len(openings)} openings, {len(plan.rooms)} rooms read from the ezdxf drawing")


if __name__ == "__main__":
    main()
