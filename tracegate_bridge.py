import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Literal, Optional

import anthropic
import openai
from google import genai
from google.genai import types as genai_types

import tracegate_mcp as tg

# Shared stream-event dataclass lives in the streaming engine module.
from llm_stream_router import UnifiedStreamChunk
class AgentToolRegistry:
   """
   Registers callable tools once and compiles them into Anthropic, Gemini,
   and OpenAI tool definitions, while executing tool calls safely with
   automatic token-weight measurement.
   """
   def __init__(self) -> None:
       self._tools: Dict[str, Dict[str, Any]] = {}
       self._register_default_workspace_tools()
   def register(
       self,
       name: str,
       description: str,
       parameters_schema: Dict[str, Any],
       handler: Callable[..., Awaitable[str] | str],
   ) -> None:
       self._tools[name] = {
           "name": name,
           "description": description,
           "parameters": parameters_schema,
           "handler": handler,
       }
   # -----------------------------------------------------------------------
   # PROVIDER-SPECIFIC TOOL SCHEMA COMPILERS
   # -----------------------------------------------------------------------
   def to_anthropic_tools(self) -> List[Dict[str, Any]]:
       return [
           {
               "name": t["name"],
               "description": t["description"],
               "input_schema": t["parameters"],
           }
           for t in self._tools.values()
       ]
   def to_openai_tools(self) -> List[Dict[str, Any]]:
       return [
           {
               "type": "function",
               "function": {
                   "name": t["name"],
                   "description": t["description"],
                   "parameters": t["parameters"],
               },
           }
           for t in self._tools.values()
       ]
   def to_gemini_tools(self) -> List[genai_types.Tool]:
       declarations = [
           genai_types.FunctionDeclaration(
               name=t["name"],
               description=t["description"],
               parameters=t["parameters"],
           )
           for t in self._tools.values()
       ]
       return [genai_types.Tool(function_declarations=declarations)] if declarations else []
   # -----------------------------------------------------------------------
   # SAFE ASYNC TOOL EXECUTOR
   # -----------------------------------------------------------------------
   async def execute(self, tool_name: str, raw_arguments_json: str) -> Dict[str, Any]:
       if tool_name not in self._tools:
           output = json.dumps({"error": f"Unknown tool '{tool_name}'."})
           return {
               "tool_name": tool_name,
               "raw_output": output,
               "token_weight": tg.TOKEN_ENGINE.count_tokens(output),
               "ok": False,
           }
       try:
           args = json.loads(raw_arguments_json) if raw_arguments_json.strip() else {}
       except json.JSONDecodeError as exc:
           output = json.dumps({
               "error": f"Invalid JSON arguments for tool '{tool_name}': {exc}",
               "raw_received": raw_arguments_json,
           })
           return {
               "tool_name": tool_name,
               "raw_output": output,
               "token_weight": tg.TOKEN_ENGINE.count_tokens(output),
               "ok": False,
           }
       handler = self._tools[tool_name]["handler"]
       try:
           if inspect.iscoroutinefunction(handler):
               result = await handler(**args)
           else:
               result = await asyncio.to_thread(handler, **args)
           output = result if isinstance(result, str) else json.dumps(result, indent=2)
           ok = True
       except Exception as exc:
           output = json.dumps({"error": f"Tool execution failed: {str(exc)}"})
           ok = False
       return {
           "tool_name": tool_name,
           "raw_output": output,
           "token_weight": tg.TOKEN_ENGINE.count_tokens(output),
           "ok": ok,
       }
   # -----------------------------------------------------------------------
   # BUILT-IN WORKSPACE & SKILL TOOLS FOR THE AGENT
   # -----------------------------------------------------------------------
   def _register_default_workspace_tools(self) -> None:
       def read_workspace_file(path: str, start_line: int = 1, end_line: int = 120) -> str:
           target = Path(path).resolve()
           if not target.exists() or not target.is_file():
               return json.dumps({"error": f"File '{path}' not found."})
           lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
           sliced = lines[max(0, start_line - 1) : min(len(lines), end_line)]
           return "\n".join(f"{idx}: {line}" for idx, line in enumerate(sliced, start=start_line))
       def search_github_skills(query: str) -> str:
           res = tg.STORE.match_relevant_skills(query, top_k=3)
           return json.dumps(res["matched_skills"], indent=2)
       self.register(
           name="read_workspace_file",
           description="Read lines from a local source file in the workspace.",
           parameters_schema={
               "type": "object",
               "properties": {
                   "path": {"type": "string", "description": "Relative file path"},
                   "start_line": {"type": "integer", "description": "1-based start line"},
                   "end_line": {"type": "integer", "description": "1-based end line"},
               },
               "required": ["path"],
           },
           handler=read_workspace_file,
       )
       self.register(
           name="search_github_skills",
           description="Search the indexed GitHub Skill Library for relevant SKILL.md modules.",
           parameters_schema={
               "type": "object",
               "properties": {
                   "query": {"type": "string", "description": "Search keywords or task description"},
               },
               "required": ["query"],
           },
           handler=search_github_skills,
       )
TOOL_REGISTRY = AgentToolRegistry()
class MultiProviderStreamRouter:
   """
   Extended with native multi-turn tool call assembly, tool_call_id tracking,
   and provider-specific tool_result replay from the DAG lineage.
   """
   def __init__(self, tool_registry: AgentToolRegistry) -> None:
       self.registry = tool_registry
       self.anthropic_client = (
           anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
           if os.environ.get("ANTHROPIC_API_KEY")
           else None
       )
       self.gemini_client = (
           genai.Client(api_key=os.environ["GEMINI_API_KEY"])
           if os.environ.get("GEMINI_API_KEY")
           else None
       )
       self.openai_client = (
           openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
           if os.environ.get("OPENAI_API_KEY")
           else None
       )
   def detect_provider(self, model: str) -> Literal["anthropic", "gemini", "openai"]:
       m = model.lower()
       if "claude" in m or "anthropic" in m:
           return "anthropic"
       if "gemini" in m:
           return "gemini"
       return "openai"
   async def stream_turn(
       self,
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str = "",
       enable_tools: bool = True,
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       provider = self.detect_provider(model)
       if provider == "anthropic":
           tools = self.registry.to_anthropic_tools() if enable_tools else None
           async for chunk in self._stream_anthropic(
               model, system_prompt, lineage_messages, max_output_tokens, assistant_prefill, tools
           ):
               yield chunk
       elif provider == "gemini":
           tools = self.registry.to_gemini_tools() if enable_tools else None
           async for chunk in self._stream_gemini(
               model, system_prompt, lineage_messages, max_output_tokens, assistant_prefill, tools
           ):
               yield chunk
       else:
           tools = self.registry.to_openai_tools() if enable_tools else None
           async for chunk in self._stream_openai(
               model, system_prompt, lineage_messages, max_output_tokens, assistant_prefill, tools
           ):
               yield chunk
   # -----------------------------------------------------------------------
   # PROVIDER MESSAGE FORMATTERS WITH NATIVE TOOL_CALL + TOOL_RESULT REPLAY
   # -----------------------------------------------------------------------
   @staticmethod
   def _format_messages_for_anthropic(lineage: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
       formatted: List[Dict[str, Any]] = []
       for msg in lineage:
           role = msg.get("role", "user")
           channel = msg.get("channel")
           content = msg.get("content", "")
           tool_name = msg.get("tool_name") or "workspace_tool"
           tool_call_id = msg.get("tool_call_id") or f"toolu_{tool_name}"
           if role == "system":
               formatted.append({"role": "user", "content": content})
           elif channel == "thinking":
               if msg.get("is_user_modified"):
                   formatted.append({"role": "user", "content": content})
               else:
                   formatted.append({"role": "assistant", "content": content})
           elif channel == "tool_call":
               try:
                   parsed_input = json.loads(content) if content.strip() else {}
               except Exception:
                   parsed_input = {"raw": content}
               formatted.append({
                   "role": "assistant",
                   "content": [
                       {
                           "type": "tool_use",
                           "id": tool_call_id,
                           "name": tool_name,
                           "input": parsed_input,
                       }
                   ],
               })
           elif role == "tool":
               formatted.append({
                   "role": "user",
                   "content": [
                       {
                           "type": "tool_result",
                           "tool_use_id": tool_call_id,
                           "content": content,
                       }
                   ],
               })
           else:
               formatted.append({"role": "assistant" if role == "assistant" else "user", "content": content})
       return formatted
   @staticmethod
   def _format_messages_for_gemini(lineage: List[Dict[str, Any]]) -> List[genai_types.Content]:
       contents: List[genai_types.Content] = []
       for msg in lineage:
           role = msg.get("role", "user")
           channel = msg.get("channel")
           content = str(msg.get("content", ""))
           tool_name = msg.get("tool_name") or "workspace_tool"
           if channel == "tool_call":
               try:
                   args = json.loads(content) if content.strip() else {}
               except Exception:
                   args = {"raw": content}
               contents.append(
                   genai_types.Content(
                       role="model",
                       parts=[genai_types.Part.from_function_call(name=tool_name, args=args)],
                   )
               )
           elif role == "tool":
               contents.append(
                   genai_types.Content(
                       role="user",
                       parts=[
                           genai_types.Part.from_function_response(
                               name=tool_name,
                               response={"result": content},
                           )
                       ],
                   )
               )
           else:
               gemini_role = "model" if role == "assistant" else "user"
               contents.append(
                   genai_types.Content(
                       role=gemini_role,
                       parts=[genai_types.Part.from_text(text=content)],
                   )
               )
       return contents
   @staticmethod
   def _format_messages_for_openai(lineage: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
       oa: List[Dict[str, Any]] = []
       for msg in lineage:
           role = msg.get("role", "user")
           channel = msg.get("channel")
           content = str(msg.get("content", ""))
           tool_name = msg.get("tool_name") or "workspace_tool"
           tool_call_id = msg.get("tool_call_id") or f"call_{tool_name}"
           if channel == "tool_call":
               oa.append({
                   "role": "assistant",
                   "content": None,
                   "tool_calls": [
                       {
                           "id": tool_call_id,
                           "type": "function",
                           "function": {
                               "name": tool_name,
                               "arguments": content if content.strip() else "{}",
                           },
                       }
                   ],
               })
           elif role == "tool":
               oa.append({
                   "role": "tool",
                   "tool_call_id": tool_call_id,
                   "content": content,
               })
           elif role in ("system", "user", "assistant"):
               oa.append({"role": role, "content": content})
       return oa
# NOTE (from the source chat): also add "tool_call_id": p.get("tool_call_id") to the
# TOOL_CALL and TOOL_RESULT entries in _compile_ancestor_lineage, and log TOOL_RESULT
# (delta_input_tokens = tool_output_token_weight) to the DAG.
MAX_AGENT_TOOL_LOOPS = 8
async def wait_for_user_budget_resolution(
   session_id: str,
   interrupt_node_id: str,
   interrupted_at_node_id: str,
   cumulative_tokens: int,
   approved_ceiling: int,
   reason_message: str,
) -> str:
   """
   Freezes the coroutine on an asyncio.Event until the user clicks
   '+Extra Tokens' in the UI, then returns any saved Assistant Prefill buffer.
   """
   gate = asyncio.Event()
   MANAGER.pause_gates[session_id] = gate
   await MANAGER.broadcast(
       session_id,
       {
           "type": "BUDGET_CIRCUIT_BREAKER_TRIPPED",
           "interrupt_node_id": interrupt_node_id,
           "interrupted_at_node_id": interrupted_at_node_id,
           "cumulative_tokens": cumulative_tokens,
           "approved_ceiling": approved_ceiling,
           "message": reason_message,
       },
   )
   await gate.wait()
   MANAGER.pause_gates.pop(session_id, None)
   with tg.STORE._conn() as conn:
       int_node = conn.execute(
           "SELECT payload_json FROM dag_nodes WHERE node_id = ?",
           (interrupt_node_id,),
       ).fetchone()
       if int_node:
           int_payload = json.loads(int_node["payload_json"])
           return int_payload.get("partial_assistant_buffer", "")
   return ""
async def execute_budget_gated_pipeline(
   session_id: str,
   lock_node_id: str,
   prompt: str,
   matched_skills: List[str],
   start_parent_node_id: Optional[str] = None,
) -> None:
   """
   Full Agentic Execution Loop:
     [LLM Stream] -> [THOUGHT_BLOCK] -> [TOOL_CALL] -> [Auto-Execute Tool]
     -> [Log TOOL_RESULT Node] -> [Budget Firewall Check] -> [Loop Back to LLM]
     -> [Final ASSISTANT_OUTPUT]
   """
   current_parent_id = start_parent_node_id or lock_node_id
   # 1. Lazy-load approved GitHub skills into the DAG
   for skill_name in matched_skills:
       skill_node = tg.load_approved_github_skill(
           session_id=session_id,
           parent_node_id=current_parent_id,
           skill_name=skill_name,
       )
       if "skill_load_node_id" in skill_node:
           current_parent_id = skill_node["skill_load_node_id"]
           await MANAGER.broadcast_dag_snapshot(session_id)
   assistant_prefill_buffer = ""
   loop_iteration = 0
   # 2. Multi-Turn Agentic Loop (Continues as long as the model emits TOOL_CALLs
   #    or resumes after a Mid-Flight Budget Interrupt)
   while loop_iteration < MAX_AGENT_TOOL_LOOPS:
       loop_iteration += 1
       with tg.STORE._conn() as conn:
           sess = conn.execute(
               "SELECT model FROM trace_sessions WHERE session_id = ?", (session_id,)
           ).fetchone()
           model = sess["model"] if sess else "claude-sonnet-5-5"
           parent_node = conn.execute(
               "SELECT tree_path, cumulative_branch_tokens, approved_budget_ceiling "
               "FROM dag_nodes WHERE node_id = ?",
               (current_parent_id,),
           ).fetchone()
           if not parent_node:
               break
           lineage_messages = tg._compile_ancestor_lineage(conn, session_id, parent_node["tree_path"])
           cum_tokens_at_turn_start = parent_node["cumulative_branch_tokens"]
           approved_ceiling = parent_node["approved_budget_ceiling"]
       remaining_token_budget = max(64, approved_ceiling - cum_tokens_at_turn_start)
       active_channel: Optional[Literal["THOUGHT_BLOCK", "TOOL_CALL", "ASSISTANT_OUTPUT"]] = None
       channel_buffer = ""
       channel_tool_name: Optional[str] = None
       channel_tool_call_id: Optional[str] = None
       channel_signature: Optional[str] = None
       live_turn_tokens = 0
       tripped_mid_stream = False
       # Queue of tool calls emitted during this LLM turn that need automatic execution
       pending_tool_calls: List[Dict[str, str]] = []
       async def flush_active_channel_to_dag() -> Optional[Dict[str, Any]]:
           nonlocal current_parent_id, channel_buffer, active_channel
           nonlocal channel_tool_name, channel_tool_call_id, channel_signature
           if not active_channel or not channel_buffer.strip():
               return None
           full_text = (
               (assistant_prefill_buffer + channel_buffer)
               if active_channel == "ASSISTANT_OUTPUT" and assistant_prefill_buffer
               else channel_buffer
           )
           step_res = tg.record_dag_step(
               session_id=session_id,
               parent_node_id=current_parent_id,
               node_type=active_channel,
               content=full_text,
               tool_name=channel_tool_name,
               provider_signature=channel_signature,
           )
           current_parent_id = step_res["node_id"]
           await MANAGER.broadcast_dag_snapshot(session_id)
           # If we just committed a TOOL_CALL node, enqueue it for automatic execution!
           if active_channel == "TOOL_CALL":
               pending_tool_calls.append({
                   "tool_call_node_id": step_res["node_id"],
                   "tool_name": channel_tool_name or "workspace_tool",
                   "tool_call_id": channel_tool_call_id or f"call_{step_res['node_id']}",
                   "arguments_json": full_text,
               })
           channel_buffer = ""
           channel_tool_name = None
           channel_tool_call_id = None
           channel_signature = None
           return step_res
       # -------------------------------------------------------------------
       # A. STREAM LIVE CHUNKS FROM CLAUDE / GEMINI / OPENAI
       # -------------------------------------------------------------------
       async for chunk in STREAM_ROUTER.stream_turn(
           model=model,
           system_prompt="You are operating inside the TraceGate Token Firewall & Execution DAG.",
           lineage_messages=lineage_messages,
           max_output_tokens=remaining_token_budget,
           assistant_prefill=assistant_prefill_buffer,
           enable_tools=True,
       ):
           # Flush if switching between THOUGHT_BLOCK -> TOOL_CALL -> ASSISTANT_OUTPUT,
           # or if the model emits multiple distinct tool calls in one turn
           switching_channel = active_channel and chunk.channel != active_channel
           switching_tool_id = (
               active_channel == "TOOL_CALL"
               and chunk.tool_call_id
               and channel_tool_call_id
               and chunk.tool_call_id != channel_tool_call_id
           )
           if (switching_channel or switching_tool_id) and channel_buffer.strip():
               await flush_active_channel_to_dag()
           active_channel = chunk.channel
           if chunk.tool_name:
               channel_tool_name = chunk.tool_name
           if chunk.tool_call_id:
               channel_tool_call_id = chunk.tool_call_id
           if chunk.provider_signature:
               channel_signature = chunk.provider_signature
           if chunk.delta_text:
               channel_buffer += chunk.delta_text
               delta_tok = tg.TOKEN_ENGINE.count_tokens(chunk.delta_text)
               live_turn_tokens += delta_tok
               await MANAGER.broadcast(
                   session_id,
                   {
                       "type": "LIVE_STREAM_DELTA",
                       "channel": chunk.channel,
                       "tool_name": channel_tool_name,
                       "delta": chunk.delta_text,
                       "delta_tokens": delta_tok,
                       "live_cumulative_tokens": cum_tokens_at_turn_start + live_turn_tokens,
                       "approved_ceiling": approved_ceiling,
                   },
               )
               # Hard Mid-Flight Circuit Breaker Check during streaming
               if (cum_tokens_at_turn_start + live_turn_tokens) >= approved_ceiling:
                   tripped_mid_stream = True
                   break
       final_step_res = await flush_active_channel_to_dag()
       # Clear prefill buffer once consumed
       assistant_prefill_buffer = ""
       # -------------------------------------------------------------------
       # B. CHECKPOINT 1: DID THE STREAM TRIP THE BUDGET MID-GENERATION?
       # -------------------------------------------------------------------
       if tripped_mid_stream or (final_step_res and final_step_res.get("circuit_breaker_tripped")):
           int_id = final_step_res.get("interrupt_node_id") if final_step_res else current_parent_id
           assistant_prefill_buffer = await wait_for_user_budget_resolution(
               session_id=session_id,
               interrupt_node_id=int_id,
               interrupted_at_node_id=final_step_res["node_id"] if final_step_res else current_parent_id,
               cumulative_tokens=final_step_res["cumulative_branch_tokens"] if final_step_res else approved_ceiling,
               approved_ceiling=approved_ceiling,
               reason_message=(
                   f"Paused mid-stream at {approved_ceiling:,} tokens. "
                   "Approve extra tokens to resume from the exact cutoff via Assistant Prefill."
               ),
           )
           current_parent_id = int_id
           continue
       # -------------------------------------------------------------------
       # C. IF NO TOOL CALLS WERE EMITTED, THE TURN IS COMPLETE!
       # -------------------------------------------------------------------
       if not pending_tool_calls:
           await MANAGER.broadcast(
               session_id,
               {
                   "type": "EXECUTION_COMPLETE",
                   "head_node_id": current_parent_id,
                   "tool_loops_executed": loop_iteration - 1,
               },
           )
           break
       # -------------------------------------------------------------------
       # D. AUTO-EXECUTE EVERY PENDING TOOL CALL & LOG `TOOL_RESULT` NODES
       # -------------------------------------------------------------------
       for tc in pending_tool_calls:
           await MANAGER.broadcast(
               session_id,
               {
                   "type": "TOOL_EXECUTION_STARTED",
                   "tool_name": tc["tool_name"],
                   "tool_call_node_id": tc["tool_call_node_id"],
               },
           )
           exec_result = await TOOL_REGISTRY.execute(
               tool_name=tc["tool_name"],
               raw_arguments_json=tc["arguments_json"],
           )
           # Commit the TOOL_RESULT node to SQLite DAG
           tool_res_step = tg.record_dag_step(
               session_id=session_id,
               parent_node_id=current_parent_id,
               node_type="TOOL_RESULT",
               content=exec_result["raw_output"],
               tool_name=tc["tool_name"],
           )
           current_parent_id = tool_res_step["node_id"]
           await MANAGER.broadcast_dag_snapshot(session_id)
           # ---------------------------------------------------------------
           # E. CHECKPOINT 2: POST-TOOL-RESULT FIREWALL GATE
           #    Did the tool output blow past the budget ceiling before we
           #    re-prompt the LLM on the next loop iteration?
           # ---------------------------------------------------------------
           if tool_res_step.get("circuit_breaker_tripped") or tool_res_step["budget_utilization_pct"] >= 95.0:
               int_id = tool_res_step.get("interrupt_node_id")
               if not int_id:
                   # Insert an explicit pre-next-turn interrupt node if >=95% utilized
                   with tg.STORE._conn() as conn:
                       int_id = tg._insert_dag_node(
                           conn=conn,
                           session_id=session_id,
                           parent_node_id=current_parent_id,
                           node_type="BUDGET_INTERRUPT",
                           status="PAUSED_OVER_BUDGET",
                           delta_in=0,
                           delta_thought=0,
                           delta_out=0,
                           ceiling=tool_res_step["approved_budget_ceiling"],
                           step_usd=0.0,
                           payload={
                               "interrupted_at_node": current_parent_id,
                               "interrupted_stage": "BEFORE_NEXT_TOOL_TURN",
                               "tool_name": tc["tool_name"],
                               "tool_result_tokens": exec_result["token_weight"],
                               "cumulative_tokens": tool_res_step["cumulative_branch_tokens"],
                               "approved_ceiling": tool_res_step["approved_budget_ceiling"],
                           },
                       )
                   await MANAGER.broadcast_dag_snapshot(session_id)
               await wait_for_user_budget_resolution(
                   session_id=session_id,
                   interrupt_node_id=int_id,
                   interrupted_at_node_id=tool_res_step["node_id"],
                   cumulative_tokens=tool_res_step["cumulative_branch_tokens"],
                   approved_ceiling=tool_res_step["approved_budget_ceiling"],
                   reason_message=(
                       f"Tool `{tc['tool_name']}` returned {exec_result['token_weight']:,} tokens, "
                       f"bringing branch usage to {tool_res_step['cumulative_branch_tokens']:,} / "
                       f"{tool_res_step['approved_budget_ceiling']:,} tokens. "
                       f"Approve extra tokens to run the next LLM turn, or prune & fork node `{tool_res_step['node_id']}`."
                   ),
               )
               current_parent_id = int_id


# Module-level singletons used by the test suite and the FastAPI bridge wiring.
# NOTE: the FastAPI app / ConnectionManager (MANAGER) and WebSocket endpoints are NOT
# in the source chat export; they must be added before this can run as a server.
STREAM_ROUTER = MultiProviderStreamRouter(tool_registry=TOOL_REGISTRY)
