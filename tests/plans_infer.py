"""Known plans for the inference tests (mm): drawn by tests/drafter.py, read back by plomada_bridge.infer."""

from __future__ import annotations

import math
from typing import Any

from plomada_bridge.models import Plan, parse_plan


def _wall(id: str, axis: list, t: float, closed: bool = False) -> dict[str, Any]:
    return {"id": id, "axis": axis, "thickness": t, "closed": closed}


def _op(id: str, wall: str, kind: str, offset: float, width: float, swing: str = "in", hand: str = "left") -> dict:
    return {
        "id": id,
        "wall": wall,
        "opening_kind": kind,
        "offset": offset,
        "width": width,
        "swing": swing,
        "hand": hand,
    }


def _room(id: str, name: str, at: tuple[float, float], area: float = 0.0) -> dict[str, Any]:
    return {"id": id, "name": name, "at": at, "area": area}


def house() -> Plan:
    """12 x 8 m, a 150 partition teeing into both long walls, a 100 partition teeing into it and the east wall."""
    return parse_plan(
        {
            "walls": [
                _wall("EXT", [[0, 0], [12000, 0], [12000, 8000], [0, 8000]], 200, True),
                _wall("P1", [[6000, 0], [6000, 8000]], 150),
                _wall("P2", [[6000, 4000], [12000, 4000]], 100),
            ],
            "openings": [
                _op("D1", "EXT", "door", 2000, 1000),
                _op("V1", "EXT", "window", 7500, 2000),
                _op("V2", "EXT", "window", 13500, 1500),
                _op("V3", "EXT", "window", 22000, 2500),
                _op("V4", "EXT", "window", 35000, 1200),
                _op("D2", "P1", "door", 1000, 900, "out", "right"),
                _op("D3", "P1", "door", 5500, 900),
                _op("D4", "P2", "door", 2000, 800, "in", "right"),
            ],
            "rooms": [
                _room("R1", "LIVING", (3000, 4000), 46.5e6),
                _room("R2", "DORMITORIO", (9000, 2000), 23.1e6),
                _room("R3", "BAÑO", (9000, 6000), 22.9e6),
            ],
        }
    )


def l_house() -> Plan:
    """An L with a reflex corner, a partition across each arm."""
    return parse_plan(
        {
            "walls": [
                _wall("EXT", [[0, 0], [10000, 0], [10000, 5000], [5000, 5000], [5000, 10000], [0, 10000]], 200, True),
                _wall("Q1", [[0, 6000], [5000, 6000]], 120),
                _wall("Q2", [[7000, 0], [7000, 5000]], 120),
            ],
            "openings": [
                _op("D1", "EXT", "door", 1500, 1000, "in", "right"),
                _op("V1", "EXT", "window", 8000, 1500),
                _op("V2", "EXT", "window", 11500, 2000),
                _op("V3", "EXT", "window", 21500, 1800),
                _op("V4", "EXT", "window", 27000, 1500),
                _op("D2", "Q1", "door", 2000, 900, "out", "left"),
                _op("D3", "Q2", "door", 3000, 800),
            ],
            "rooms": [
                _room("R1", "ESTAR", (2500, 3000)),
                _room("R2", "COCINA", (8500, 2500)),
                _room("R3", "DORMITORIO", (2500, 8000)),
            ],
        }
    )


def curved() -> Plan:
    """A house whose south wall bows out as one arc (bulge -0.4) meeting the side walls at 46 degrees."""
    return parse_plan(
        {
            "walls": [
                _wall("EXT", [[0, 0, -0.4], [10000, 0], [10000, 6000], [0, 6000]], 250, True),
                _wall("P", [[0, 3000], [10000, 3000]], 120),
            ],
            "openings": [
                _op("V1", "EXT", "window", 1500, 1500),
                _op("V2", "EXT", "window", 7500, 1500),
                _op("D1", "EXT", "door", 12000, 1000),
                _op("D2", "P", "door", 4000, 900),
            ],
            "rooms": [_room("R1", "SALA", (5000, 1000)), _room("R2", "ESTUDIO", (5000, 4500))],
        }
    )


def box() -> Plan:
    """One rectangle, three windows and a door: the closed-outline style."""
    return parse_plan(
        {
            "walls": [_wall("EXT", [[0, 0], [9000, 0], [9000, 7000], [0, 7000]], 200, True)],
            "openings": [
                _op("D1", "EXT", "door", 4000, 1000),
                _op("V1", "EXT", "window", 1000, 2000),
                _op("V2", "EXT", "window", 11000, 2000),
                _op("V3", "EXT", "window", 18000, 3000),
            ],
            "rooms": [_room("R1", "TALLER", (4500, 3500))],
        }
    )


def moved(plan: Plan, deg: float = 0.0, dx: float = 0.0, dy: float = 0.0) -> Plan:
    """The plan rotated deg about the origin, then shifted; offsets along the walls do not change."""
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))

    def tr(p: Any) -> list[float]:
        return [p[0] * c - p[1] * s + dx, p[0] * s + p[1] * c + dy]

    wire = plan.to_wire()
    for w in wire["walls"]:
        w["axis"] = [[*tr(v), *v[2:]] for v in w["axis"]]
    for r in wire["rooms"]:
        r["at"] = tr(r["at"])
    return parse_plan(wire)


def corners() -> Plan:
    """Openings hard against junctions: a door against a tee's face, a double door, a sliding door."""
    return parse_plan(
        {
            "walls": [
                _wall("EXT", [[0, 0], [10000, 0], [10000, 7000], [0, 7000]], 200, True),
                _wall("P", [[5000, 0], [5000, 7000]], 120),
                _wall("Q", [[5000, 3500], [10000, 3500]], 100),
            ],
            "openings": [
                _op("D1", "EXT", "door", 1000, 1600),  # drawn as a double door
                _op("D2", "Q", "door", 60, 800, "out", "left"),  # against P's face
                _op("D3", "P", "door", 1000, 900),  # drawn as a sliding door: no arc
                _op("V1", "EXT", "window", 6000, 2000),
            ],
            "rooms": [_room("R1", "ESTAR", (2500, 3500))],
        }
    )


def mixed() -> Plan:
    """A 250 mm front wall meeting a 150 mm U of the other three sides end to end at both corners, and a
    partition crossing the room in an X with another."""
    return parse_plan(
        {
            "walls": [
                _wall("F", [[0, 0], [8000, 0]], 250),
                _wall("U", [[8000, 0], [8000, 6000], [0, 6000], [0, 0]], 150),
                _wall("X1", [[0, 3000], [8000, 3000]], 100),
                _wall("X2", [[4000, 0], [4000, 6000]], 100),
            ],
            "openings": [
                _op("D1", "F", "door", 1000, 1000),
                _op("V1", "U", "window", 1500, 1200),
                _op("D2", "X1", "door", 1000, 800, "out", "right"),
                _op("D3", "X2", "door", 4000, 800),
            ],
            "rooms": [_room("R1", "COCINA", (2000, 1500)), _room("R2", "PATIO", (6000, 4500))],
        }
    )
