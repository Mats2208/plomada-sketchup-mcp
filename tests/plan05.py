"""A plan with every 0.5 record kind, arcs and the 0.4 kinds, shared by the 0.5 tests."""

from __future__ import annotations

import copy
from typing import Any

QUARTER = 0.41421356237309503  # tan(90 / 4 degrees): a quarter circle

_FULL: dict[str, Any] = {
    "walls": [
        {
            "id": "EXT",
            "axis": [[0, 0], [12000, 0, 0.25], [12000, 8000], [0, 8000, 0]],
            "thickness": 200,
            "closed": True,
        },
        {
            "id": "C1",
            "axis": [[2000, 2000, QUARTER], [6000, 2000]],
            "thickness": 150,
            "justification": "left",
            "material": "gypsum_board",
            "height": 2600,
        },
        {"id": "RING", "axis": [[20000, 0, 1.0], [24000, 0, 1.0]], "thickness": 200, "closed": True},
    ],
    "openings": [
        {"id": "D1", "wall": "EXT", "opening_kind": "door", "offset": 1000, "width": 900},
        {
            "id": "W1",
            "wall": "EXT",
            "opening_kind": "window",
            "offset": 4000,
            "width": 1500,
            "sill": 900,
            "height": 1200,
            "head": "arch",
            "arch_rise": 400,
        },
        {
            "id": "W2",
            "wall": "C1",
            "opening_kind": "window",
            "offset": 500,
            "width": 1200,
            "panel_width": 600,
            "tag": "V2",
        },
    ],
    "rooms": [{"id": "R1", "name": "LIVING", "number": "01", "at": [3000, 3000], "area": 10000000}],
    "stairs": [
        {
            "id": "S1",
            "start": [8000, 1000],
            "direction_deg": 90,
            "width": 1000,
            "risers": 17,
            "riser_height": 173.5,
            "going": 280,
            "stair_kind": "l",
            "turn": "right",
        }
    ],
    "furniture": [
        {"id": "F1", "item": "sofa_3_seat", "at": [1000, 5000], "rotation": 270},
        {"id": "F2", "item": "Mesa Comedor", "at": [2000.25, 6000.5]},
    ],
    "site": {
        "objects": [{"id": "T1", "item": "ARBOL", "at": [15000, 2000], "rotation": 30}],
        "paving": [{"id": "PV1", "points": [[12000, 0], [18000, 0, -0.3], [18000, 5000], [12000, 5000]]}],
        "decks": [{"id": "DK1", "points": [[0, -3000], [4000, -3000], [4000, -1000], [0, -1000]]}],
        "pools": [
            {"id": "SP1", "points": [[5000, -3000], [9000, -3000], [9000, -1000, 1.0], [5000, -1000]], "depth": 1600}
        ],
        "fences": [
            {"id": "CE1", "points": [[-2000, -2000], [10000, -2000, 0.2], [20000, -2000, 0.5]], "height": 2000},
            {"id": "CE2", "points": [[-500, -500], [500, -500], [500, 500]], "closed": True},
        ],
    },
    "slabs": [
        {
            "id": "L1",
            "outline": [[0, 0], [8000, 0, QUARTER], [12000, 4000], [0, 4000]],
            "holes": [[[1000, 1000], [2000, 1000], [2000, 2000], [1000, 2000]], [[3000, 1000, 1.0], [4000, 1000, 1.0]]],
            "thickness": 180,
            "top_offset": -50,
            "material": "MAT_madera",
            "edge_band": {"height": 450, "depth": 150},
        },
        {"id": "L2", "outline": [[0, 0], [1000, 0], [1000, 1000]]},
    ],
    "railings": [
        {"id": "B1", "path": [[0, 0], [4000, 0, QUARTER], [8000, 4000]], "style": "bars", "on": "L1"},
        {"id": "B2", "path": [[0, 0], [1000, 0], [1000, 1000, -0.2]], "closed": True, "rail": True, "height": 1000},
    ],
    "planters": [
        {
            "id": "J1",
            "outline": [[0, 0], [3000, 0, 0.3], [3000, 1200], [0, 1200]],
            "on": "L1",
            "plants": {"keys": ["hedge", "ARBUSTO"], "spacing": 500, "seed": 7},
        },
        {"id": "J2", "outline": [[0, 0], [1000, 0], [1000, 500], [0, 500]], "height": 300, "curb_thickness": 80},
    ],
    "columns": [
        {"id": "C1", "at": [4000, 2500], "diameter": 600, "height": 3000},
        {"id": "C2", "outline": [[0, 0], [300, 0], [300, 600], [0, 600]], "material": "MAT_acero"},
    ],
    "curtain_walls": [
        {
            "id": "M1",
            "path": [[0, 0], [6000, 0, 0.2]],
            "sill": 100,
            "head": 2700,
            "transoms": [2100, 1100],
            "profile": [60, 120],
            "glass": 12,
        }
    ],
    "pools": [
        {
            "id": "P1",
            "outline": [[0, 0], [8000, 0], [8000, 3500], [0, 3500]],
            "depth": 1200,
            "raised": True,
            "on": "L1",
        },
        {"id": "P2", "outline": [[0, 0], [4000, 0, 0.5], [4000, 2000], [0, 2000]], "coping": 0},
    ],
    "ramps": [
        {"id": "R1", "axis": [[0, 0], [9000, 0]], "width": 3000, "z_end": -1620, "wall_height": 1000},
        {
            "id": "R2",
            "axis": [[0, 0, 0.3], [5000, 0]],
            "width": 1200,
            "z_start": 100,
            "z_end": 600,
            "side_walls": False,
        },
    ],
    "site_on_ground": True,
    "storey": {"name": "N01", "height": 3000, "elevation": 3100},
}


def full_plan() -> dict[str, Any]:
    """A fresh copy of the plan with every record kind."""
    return copy.deepcopy(_FULL)
