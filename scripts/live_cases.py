"""Live checks of the 0.5 records in the dev SketchUp (scripts/sketchup-dev.cmd, port 7884), one case at a time.

    uv run python scripts/live_cases.py <case> [--capture <name>]

Each case resets Plomada's objects, builds one plan through build_plan on the wire (the extension normalizes it,
exactly as the MCP tool sends it), then prints the build summary and model_info(detail=true) for the walls, and
optionally saves a shaded capture to docs/img/<name>.jpg. Cases are small, readable plans that exercise one
feature each; scripts/e2e_curvas.py is the end-to-end run through the MCP tools.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dev

ROOT = Path(__file__).resolve().parents[1]
Q = math.tan(math.pi / 8.0)  # bulge of a quarter circle


def rect(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def wing() -> dict[str, Any]:
    """A curved hotel wing: a 14 x 9 m box whose north-east corner is a quarter circle of R 4000."""
    return {
        "walls": [
            {
                "id": "EXT",
                "axis": [[0, 0], [14000, 0], [14000, 5000, Q], [10000, 9000], [0, 9000]],
                "thickness": 200,
                "closed": True,
            },
            {"id": "P1", "axis": [[7000, 0], [7000, 9000]], "thickness": 120},
        ],
        "openings": [
            {
                "id": "W1",
                "wall": "EXT",
                "opening_kind": "window",
                "offset": 15500,
                "width": 2400,
                "sill": 0,
                "height": 2400,
            },
            {"id": "W2", "wall": "EXT", "opening_kind": "window", "offset": 2000, "width": 1500},
            {"id": "D1", "wall": "P1", "opening_kind": "door", "offset": 4000, "width": 900},
        ],
    }


CASES: dict[str, dict[str, Any]] = {
    "quarter": {
        "plan": {
            "walls": [{"id": "M1", "axis": [[2000, 0, Q], [0, 2000]], "thickness": 200}],
            "openings": [
                {
                    "id": "W1",
                    "wall": "M1",
                    "opening_kind": "window",
                    "offset": 800,
                    "width": 1500,
                    "sill": 900,
                    "height": 1200,
                }
            ],
        },
        "options": {"slab": False, "roof": "none"},
    },
    "slabs": {
        "plan": {
            **wing(),
            "slabs": [
                {"id": "L1", "outline": [[-100, -100], [14100, -100], [14100, 5000, Q], [10000, 9100], [-100, 9100]]},
                {
                    "id": "B1",
                    "outline": [
                        [14100, 1000],
                        [16100, 1000],
                        [16100, 5000, Q],
                        [10000, 11100],
                        [10000, 9100],
                        [14100, 5000],
                    ],
                    "edge_band": {"height": 450, "depth": 150},
                },
            ],
        },
        "options": {"roof": "flat"},
    },
}


async def run(case: str, capture: str | None) -> None:
    spec = CASES[case]
    client = dev.dev_client()
    try:
        await dev.call(client, "reset_plomada", {})
        opts = {"storey": "N00", "furniture": "massing", **spec.get("options", {})}
        res = await dev.call(client, "build_plan", {"plan": spec["plan"], "options": opts})
        print(json.dumps({k: res[k] for k in ("wall_faces", "manifold", "groups", "warnings", "elapsed_ms")}, indent=1))
        info = await dev.call(client, "model_info", {"detail": True})
        print(json.dumps(info["detail"]["walls"], indent=1))
        if capture:
            cap = await dev.call(
                client, "capture_view", {"width": 1280, "height": 720, "style": "shaded", "view": "fit"}
            )
            out = ROOT / "docs" / "img" / f"{capture}.jpg"
            out.write_bytes(base64.b64decode(cap["data_base64"]))
            print("capture", out)
    finally:
        await client.close()


if __name__ == "__main__":
    name = sys.argv[1]
    cap = sys.argv[sys.argv.index("--capture") + 1] if "--capture" in sys.argv else None
    asyncio.run(run(name, cap))
