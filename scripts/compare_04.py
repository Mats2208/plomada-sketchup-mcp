"""Builds a 0.4 fixture in the dev SketchUp and records its geometry, to compare two extension versions.

    uv run python scripts/compare_04.py record <label>     # writes bench/compare_04-<label>.json
    uv run python scripts/compare_04.py diff <label_a> <label_b>

Start the dev SketchUp on the version to record (scripts/sketchup-dev.cmd for this worktree; for 0.4.0 set
PLOMADA_DEV_DIR to the main checkout's extension\\plomada first). The fixture is tests/fixtures/casa_completa_N00.dxf,
read by this worktree's bridge (its wire is byte-identical to 0.4's, see tests/fixtures/wire_0.4_snapshot.json) and
built with the build_plan defaults. Recorded per top-level group: faces, edges and bounding box (mm), and the model's
bounding box.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dev
from plomada_bridge.dxf import read_plan

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "casa_completa_N00.dxf"

PROBE = """
m = Sketchup.active_model
mm = ->(v) { (v.to_f * 25.4).round(1) }
box = ->(bb) { [bb.min.to_a.map(&mm), bb.max.to_a.map(&mm)] }
count = lambda do |ents|
  f = ents.grep(Sketchup::Face).size
  e = ents.grep(Sketchup::Edge).size
  ents.grep(Sketchup::Group).each { |g| a, b = count.call(g.entities); f += a; e += b }
  ents.grep(Sketchup::ComponentInstance).each { |i| a, b = count.call(i.definition.entities); f += a; e += b }
  [f, e]
end
top = [Sketchup::Group, Sketchup::ComponentInstance]
plomada = ->(x) { top.any? { |k| x.is_a?(k) } && x.attribute_dictionary('plomada') }
inner = ->(g) { g.is_a?(Sketchup::Group) ? g.entities : g.definition.entities }
rows = m.entities.select(&plomada).map { |g| [g.name, *count.call(inner.call(g)), box.call(g.bounds)] }
rows = rows.sort_by { |r| [r[0], r[3].inspect] }
{ 'version' => Plomada::VERSION, 'groups' => rows, 'model_bbox' => box.call(m.bounds) }.to_json
"""


async def record(label: str) -> Path:
    client = dev.dev_client()
    try:
        await dev.call(client, "reset_plomada", {})
        plan = read_plan(FIXTURE).plan.to_wire()
        opts = {"storey": "N00", "furniture": "massing", "storey_height": 2800, "roof": "flat", "fit_view": False}
        build = await dev.call(client, "build_plan", {"plan": plan, "options": opts})
        res = await dev.ruby(client, PROBE)
        data = json.loads(json.loads(res["result"]))
        data["build"] = {k: build.get(k) for k in ("wall_faces", "manifold", "internal_faces_removed", "groups")}
    finally:
        await client.close()
    out = ROOT / "bench" / f"compare_04-{label}.json"
    out.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    return out


def diff(a: str, b: str) -> int:
    da = json.loads((ROOT / "bench" / f"compare_04-{a}.json").read_text(encoding="utf-8"))
    db = json.loads((ROOT / "bench" / f"compare_04-{b}.json").read_text(encoding="utf-8"))
    same = True
    for key in ("groups", "model_bbox"):
        if da[key] != db[key]:
            same = False
            print(f"DIFF {key}:\n  {a}: {da[key]}\n  {b}: {db[key]}")
    faces = sum(g[1] for g in da["groups"]), sum(g[1] for g in db["groups"])
    print(
        f"{a} {da['version']} vs {b} {db['version']}: faces {faces[0]} / {faces[1]}, bbox {da['model_bbox']} / "
        f"{db['model_bbox']} -> {'IDENTICAL' if same else 'DIFFERENT'}"
    )
    return 0 if same else 1


if __name__ == "__main__":
    if sys.argv[1] == "record":
        print(asyncio.run(record(sys.argv[2])))
    else:
        sys.exit(diff(sys.argv[2], sys.argv[3]))
