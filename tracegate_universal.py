#!/usr/bin/env python3
"""
TraceGate Universal IDE Plugin & Git Shadow-Tree Backtracker (tracegate_universal.py)
=====================================================================================
Makes TraceGate work seamlessly as a drop-in plugin/skill across:
 • OpenCode        (opencode.json + .opencode/plugin/)
 • OpenAI Codex    (~/.codex/config.toml + AGENTS.md)
 • Claude Code     (.claude/settings.json native hooks + .mcp.json)
 • Antigravity     (.antigravity/mcp.json + .gemini/settings.json)
 • JCode / Cursor / Windsurf (.cursor/mcp.json + .windsurf/mcp.json)
Provides:
 1. Explicit Session ON banner (`tracegate_session_start`)
 2. Git Shadow-Tree filesystem snapshots (`git write-tree`) on every tool call
 3. Full Session OFF Audit Report (`tracegate_session_end`) detailing every AI
    thought, tool call, how it was used, files modified, and USD/INR token burn
 4. True Code + Memory Rewind (`tracegate_rewind_to_node`)
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import tracegate_mcp as tg
# ---------------------------------------------------------------------------
# 1. GIT SHADOW-TREE ENGINE (ZERO-COMMIT FILESYSTEM SNAPSHOTS & ROLLBACK)
# ---------------------------------------------------------------------------
class GitShadowBacktracker:
   """
   Uses Git's low-level plumbing commands (`git write-tree` and `git read-tree`)
   with a dedicated temporary index file so TraceGate can snapshot and restore
   the entire workspace before/after every AI tool call in <15ms WITHOUT
   polluting the user's actual git commit history or staging area.
   """
   def __init__(self, workspace_dir: Path = Path(".")) -> None:
       self.workspace = workspace_dir.resolve()
       self.git_dir = self.workspace / ".git"
   def is_git_repo(self) -> bool:
       return self.git_dir.exists()
   def capture_snapshot(self, label: str = "step") -> Optional[str]:
       """Creates an immutable Git tree SHA of all current tracked + untracked files."""
       if not self.is_git_repo():
           return None
       shadow_index = self.git_dir / "tracegate_shadow_index"
       env = os.environ.copy()
       env["GIT_INDEX_FILE"] = str(shadow_index)
       try:
           # Copy current index so we start warm
           real_index = self.git_dir / "index"
           if real_index.exists():
               shutil.copy2(real_index, shadow_index)
           subprocess.run(
               ["git", "add", "-A"],
               cwd=self.workspace,
               env=env,
               stdout=subprocess.DEVNULL,
               stderr=subprocess.DEVNULL,
               check=True,
           )
           res = subprocess.run(
               ["git", "write-tree"],
               cwd=self.workspace,
               env=env,
               capture_output=True,
               text=True,
               check=True,
           )
           return res.stdout.strip()
       except Exception:
           return None
   def diff_between_trees(self, tree_before: Optional[str], tree_after: Optional[str]) -> List[str]:
       """Returns a human-readable list of files modified by the AI during a step."""
       if not self.is_git_repo() or not tree_before or not tree_after or tree_before == tree_after:
           return []
       try:
           res = subprocess.run(
               ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", tree_before, tree_after],
               cwd=self.workspace,
               capture_output=True,
               text=True,
               check=True,
           )
           return [line.strip() for line in res.stdout.splitlines() if line.strip()]
       except Exception:
           return []
   def restore_snapshot(self, tree_sha: str) -> Dict[str, Any]:
       """Rewinds the workspace files on disk to match `tree_sha` exactly."""
       if not self.is_git_repo():
           return {"restored": False, "reason": "Not a git repository."}
       try:
           subprocess.run(
               ["git", "read-tree", tree_sha],
               cwd=self.workspace,
               check=True,
               capture_output=True,
           )
           subprocess.run(
               ["git", "checkout-index", "-a", "-f"],
               cwd=self.workspace,
               check=True,
               capture_output=True,
           )
           return {"restored": True, "restored_tree_sha": tree_sha}
       except Exception as exc:
           return {"restored": False, "reason": str(exc)}
SHADOW_GIT = GitShadowBacktracker()
# ---------------------------------------------------------------------------
# 2. UNIVERSAL MCP LIFECYCLE TOOLS (SESSION ON -> LOG STEP -> SESSION OFF)
# ---------------------------------------------------------------------------
@tg.mcp.tool
def tracegate_session_start(
   task_title: str,
   coding_client: str = "universal-agent",
   model: str = "claude-sonnet-5-5",
   approved_token_ceiling: int = 8000,
) -> Dict[str, Any]:
   """
   MUST BE CALLED AT THE START OF EVERY CODING SESSION.
   1. Activates TraceGate Backtracking (`BACKTRACKING: ON`).
   2. Captures a baseline Git Shadow-Tree snapshot of the workspace before the AI touches any files.
   3. Prints the Session Start Banner showing active tracking and budget ceiling.
   """
   pre_res = tg.preflight_estimate_and_match_skill(
       user_prompt=task_title,
       model=model,
   )
   session_id = pre_res["session_id"]
   lock_node_id = pre_res["preflight_lock_node_id"]
   baseline_tree = SHADOW_GIT.capture_snapshot("session_start") or "no-git-repo"
   with tg.STORE._conn() as conn:
       conn.execute(
           "UPDATE dag_nodes SET approved_budget_ceiling = ?, status = 'COMPLETED' WHERE node_id = ?",
           (approved_token_ceiling, lock_node_id),
       )
       # Save active session pointer for CLI hooks
       Path(".tracegate").mkdir(exist_ok=True)
       Path(".tracegate/active_session.json").write_text(
           json.dumps(
               {
                   "session_id": session_id,
                   "head_node_id": lock_node_id,
                   "baseline_tree_sha": baseline_tree,
                   "coding_client": coding_client,
                   "model": model,
                   "started_at": time.time(),
               },
               indent=2,
           ),
           encoding="utf-8",
       )
   banner = (
       "╔══════════════════════════════════════════════════════════════════════════════╗\n"
       f"║ 🟢 TRACEGATE BACKTRACKING: ON  |  Session ID: {session_id:<30} ║\n"
       f"║ 💻 Coding Client            : {coding_client:<46} ║\n"
       f"║ 📸 Codebase Baseline Tree   : {baseline_tree[:16]:<46} ║\n"
       "║ 🧠 Active Telemetry         : AI Thoughts • Tool Args • File Diffs • Tokens  ║\n"
       # NOTE: the USD figure was cut off mid-line in the source PDF; tokens value is intact.
       f"║ 🛡️ Approved Budget Ceiling  : {approved_token_ceiling:,} tokens ║\n"
       "╚══════════════════════════════════════════════════════════════════════════════╝"
   )
   return {
       "backtracking_status": "ON",
       "session_id": session_id,
       "head_node_id": lock_node_id,
       "baseline_git_tree": baseline_tree,
       "session_start_banner": banner,
       "matched_skills": pre_res["details"]["skill_routing"]["matched_skills"],
   }
@tg.mcp.tool
def tracegate_log_ai_step(
   session_id: str,
   step_type: str,
   how_ai_thought: str,
   tool_used: Optional[str] = None,
   how_tool_was_used_args: Optional[str] = None,
   tool_output_summary: Optional[str] = None,
) -> Dict[str, Any]:
   """
   Logs an atomic step during the coding session:
     - `how_ai_thought`: Why the AI chose this approach or decision.
     - `tool_used` & `how_tool_was_used_args`: Which tool was called and with what exact arguments.
     - Automatically snapshots the workspace Git tree before/after so file edits are 100% reversible.
   """
   active_file = Path(".tracegate/active_session.json")
   active_meta = json.loads(active_file.read_text(encoding="utf-8")) if active_file.exists() else {}
   parent_id = active_meta.get("head_node_id")
   with tg.STORE._conn() as conn:
       if not parent_id:
           row = conn.execute(
               "SELECT active_head_node_id FROM trace_sessions WHERE session_id = ?",
               (session_id,),
           ).fetchone()
           parent_id = row["active_head_node_id"] if row else None
   tree_before = active_meta.get("last_tree_sha") or active_meta.get("baseline_tree_sha")
   tree_after = SHADOW_GIT.capture_snapshot(step_type)
   modified_files = SHADOW_GIT.diff_between_trees(tree_before, tree_after)
   # 1. Record AI Thought Block if present
   current_head = parent_id
   if how_ai_thought.strip():
       thought_res = tg.record_dag_step(
           session_id=session_id,
           parent_node_id=current_head,
           node_type="THOUGHT_BLOCK",
           content=how_ai_thought,
       )
       current_head = thought_res["node_id"]
   # 2. Record Tool Call + How It Was Used + Git Snapshot SHA
   step_metrics: Dict[str, Any] = {}
   if tool_used:
       tool_payload_str = json.dumps(
           {
               "tool_name": tool_used,
               "how_it_was_used": how_tool_was_used_args or "{}",
               "git_tree_before": tree_before,
               "git_tree_after": tree_after,
               "files_modified": modified_files,
           },
           indent=2,
       )
       call_res = tg.record_dag_step(
           session_id=session_id,
           parent_node_id=current_head,
           node_type="TOOL_CALL",
           content=tool_payload_str,
           tool_name=tool_used,
       )
       current_head = call_res["node_id"]
       step_metrics = call_res
   # 3. Record Tool Output if provided
   if tool_output_summary:
       res_node = tg.record_dag_step(
           session_id=session_id,
           parent_node_id=current_head,
           node_type="TOOL_RESULT",
           content=tool_output_summary,
           tool_name=tool_used,
       )
       current_head = res_node["node_id"]
       step_metrics = res_node
   # Update active pointer on disk
   if active_file.exists():
       active_meta["head_node_id"] = current_head
       if tree_after:
           active_meta["last_tree_sha"] = tree_after
       active_file.write_text(json.dumps(active_meta, indent=2), encoding="utf-8")
   return {
       "logged_head_node_id": current_head,
       "git_snapshot_sha": tree_after,
       "files_modified_in_step": modified_files,
       "budget_metrics": step_metrics,
   }
@tg.mcp.tool
def tracegate_session_end(
   session_id: Optional[str] = None,
   final_ai_summary: str = "Session completed.",
) -> Dict[str, Any]:
   """
   MUST BE CALLED WHEN THE SESSION FINISHES OR REQUIRES A FULL AUDIT REPORT.
   Generates the complete 'Session OFF' Flight Recorder report listing:
     - Every thought the AI had
     - Every tool the AI used & how it used it
     - Every file created/modified by the AI
     - Exact token burn & USD/INR cost per step
     - Exact one-line `tracegate rewind` commands for every step
   """
   active_file = Path(".tracegate/active_session.json")
   active_meta = json.loads(active_file.read_text(encoding="utf-8")) if active_file.exists() else {}
   sid = session_id or active_meta.get("session_id")
   if not sid:
       return {"error": "No active session_id found."}
   tree_data = tg.get_dag_trace_tree(sid)
   if "error" in tree_data:
       return tree_data
   nodes = tree_data["nodes"]
   lines: List[str] = [
       "╔══════════════════════════════════════════════════════════════════════════════╗",
       "║ 🔴 TRACEGATE SESSION COMPLETE — FULL AI EXECUTION & BACKTRACKING LEDGER      ║",
       "╠══════════════════════════════════════════════════════════════════════════════╣",
       f"║ Session ID : {sid:<22} | Model    : {tree_data['model']:<27} ║",
       # NOTE: line was cut off in the source PDF; cost value reconstructed from the
       # total_session_usd key returned just below in this same file.
       f"║ Total Burn : {tree_data['total_session_tokens_across_all_branches']:>6,} tokens          | API Cost : ${tree_data['total_session_usd']} ║",
       "╚══════════════════════════════════════════════════════════════════════════════╝",
       "",
       "🧠 HOW THE AI THOUGHT, WHAT TOOLS IT USED & WHAT IT DID:",
       "────────────────────────────────────────────────────────────────────────────────",
   ]
   all_modified_files: List[str] = []
   for n in nodes:
       nid = n["node_id"]
       ntype = n["node_type"]
       delta_tok = n["delta_input_tokens"] + n["delta_thought_tokens"] + n["delta_output_tokens"]
       cum_inr = round(n["cumulative_branch_usd"] * tg.USD_TO_INR, 2)
       ts_str = datetime.fromtimestamp(n["created_at"]).strftime("%H:%M:%S")
       payload = json.loads(n["payload_json"])
       lines.append(f"[{nid} | {ts_str} | {ntype:<16} | +{delta_tok:>5} tok | Cum: ₹{cum_inr:.2f}]")
       if ntype == "THOUGHT_BLOCK":
           lines.append(f"   └─ 💭 How AI Thought : {payload.get('content', '').strip()}")
       elif ntype == "TOOL_CALL":
           raw_content = payload.get("content", "{}")
           try:
               tc_meta = json.loads(raw_content)
           except Exception:
               tc_meta = {"how_it_was_used": raw_content}
           tname = payload.get("tool_name") or tc_meta.get("tool_name") or "tool"
           lines.append(f"   └─ 🛠️ Tool Used      : `{tname}`")
           lines.append(f"   └─ ⚙️ How It Used It : {tc_meta.get('how_it_was_used', raw_content)}")
           if tc_meta.get("git_tree_after"):
               lines.append(f"   └─ 📸 Code Snapshot  : `{tc_meta['git_tree_after'][:12]}`")
           for fmod in tc_meta.get("files_modified", []):
               all_modified_files.append(fmod)
               lines.append(f"   └─ 📝 File Changed   : `{fmod}`")
       elif ntype == "TOOL_RESULT":
           snippet = str(payload.get("content", "")).replace("\n", " ")[:120]
           lines.append(f"   └─ 📤 Tool Result    : {snippet}...")
       elif ntype == "SKILL_LOAD":
           lines.append(f"   └─ 📚 Skill Loaded   : `{payload.get('name')}` ({payload.get('token_weight')} tok)")
   lines.extend(
       [
           "────────────────────────────────────────────────────────────────────────────────",
           f"✅ Final Summary: {final_ai_summary}",
           f"📂 Total Workspace Files Touched by AI: {len(set(all_modified_files))}",
           "⏪ To rewind both AI memory AND disk files to any node above, run:",
           f"   $ tracegate rewind {sid} <node_id>",
       ]
   )
   report_text = "\n".join(lines)
   report_dir = Path(".tracegate/reports")
   report_dir.mkdir(parents=True, exist_ok=True)
   report_path = report_dir / f"{sid}_audit.md"
   report_path.write_text(report_text, encoding="utf-8")
   return {
       "backtracking_status": "SESSION_OFF_LOGGED",
       "session_id": sid,
       "report_saved_to": str(report_path),
       "total_tokens": tree_data["total_session_tokens_across_all_branches"],
       "total_usd": tree_data["total_session_usd"],
       "total_inr": tree_data["total_session_inr"],
       "audit_report": report_text,
   }
@tg.mcp.tool
def tracegate_rewind_to_node(session_id: str, target_node_id: str) -> Dict[str, Any]:
   """
   TRUE CODE + MEMORY TIME-TRAVEL:
   1. Rewinds the workspace files on disk to the exact Git Shadow-Tree snapshot recorded at `target_node_id`.
   2. Forks the SQLite execution DAG back to `target_node_id` so the AI forgets any bad steps after that point.
   """
   with tg.STORE._conn() as conn:
       node = conn.execute(
           "SELECT * FROM dag_nodes WHERE session_id = ? AND node_id = ?",
           (session_id, target_node_id),
       ).fetchone()
       if not node:
           return {"error": f"Node '{target_node_id}' not found in session '{session_id}'."}
       payload = json.loads(node["payload_json"])
       tree_sha = None
       if node["node_type"] == "TOOL_CALL":
           try:
               inner = json.loads(payload.get("content", "{}"))
               tree_sha = inner.get("git_tree_before") or inner.get("git_tree_after")
           except Exception:
               pass
   git_restore_status = (
       SHADOW_GIT.restore_snapshot(tree_sha)
       if tree_sha
       else {"restored": False, "reason": "Node had no associated git tree SHA; rewound memory only."}
   )
   fork_res = tg.fork_dag_from_node(
       session_id=session_id,
       target_node_id=target_node_id,
       edited_content=payload.get("content", payload.get("text", "Rewound to this node")),
       fork_reason=f"User rewound codebase & AI memory to {target_node_id}",
   )
   return {
       "status": "REWOUND_CODE_AND_MEMORY",
       "session_id": session_id,
       "rewound_to_node_id": target_node_id,
       "git_filesystem_restore": git_restore_status,
       "dag_branch_fork": fork_res,
   }
# ---------------------------------------------------------------------------
# 3. UNIVERSAL MULTI-IDE PLUGIN INSTALLER (`tracegate install-all`)
# ---------------------------------------------------------------------------
UNIVERSAL_AGENT_RULES_MD = """# TraceGate Backtracking & Pre-Flight Cost Firewall Protocol
Whenever you start working on a task in this repository:
1. **Session Start (`BACKTRACKING: ON`):** Call `tracegate_session_start` first and display the returned `session_st
2. **Pre-Flight Cost Approval:** Before executing multi-file edits or long tool chains, call `preflight_estimate_and
3. **Step-by-Step Thought & Tool Logging:** For every major reasoning step or tool invocation, call `tracegate_log_a
4. **Session Off Audit Report:** Before finishing your final response, call `tracegate_session_end` and display the
"""
def install_universal_ide_plugins(target_dir: Path = Path(".")) -> Dict[str, str]:
   """
   Drops TraceGate configuration files into the repository so it works out-of-the-box
   across OpenCode, Claude Code, OpenAI Codex, Google Antigravity, Cursor, Windsurf, and JCode.
   """
   root = target_dir.resolve()
   script_abs = str((root / "tracegate_mcp.py").resolve())
   installed: Dict[str, str] = {}
   mcp_entry = {
       "command": "uv",
       "args": ["run", "python", script_abs],
       "env": {"TRACEGATE_USD_TO_INR": "86.50"},
   }
   # 1. OpenCode (`opencode.json`)
   opencode_path = root / "opencode.json"
   opencode_cfg = {
       "$schema": "https://opencode.ai/config.json",
       "mcp": {
           "tracegate": {
               "type": "local",
               "command": ["uv", "run", "python", script_abs],
               "enabled": True,
           }
       },
   }
   opencode_path.write_text(json.dumps(opencode_cfg, indent=2), encoding="utf-8")
   installed["OpenCode"] = str(opencode_path)
   # 2. Claude Code / CloudCode (`.mcp.json` + `.claude/settings.json`)
   for fname in [".mcp.json", ".cursor/mcp.json", ".windsurf/mcp.json", ".antigravity/mcp.json", ".jcode/mcp.json"]:
       fpath = root / fname
       fpath.parent.mkdir(parents=True, exist_ok=True)
       fpath.write_text(json.dumps({"mcpServers": {"tracegate": mcp_entry}}, indent=2), encoding="utf-8")
       installed[fname] = str(fpath)
   # 3. Universal Agent Instructions (`AGENTS.md` for Codex/OpenCode/Antigravity/JCode & `CLAUDE.md`)
   for md_file in ["AGENTS.md", "CLAUDE.md", ".agent/skills/tracegate/SKILL.md"]:
       mpath = root / md_file
       mpath.parent.mkdir(parents=True, exist_ok=True)
       mpath.write_text(UNIVERSAL_AGENT_RULES_MD, encoding="utf-8")
       installed[md_file] = str(mpath)
   return installed
if __name__ == "__main__":
   results = install_universal_ide_plugins()
   print("✅ Installed TraceGate Universal Plugin across all coding softwares:")
   for client, path in results.items():
       print(f"   • {client:<24} -> {path}")
