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

BALCONY_EDGE = [[16100, 1000], [16100, 5000, Q], [10000, 11100]]
CASES["balcony"] = {
    "plan": {
        **CASES["slabs"]["plan"],
        "railings": [
            {"id": "BR1", "path": BALCONY_EDGE, "on": "B1", "style": "glass"},
            {"id": "BR2", "path": [[-100, -100], [7000, -100]], "on": "L1", "style": "bars"},
        ],
        "planters": [
            {
                "id": "J1",
                "outline": [[1000, 1000], [5000, 1000, 0.3], [5000, 3000], [1000, 3000]],
                "on": "L1",
                "plants": {"keys": ["ARBUSTO", "MACETA"], "spacing": 700},
            }
        ],
    },
    "options": {"roof": "none", "library": "C:/mcp/biblioteca"},
}

BOX = {"id": "EXT", "axis": [[0, 0], [12000, 0], [12000, 8000], [0, 8000]], "thickness": 250, "closed": True}
CASES["storeys"] = {
    "builds": [
        {
            "plan": {
                "walls": [BOX],
                "ramps": [
                    {"id": "R1", "axis": [[3000, -9000], [3000, 0]], "width": 3000, "z_start": 1500, "z_end": 120}
                ],
            },
            "options": {
                "storey": "SS",
                "elevation": -1500,
                "storey_height": 2350,
                "roof": "none",
                "ground_level_mm": 0,
            },
        },
        {
            "plan": {
                "walls": [{**BOX, "axis": [[0, 0], [12000, 0], [12000, 4000, Q], [8000, 8000], [0, 8000]]}],
                "columns": [{"id": f"C{i}", "at": [2000 + 4000 * i, -2500], "diameter": 1000} for i in range(3)],
                "slabs": [
                    {
                        "id": "L1",
                        "outline": [[-125, -4000], [12125, -4000], [12125, 4000, Q], [8000, 8125], [-125, 8125]],
                        "edge_band": {"height": 400, "depth": 150},
                    }
                ],
                "stairs": [
                    {
                        "id": "S1",
                        "start": [1000, 6000],
                        "direction_deg": 0,
                        "width": 1200,
                        "risers": 26,
                        "riser_height": 182.7,
                        "going": 280,
                    }
                ],
            },
            "options": {"storey": "N00", "elevation": 1000, "storey_height": 4600, "roof": "none"},
        },
        {
            "plan": {
                "walls": [{**BOX, "axis": [[0, 0], [12000, 0], [12000, 4000, Q], [8000, 8000], [0, 8000]]}],
                "slabs": [
                    {"id": "L2", "outline": [[-125, -125], [12125, -125], [12125, 4000, Q], [8000, 8125], [-125, 8125]]}
                ],
                "pools": [{"id": "P1", "outline": [[2000, 1500], [7000, 1500], [7000, 4500], [2000, 4500]]}],
                "railings": [{"id": "BR", "path": [[-125, -125], [12125, -125]], "on": "L2"}],
            },
            "options": {"storey": "N01", "elevation": 5750, "storey_height": 3100, "roof": "flat"},
        },
    ]
}


CASES["arches"] = {
    "plan": {
        "walls": [
            {"id": "EXT", "axis": [[0, 0], [14000, 0], [14000, 8000], [0, 8000]], "thickness": 250, "closed": True}
        ],
        "openings": [
            {
                "id": f"A{i}",
                "wall": "EXT",
                "opening_kind": "window",
                "offset": 1000 + 3000 * i,
                "width": 1900,
                "sill": 400,
                "height": 2300,
                "head": "arch",
            }
            for i in range(4)
        ],
        "curtain_walls": [{"id": "M1", "path": [[14000, 1000], [14000, 7000]], "transoms": [1100]}],
    },
    "options": {"roof": "flat", "storey_height": 3200},
}


async def run(case: str, capture: str | None) -> None:
    spec = CASES[case]
    client = dev.dev_client()
    try:
        await dev.call(client, "reset_plomada", {})
        for build in spec.get("builds", [spec]):
            opts = {"storey": "N00", "furniture": "massing", **build.get("options", {})}
            res = await dev.call(client, "build_plan", {"plan": build["plan"], "options": opts})
            keys = ("storey", "elevation", "wall_faces", "manifold", "groups", "stair_wells", "warnings", "elapsed_ms")
            print(json.dumps({k: res.get(k) for k in keys}))
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
