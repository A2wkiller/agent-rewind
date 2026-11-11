import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional
import anthropic
import openai
from google import genai
from google.genai import types as genai_types
import tracegate_mcp as tg
# ---------------------------------------------------------------------------
# 1. UNIFIED STREAM EVENT & PROVIDER CLIENTS
# ---------------------------------------------------------------------------
@dataclass
class UnifiedStreamChunk:
   channel: Literal["THOUGHT_BLOCK", "TOOL_CALL", "ASSISTANT_OUTPUT"]
   delta_text: str = ""
   tool_name: Optional[str] = None
   tool_call_id: Optional[str] = None
   provider_signature: Optional[str] = None
class MultiProviderStreamRouter:
   """
   Normalizes live async token streams across Anthropic Claude, Google Gemini,
   and OpenAI into UnifiedStreamChunk events, with native support for:
     - Dynamic `max_tokens` ceiling enforcement
     - Mid-sentence Assistant Prefill continuation after a budget interrupt
     - Thought signature capture for counterfactual DAG backtracking
   """
   def __init__(self) -> None:
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
       max_Output_tokens: int,
       assistant_prefill: str = "",
       tools: Optional[List[Dict[str, Any]]] = None,
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       provider = self.detect_provider(model)
       if provider == "anthropic":
           async for chunk in self._stream_anthropic(
               model, system_prompt, lineage_messages, max_Output_tokens, assistant_prefill, tools
           ):
               yield chunk
       elif provider == "gemini":
           async for chunk in self._stream_gemini(
               model, system_prompt, lineage_messages, max_Output_tokens, assistant_prefill, tools
           ):
               yield chunk
       else:
           async for chunk in self._stream_openai(
               model, system_prompt, lineage_messages, max_Output_tokens, assistant_prefill, tools
           ):
               yield chunk
   # -----------------------------------------------------------------------
   # A. ANTHROPIC CLAUDE ASYNC STREAMING + NATIVE ASSISTANT PREFILL
   # -----------------------------------------------------------------------
   async def _stream_anthropic(
       self,
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str,
       tools: Optional[List[Dict[str, Any]]],
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       if not self.anthropic_client:
           raise RuntimeError("ANTHROPIC_API_KEY is not set in environment.")
       api_messages = self._format_messages_for_anthropic(lineage_messages)
       # Native Assistant Prefill: if resuming after a mid-flight budget interrupt,
       # append the partial assistant text as the final message so Claude continues seamlessly.
       if assistant_prefill.strip():
           api_messages.append({
               "role": "assistant",
               "content": [{"type": "text", "text": assistant_prefill.rstrip()}],
           })
       kwargs: Dict[str, Any] = {
           "model": model,
           "max_tokens": max(64, max_output_tokens),
           "messages": api_messages,
           # Cache the system prompt automatically to get 90% cached input discount
           "system": [
               {
                   "type": "text",
                   "text": system_prompt,
                   "cache_control": {"type": "ephemeral"},
               }
           ] if system_prompt else [],
       }
       # Enable extended thinking when not inside an assistant-prefill text continuation
       # (Anthropic disallows simultaneously prefilling final assistant text + new thinking blocks)
       if not assistant_prefill.strip() and max_output_tokens >= 1200:
           thinking_budget = max(1024, min(max_output_tokens // 2, 4096))
           kwargs["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
       if tools:
           kwargs["tools"] = tools
       current_tool_name: Optional[str] = None
       current_tool_id: Optional[str] = None
       async with self.anthropic_client.messages.stream(**kwargs) as stream:
           async for event in stream:
               etype = event.type
               if etype == "content_block_start":
                   block = event.content_block
                   if block.type == "tool_use":
                       current_tool_name = block.name
                       current_tool_id = block.id
               elif etype == "content_block_delta":
                   delta = event.delta
                   if delta.type == "thinking_delta":
                       yield UnifiedStreamChunk(
                           channel="THOUGHT_BLOCK",
                           delta_text=delta.thinking,
                       )
                   elif delta.type == "signature_delta":
                       yield UnifiedStreamChunk(
                           channel="THOUGHT_BLOCK",
                           delta_text="",
                           provider_signature=delta.signature,
                       )
                   elif delta.type == "input_json_delta":
                       yield UnifiedStreamChunk(
                           channel="TOOL_CALL",
                           delta_text=delta.partial_json,
                           tool_name=current_tool_name,
                           tool_call_id=current_tool_id,
                       )
                   elif delta.type == "text_delta":
                       yield UnifiedStreamChunk(
                           channel="ASSISTANT_OUTPUT",
                           delta_text=delta.text,
                       )
   # -----------------------------------------------------------------------
   # B. GOOGLE GEMINI ASYNC STREAMING (`google-genai` SDK) + CONTINUATION
   # -----------------------------------------------------------------------
   async def _stream_gemini(
       self,
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str,
       tools: Optional[List[Dict[str, Any]]],
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       if not self.gemini_client:
           raise RuntimeError("GEMINI_API_KEY is not set in environment.")
       contents = self._format_messages_for_gemini(lineage_messages)
       # Mid-stream continuation: inject the partial output as a model turn and
       # anchor continuation from the exact trailing substring
       if assistant_prefill.strip():
           tail_anchor = assistant_prefill[-120:]
           contents.append(
               genai_types.Content(
                   role="model",
                   parts=[genai_types.Part.from_text(text=assistant_prefill)],
               )
           )
           contents.append(
               genai_types.Content(
                   role="user",
                   parts=[
                       genai_types.Part.from_text(
                           text=(
                               "Continue generating from the exact point where you paused. "
                               f"Do not repeat earlier text. Resume immediately after: `{tail_anchor}`"
                           )
                       )
                   ],
               )
           )
       config = genai_types.GenerateContentConfig(
           system_instruction=system_prompt if system_prompt else None,
           max_output_tokens=max(64, max_output_tokens),
           thinking_config=genai_types.ThinkingConfig(include_thoughts=True),
       )
       response_stream = await self.gemini_client.aio.models.generate_content_stream(
           model=model,
           contents=contents,
           config=config,
       )
       async for chunk in response_stream:
           if not chunk.candidates:
               continue
           content = chunk.candidates[0].content
           if not content or not content.parts:
               continue
           for part in content.parts:
               if getattr(part, "thought", False) and part.text:
                   yield UnifiedStreamChunk(
                       channel="THOUGHT_BLOCK",
                       delta_text=part.text,
                       provider_signature=getattr(part, "thought_signature", None),
                   )
               elif getattr(part, "function_call", None):
                   fn = part.function_call
                   yield UnifiedStreamChunk(
                       channel="TOOL_CALL",
                       delta_text=json.dumps(dict(fn.args) if fn.args else {}),
                       tool_name=fn.name,
                       tool_call_id=getattr(fn, "id", f"call_{fn.name}"),
                   )
               elif part.text:
                   yield UnifiedStreamChunk(
                       channel="ASSISTANT_OUTPUT",
                       delta_text=part.text,
                   )
   # -----------------------------------------------------------------------
   # C. OPENAI ASYNC STREAMING + PREFIX-ANCHORED CONTINUATION
   # -----------------------------------------------------------------------
   async def _stream_openai(
       self,
       model: str,
       system_prompt: str,
       lineage_messages: List[Dict[str, Any]],
       max_output_tokens: int,
       assistant_prefill: str,
       tools: Optional[List[Dict[str, Any]]],
   ) -> AsyncGenerator[UnifiedStreamChunk, None]:
       if not self.openai_client:
           raise RuntimeError("OPENAI_API_KEY is not set in environment.")
       oa_messages: List[Dict[str, Any]] = []
       if system_prompt:
           oa_messages.append({"role": "system", "content": system_prompt})
       oa_messages.extend(self._format_messages_for_openai(lineage_messages))
       if assistant_prefill.strip():
           tail_anchor = assistant_prefill[-120:]
           oa_messages.append({"role": "assistant", "content": assistant_prefill})
           oa_messages.append({
               "role": "user",
               "content": (
                   "Continue from the exact character where the previous assistant message stopped. "
                   f"Output ONLY the remaining continuation right after: `{tail_anchor}`"
               ),
           })
       kwargs: Dict[str, Any] = {
           "model": model,
           "messages": oa_messages,
           "max_completion_tokens": max(64, max_output_tokens),
           "stream": True,
       }
       if tools:
           kwargs["tools"] = tools
       stream = await self.openai_client.chat.completions.create(**kwargs)
       active_tool_name: Optional[str] = None
       active_tool_id: Optional[str] = None
       async for chunk in stream:
           if not chunk.choices:
               continue
           delta = chunk.choices[0].delta
           # Capture reasoning tokens / summaries when exposed by OpenAI reasoning models
           reasoning_txt = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
           if reasoning_txt:
               yield UnifiedStreamChunk(
                   channel="THOUGHT_BLOCK",
                   delta_text=str(reasoning_txt),
               )
           if delta.tool_calls:
               for tc in delta.tool_calls:
                   if tc.function and tc.function.name:
                       active_tool_name = tc.function.name
                   if tc.id:
                       active_tool_id = tc.id
                   if tc.function and tc.function.arguments:
                       yield UnifiedStreamChunk(
                           channel="TOOL_CALL",
                           delta_text=tc.function.arguments,
                           tool_name=active_tool_name,
                           tool_call_id=active_tool_id,
                       )
           if delta.content:
               yield UnifiedStreamChunk(
                   channel="ASSISTANT_OUTPUT",
                   delta_text=delta.content,
               )
   # -----------------------------------------------------------------------
   # D. DAG LINEAGE -> PROVIDER MESSAGE FORMATTERS
   # -----------------------------------------------------------------------
   @staticmethod
   def _format_messages_for_anthropic(lineage: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
       formatted: List[Dict[str, Any]] = []
       for msg in lineage:
           role = msg.get("role", "user")
           channel = msg.get("channel")
           content = msg.get("content", "")
           if role == "system":
               # Loaded GitHub skills are injected into the user context block
               formatted.append({"role": "user", "content": content})
           elif channel == "thinking":
               if msg.get("is_user_modified"):
                   # User-edited thoughts cannot replay a cryptographic signature
                   formatted.append({"role": "user", "content": content})
               else:
                   formatted.append({"role": "assistant", "content": content})
           elif role == "tool":
               formatted.append({
                   "role": "user",
                   "content": f"<tool_result tool=\"{msg.get('tool_name', 'tool')}\">\n{content}\n</tool_result>",
               })
           else:
               formatted.append({"role": "assistant" if role == "assistant" else "user", "content": content})
       return formatted
   @staticmethod
   def _format_messages_for_gemini(lineage: List[Dict[str, Any]]) -> List[genai_types.Content]:
       contents: List[genai_types.Content] = []
       for msg in lineage:
           role = "model" if msg.get("role") == "assistant" else "user"
           contents.append(
               genai_types.Content(
                   role=role,
                   parts=[genai_types.Part.from_text(text=str(msg.get("content", "")))],
               )
           )
       return contents
   @staticmethod
   def _format_messages_for_openai(lineage: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
       oa: List[Dict[str, Any]] = []
       for msg in lineage:
           role = msg.get("role", "user")
           if role == "tool":
               oa.append({
                   "role": "user",
                   "content": f"<tool_result name=\"{msg.get('tool_name', 'tool')}\">\n{msg.get('content', '')}\n</tool_result>"
               })
           elif role in ("system", "user", "assistant"):
               oa.append({"role": role, "content": str(msg.get("content", ""))})
       return oa
STREAM_ROUTER = MultiProviderStreamRouter()
# ---------------------------------------------------------------------------
# 2. REAL BUDGET-GATED PIPELINE WITH LIVE CIRCUIT BREAKER & PREFILL RESUME
# ---------------------------------------------------------------------------
async def execute_budget_gated_pipeline(
   session_id: str,
   lock_node_id: str,
   prompt: str,
   matched_skills: List[str],
   start_parent_node_id: Optional[str] = None,
) -> None:
   """
   Replaces the mock stage loop in `tracegate_bridge.py` with real async streaming
   across Anthropic, Gemini, and OpenAI.
   - Tracks live token burn on every SSE chunk.
   - Commits completed `THOUGHT_BLOCK`, `TOOL_CALL`, and `ASSISTANT_OUTPUT` nodes to SQLite.
   - Trips the Mid-Flight Circuit Breakerthe moment `cumulative_tokens >= approved_ceiling`,
     saves the partial buffer, waits on `MANAGER.pause_gates[session_id]`, and resumes via
     Assistant Prefill when the user grants extra tokens!
   """
   current_parent_id = start_parent_node_id or lock_node_id
   # Step 1: Lazy-Load Approved GitHub Skills into the DAG
   for skill_name in matched_skills:
       skill_node = tg.load_approved_github_skill(
           session_id=session_id,
           parent_node_id=current_parent_id,
           skill_name=skill_name,
       )
       if "skill_load_node_id" in skill_node:
           current_parent_id = skill_node["skill_load_node_id"]
           await MANAGER.broadcast_dag_snapshot(session_id)
   # Step 2: Outer Continuation Loop (Resumes automatically via Assistant Prefill after budget top-ups)
   assistant_prefill_buffer = ""
   while True:
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
       # Accumulators for the active streaming turn
       active_channel: Optional[Literal["THOUGHT_BLOCK", "TOOL_CALL", "ASSISTANT_OUTPUT"]] = None
       channel_buffer = ""
       channel_tool_name: Optional[str] = None
       channel_signature: Optional[str] = None
       live_turn_tokens = 0
       tripped_mid_stream = False
       async def flush_active_channel_to_dag() -> Optional[Dict[str, Any]]:
           nonlocal current_parent_id, channel_buffer, active_channel, channel_tool_name, channel_signature
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
           channel_buffer = ""
           channel_tool_name = None
           channel_signature = None
           return step_res
       # Stream real chunks from Anthropic / Gemini / OpenAI
       async for chunk in STREAM_ROUTER.stream_turn(
           model=model,
           system_prompt="You are operating inside the TraceGate Token Firewall & Execution DAG.",
           lineage_messages=lineage_messages,
           max_Output_tokens=remaining_token_budget,
           assistant_prefill=assistant_prefill_buffer,
       ):
           # If the stream transitions from THOUGHT_BLOCK -> TOOL_CALL or ASSISTANT_OUTPUT,
           # commit the previous block as an atomic DAG node immediately!
           if active_channel and chunk.channel != active_channel and channel_buffer.strip():
               await flush_active_channel_to_dag()
           active_channel = chunk.channel
           if chunk.tool_name:
               channel_tool_name = chunk.tool_name
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
                       "delta": chunk.delta_text,
                       "delta_tokens": delta_tok,
                       "live_cumulative_tokens": cum_tokens_at_turn_start + live_turn_tokens,
                       "approved_ceiling": approved_ceiling,
                   },
               )
               # Hard Mid-Flight Circuit Breaker Check on every streaming chunk
               if (cum_tokens_at_turn_start + live_turn_tokens) >= approved_ceiling:
                   tripped_mid_stream = True
                   break
       # Flush whatever was in flight when the stream finished or tripped the circuit breaker
       final_step_res = await flush_active_channel_to_dag()
       # If we didn't trip the budget ceiling, the turn completed cleanly!
       if not tripped_mid_stream and not (final_step_res and final_step_res.get("circuit_breaker_tripped")):
           await MANAGER.broadcast(
               session_id,
               {
                   "type": "EXECUTION_COMPLETE",
                   "head_node_id": current_parent_id,
               },
           )
           break
       # -------------------------------------------------------------------
       # MID-FLIGHT CIRCUIT BREAKER PAUSE & ASSISTANT PREFILL HANDOFF
       # -------------------------------------------------------------------
       interrupt_node_id = final_step_res.get("interrupt_node_id") if final_step_res else current_parent_id
       gate = asyncio.Event()
       MANAGER.pause_gates[session_id] = gate
       await MANAGER.broadcast(
           session_id,
           {
               "type": "BUDGET_CIRCUIT_BREAKER_TRIPPED",
               "interrupt_node_id": interrupt_node_id,
               "interrupted_at_node_id": final_step_res["node_id"] if final_step_res else current_parent_id,
               "cumulative_tokens": final_step_res["cumulative_branch_tokens"] if final_step_res else approved_ceiling,
               "approved_ceiling": approved_ceiling,
               "message": (
                   f"Paused mid-stream at {approved_ceiling:,} tokens. "
                   "Approve extra tokens to resume from the exact cutoff via Assistant Prefill."
               ),
           },
       )
       # Freeze execution until user clicks "+Extra Tokens" in the UI
       await gate.wait()
       MANAGER.pause_gates.pop(session_id, None)
       # Retrieve the saved partial buffer from the resolved BUDGET_INTERRUPT node
       with tg.STORE._conn() as conn:
           int_node = conn.execute(
               "SELECT payload_json FROM dag_nodes WHERE node_id = ?",
               (interrupt_node_id,),
           ).fetchone()
           if int_node:
               int_payload = json.loads(int_node["payload_json"])
               assistant_prefill_buffer = int_payload.get("partial_assistant_buffer", "")
       current_parent_id = interrupt_node_id
       # Loop now continues seamlessly with the newly expanded ceiling and `assistant_prefill_buffer`!
async def continue_generation_from_fork(session_id: str, forked_node_id: str) -> None:
   """
   Reconstructs ancestor context up to `forked_node_id` (including `<corrected_reasoning>`
   or pruned `TOOL_RESULT` payloads) and streams a real LLM completion along the new branch.
   """
   await execute_budget_gated_pipeline(
       session_id=session_id,
       lock_node_id=forked_node_id,
       prompt="Continue from forked node state",
       matched_skills=[],
       start_parent_node_id=forked_node_id,
   )
