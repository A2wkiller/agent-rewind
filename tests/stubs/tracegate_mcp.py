"""
Test stub for the missing `tracegate_mcp` module.

The real tracegate_mcp.py (FastMCP server + SQLite DAG store + pre-flight
token estimator) was not present in the source Gemini chat export, so this
stub re-implements the exact API surface that the other modules and the
original test suite use, backed by a real on-disk SQLite database.

It is ONLY used by the test suite (see conftest.py): if a real
tracegate_mcp.py is added to the repository root it takes precedence.
No network calls, no LLM provider calls, no secrets.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

USD_TO_INR = float(os.environ.get("TRACEGATE_USD_TO_INR", "86.50"))

# USD per-token list prices used purely for ledger bookkeeping in tests.
_USD_PER_TOKEN = {
    "input": 3.0 / 1_000_000,
    "thought": 15.0 / 1_000_000,
    "output": 15.0 / 1_000_000,
}


class _TokenEngine:
    """Deterministic offline token estimator (~4 chars per token)."""

    @staticmethod
    def count_tokens(text: Optional[str]) -> int:
        if not text:
            return 0
        return max(1, (len(text) + 3) // 4)


TOKEN_ENGINE = _TokenEngine()


class TraceGateStore:
    """Minimal SQLite-backed DAG store matching the real module's API."""

    def __init__(self, db_path: Path | str = "tracegate_dag.sqlite3") -> None:
        self.db_path = str(db_path)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_sessions (
                    session_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    total_session_tokens INTEGER DEFAULT 0,
                    total_session_usd REAL DEFAULT 0.0,
                    active_head_node_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dag_nodes (
                    node_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    parent_node_id TEXT,
                    tree_path TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'COMPLETED',
                    delta_input_tokens INTEGER DEFAULT 0,
                    delta_thought_tokens INTEGER DEFAULT 0,
                    delta_output_tokens INTEGER DEFAULT 0,
                    cumulative_branch_tokens INTEGER DEFAULT 0,
                    cumulative_branch_usd REAL DEFAULT 0.0,
                    approved_budget_ceiling INTEGER DEFAULT 8000,
                    payload_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )

    def match_relevant_skills(self, query: str, top_k: int = 3) -> Dict[str, Any]:
        # No skill library is indexed in the stub; always an empty match list.
        return {"matched_skills": [], "query": query, "top_k": top_k}


STORE = TraceGateStore(os.environ.get("TRACEGATE_DB_PATH", "tracegate_dag.sqlite3"))


def _new_node_id() -> str:
    return uuid.uuid4().hex[:12]


def _token_bucket(node_type: str) -> str:
    if node_type == "THOUGHT_BLOCK":
        return "thought"
    if node_type in ("TOOL_CALL", "ASSISTANT_OUTPUT"):
        return "output"
    return "input"


def _insert_dag_node(
    conn: sqlite3.Connection,
    session_id: str,
    parent_node_id: Optional[str],
    node_type: str,
    status: str,
    delta_in: int,
    delta_thought: int,
    delta_out: int,
    ceiling: int,
    step_usd: float,
    payload: Dict[str, Any],
) -> str:
    node_id = _new_node_id()
    parent_path = ""
    parent_cum_tokens = 0
    parent_cum_usd = 0.0
    if parent_node_id:
        parent = conn.execute(
            "SELECT tree_path, cumulative_branch_tokens, cumulative_branch_usd "
            "FROM dag_nodes WHERE node_id = ?",
            (parent_node_id,),
        ).fetchone()
        if parent:
            parent_path = parent["tree_path"]
            parent_cum_tokens = parent["cumulative_branch_tokens"]
            parent_cum_usd = parent["cumulative_branch_usd"]
    tree_path = f"{parent_path}/{node_id}" if parent_path else node_id
    cum_tokens = parent_cum_tokens + delta_in + delta_thought + delta_out
    cum_usd = parent_cum_usd + step_usd
    conn.execute(
        """
        INSERT INTO dag_nodes (
            node_id, session_id, parent_node_id, tree_path, node_type, status,
            delta_input_tokens, delta_thought_tokens, delta_output_tokens,
            cumulative_branch_tokens, cumulative_branch_usd,
            approved_budget_ceiling, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            session_id,
            parent_node_id,
            tree_path,
            node_type,
            status,
            delta_in,
            delta_thought,
            delta_out,
            cum_tokens,
            cum_usd,
            ceiling,
            json.dumps(payload),
            time.time(),
        ),
    )
    return node_id


def _ensure_session(conn: sqlite3.Connection, session_id: str, model: str = "claude-sonnet-5-5", title: str = "") -> None:
    row = conn.execute(
        "SELECT session_id FROM trace_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO trace_sessions (session_id, model, title, created_at) VALUES (?, ?, ?, ?)",
            (session_id, model, title, time.time()),
        )


def preflight_estimate_and_match_skill(
    user_prompt: str,
    model: str,
    session_id: Optional[str] = None,
    system_and_history_tokens: int = 0,
    cached_prefix_tokens: int = 0,
    tool_schema_tokens: int = 0,
) -> Dict[str, Any]:
    """Creates the session, USER_PROMPT node and PREFLIGHT_LOCK node."""
    sid = session_id or uuid.uuid4().hex[:12]
    prompt_tokens = TOKEN_ENGINE.count_tokens(user_prompt)
    estimated_input = system_and_history_tokens + tool_schema_tokens + prompt_tokens
    ceiling = 8000
    with STORE._conn() as conn:
        _ensure_session(conn, sid, model=model, title=user_prompt[:60])
        prompt_node_id = _insert_dag_node(
            conn, sid, None, "USER_PROMPT", "COMPLETED",
            prompt_tokens, 0, 0, ceiling,
            prompt_tokens * _USD_PER_TOKEN["input"],
            {"content": user_prompt},
        )
        lock_node_id = _insert_dag_node(
            conn, sid, prompt_node_id, "PREFLIGHT_LOCK", "COMPLETED",
            0, 0, 0, ceiling, 0.0,
            {
                "approved_ceiling": ceiling,
                "estimated_input_tokens": estimated_input,
                "cached_prefix_tokens": cached_prefix_tokens,
            },
        )
        conn.execute(
            "UPDATE trace_sessions SET active_head_node_id = ? WHERE session_id = ?",
            (lock_node_id, sid),
        )
    card = (
        "### Pre-Flight Handshake\n"
        f"- Session: `{sid}`\n"
        f"- Model: `{model}`\n"
        f"- Estimated input tokens: {estimated_input}\n"
        f"- Approved ceiling: {ceiling} tokens\n"
    )
    return {
        "session_id": sid,
        "preflight_lock_node_id": lock_node_id,
        "handshake_card_markdown": card,
        "details": {
            "estimated_input_tokens": estimated_input,
            "skill_routing": {"matched_skills": []},
        },
    }


def record_dag_step(
    session_id: str,
    parent_node_id: str,
    node_type: str,
    content: str,
    tool_name: Optional[str] = None,
    provider_signature: Optional[str] = None,
) -> Dict[str, Any]:
    weight = TOKEN_ENGINE.count_tokens(content)
    bucket = _token_bucket(node_type)
    deltas = {"input": 0, "thought": 0, "output": 0}
    deltas[bucket] = weight
    with STORE._conn() as conn:
        parent = conn.execute(
            "SELECT cumulative_branch_tokens, approved_budget_ceiling "
            "FROM dag_nodes WHERE node_id = ?",
            (parent_node_id,),
        ).fetchone()
        if not parent:
            raise ValueError(f"Parent node '{parent_node_id}' not found.")
        ceiling = parent["approved_budget_ceiling"]
        payload: Dict[str, Any] = {"content": content}
        if tool_name:
            payload["tool_name"] = tool_name
        if provider_signature:
            payload["provider_signature"] = provider_signature
        node_id = _insert_dag_node(
            conn, session_id, parent_node_id, node_type, "COMPLETED",
            deltas["input"], deltas["thought"], deltas["output"], ceiling,
            weight * _USD_PER_TOKEN[bucket], payload,
        )
        row = conn.execute(
            "SELECT cumulative_branch_tokens FROM dag_nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        cumulative = row["cumulative_branch_tokens"]
        conn.execute(
            "UPDATE trace_sessions SET active_head_node_id = ?, "
            "total_session_tokens = total_session_tokens + ?, "
            "total_session_usd = total_session_usd + ? WHERE session_id = ?",
            (node_id, weight, weight * _USD_PER_TOKEN[bucket], session_id),
        )
        tripped = cumulative >= ceiling
        interrupt_node_id = None
        if tripped:
            interrupt_node_id = _insert_dag_node(
                conn, session_id, node_id, "BUDGET_INTERRUPT", "PAUSED_OVER_BUDGET",
                0, 0, 0, ceiling, 0.0,
                {
                    "interrupted_at_node": node_id,
                    "interrupted_stage": "MID_STREAM",
                    "partial_assistant_buffer": content if node_type == "ASSISTANT_OUTPUT" else "",
                    "cumulative_tokens": cumulative,
                    "approved_ceiling": ceiling,
                },
            )
    return {
        "node_id": node_id,
        "cumulative_branch_tokens": cumulative,
        "approved_budget_ceiling": ceiling,
        "budget_utilization_pct": round(100.0 * cumulative / max(1, ceiling), 2),
        "circuit_breaker_tripped": tripped,
        "interrupt_node_id": interrupt_node_id,
    }


def _compile_ancestor_lineage(
    conn: sqlite3.Connection, session_id: str, tree_path: str
) -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    node_ids = [nid for nid in tree_path.split("/") if nid]
    for nid in node_ids:
        row = conn.execute(
            "SELECT node_type, payload_json FROM dag_nodes WHERE node_id = ? AND session_id = ?",
            (nid, session_id),
        ).fetchone()
        if not row:
            continue
        payload = json.loads(row["payload_json"])
        ntype = row["node_type"]
        content = payload.get("content", "")
        if ntype == "USER_PROMPT":
            messages.append({"role": "user", "content": content})
        elif ntype == "THOUGHT_BLOCK":
            messages.append({
                "role": "assistant",
                "channel": "thinking",
                "content": content,
                "is_user_modified": bool(payload.get("is_user_modified")),
            })
        elif ntype == "TOOL_CALL":
            messages.append({
                "role": "assistant",
                "channel": "tool_call",
                "content": content,
                "tool_name": payload.get("tool_name"),
                "tool_call_id": payload.get("tool_call_id"),
            })
        elif ntype == "TOOL_RESULT":
            messages.append({
                "role": "tool",
                "content": content,
                "tool_name": payload.get("tool_name"),
                "tool_call_id": payload.get("tool_call_id"),
            })
        elif ntype == "ASSISTANT_OUTPUT":
            messages.append({"role": "assistant", "content": content})
        elif ntype == "SKILL_LOAD":
            messages.append({"role": "system", "content": content})
        # PREFLIGHT_LOCK and BUDGET_INTERRUPT nodes carry no LLM context.
    return messages


def fork_dag_from_node(
    session_id: str,
    target_node_id: str,
    edited_content: str,
    fork_reason: str,
) -> Dict[str, Any]:
    with STORE._conn() as conn:
        target = conn.execute(
            "SELECT * FROM dag_nodes WHERE node_id = ? AND session_id = ?",
            (target_node_id, session_id),
        ).fetchone()
        if not target:
            return {"status": "ERROR", "error": f"Node '{target_node_id}' not found."}
        original_tokens = (
            target["delta_input_tokens"]
            + target["delta_thought_tokens"]
            + target["delta_output_tokens"]
        )
        edited_tokens = TOKEN_ENGINE.count_tokens(edited_content)
        is_thought = target["node_type"] == "THOUGHT_BLOCK"
        stored_content = (
            f"<corrected_reasoning>\n{edited_content}\n</corrected_reasoning>"
            if is_thought
            else edited_content
        )
        bucket = _token_bucket(target["node_type"])
        deltas = {"input": 0, "thought": 0, "output": 0}
        deltas[bucket] = edited_tokens
        payload: Dict[str, Any] = {
            "content": stored_content,
            "forked_from_node_id": target_node_id,
            "fork_reason": fork_reason,
        }
        if is_thought:
            payload["is_user_modified"] = True
            payload["provider_signature"] = None
        fork_node_id = _insert_dag_node(
            conn, session_id, target["parent_node_id"],
            target["node_type"], "COMPLETED",
            deltas["input"], deltas["thought"], deltas["output"],
            target["approved_budget_ceiling"],
            edited_tokens * _USD_PER_TOKEN[bucket], payload,
        )
        fork_row = conn.execute(
            "SELECT tree_path FROM dag_nodes WHERE node_id = ?", (fork_node_id,)
        ).fetchone()
        lineage = _compile_ancestor_lineage(conn, session_id, fork_row["tree_path"])
    return {
        "status": "FORK_CREATED",
        "fork_node_id": fork_node_id,
        "immediate_token_savings": original_tokens - edited_tokens,
        "forked_node_tokens": edited_tokens,
        "reconstructed_context_messages": lineage,
    }


def approve_midflight_budget_topup(
    session_id: str, interrupt_node_id: str, extra_tokens: int
) -> Dict[str, Any]:
    with STORE._conn() as conn:
        row = conn.execute(
            "SELECT approved_budget_ceiling FROM dag_nodes WHERE node_id = ? AND session_id = ?",
            (interrupt_node_id, session_id),
        ).fetchone()
        if not row:
            return {"status": "ERROR", "error": f"Interrupt node '{interrupt_node_id}' not found."}
        new_ceiling = row["approved_budget_ceiling"] + extra_tokens
        conn.execute(
            "UPDATE dag_nodes SET approved_budget_ceiling = ?, status = 'RESUMED' "
            "WHERE node_id = ?",
            (new_ceiling, interrupt_node_id),
        )
    return {"status": "RESUMED", "new_approved_ceiling": new_ceiling}


def get_dag_trace_tree(session_id: str) -> Dict[str, Any]:
    with STORE._conn() as conn:
        sess = conn.execute(
            "SELECT * FROM trace_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not sess:
            return {"error": f"Session '{session_id}' not found."}
        rows = conn.execute(
            "SELECT * FROM dag_nodes WHERE session_id = ? ORDER BY rowid ASC",
            (session_id,),
        ).fetchall()
    nodes = [dict(r) for r in rows]
    total_tokens = sum(
        n["delta_input_tokens"] + n["delta_thought_tokens"] + n["delta_output_tokens"]
        for n in nodes
    )
    total_usd = sess["total_session_usd"]
    ascii_lines = [f"session:{session_id}"]
    for n in nodes:
        depth = n["tree_path"].count("/")
        ascii_lines.append(f"{'  ' * depth}- {n['node_type']} ({n['node_id']})")
    return {
        "session_id": session_id,
        "model": sess["model"],
        "nodes": nodes,
        "total_session_tokens_across_all_branches": total_tokens,
        "total_session_usd": total_usd,
        "total_session_inr": round(total_usd * USD_TO_INR, 2),
        "ascii_dag_tree": "\n".join(ascii_lines),
    }


def load_approved_github_skill(
    session_id: str, parent_node_id: str, skill_name: str
) -> Dict[str, Any]:
    content = f"Loaded GitHub skill: {skill_name}"
    weight = TOKEN_ENGINE.count_tokens(content)
    with STORE._conn() as conn:
        parent = conn.execute(
            "SELECT approved_budget_ceiling FROM dag_nodes WHERE node_id = ?",
            (parent_node_id,),
        ).fetchone()
        ceiling = parent["approved_budget_ceiling"] if parent else 8000
        node_id = _insert_dag_node(
            conn, session_id, parent_node_id, "SKILL_LOAD", "COMPLETED",
            weight, 0, 0, ceiling, weight * _USD_PER_TOKEN["input"],
            {"content": content, "name": skill_name, "token_weight": weight},
        )
    return {"skill_load_node_id": node_id, "skill_name": skill_name, "token_weight": weight}


async def sync_github_skill_library(
    repo: str, branch: str = "main", github_token: Optional[str] = None
) -> Dict[str, Any]:
    # Offline stub: indexes nothing.
    return {"skills_indexed_count": 0, "indexed_skills": [], "repo": repo, "branch": branch}


class _MCPStub:
    """Stand-in for the FastMCP server object."""

    def tool(self, fn=None, **_kwargs):
        def decorator(f):
            return f
        if fn is not None:
            return decorator(fn)
        return decorator

    def run(self, *args, **kwargs):  # pragma: no cover - never actually served in tests
        raise RuntimeError("The stub MCP server cannot run; real tracegate_mcp.py is missing.")


mcp = _MCPStub()
