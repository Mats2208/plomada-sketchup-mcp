"""Talks to the isolated development SketchUp (scripts/sketchup-dev.cmd), never to the user's.

Sets PLOMADA_PORT=7884 and PLOMADA_HOME=%LOCALAPPDATA%\\Plomada-dev before importing the bridge, and refuses
to run when the announced endpoint is on any other port. Usable as a library (``dev_client``, ``call``) and as
a command line:

    uv run python scripts/dev.py ruby "Plomada::PLUGIN_DIR"
    uv run python scripts/dev.py reload
    uv run python scripts/dev.py call model_info '{"detail": true}'
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

DEV_PORT = 7884
DEV_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Plomada-dev"
os.environ["PLOMADA_PORT"] = str(DEV_PORT)
os.environ["PLOMADA_HOME"] = str(DEV_HOME)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from plomada_bridge.config import load_config  # noqa: E402
from plomada_bridge.connection import SketchUpClient  # noqa: E402

READS = {"model_info", "list_entities", "list_tags", "list_materials", "get_plan", "capture_view", "status"}


def dev_client() -> SketchUpClient:
    cfg = load_config()
    if cfg.port != DEV_PORT or Path(cfg.data_dir) != DEV_HOME:
        raise SystemExit(f"refusing: config points at port {cfg.port}, {cfg.data_dir}; expected {DEV_PORT}, {DEV_HOME}")
    return SketchUpClient(cfg)


async def call(client: SketchUpClient, method: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
    return await client.call(method, params or {}, read_only=method in READS, **kw)


async def ruby(client: SketchUpClient, code: str) -> Any:
    return await call(client, "execute_ruby", {"code": code})


async def reload(client: SketchUpClient) -> Any:
    """Plomada::App.reload! restarts the server, so the reply never comes: fire it and reconnect."""
    try:
        await asyncio.wait_for(ruby(client, "UI.start_timer(0.2, false) { Plomada::App.reload! }; :scheduled"), 10)
    finally:
        await client.close()
    for _ in range(50):
        await asyncio.sleep(0.2)
        fresh = dev_client()
        try:
            out = await ruby(fresh, "[Plomada::VERSION, Plomada::PLUGIN_DIR]")
            await fresh.close()
            return out
        except Exception:  # the server is still restarting
            await fresh.close()
    raise SystemExit("the dev server did not come back after reload!")


async def _main(argv: list[str]) -> None:
    client = dev_client()
    try:
        if argv[0] == "ruby":
            out = await ruby(client, argv[1])
        elif argv[0] == "reload":
            out = await reload(client)
        else:
            out = await call(client, argv[1], json.loads(argv[2]) if len(argv) > 2 else {})
        print(json.dumps(out, indent=2, ensure_ascii=False))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
