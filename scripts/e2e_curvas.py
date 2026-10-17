"""The 0.5 live test: curves end to end through the MCP tools, in the isolated dev SketchUp.

Needs scripts/sketchup-dev.cmd running (port 7884, %LOCALAPPDATA%\\Plomada-dev). Launches the plomada-mcp console
script over stdio with PLOMADA_PORT and PLOMADA_HOME pointing at that instance (the user's SketchUp on 7883 is never
reached), then:

  status (the pid must be the dev instance's) -> reset_plomada -> build_plan: a quarter-circle wall of R 2000 with a
  1500 mm window -> model_info(detail) manifold, no internal faces, two panels -> capture -> get_plan keeps the bulge
  -> write_plan_dxf + build_from_autocad of that DXF give the same walls -> add_slab with an id and a curved outline,
  set_slab adds an edge band -> get_plan lists the slab record

and writes bench/e2e-curvas-<date>.json and docs/img/e2e_curvas_*.jpg.

    uv run python scripts/e2e_curvas.py
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
DEV_PORT = 7884
DEV_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Plomada-dev"
Q = math.tan(math.pi / 8.0)

QUARTER = {
    "walls": [{"id": "M1", "axis": [[2000, 0, Q], [0, 2000]], "thickness": 200}],
    "openings": [
        {"id": "W1", "wall": "M1", "opening_kind": "window", "offset": 800, "width": 1500, "sill": 900, "height": 1200}
    ],
}
BALCONY = [[0, -100], [2100, -100], [2100, 0, Q], [0, 2100], [-100, 2100], [-100, -100]]


def server_params() -> StdioServerParameters:
    exe = ROOT / ".venv" / ("Scripts/plomada-mcp.exe" if os.name == "nt" else "bin/plomada-mcp")
    env = {**os.environ, "PLOMADA_PORT": str(DEV_PORT), "PLOMADA_HOME": str(DEV_HOME)}
    return StdioServerParameters(command=str(exe), args=[], env=env)


def structured(result: Any) -> dict[str, Any]:
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise AssertionError(f"tool error: {text}")
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(next(c.text for c in result.content if getattr(c, "type", "") == "text"))


async def capture(client: Client, name: str, camera: dict[str, Any] | None = None) -> int:
    if camera:
        await client.call_tool("create_scene", camera)
    res = await client.call_tool("capture_view", {"style": "shaded", "view": "current" if camera else "fit"})
    image = next(c for c in res.content if getattr(c, "type", "") == "image")
    data = base64.b64decode(image.data)
    (ROOT / "docs" / "img" / name).write_bytes(data)
    return len(data)


async def run() -> dict[str, Any]:
    report: dict[str, Any] = {"date": dt.datetime.now().isoformat(timespec="seconds"), "checks": {}}
    checks = report["checks"]
    endpoint = json.loads((DEV_HOME / "endpoint.json").read_text(encoding="utf-8"))
    async with Client(server_params(), read_timeout_seconds=180) as client:
        report["tool_count"] = len((await client.list_tools()).tools)
        checks["29 tools"] = report["tool_count"] == 29
        st = structured(await client.call_tool("status", {}))
        report["status"] = {k: st.get(k) for k in ("server_version", "bridge_version", "sketchup_version")}
        checks["talking to the dev instance on 7884"] = endpoint["port"] == DEV_PORT and st.get("pid", endpoint["pid"]) == endpoint["pid"]

        structured(await client.call_tool("reset_plomada", {}))
        t0 = time.perf_counter()
        build = structured(
            await client.call_tool("build_plan", {"plan": QUARTER, "slab": False, "roof": "none", "furniture": "massing"})
        )
        report["quarter_build_s"] = round(time.perf_counter() - t0, 3)
        report["quarter"] = {k: build.get(k) for k in ("wall_faces", "manifold", "internal_faces_removed", "warnings")}
        info = structured(await client.call_tool("model_info", {"detail": True}))["detail"]
        report["quarter_detail"] = {"walls": info["walls"], "windows": info["windows"], "glass_panes": info["glass_panes"]}
        checks["quarter wall R 2000 manifold"] = all(w["manifold"] for w in info["walls"])
        checks["zero internal faces"] = all(w["internal_faces"] == 0 for w in info["walls"])
        checks["1500 window as 2 flat panels"] = info["windows"] == 2 and info["glass_panes"] == 2
        cam = {"name": "e2e_curvas", "eye": [-1800, -2600], "eye_height": 2600, "target": [1100, 1100, 1100], "fov": 45}
        report["capture_bytes"] = await capture(client, "e2e_curvas_cuarto.jpg", cam)

        plan = structured(await client.call_tool("get_plan", {}))
        axis = plan["walls"][0]["axis"]
        checks["get_plan keeps the bulge"] = len(axis[0]) == 3 and abs(axis[0][2] - Q) < 1e-9
        checks["get_plan reads one opening record for two panels"] = [o["id"] for o in plan["openings"]] == ["W1"]

        dxf = Path(tempfile.gettempdir()) / "plomada_e2e_curvas.dxf"
        written = structured(await client.call_tool("write_plan_dxf", {"plan": QUARTER, "path": str(dxf)}))
        report["write_plan_dxf"] = {k: written.get(k) for k in ("entities", "records", "layers")}
        again = structured(
            await client.call_tool(
                "build_from_autocad", {"dxf_path": str(dxf), "slab": False, "roof": "none", "furniture": "massing"}
            )
        )
        checks["DXF route builds the same walls"] = again["wall_faces"] == build["wall_faces"] and again["manifold"]

        slab = structured(await client.call_tool("add_slab", {"id": "B1", "outline": BALCONY}))
        report["add_slab"] = slab
        band = structured(await client.call_tool("set_slab", {"id": "B1", "edge_band": {"height": 450, "depth": 150}}))
        report["set_slab"] = band
        plan = structured(await client.call_tool("get_plan", {}))
        slabs = plan.get("slabs", [])
        checks["slab record with a curved outline and a band"] = (
            len(slabs) == 1 and slabs[0]["edge_band"] == {"height": 450.0, "depth": 150.0}
            and any(len(v) == 3 for v in slabs[0]["outline"])
        )
        checks["band group built"] = any(g.startswith("N00_canto_B1") for g in band.get("groups", []))
        report["capture_slab_bytes"] = await capture(client, "e2e_curvas_losa.jpg")

    report["passed"] = all(checks.values())
    return report


def main() -> int:
    report = asyncio.run(run())
    out = ROOT / "bench" / f"e2e-curvas-{dt.date.today().isoformat()}.json"
    text = json.dumps(report, indent=2, ensure_ascii=False)
    out.write_text(text.replace(Path.home().as_posix(), "~") + "\n", encoding="utf-8")
    for name, ok in report["checks"].items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"report: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
