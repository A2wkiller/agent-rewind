"""
Pytest bootstrap for the TraceGate repository.

- Puts `tests/stubs` on sys.path so the stub `tracegate_mcp` module (which
  re-implements the missing core module's API) can be imported. A real
  tracegate_mcp.py at the repository root takes precedence because pytest
  inserts the root directory ahead of it.
- Supplies the `MANAGER` WebSocket connection manager and
  `configure_sqlite_wal` hook that the source modules reference but do not
  define (the FastAPI app was not part of the source chat export).
No network access or real API keys are used anywhere in the suite.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
STUBS = ROOT / "tests" / "stubs"
if str(STUBS) not in sys.path:
    sys.path.append(str(STUBS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_stream_router  # noqa: E402
import tracegate_bridge  # noqa: E402


class StubConnectionManager:
    """Records broadcasts and holds per-session asyncio pause gates."""

    def __init__(self) -> None:
        self.pause_gates: dict = {}
        self.broadcast_log: list = []

    async def broadcast(self, session_id: str, payload: dict) -> None:
        self.broadcast_log.append((session_id, payload))

    async def broadcast_dag_snapshot(self, session_id: str) -> None:
        return None


for _mod in (tracegate_bridge, llm_stream_router):
    if not hasattr(_mod, "MANAGER"):
        _mod.MANAGER = StubConnectionManager()

if not hasattr(tracegate_bridge, "configure_sqlite_wal"):
    def configure_sqlite_wal() -> None:
        return None
    tracegate_bridge.configure_sqlite_wal = configure_sqlite_wal
