"""Tests for the budget-gated execution pipelines in tracegate_bridge and
llm_stream_router, driven by mocked LLM streams against the stub DAG store.
No real provider calls."""
from __future__ import annotations

import asyncio
import json

import pytest

import tracegate_mcp as tg
import tracegate_bridge as bridge
import llm_stream_router as lsr
from tracegate_bridge import UnifiedStreamChunk, execute_budget_gated_pipeline

from .conftest import make_session


@pytest.fixture
def events(monkeypatch):
    captured = []

    async def spy(session_id, payload):
        captured.append(payload)

    async def snap(session_id):
        return None

    monkeypatch.setattr(bridge.MANAGER, "broadcast", spy)
    monkeypatch.setattr(bridge.MANAGER, "broadcast_dag_snapshot", snap)
    yield captured
    bridge.MANAGER.pause_gates.clear()


@pytest.fixture
def lsr_events(monkeypatch):
    captured = []

    async def spy(session_id, payload):
        captured.append(payload)

    async def snap(session_id):
        return None

    monkeypatch.setattr(lsr.MANAGER, "broadcast", spy)
    monkeypatch.setattr(lsr.MANAGER, "broadcast_dag_snapshot", snap)
    yield captured
    lsr.MANAGER.pause_gates.clear()


def stream_of(chunks):
    async def _fake(**kwargs):
        for c in chunks:
            yield c
    return _fake


async def wait_for_gate(sid, manager):
    for _ in range(200):
        if sid in manager.pause_gates:
            return manager.pause_gates[sid]
        await asyncio.sleep(0.01)
    raise AssertionError(f"pause gate for {sid} was never registered")


# ---------------------------------------------------------------------------
# Clean completion
# ---------------------------------------------------------------------------

async def test_clean_turn_completes(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([
        UnifiedStreamChunk(channel="THOUGHT_BLOCK", delta_text="thinking"),
        UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="final answer"),
    ]))
    pre = make_session("s_clean")
    await execute_budget_gated_pipeline(sid := "s_clean", pre["preflight_lock_node_id"], "p", [])
    done = [e for e in events if e["type"] == "EXECUTION_COMPLETE"]
    assert len(done) == 1
    assert done[0]["tool_loops_executed"] == 0
    tree = tg.get_dag_trace_tree(sid)
    assert [n["node_type"] for n in tree["nodes"]] == [
        "USER_PROMPT", "PREFLIGHT_LOCK", "THOUGHT_BLOCK", "ASSISTANT_OUTPUT",
    ]


async def test_live_stream_deltas_broadcast_with_cumulative(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([
        UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="hello world"),
    ]))
    pre = make_session("s_deltas")
    await execute_budget_gated_pipeline("s_deltas", pre["preflight_lock_node_id"], "p", [])
    deltas = [e for e in events if e["type"] == "LIVE_STREAM_DELTA"]
    assert len(deltas) == 1
    assert deltas[0]["delta"] == "hello world"
    assert deltas[0]["delta_tokens"] > 0
    assert deltas[0]["live_cumulative_tokens"] >= deltas[0]["delta_tokens"]
    assert deltas[0]["approved_ceiling"] == 8000


async def test_empty_stream_completes_immediately(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([]))
    pre = make_session("s_empty")
    await execute_budget_gated_pipeline("s_empty", pre["preflight_lock_node_id"], "p", [])
    done = [e for e in events if e["type"] == "EXECUTION_COMPLETE"]
    assert len(done) == 1
    assert done[0]["head_node_id"] == pre["preflight_lock_node_id"]


async def test_provider_signature_stored_on_thought_node(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([
        UnifiedStreamChunk(channel="THOUGHT_BLOCK", delta_text="signed thought",
                           provider_signature="sig_abc"),
    ]))
    pre = make_session("s_sig")
    await execute_budget_gated_pipeline("s_sig", pre["preflight_lock_node_id"], "p", [])
    tree = tg.get_dag_trace_tree("s_sig")
    thought = [n for n in tree["nodes"] if n["node_type"] == "THOUGHT_BLOCK"][0]
    assert json.loads(thought["payload_json"])["provider_signature"] == "sig_abc"


async def test_channel_switch_flushes_in_order(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([
        UnifiedStreamChunk(channel="THOUGHT_BLOCK", delta_text="plan"),
        UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="answer"),
    ]))
    pre = make_session("s_order")
    await execute_budget_gated_pipeline("s_order", pre["preflight_lock_node_id"], "p", [])
    tree = tg.get_dag_trace_tree("s_order")
    types = [n["node_type"] for n in tree["nodes"]]
    assert types.index("THOUGHT_BLOCK") < types.index("ASSISTANT_OUTPUT")


async def test_remaining_budget_floor_is_64(store, events, monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen["max_output_tokens"] = kwargs["max_output_tokens"]
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="x")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_floor", ceiling=100)
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET cumulative_branch_tokens = 95 WHERE node_id = ?",
            (pre["preflight_lock_node_id"],),
        )
    await execute_budget_gated_pipeline("s_floor", pre["preflight_lock_node_id"], "p", [])
    assert seen["max_output_tokens"] == 64  # max(64, 100 - 95)


async def test_missing_parent_node_breaks_loop_silently(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([
        UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="never"),
    ]))
    make_session("s_ghost")
    await execute_budget_gated_pipeline("s_ghost", "ghost_node", "p", [])
    assert events == []


async def test_matched_skills_loaded_into_dag(store, events, monkeypatch):
    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", stream_of([
        UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="done"),
    ]))
    pre = make_session("s_skills")
    await execute_budget_gated_pipeline(
        "s_skills", pre["preflight_lock_node_id"], "p", ["pdf-parsing", "web-search"]
    )
    tree = tg.get_dag_trace_tree("s_skills")
    skill_nodes = [n for n in tree["nodes"] if n["node_type"] == "SKILL_LOAD"]
    assert len(skill_nodes) == 2
    names = {json.loads(n["payload_json"])["name"] for n in skill_nodes}
    assert names == {"pdf-parsing", "web-search"}


# ---------------------------------------------------------------------------
# Tool-call loop
# ---------------------------------------------------------------------------

async def test_tool_call_auto_executed_and_result_logged(store, events, monkeypatch, tmp_path):
    target = tmp_path / "conf.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    turns = []

    async def fake(**kwargs):
        turns.append(kwargs)
        if len(turns) == 1:
            yield UnifiedStreamChunk(
                channel="TOOL_CALL",
                delta_text=json.dumps({"path": str(target), "start_line": 1, "end_line": 3}),
                tool_name="read_workspace_file",
                tool_call_id="call_1",
            )
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="read it")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_tool")
    await execute_budget_gated_pipeline("s_tool", pre["preflight_lock_node_id"], "p", [])
    assert len(turns) == 2
    started = [e for e in events if e["type"] == "TOOL_EXECUTION_STARTED"]
    assert [e["tool_name"] for e in started] == ["read_workspace_file"]
    tree = tg.get_dag_trace_tree("s_tool")
    result_nodes = [n for n in tree["nodes"] if n["node_type"] == "TOOL_RESULT"]
    assert len(result_nodes) == 1
    payload = json.loads(result_nodes[0]["payload_json"])
    assert "1: alpha" in payload["content"]
    assert result_nodes[0]["delta_input_tokens"] > 0
    done = [e for e in events if e["type"] == "EXECUTION_COMPLETE"]
    assert done[0]["tool_loops_executed"] == 1


async def test_multiple_tool_calls_in_one_turn_all_executed(store, events, monkeypatch, tmp_path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("aaa", encoding="utf-8")
    f2.write_text("bbb", encoding="utf-8")
    turns = []

    async def fake(**kwargs):
        turns.append(kwargs)
        if len(turns) == 1:
            yield UnifiedStreamChunk(channel="TOOL_CALL", delta_text=json.dumps({"path": str(f1)}),
                                     tool_name="read_workspace_file", tool_call_id="call_a")
            yield UnifiedStreamChunk(channel="TOOL_CALL", delta_text=json.dumps({"path": str(f2)}),
                                     tool_name="read_workspace_file", tool_call_id="call_b")
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="both read")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_2tools")
    await execute_budget_gated_pipeline("s_2tools", pre["preflight_lock_node_id"], "p", [])
    started = [e for e in events if e["type"] == "TOOL_EXECUTION_STARTED"]
    assert len(started) == 2
    tree = tg.get_dag_trace_tree("s_2tools")
    results = [n for n in tree["nodes"] if n["node_type"] == "TOOL_RESULT"]
    assert len(results) == 2
    contents = [json.loads(n["payload_json"])["content"] for n in results]
    assert any("aaa" in c for c in contents)
    assert any("bbb" in c for c in contents)


async def test_unknown_tool_error_recorded_and_loop_continues(store, events, monkeypatch):
    turns = []

    async def fake(**kwargs):
        turns.append(1)
        if len(turns) == 1:
            yield UnifiedStreamChunk(channel="TOOL_CALL", delta_text="{}",
                                     tool_name="does_not_exist", tool_call_id="call_x")
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="recovered")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_badtool")
    await execute_budget_gated_pipeline("s_badtool", pre["preflight_lock_node_id"], "p", [])
    tree = tg.get_dag_trace_tree("s_badtool")
    result = [n for n in tree["nodes"] if n["node_type"] == "TOOL_RESULT"][0]
    assert "Unknown tool" in json.loads(result["payload_json"])["content"]
    assert any(e["type"] == "EXECUTION_COMPLETE" for e in events)


async def test_tool_loop_capped_at_max_iterations(store, events, monkeypatch, tmp_path):
    f = tmp_path / "loop.txt"
    f.write_text("x", encoding="utf-8")
    calls = []

    async def fake(**kwargs):
        calls.append(1)
        yield UnifiedStreamChunk(channel="TOOL_CALL", delta_text=json.dumps({"path": str(f)}),
                                 tool_name="read_workspace_file", tool_call_id=f"call_{len(calls)}")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_cap")
    await execute_budget_gated_pipeline("s_cap", pre["preflight_lock_node_id"], "p", [])
    assert len(calls) == bridge.MAX_AGENT_TOOL_LOOPS
    assert not [e for e in events if e["type"] == "EXECUTION_COMPLETE"]


# ---------------------------------------------------------------------------
# Mid-stream budget interrupt and resume
# ---------------------------------------------------------------------------

async def test_midstream_trip_pauses_then_resumes_with_prefill(store, events, monkeypatch):
    prefills = []

    async def fake(**kwargs):
        prefills.append(kwargs["assistant_prefill"])
        if not kwargs["assistant_prefill"]:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="aaaaaaaa")
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="bbbbbbbb")
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="--continued")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_mid", ceiling=30)
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET cumulative_branch_tokens = 29 WHERE node_id = ?",
            (pre["preflight_lock_node_id"],),
        )
    task = asyncio.create_task(
        execute_budget_gated_pipeline("s_mid", pre["preflight_lock_node_id"], "p", [])
    )
    gate = await wait_for_gate("s_mid", bridge.MANAGER)
    cb = [e for e in events if e["type"] == "BUDGET_CIRCUIT_BREAKER_TRIPPED"]
    assert len(cb) == 1
    assert cb[0]["approved_ceiling"] == 30
    assert "Paused mid-stream" in cb[0]["message"]
    tg.approve_midflight_budget_topup(
        session_id="s_mid", interrupt_node_id=cb[0]["interrupt_node_id"], extra_tokens=500
    )
    gate.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(prefills) == 2
    assert prefills[0] == ""
    assert prefills[1] != ""  # partial buffer handed back for continuation
    assert any(e["type"] == "EXECUTION_COMPLETE" for e in events)
    tree = tg.get_dag_trace_tree("s_mid")
    final = tree["nodes"][-1]
    content = json.loads(final["payload_json"])["content"]
    assert content.endswith("--continued")


async def test_post_tool_result_breaker_pauses_before_next_turn(store, events, monkeypatch, tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("y" * 400, encoding="utf-8")  # ~100+ tokens in the stub counter
    turns = []

    async def fake(**kwargs):
        turns.append(1)
        if len(turns) == 1:
            yield UnifiedStreamChunk(channel="TOOL_CALL",
                                     delta_text=json.dumps({"path": str(big), "start_line": 1, "end_line": 5}),
                                     tool_name="read_workspace_file", tool_call_id="c1")
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="done")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_post", ceiling=80)
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET cumulative_branch_tokens = 50 WHERE node_id = ?",
            (pre["preflight_lock_node_id"],),
        )
    task = asyncio.create_task(
        execute_budget_gated_pipeline("s_post", pre["preflight_lock_node_id"], "p", [])
    )
    gate = await wait_for_gate("s_post", bridge.MANAGER)
    assert len(turns) == 1  # second LLM turn must not fire before user resolution
    tg.approve_midflight_budget_topup(
        session_id="s_post",
        interrupt_node_id=[e for e in events if e["type"] == "BUDGET_CIRCUIT_BREAKER_TRIPPED"][0]["interrupt_node_id"],
        extra_tokens=500,
    )
    gate.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert len(turns) == 2


async def test_95pct_utilization_inserts_pre_turn_interrupt(store, events, monkeypatch, tmp_path):
    # Deterministic token counter: every string costs exactly 10 tokens, so the
    # TOOL_CALL (+10) and TOOL_RESULT (+10) land at 78+20 = 98/100 = 98% utilized,
    # below the 100 ceiling but above the 95% Checkpoint-2 threshold.
    monkeypatch.setattr(tg.TOKEN_ENGINE, "count_tokens", lambda text: 10 if text else 0)
    f = tmp_path / "mod.txt"
    f.write_text("z" * 60, encoding="utf-8")
    turns = []

    async def fake(**kwargs):
        turns.append(1)
        if len(turns) == 1:
            yield UnifiedStreamChunk(channel="TOOL_CALL", delta_text=json.dumps({"path": str(f)}),
                                     tool_name="read_workspace_file", tool_call_id="c1")
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="done")

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_95", ceiling=100)
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET cumulative_branch_tokens = 78 WHERE node_id = ?",
            (pre["preflight_lock_node_id"],),
        )
    task = asyncio.create_task(
        execute_budget_gated_pipeline("s_95", pre["preflight_lock_node_id"], "p", [])
    )
    gate = await wait_for_gate("s_95", bridge.MANAGER)
    tree = tg.get_dag_trace_tree("s_95")
    interrupts = [n for n in tree["nodes"] if n["node_type"] == "BUDGET_INTERRUPT"]
    assert len(interrupts) == 1
    payload = json.loads(interrupts[0]["payload_json"])
    assert payload["interrupted_stage"] == "BEFORE_NEXT_TOOL_TURN"
    tg.approve_midflight_budget_topup(
        session_id="s_95", interrupt_node_id=interrupts[0]["node_id"], extra_tokens=500
    )
    gate.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_circuit_breaker_trips_at_exact_ceiling(store, events, monkeypatch):
    async def fake(**kwargs):
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="aaaa")  # 1 token
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="bbbb")  # hits exactly

    monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_exact", ceiling=52)
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET cumulative_branch_tokens = 50 WHERE node_id = ?",
            (pre["preflight_lock_node_id"],),
        )
    task = asyncio.create_task(
        execute_budget_gated_pipeline("s_exact", pre["preflight_lock_node_id"], "p", [])
    )
    gate = await wait_for_gate("s_exact", bridge.MANAGER)
    cb = [e for e in events if e["type"] == "BUDGET_CIRCUIT_BREAKER_TRIPPED"][0]
    assert cb["cumulative_tokens"] >= cb["approved_ceiling"]
    tg.approve_midflight_budget_topup(
        session_id="s_exact", interrupt_node_id=cb["interrupt_node_id"], extra_tokens=100
    )
    gate.set()
    await asyncio.wait_for(task, timeout=2.0)


async def test_wait_for_budget_resolution_returns_buffer(store, monkeypatch):
    async def noop(session_id, payload):
        return None

    monkeypatch.setattr(bridge.MANAGER, "broadcast", noop)
    pre = make_session("s_wait", ceiling=100)
    step = tg.record_dag_step(
        session_id="s_wait",
        parent_node_id=pre["preflight_lock_node_id"],
        node_type="ASSISTANT_OUTPUT",
        content="x" * 9000,  # 2250 stub tokens >> 100-token ceiling
    )
    assert step["circuit_breaker_tripped"] is True
    task = asyncio.create_task(
        bridge.wait_for_user_budget_resolution(
            session_id="s_wait",
            interrupt_node_id=step["interrupt_node_id"],
            interrupted_at_node_id=step["node_id"],
            cumulative_tokens=step["cumulative_branch_tokens"],
            approved_ceiling=step["approved_budget_ceiling"],
            reason_message="paused",
        )
    )
    gate = await wait_for_gate("s_wait", bridge.MANAGER)
    gate.set()
    buf = await asyncio.wait_for(task, timeout=2.0)
    assert buf == "x" * 9000
    assert "s_wait" not in bridge.MANAGER.pause_gates


# ---------------------------------------------------------------------------
# llm_stream_router pipeline variant
# ---------------------------------------------------------------------------

async def test_router_pipeline_completes(store, lsr_events, monkeypatch):
    async def fake(**kwargs):
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="router answer")

    monkeypatch.setattr(lsr.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_router")
    await lsr.execute_budget_gated_pipeline(
        session_id="s_router",
        lock_node_id=pre["preflight_lock_node_id"],
        prompt="p",
        matched_skills=[],
    )
    done = [e for e in lsr_events if e["type"] == "EXECUTION_COMPLETE"]
    assert len(done) == 1
    tree = tg.get_dag_trace_tree("s_router")
    assert tree["nodes"][-1]["node_type"] == "ASSISTANT_OUTPUT"


async def test_router_pipeline_uses_max_Output_tokens_keyword(store, lsr_events, monkeypatch):
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="ok")

    monkeypatch.setattr(lsr.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_kw")
    await lsr.execute_budget_gated_pipeline("s_kw", pre["preflight_lock_node_id"], "p", [])
    assert "max_Output_tokens" in seen  # NOTE: capital-O keyword is the current API
    assert "assistant_prefill" in seen


async def test_router_pipeline_midstream_resume(store, lsr_events, monkeypatch):
    prefills = []

    async def fake(**kwargs):
        prefills.append(kwargs["assistant_prefill"])
        if not kwargs["assistant_prefill"]:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="aaaaaaaa")
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="bbbbbbbb")
        else:
            yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text="!!done")

    monkeypatch.setattr(lsr.STREAM_ROUTER, "stream_turn", fake)
    pre = make_session("s_rmid", ceiling=30)
    with tg.STORE._conn() as conn:
        conn.execute(
            "UPDATE dag_nodes SET cumulative_branch_tokens = 29 WHERE node_id = ?",
            (pre["preflight_lock_node_id"],),
        )
    task = asyncio.create_task(
        lsr.execute_budget_gated_pipeline("s_rmid", pre["preflight_lock_node_id"], "p", [])
    )
    gate = await wait_for_gate("s_rmid", lsr.MANAGER)
    cb = [e for e in lsr_events if e["type"] == "BUDGET_CIRCUIT_BREAKER_TRIPPED"][0]
    tg.approve_midflight_budget_topup(
        session_id="s_rmid", interrupt_node_id=cb["interrupt_node_id"], extra_tokens=500
    )
    gate.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert prefills[0] == ""
    assert "aaaaaaaa" in prefills[1]


async def test_continue_generation_from_fork_delegates(store, lsr_events, monkeypatch):
    calls = []

    async def fake_pipeline(session_id, lock_node_id, prompt, matched_skills, start_parent_node_id=None):
        calls.append({
            "session_id": session_id,
            "lock_node_id": lock_node_id,
            "start_parent_node_id": start_parent_node_id,
        })

    monkeypatch.setattr(lsr, "execute_budget_gated_pipeline", fake_pipeline)
    await lsr.continue_generation_from_fork("sessX", "nodeY")
    assert calls == [{
        "session_id": "sessX",
        "lock_node_id": "nodeY",
        "start_parent_node_id": "nodeY",
    }]
