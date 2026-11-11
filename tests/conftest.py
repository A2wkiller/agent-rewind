"""Shared fixtures for the TraceGate test suite (offline stub store)."""
from __future__ import annotations

import pytest

import tracegate_mcp as tg


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated fresh SQLite DAG store per test."""
    fresh = tg.TraceGateStore(tmp_path / "test_tracegate.sqlite3")
    monkeypatch.setattr(tg, "STORE", fresh)
    return fresh


def make_session(sid: str, ceiling: int = 8000, model: str = "claude-sonnet-5-5"):
    """Create a preflight session and pin its budget ceiling."""
    pre = tg.preflight_estimate_and_match_skill(
        user_prompt=f"prompt for {sid}", model=model, session_id=sid
    )
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET approved_budget_ceiling = ? WHERE node_id = ?",
            (ceiling, pre["preflight_lock_node_id"]),
        )
    return pre
