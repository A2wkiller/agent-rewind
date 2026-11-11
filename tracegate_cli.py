#!/usr/bin/env python3
"""
TraceGate Unified CLI (tracegate_cli.py)
========================================
Commands:
 tracegate up                        -> Launches the FastAPI WebSocket Bridge + Web UI on :8000
 tracegate mcp [--transport stdio]   -> Runs the TraceGate MCP Server (stdio or sse)
 tracegate sync <owner/repo>         -> Indexes all SKILL.md files from a GitHub repo
 tracegate estimate "<prompt>"       -> Runs a terminal Pre-Flight Token & USD/INR Cost audit
 tracegate tree <session_id>         -> Prints the visual ASCII DAG execution tree & cost ledger
 tracegate test                      -> Runs the automated pytest verification suite
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import subprocess
import sys
import webbrowser
from typing import Optional
import uvicorn
import tracegate_mcp as tg
def cmd_up(args: argparse.Namespace) -> None:
   """Starts the FastAPI WebSocket Bridge + Interactive Dark UI."""
   os.environ["TRACEGATE_DB_PATH"] = args.db
   os.environ["TRACEGATE_USD_TO_INR"] = str(args.inr_rate)
   url = f"http://{args.host}:{args.port}"
   print(f"\n⚡ TraceGate Execution Proxy & DAG Backtracker running at: {url}")
   print(f"📦 SQLite WAL Database: {os.path.abspath(args.db)}")
   print(f"💱 Exchange Rate: $1.00 USD = ₹{args.inr_rate:.2f} INR\n")
   if args.open_browser:
       webbrowser.open(url)
   uvicorn.run(
       "tracegate_bridge:app",
       host=args.host,
       port=args.port,
       reload=args.reload,
       log_level="info",
   )
def cmd_mcp(args: argparse.Namespace) -> None:
   """Runs the FastMCP server over stdio (for Claude/Cursor) or SSE (for Docker/remote)."""
   os.environ["TRACEGATE_DB_PATH"] = args.db
   if args.transport == "sse":
       tg.mcp.run(transport="sse", host=args.host, port=args.port)
   else:
       tg.mcp.run(transport="stdio")
def cmd_sync(args: argparse.Namespace) -> None:
   """Indexes SKILL.md files from a GitHub repository into SQLite."""
   os.environ["TRACEGATE_DB_PATH"] = args.db
   print(f"🔍 Indexing SKILL.md frontmatter from github:{args.repo} ({args.branch})...")
   res = asyncio.run(
       tg.sync_github_skill_library(
           repo=args.repo,
           branch=args.branch,
           github_token=args.token,
       )
   )
   print(f"✅ Indexed {res['skills_indexed_count']} skills from {args.repo}:")
   for s in res["indexed_skills"]:
       print(
           f"   • {s['name']:<28} | Frontmatter: {s['frontmatter_tokens']:>3} tok | "
           f"Full Body: {s['full_body_tokens']:>4} tok ({s['path']})"
       )
def cmd_estimate(args: argparse.Namespace) -> None:
   """Runs a quick Pre-Flight Token & USD/INR Cost Estimate from the terminal."""
   os.environ["TRACEGATE_DB_PATH"] = args.db
   res = tg.preflight_estimate_and_match_skill(
       user_prompt=args.prompt,
       model=args.model,
   )
   print("\n" + res["handshake_card_markdown"] + "\n")
def cmd_tree(args: argparse.Namespace) -> None:
   """Prints the ASCII DAG execution tree for a given session ID (or lists recent sessions)."""
   os.environ["TRACEGATE_DB_PATH"] = args.db
   if not args.session_id:
       with tg.STORE._conn() as conn:
           rows = conn.execute(
               "SELECT session_id, model, total_session_tokens, total_session_usd, title "
               "FROM trace_sessions ORDER BY created_at DESC LIMIT 15"
           ).fetchall()
       if not rows:
           print("No sessions recorded yet. Run `tracegate up` or `tracegate estimate` first.")
           return
       print("\nRecent TraceGate DAG Sessions:")
       print("-" * 85)
       for r in rows:
           inr = r["total_session_usd"] * tg.USD_TO_INR
           print(
               f"• {r['session_id']:<16} | {r['model']:<18} | "
               f"{r['total_session_tokens']:>6} tok | ${r['total_session_usd']:.4f} (₹{inr:.2f}) | {r['title']}"
           )
       print("-" * 85)
       return
   data = tg.get_dag_trace_tree(args.session_id)
   if "error" in data:
       print(f"❌ {data['error']}")
       sys.exit(1)
   print(f"\n🌳 DAG Execution Tree — Session `{data['session_id']}` ({data['model']})")
   print(
       f"💰 Total Spend: {data['total_session_tokens_across_all_branches']:,} tokens | "
       f"${data['total_session_usd']:.4f} (₹{data['total_session_inr']:.2f})\n"
   )
   print(data["ascii_dag_tree"] + "\n")
def cmd_test(_args: argparse.Namespace) -> None:
   """Runs the end-to-end pytest verification suite."""
   sys.exit(subprocess.call([sys.executable, "-m", "pytest", "test_tracegate_agentic_loop.py", "-v"]))
def main() -> None:
   parser = argparse.ArgumentParser(
       prog="tracegate",
       description="TraceGate — AI Execution Proxy, Pre-Flight Token Firewall & DAG Backtracker",
   )
   parser.add_argument(
       "--db",
       default=os.environ.get("TRACEGATE_DB_PATH", "tracegate_dag.sqlite3"),
       help="Path to SQLite WAL database (default: tracegate_dag.sqlite3)",
   )
   sub = parser.add_subparsers(dest="command", required=True)
   # 1. `tracegate up`
   p_up = sub.add_parser("up", help="Start the FastAPI WebSocket Bridge and Dark Web UI")
   p_up.add_argument("--host", default="0.0.0.0")
   p_up.add_argument("--port", type=int, default=8000)
   p_up.add_argument("--inr-rate", type=float, default=86.50, help="USD to INR conversion rate")
   p_up.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
   p_up.add_argument("--open-browser", action="store_true", help="Open Web UI in browser on launch")
   p_up.set_defaults(func=cmd_up)
   # 2. `tracegate mcp`
   p_mcp = sub.add_parser("mcp", help="Run the TraceGate MCP Server (stdio or sse)")
   p_mcp.add_argument("--transport", choices=["stdio", "sse"], default="stdio")
   p_mcp.add_argument("--host", default="0.0.0.0")
   p_mcp.add_argument("--port", type=int, default=8001)
   p_mcp.set_defaults(func=cmd_mcp)
   # 3. `tracegate sync`
   p_sync = sub.add_parser("sync", help="Index SKILL.md files from a GitHub repository")
   p_sync.add_argument("repo", help="GitHub repository in 'owner/repo' format")
   p_sync.add_argument("--branch", default="main")
   p_sync.add_argument("--token", default=None, help="Optional GitHub PAT")
   p_sync.set_defaults(func=cmd_sync)
   # 4. `tracegate estimate`
   p_est = sub.add_parser("estimate", help="Run a Pre-Flight Token & USD/INR Cost audit")
   p_est.add_argument("prompt", help="User prompt to estimate")
   p_est.add_argument("--model", default="claude-sonnet-5-5")
   p_est.set_defaults(func=cmd_estimate)
   # 5. `tracegate tree`
   p_tree = sub.add_parser("tree", help="Inspect the ASCII DAG tree for a session")
   p_tree.add_argument("session_id", nargs="?", default=None, help="Session ID (omit to list recent)")
   p_tree.set_defaults(func=cmd_tree)
   # 6. `tracegate test`
   p_test = sub.add_parser("test", help="Run the automated pytest suite")
   p_test.set_defaults(func=cmd_test)
   args = parser.parse_args()
   args.func(args)
if __name__ == "__main__":
   main()
