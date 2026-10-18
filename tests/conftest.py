"""Shared fixtures: an asyncio fake of the SketchUp extension that speaks the
real framing (4-byte length + JSON-RPC), so the bridge is tested without SketchUp."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from plomada_bridge.config import BridgeConfig

FIXTURES = Path(__file__).parent / "fixtures"
DXF = FIXTURES / "casa_minimalista.dxf"
TOKEN = "c0ffee" * 10 + "c0fe"  # 64 hex characters


class Reject(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


@dataclasses.dataclass
class FakeExtension:
    token: str = TOKEN
    answer_hello: bool = True
    handlers: dict[str, Callable[[dict[str, Any]], Any]] = dataclasses.field(default_factory=dict)
    drop_first: dict[str, int] = dataclasses.field(default_factory=dict)  # method -> connections to drop
    silent: set[str] = dataclasses.field(default_factory=set)  # methods never answered
    progress: dict[str, int] = dataclasses.field(default_factory=dict)  # method -> notifications before result
    seen: list[tuple[str, dict[str, Any]]] = dataclasses.field(default_factory=list)
    hellos: int = 0
    port: int = 0
    _server: asyncio.base_events.Server | None = None

    async def start(self) -> FakeExtension:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def methods(self) -> list[str]:
        return [m for m, _ in self.seen]

    async def _send(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        body = json.dumps(msg).encode()
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                (size,) = struct.unpack(">I", await reader.readexactly(4))
                msg = json.loads(await reader.readexactly(size))
                method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
                self.seen.append((method, params))
                if method == "hello":
                    self.hellos += 1
                    if not self.answer_hello:
                        continue
                    if params.get("token") != self.token:
                        await self._send(
                            writer,
                            {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": "token rejected"}},
                        )
                        break
                    await self._send(
                        writer,
                        {
                            "jsonrpc": "2.0",
                            "id": rid,
                            "result": {
                                "server_version": "0.1.0",
                                "protocol": 1,
                                "client_id": f"c{self.hellos}",
                                "capabilities": {"pro": True, "entities_build": True, "pbr": True, "fbx_export": True},
                            },
                        },
                    )
                    continue
                if self.drop_first.get(method, 0) > 0:
                    self.drop_first[method] -= 1
                    break
                if method in self.silent:
                    continue
                for i in range(self.progress.get(method, 0)):
                    await self._send(
                        writer,
                        {
                            "jsonrpc": "2.0",
                            "method": "progress",
                            "params": {
                                "id": rid,
                                "progress": i + 1,
                                "total": self.progress[method],
                                "message": f"step {i + 1}",
                            },
                        },
                    )
                handler = self.handlers.get(method, lambda p, m=method: {"ok": True, "method": m})
                try:
                    result = handler(params)
                    await self._send(writer, {"jsonrpc": "2.0", "id": rid, "result": result})
                except Reject as exc:
                    err = {"code": exc.code, "message": exc.message}
                    if exc.data is not None:
                        err["data"] = exc.data
                    await self._send(writer, {"jsonrpc": "2.0", "id": rid, "error": err})
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def make_config(tmp_path: Path, port: int, **overrides: Any) -> BridgeConfig:
    (tmp_path / "bridge.token").write_text(TOKEN, encoding="ascii")
    base = dict(
        port=port,
        data_dir=tmp_path,
        connect_timeout_s=0.5,
        status_timeout_s=0.5,
        read_timeout_s=1.0,
        mutation_grace_s=0.3,
        default_deadline_ms=500,
        library_dir=None,
    )
    base.update(overrides)
    return BridgeConfig(**base)


@pytest.fixture
def run() -> Callable[..., Any]:
    def _run(coro: Any) -> Any:
        return asyncio.run(coro)

    return _run


def broken_dxfs(folder: Path) -> list[Path]:
    """Two damaged copies of casos_prueba.dxf that ezdxf rejects with neither OSError nor DXFStructureError:
    cut off inside its header (StopIteration) and with an entity handle garbled (ValueError)."""
    lines = (FIXTURES / "casos_prueba.dxf").read_bytes().split(b"\n")
    cut = folder / "cortado.dxf"
    cut.write_bytes(b"\n".join(lines[:748]))
    start = next(i for i, ln in enumerate(lines) if ln.strip() == b"ENTITIES")
    handle = next(i for i in range(start, len(lines)) if lines[i].strip() == b"5") + 1
    garbled = folder / "ilegible.dxf"
    garbled.write_bytes(b"\n".join([*lines[:handle], b"garbage\xff", *lines[handle + 1 :]]))
    return [cut, garbled]
