#!/usr/bin/env python3
"""
End-to-End Pytest Suite for TraceGate (test_tracegate_agentic_loop.py)
======================================================================
Tests:
 1. `AgentToolRegistry` schema compilation (Anthropic, Gemini, OpenAI) & execution
 2. Multi-turn `TOOL_CALL` -> `TOOL_RESULT` automatic execution & LLM lineage replay
 3. Post-Tool-Result budget circuit breaker freeze & async gate resume
 4. Counterfactual DAG forking on bloated `TOOL_RESULT` nodes (token & USD/INR savings)
 5. Cryptographic signature stripping & `<corrected_reasoning>` wrapping on edited thoughts
 6. Mid-sentence `ASSISTANT_OUTPUT` pause & Assistant Prefill continuation
Run with:
   uv pip install pytest pytest-asyncio
   pytest test_tracegate_agentic_loop.py -v
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional
import pytest
import tracegate_mcp as tg
import tracegate_bridge as bridge
from tracegate_bridge import (
   AgentToolRegistry,
   UnifiedStreamChunk,
   execute_budget_gated_pipeline,
)
# ---------------------------------------------------------------------------
# 1. ISOLATED SQLITE DB & WEBSOCKET BROADCAST SPY FIXTURES
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_tracegate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
   """
   Redirects `tg.STORE` to a fresh temporary SQLite database in WAL mode
   for every single test so tests never pollute each other.
   """
   test_db = tmp_path / "test_tracegate.sqlite3"
   fresh_store = tg.TraceGateStore(test_db)
   monkeypatch.setattr(tg, "STORE", fresh_store)
   bridge.configure_sqlite_wal()
   return fresh_store
@pytest.fixture
def broadcast_events(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
   """
   Captures all WebSocket broadcasts emitted by `bridge.MANAGER.broadcast`
   so tests can assert on `LIVE_STREAM_DELTA`, `TOOL_EXECUTION_STARTED`,
   `BUDGET_CIRCUIT_BREAKER_TRIPPED`, and `EXECUTION_COMPLETE` events.
   """
   captured: List[Dict[str, Any]] = []
   async def _spy_broadcast(session_id: str, payload: Dict[str, Any]) -> None:
       captured.append(payload)
   monkeypatch.setattr(bridge.MANAGER, "broadcast", _spy_broadcast)
   return captured
# ---------------------------------------------------------------------------
# TEST 1: AGENT TOOL REGISTRY SCHEMA TRANSLATION & SAFE EXECUTION
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_1_agent_tool_registry_schemas_and_execution(tmp_path: Path):
   registry = AgentToolRegistry()
   # Register a custom async tool
   async def fetch_service_metrics(service_name: str, window_mins: int = 15) -> Dict[str, Any]:
       await asyncio.sleep(0.01)
       return {"service": service_name, "window_mins": window_mins, "p99_ms": 42.5}
   registry.register(
       name="fetch_service_metrics",
       description="Fetches latency metrics for a microservice.",
       parameters_schema={
           "type": "object",
           "properties": {
               "service_name": {"type": "string"},
               "window_mins": {"type": "integer"},
           },
           "required": ["service_name"],
       },
       handler=fetch_service_metrics,
   )
   # 1. Verify Anthropic, OpenAI, and Gemini schema compilers
   anth_tools = registry.to_anthropic_tools()
   oa_tools = registry.to_openai_tools()
   gem_tools = registry.to_gemini_tools()
   assert any(t["name"] == "fetch_service_metrics" and "input_schema" in t for t in anth_tools)
   assert any(
       t["type"] == "function" and t["function"]["name"] == "fetch_service_metrics"
       for t in oa_tools
   )
   assert len(gem_tools) == 1
   # 2. Verify valid async execution & token weight calculation
   ok_res = await registry.execute(
       "fetch_service_metrics",
       json.dumps({"service_name": "auth-gateway", "window_mins": 60}),
   )
   assert ok_res["ok"] is True
   assert ok_res["token_weight"] > 5
   parsed_out = json.loads(ok_res["raw_output"])
   assert parsed_out["service"] == "auth-gateway"
   assert parsed_out["p99_ms"] == 42.5
   # 3. Verify graceful handling of malformed JSON arguments
   bad_json_res = await registry.execute("fetch_service_metrics", "{invalid_json")
   assert bad_json_res["ok"] is False
   assert "Invalid JSON arguments" in bad_json_res["raw_output"]
   # 4. Verify unknown tool fallback
   unknown_res = await registry.execute("non_existent_tool", "{}")
   assert unknown_res["ok"] is False
   assert "Unknown tool" in unknown_res["raw_output"]
# ---------------------------------------------------------------------------
# TEST 2: MULTI-TURN `TOOL_CALL` -> `TOOL_RESULT` AUTO-EXECUTION & REPLAY
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_2_multi_turn_tool_call_and_result_replay(
   tmp_path: Path,
   monkeypatch: pytest.MonkeyPatch,
   broadcast_events: List[Dict[str, Any]],
):
   # Create a real file for `read_workspace_file` to inspect
   sample_file = tmp_path / "config.py"
   sample_file.write_text("MAX_RETRIES = 5\nTIMEOUT_SEC = 30\n", encoding="utf-8")
   # Track the lineage_messages received by `stream_turn` on each iteration
   recorded_turn_lineages: List[List[Dict[str, Any]]] = []
   async def mock_multi_turn_stream(
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str = "",
       enable_tools: bool = True,
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       recorded_turn_lineages.append(list(lineage_messages))
       turn_idx = len(recorded_turn_lineages)
       if turn_idx == 1:
           # Turn 1: Model thinks, then calls `read_workspace_file`
           yield UnifiedStreamChunk(
               channel="THOUGHT_BLOCK",
               delta_text="I need to read config.py to check MAX_RETRIES.",
               provider_signature="sig_turn_1",
           )
           yield UnifiedStreamChunk(
               channel="TOOL_CALL",
               delta_text=json.dumps({"path": str(sample_file), "start_line": 1, "end_line": 10}),
               tool_name="read_workspace_file",
               tool_call_id="call_cfg_01",
           )
       else:
           # Turn 2: Model sees the TOOL_RESULT in lineage and emits final answer
           yield UnifiedStreamChunk(
               channel="THOUGHT_BLOCK",
               delta_text="The file shows MAX_RETRIES = 5.",
               provider_signature="sig_turn_2",
           )
           yield UnifiedStreamChunk(
               channel="ASSISTANT_OUTPUT",
               delta_text="Based on `config.py`, `MAX_RETRIES` is set to **5**.",
           )
   monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", mock_multi_turn_stream)
   # 1. Create Pre-Flight Lock with generous ceiling (6,000 tokens)
   preflight = tg.preflight_estimate_and_match_skill(
       user_prompt="Check MAX_RETRIES in config.py",
       model="claude-sonnet-5-5",
       session_id="sess_multiturn_test",
   )
   sid = preflight["session_id"]
   lock_id = preflight["preflight_lock_node_id"]
   # 2. Run the budget-gated agentic pipeline
   await execute_budget_gated_pipeline(
       session_id=sid,
       lock_node_id=lock_id,
       prompt="Check MAX_RETRIES in config.py",
       matched_skills=[],
   )
   # 3. Verify the LLM was called twice and Turn 2 received the TOOL_CALL + TOOL_RESULT
   assert len(recorded_turn_lineages) == 2
   turn_2_lineage = recorded_turn_lineages[1]
   tool_call_msgs = [m for m in turn_2_lineage if m.get("channel") == "tool_call"]
   tool_result_msgs = [m for m in turn_2_lineage if m.get("role") == "tool"]
   assert len(tool_call_msgs) == 1
   assert tool_call_msgs[0]["tool_name"] == "read_workspace_file"
   assert len(tool_result_msgs) == 1
   assert "MAX_RETRIES = 5" in tool_result_msgs[0]["content"]
   # 4. Verify the SQLite DAG recorded all 6 atomic nodes in order
   tree = tg.get_dag_trace_tree(sid)
   node_types = [n["node_type"] for n in tree["nodes"]]
   assert node_types == [
       "USER_PROMPT",
       "PREFLIGHT_LOCK",
       "THOUGHT_BLOCK",
       "TOOL_CALL",
       "TOOL_RESULT",
       "THOUGHT_BLOCK",
       "ASSISTANT_OUTPUT",
   ]
   # Verify `EXECUTION_COMPLETE` event fired with `tool_loops_executed == 1`
   complete_events = [e for e in broadcast_events if e.get("type") == "EXECUTION_COMPLETE"]
   assert len(complete_events) == 1
   assert complete_events[0]["tool_loops_executed"] == 1
# ---------------------------------------------------------------------------
# TEST 3 & 4: POST-TOOL BUDGET INTERRUPT, TOP-UP RESUME & BLOATED TOOL FORK
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_3_and_4_post_tool_budget_interrupt_and_counterfactual_fork(
   tmp_path: Path,
   monkeypatch: pytest.MonkeyPatch,
   broadcast_events: List[Dict[str, Any]],
):
   # Create a bloated source file (~450 tokens) that will push the branch over a tight ceiling
   bloated_file = tmp_path / "bloated_dump.txt"
   bloated_file.write_text(
       "\n".join([f"VERBOSE_LOG_LINE_{i}: " + ("x" * 60) for i in range(40)]),
       encoding="utf-8",
   )
   turn_counter = 0
   async def mock_bloated_tool_stream(
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str = "",
       enable_tools: bool = True,
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       nonlocal turn_counter
       turn_counter += 1
       if turn_counter == 1:
           yield UnifiedStreamChunk(
               channel="TOOL_CALL",
               delta_text=json.dumps({"path": str(bloated_file), "start_line": 1, "end_line": 40}),
               tool_name="read_workspace_file",
               tool_call_id="call_bloat_01",
           )
       else:
           yield UnifiedStreamChunk(
               channel="ASSISTANT_OUTPUT",
               delta_text="Processed the log dump after budget approval.",
           )
   monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", mock_bloated_tool_stream)
   preflight = tg.preflight_estimate_and_match_skill(
       user_prompt="Analyze bloated_dump.txt",
       model="claude-sonnet-5-5",
       system_and_history_tokens=200,
       cached_prefix_tokens=100,
       tool_schema_tokens=100,
       session_id="sess_bloat_interrupt",
   )
   sid = preflight["session_id"]
   lock_id = preflight["preflight_lock_node_id"]
   # Force a tight budget ceiling (350 tokens) so the bloated TOOL_RESULT trips Checkpoint 2!
   with tg.STORE._conn() as conn:
       conn.execute(
           "UPDATE dag_nodes SET approved_budget_ceiling = 350, cumulative_branch_tokens = 250 WHERE node_id = ?",
           (lock_id,),
       )
   # Launch pipeline as a background task because it will pause on `MANAGER.pause_gates[sid]`
   pipeline_task = asyncio.create_task(
       execute_budget_gated_pipeline(
           session_id=sid,
           lock_node_id=lock_id,
           prompt="Analyze bloated_dump.txt",
           matched_skills=[],
       )
   )
   # Wait briefly until the Post-Tool Circuit Breaker trips
   for _ in range(40):
       if sid in bridge.MANAGER.pause_gates:
           break
       await asyncio.sleep(0.02)
   assert sid in bridge.MANAGER.pause_gates, "Pipeline should have paused at Post-Tool-Result Firewall!"
   assert turn_counter == 1, "Turn 2 must NOT fire before user resolves the Post-Tool budget interrupt!"
   # Verify the `BUDGET_CIRCUIT_BREAKER_TRIPPED` broadcast was sent
   cb_events = [e for e in broadcast_events if e.get("type") == "BUDGET_CIRCUIT_BREAKER_TRIPPED"]
   assert len(cb_events) == 1
   interrupt_node_id = cb_events[0]["interrupt_node_id"]
   tool_result_node_id = cb_events[0]["interrupted_at_node_id"]
   # -----------------------------------------------------------------------
   # PART A: TEST COUNTERFACTUAL FORKING ON THE BLOATED `TOOL_RESULT` NODE
   # -----------------------------------------------------------------------
   fork_res = tg.fork_dag_from_node(
       session_id=sid,
       target_node_id=tool_result_node_id,
       edited_content="VERBOSE_LOG_SUMMARY: Only 1 error on line 4.",
       fork_reason="Pruned 40 lines of boilerplate down to 1 summary line",
   )
   assert fork_res["status"] == "FORK_CREATED"
   assert fork_res["immediate_token_savings"] > 150
   assert fork_res["forked_node_tokens"] < 20
   # Reconstructed lineage on the fork must contain the pruned text, not the bloated text
   fork_tool_msgs = [m for m in fork_res["reconstructed_context_messages"] if m.get("role") == "tool"]
   assert fork_tool_msgs[-1]["content"] == "VERBOSE_LOG_SUMMARY: Only 1 error on line 4."
   # -----------------------------------------------------------------------
   # PART B: APPROVE +2,000 EXTRA TOKENS TO RESUME THE PAUSED MAIN BRANCH
   # -----------------------------------------------------------------------
   topup_res = tg.approve_midflight_budget_topup(
       session_id=sid,
       interrupt_node_id=interrupt_node_id,
       extra_tokens=2000,
   )
   assert topup_res["status"] == "RESUMED"
   bridge.MANAGER.pause_gates[sid].set()
   await asyncio.wait_for(pipeline_task, timeout=2.0)
   assert turn_counter == 2, "Pipeline should have resumed and completed Turn 2 after top-up!"
# ---------------------------------------------------------------------------
# TEST 5: EDITED `THOUGHT_BLOCK` WRAPS IN `<corrected_reasoning>` TAGS
# ---------------------------------------------------------------------------
def test_5_edited_thought_block_wraps_corrected_reasoning_tag():
   preflight = tg.preflight_estimate_and_match_skill(
       user_prompt="Parse HTML document",
       model="claude-sonnet-5-5",
       session_id="sess_thought_fork",
   )
   sid = preflight["session_id"]
   lock_id = preflight["preflight_lock_node_id"]
   # Record an original signed THOUGHT_BLOCK
   thought_step = tg.record_dag_step(
       session_id=sid,
       parent_node_id=lock_id,
       node_type="THOUGHT_BLOCK",
       content="I will use a regex to parse the nested HTML tags.",
       provider_signature="crypto_sig_original_abc123",
   )
   # User forks the thought block to steer the model away from regex
   fork_res = tg.fork_dag_from_node(
       session_id=sid,
       target_node_id=thought_step["node_id"],
       edited_content="Use an AST/DOM parser like BeautifulSoup instead of regex.",
       fork_reason="Steered reasoning away from regex HTML parsing",
   )
   thinking_msgs = [
       m for m in fork_res["reconstructed_context_messages"]
       if m.get("channel") == "thinking"
   ]
   assert len(thinking_msgs) == 1
   assert thinking_msgs[0]["is_user_modified"] is True
   assert thinking_msgs[0]["content"].startswith("<corrected_reasoning>")
   assert "BeautifulSoup" in thinking_msgs[0]["content"]
# ---------------------------------------------------------------------------
# TEST 6: MID-SENTENCE PAUSE & ASSISTANT PREFILL CONTINUATION
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_6_mid_sentence_interrupt_and_assistant_prefill_continuation(
   monkeypatch: pytest.MonkeyPatch,
   broadcast_events: List[Dict[str, Any]],
):
   received_prefills: List[str] = []
   async def mock_mid_sentence_stream(
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str = "",
       enable_tools: bool = True,
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       received_prefills.append(assistant_prefill)
       if not assistant_prefill:
           # First run: stream chunks until the tight ceiling trips mid-sentence
           for chunk_txt in [
               "def calculate_rate_limit(",
               "user_id: str, ",
               "max_requests: int = 100",
               ") -> bool:\n    # Exceeding tight budget right here...",
           ]:
               yield UnifiedStreamChunk(channel="ASSISTANT_OUTPUT", delta_text=chunk_txt)
       else:
           # Continuation run after user approves +1,500 extra tokens:
           # emits ONLY the remaining suffix after `assistant_prefill`
           yield UnifiedStreamChunk(
               channel="ASSISTANT_OUTPUT",
               delta_text="\n    return redis_client.incr(user_id) <= max_requests",
           )
   monkeypatch.setattr(bridge.STREAM_ROUTER, "stream_turn", mock_mid_sentence_stream)
   preflight = tg.preflight_estimate_and_match_skill(
       user_prompt="Write rate limit function",
       model="claude-sonnet-5-5",
       session_id="sess_prefill_resume",
   )
   sid = preflight["session_id"]
   lock_id = preflight["preflight_lock_node_id"]
   # Set cumulative tokens to 990 and ceiling to 1,000 so the 1st stream trips mid-sentence!
   with tg.STORE._conn() as conn:
       conn.execute(
           "UPDATE dag_nodes SET approved_budget_ceiling = 1000, cumulative_branch_tokens = 990 WHERE node_id = ?",
           (lock_id,),
       )
   task = asyncio.create_task(
       execute_budget_gated_pipeline(
           session_id=sid,
           lock_node_id=lock_id,
           prompt="Write rate limit function",
           matched_skills=[],
       )
   )
   for _ in range(40):
       if sid in bridge.MANAGER.pause_gates:
           break
       await asyncio.sleep(0.02)
   assert sid in bridge.MANAGER.pause_gates, "Stream should have paused mid-sentence!"
   cb_event = [e for e in broadcast_events if e.get("type") == "BUDGET_CIRCUIT_BREAKER_TRIPPED"][0]
   # Grant +1,500 extra tokens and release the pause gate
   tg.approve_midflight_budget_topup(
       session_id=sid,
       interrupt_node_id=cb_event["interrupt_node_id"],
       extra_tokens=1500,
   )
   bridge.MANAGER.pause_gates[sid].set()
   await asyncio.wait_for(task, timeout=2.0)
   # Verify Turn 2 received the partial code prefix in `assistant_prefill`
   assert len(received_prefills) == 2
   assert received_prefills[0] == ""
   assert "def calculate_rate_limit(" in received_prefills[1]
   # Verify the final ASSISTANT_OUTPUT node stitched the prefill + suffix seamlessly
   tree = tg.get_dag_trace_tree(sid)
   final_node = tree["nodes"][-1]
   final_payload = json.loads(final_node["payload_json"])
   assert "def calculate_rate_limit(" in final_payload["content"]
   assert "return redis_client.incr(user_id) <= max_requests" in final_payload["content"]
